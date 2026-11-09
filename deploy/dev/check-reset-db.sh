#!/usr/bin/env bash
# Checks the database half of reset-production.sh on throw-away databases, never on `judge` (D-073):
#   sudo ./deploy/dev/check-reset-db.sh
# 1. a role judge_reset_check and its database, made by judge_role_ensure / judge_db_create (as setup-postgres.sh);
# 2. alembic upgrade head, then data that refuses `downgrade base`: groups, contest deadlines, a version check, the
#    feed, works, comments, read marks, sessions, audit, a worker token (tools/reset_check.py seed);
# 3. judge_db_recreate + alembic upgrade head - what reset-production.sh does to `judge`;
# 4. a second database judge_reset_check_fresh made the fresh way, migrated, and compared with the reset one by the
#    catalog: owner and privileges of the database and of the schema public, table owners, rows, models ==
#    migrations (tools/reset_check.py compare). Exit 0: the same; 1: the differences are printed.
# Both databases and the role are dropped at the end (and at the start, if an interrupted run left them). Python and
# alembic run as you ($SUDO_USER) from this repository's .venv; the URLs (a random password) go through the
# environment, never the command line. Needs: the repository root, .venv, password logins on 127.0.0.1.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
DEV_USER=${SUDO_USER:?run through sudo from your own account}
[ -x .venv/bin/alembic ] && [ -f tools/reset_check.py ] || { echo "run from the repository root (with .venv)"; exit 1; }
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
. "$(dirname "$0")/../lib/pg-database.sh"

ROLE=judge_reset_check
DB=$ROLE
FRESH=${ROLE}_fresh

cleanup() {
  judge_psql_admin -v db="$DB" -v fresh="$FRESH" -v role="$ROLE" <<'SQL'
SELECT format('DROP DATABASE %I', datname) FROM pg_database WHERE datname IN (:'db', :'fresh') \gexec
SELECT format('DROP ROLE %I', rolname) FROM pg_roles WHERE rolname = :'role' \gexec
SQL
}
cleanup
trap cleanup EXIT

PW=$(openssl rand -hex 24)
url() { echo "postgresql+psycopg://$ROLE:$PW@127.0.0.1:5432/$1"; }
as_dev() {  # as you, in the repository, with the URLs in the environment only
  DATABASE_URL=$(url "$DB") FRESH_DATABASE_URL=$(url "$FRESH") PYTHONPATH=.:backend \
    /usr/sbin/runuser -u "$DEV_USER" -- "$@"
}
as_dev_fresh() {
  DATABASE_URL=$(url "$FRESH") PYTHONPATH=.:backend /usr/sbin/runuser -u "$DEV_USER" -- "$@"
}

echo "--- 1. role and database as setup-postgres.sh makes them"
judge_role_ensure "$ROLE" "$PW"
judge_db_create "$DB"
echo "--- 2. migrations and the data that refuses downgrade base"
as_dev .venv/bin/alembic upgrade head
as_dev .venv/bin/python tools/reset_check.py seed
echo "--- 3. reset: judge_db_recreate + migrations (reset-production.sh)"
judge_db_recreate "$DB"
as_dev .venv/bin/alembic upgrade head
echo "--- 4. a fresh database, compared by the catalog"
judge_db_create "$FRESH" "$ROLE"
as_dev_fresh .venv/bin/alembic upgrade head
out=$(mktemp -d)
chown "$DEV_USER": "$out"; chmod 700 "$out"
if as_dev .venv/bin/python tools/reset_check.py compare > "$out/compare.json"; then
  echo "OK: the reset database is the same as a fresh installation (details: $out/compare.json)"
else
  echo "FAIL: see the differences above and $out/compare.json"; exit 1
fi
