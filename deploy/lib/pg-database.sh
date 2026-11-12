#!/usr/bin/env bash
# How a judge database is made: sourced (never run) by deploy/setup-postgres.sh, which creates the databases, and by
# deploy/dev/reset-production.sh and deploy/dev/check-reset-db.sh, which recreate one. One definition, so that a
# recreated database has exactly the owner and privileges of a fresh installation (D-073). Needs root (sudo -u
# postgres). Names go to psql as variables (:'db'), never pasted into SQL.

if [ "${BASH_SOURCE[0]}" = "$0" ]; then echo "$0: a library, sourced by the deploy scripts; not run by itself" >&2; exit 2; fi

judge_psql_admin() { sudo -u postgres psql -X -q -v ON_ERROR_STOP=1 "$@"; }

# judge_role_ensure ROLE PASSWORD: a login role (created if missing) without any special right: no superuser,
# createdb, createrole, replication; with this password (hex only: openssl rand -hex). The password goes through
# stdin, never on a command line.
judge_role_ensure() {
  [[ $2 =~ ^[0-9a-f]{16,}$ ]] || { echo "judge_role_ensure: the password must be hex" >&2; return 2; }
  judge_psql_admin -v role="$1" <<SQL
SELECT format('CREATE ROLE %I', :'role') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'role') \gexec
SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', :'role', '$2') \gexec
SQL
}

# The locale of every judge database, given explicitly rather than taken from the cluster: lower() and upper() of
# the database follow LC_CTYPE, and the unique names of 4v.3 (lower(path), lower(name)) need it to fold Cyrillic
# (lower('Ё') = 'ё'); C.UTF-8 sorts by code point, the same on every machine. tools/reset_check.py checks it.
JUDGE_DB_LOCALE=C.UTF-8

# judge_db_create DB [OWNER]: the database (if missing) owned by OWNER (default: the role named DB), UTF8 with the
# locale JUDGE_DB_LOCALE (from template0, so it never depends on the cluster's template1), CONNECT and TEMP taken
# from PUBLIC (each role reaches only its own database), the schema public owned by OWNER. Idempotent: an existing
# database keeps its locale (docs/DEPLOY_WSL2.md says how to check it).
judge_db_create() {
  local db=$1 owner=${2:-$1}
  judge_psql_admin -v db="$db" -v owner="$owner" -v locale="$JUDGE_DB_LOCALE" <<'SQL'
SELECT format('CREATE DATABASE %I OWNER %I TEMPLATE template0 ENCODING %L LC_COLLATE %L LC_CTYPE %L',
              :'db', :'owner', 'UTF8', :'locale', :'locale')
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db') \gexec
SELECT format('ALTER DATABASE %I OWNER TO %I', :'db', :'owner') \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'db') \gexec
SQL
  judge_psql_admin -d "$db" -v owner="$owner" <<'SQL'
SELECT format('ALTER SCHEMA public OWNER TO %I', :'owner') \gexec
SQL
}

# judge_db_connections DB: the sessions connected to DB now, one per line (empty: none).
judge_db_connections() {
  judge_psql_admin -tA -F ' | ' -v db="$1" <<'SQL'
SELECT pid, usename, coalesce(nullif(application_name, ''), '-'), coalesce(client_addr::text, 'local'),
       to_char(backend_start, 'YYYY-MM-DD HH24:MI:SS')
  FROM pg_stat_activity WHERE datname = :'db' AND pid <> pg_backend_pid() ORDER BY backend_start;
SQL
}

# judge_db_recreate DB [OWNER]: drop DB and create it again by judge_db_create, empty (no tables: migrations come
# next). Refuses, changing nothing, while anything is connected to DB (no FORCE: D-064); returns 3 then.
judge_db_recreate() {
  local db=$1 owner=${2:-$1} busy
  busy=$(judge_db_connections "$db")
  if [ -n "$busy" ]; then
    echo "the database $db has connections (pid | user | application | client | since):" >&2
    printf '%s\n' "$busy" >&2
    echo "stop them the normal way (a service: systemctl stop; an app: Ctrl-C in its terminal) and run again;" >&2
    echo "nothing was changed in the database" >&2
    return 3
  fi
  judge_psql_admin -v db="$db" <<'SQL'
SELECT format('DROP DATABASE %I', :'db') WHERE EXISTS (SELECT FROM pg_database WHERE datname = :'db') \gexec
SQL
  judge_db_create "$db" "$owner"
}
