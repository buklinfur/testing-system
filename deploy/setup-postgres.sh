#!/usr/bin/env bash
# Prepares the local PostgreSQL for the judge. Run as root from the repository root:
#   sudo ./deploy/setup-postgres.sh
# Idempotent: existing passwords are kept (pass --rotate to generate new ones).
#
# What it does:
#   * conf.d drop-in: listen on localhost only, password_encryption = scram-sha-256; reload (restart if needed);
#   * refuses to continue if pg_hba.conf allows TCP logins with trust/md5/password;
#   * roles judge and judge_test (LOGIN, no superuser/createdb/createrole), databases judge and judge_test
#     owned by them, CONNECT revoked from PUBLIC (each role reaches only its own database);
#   * production URL -> /etc/judge/backend.env (DATABASE_URL, 640 root:judge);
#   * test URL -> ~$SUDO_USER/.config/judge/test.env (JUDGE_TEST_DATABASE_URL, 600): a throw-away database
#     used by tests/pg and by local Alembic / API checks, so the developer does not need sudo for them.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
[ -f deploy/env/backend.env.example ] || { echo "run from the repository root"; exit 1; }
ROTATE=0; [ "${1:-}" = "--rotate" ] && ROTATE=1

PGVER=$(pg_lsclusters --no-header | awk '$2 == "main" {print $1; exit}')
[ -n "$PGVER" ] || { echo "no 'main' PostgreSQL cluster found (apt install postgresql)"; exit 1; }
CONF=/etc/postgresql/$PGVER/main
psql_admin() { sudo -u postgres psql -X -q -v ON_ERROR_STOP=1 "$@"; }

# 1. server settings
install -m 644 -o postgres -g postgres /dev/stdin "$CONF/conf.d/judge.conf" <<'EOF'
# managed by judge deploy/setup-postgres.sh
listen_addresses = 'localhost'
password_encryption = 'scram-sha-256'
EOF
grep -Eq "^\s*include_dir\s*=\s*'conf.d'" "$CONF/postgresql.conf" || { echo "$CONF/postgresql.conf does not include conf.d"; exit 1; }
if grep -Ev '^\s*(#|$)' "$CONF/pg_hba.conf" | awk '$1 ~ /^host/ && $NF ~ /^(trust|md5|password)$/' | grep -q .; then
  echo "pg_hba.conf allows TCP logins without scram-sha-256:"
  grep -Ev '^\s*(#|$)' "$CONF/pg_hba.conf" | awk '$1 ~ /^host/'
  echo "change those lines to scram-sha-256 and run again"; exit 1
fi
systemctl reload "postgresql@$PGVER-main"
if [ "$(psql_admin -tAc "SELECT bool_or(pending_restart) FROM pg_settings")" = t ]; then
  systemctl restart "postgresql@$PGVER-main"
fi

# 2. passwords: keep the existing ones unless --rotate
BACKEND_ENV=/etc/judge/backend.env
DEV_USER=${SUDO_USER:?run through sudo from your own account}
DEV_HOME=$(getent passwd "$DEV_USER" | cut -d: -f6)
TEST_ENV=$DEV_HOME/.config/judge/test.env
existing_pw() {  # $1 file, $2 variable, $3 role
  [ -f "$1" ] && sed -n "s#^$2=postgresql+psycopg://$3:\([^@]*\)@.*#\1#p" "$1" | grep -v '^CHANGE_ME$' || true
}
PW_PROD=""; PW_TEST=""
if [ $ROTATE = 0 ]; then
  PW_PROD=$(existing_pw "$BACKEND_ENV" DATABASE_URL judge)
  PW_TEST=$(existing_pw "$TEST_ENV" JUDGE_TEST_DATABASE_URL judge_test)
fi
[ -n "$PW_PROD" ] || PW_PROD=$(openssl rand -hex 24)  # hex: no escaping needed in URLs or SQL
[ -n "$PW_TEST" ] || PW_TEST=$(openssl rand -hex 24)

# 3. roles and databases (passwords go through stdin, never on a command line)
for pair in "judge:$PW_PROD" "judge_test:$PW_TEST"; do
  role=${pair%%:*}; pw=${pair#*:}
  psql_admin <<EOF
SELECT format('CREATE ROLE %I', '$role') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$role') \gexec
ALTER ROLE $role LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '$pw';
SELECT format('CREATE DATABASE %I OWNER %I', '$role', '$role') WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$role') \gexec
ALTER DATABASE $role OWNER TO $role;
REVOKE ALL ON DATABASE $role FROM PUBLIC;
EOF
  psql_admin -d "$role" -c "ALTER SCHEMA public OWNER TO $role"
done

# 4. env files
install -d -o root -g judge -m 750 /etc/judge
[ -f "$BACKEND_ENV" ] || install -m 640 -o root -g judge deploy/env/backend.env.example "$BACKEND_ENV"
URL_PROD="postgresql+psycopg://judge:$PW_PROD@127.0.0.1:5432/judge"
# the URL goes through the environment, not argv (argv is visible to everyone in ps)
URL="$URL_PROD" awk 'BEGIN {done = 0}
  /^DATABASE_URL=/ {print "DATABASE_URL=" ENVIRON["URL"]; done = 1; next} {print}
  END {if (!done) print "DATABASE_URL=" ENVIRON["URL"]}' "$BACKEND_ENV" > "$BACKEND_ENV.new"
chown root:judge "$BACKEND_ENV.new"; chmod 640 "$BACKEND_ENV.new"; mv "$BACKEND_ENV.new" "$BACKEND_ENV"

install -d -o "$DEV_USER" -g "$DEV_USER" -m 700 "$DEV_HOME/.config/judge"
install -m 600 -o "$DEV_USER" -g "$DEV_USER" /dev/stdin "$TEST_ENV" <<EOF
# throw-away test database (tests/pg, local Alembic and API checks); written by deploy/setup-postgres.sh
JUDGE_TEST_DATABASE_URL=postgresql+psycopg://judge_test:$PW_TEST@127.0.0.1:5432/judge_test
EOF

# 5. checks
echo "--- server"
psql_admin -tAc "SELECT name || ' = ' || setting FROM pg_settings WHERE name IN ('listen_addresses', 'password_encryption', 'port')"
ss -ltnH 'sport = :5432' | awk '{print "listening on " $4}'
echo "--- pg_hba (TCP lines)"
grep -Ev '^\s*(#|$)' "$CONF/pg_hba.conf" | awk '$1 ~ /^host/'
echo "--- logins"
for pair in "judge:$PW_PROD" "judge_test:$PW_TEST"; do
  role=${pair%%:*}; pw=${pair#*:}
  PGPASSWORD=$pw psql -X -h 127.0.0.1 -U "$role" -d "$role" -tAc "SELECT current_user || ': password login over TCP ok'"
done
PGPASSWORD=$PW_TEST psql -X -h 127.0.0.1 -U judge_test -d judge -tAc "SELECT 1" >/dev/null 2>&1 \
  && { echo "FAIL: judge_test can connect to the production database"; exit 1; } || echo "judge_test cannot connect to judge: ok"
PGPASSWORD=wrong psql -X -h 127.0.0.1 -U judge -d judge -tAc "SELECT 1" >/dev/null 2>&1 \
  && { echo "FAIL: wrong password accepted"; exit 1; } || echo "wrong password refused: ok"
psql_admin -tAc "SELECT rolname || ': scram=' || (rolpassword LIKE 'SCRAM-SHA-256%') FROM pg_authid WHERE rolname IN ('judge', 'judge_test')"
echo "done: $BACKEND_ENV, $TEST_ENV"
