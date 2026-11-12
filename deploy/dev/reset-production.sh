#!/usr/bin/env bash
# Wipes the test data from the production installation before students (docs/DEPLOY_WSL2.md §6 «Сброс тестовых
# данных», D-073). Destructive and NOT reversible (take a dump first if anything might be needed). Not part of
# judge-dev / sudoers: run it yourself, with your password, from the repository root:
#   sudo ./deploy/dev/reset-production.sh --yes-wipe-production
#
# Removes: the whole `judge` database - users (admins too, with their 2FA), sessions, groups, problems, contests,
# submissions, the class feed, works, comments, worker tokens, the audit log; the stored files (/var/lib/judge/blobs:
# sources, answers, problem files) and the worker cache (/var/cache/judge/worker).
# Keeps: /etc/judge/*.env (JUDGE_SECRET_KEY, DATABASE_URL and its password; the worker token is replaced), the role
# `judge`, /opt/judge, the systemd units, PostgreSQL settings, isolate.
#
# Order: checks (nothing changed yet) -> stop the services -> no connections left -> recreate the database with judge_db_create, the same
# function deploy/setup-postgres.sh creates it with (deploy/lib/pg-database.sh: owner, privileges, schema public as
# on a fresh installation) -> alembic upgrade head as `judge` -> wipe the files -> a new worker token. Services stay
# stopped. A failure prints what has happened so far and what to do next.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
[ "${1:-}" = --yes-wipe-production ] || { echo "usage: $0 --yes-wipe-production"; exit 2; }
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
. "$(dirname "$0")/../lib/pg-database.sh"
. "$(dirname "$0")/../lib/worker-token.sh"

DB=judge
BLOBS=/var/lib/judge/blobs
CACHE=/var/cache/judge/worker
UNITS="judge-worker judge-reaper judge-api-internal"

j() {  # as judge with the backend environment
  env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin HOME=/var/lib/judge LANG=C.UTF-8 /usr/sbin/runuser -u judge -- /bin/bash -c \
    'set -a; . /etc/judge/backend.env; set +a; cd /opt/judge; export PYTHONPATH=/opt/judge:/opt/judge/backend; exec "$@"' _ "$@"
}

PHASE=checks
on_exit() {
  local code=$?
  judge_token_cleanup  # the private directory of a new token, if any (a failure included)
  [ "$code" = 0 ] && return
  echo >&2
  echo "reset-production: FAILED during: $PHASE (exit $code)" >&2
  case $PHASE in
    checks|connections) echo "nothing was changed in the database or the files; fix the cause above and run again" >&2 ;;
    stop) echo "the database and the files are untouched; some services may be stopped: systemctl status $UNITS" >&2 ;;
    recreate) echo "the database $DB may be gone or empty. Run this script again; if it fails here again, create it" \
                   "by hand: sudo ./deploy/setup-postgres.sh, then sudo ./deploy/install-wsl.sh (migrations)" >&2 ;;
    migrate) echo "the database $DB is recreated but empty: run this script again, or sudo ./deploy/install-wsl.sh" \
                  "(it applies the migrations)" >&2 ;;
    files) echo "the database is fresh, the files are partly wiped: run this script again" >&2 ;;
    token) echo "the database and the files are fresh, the worker token is not written: set" \
                "JUDGE_WORKER_TOKEN=CHANGE_ME in /etc/judge/worker.env and run sudo ./deploy/install-wsl.sh" >&2 ;;
  esac
  echo "services stay stopped until you start them (docs/DEPLOY_WSL2.md §4)" >&2
}
trap on_exit EXIT

# 1. checks: nothing is changed before all of them pass
fail() { echo "$*" >&2; exit 1; }
[ -x /opt/judge/.venv/bin/alembic ] || fail "no /opt/judge/.venv: run deploy/install-wsl.sh first"
[ -f /etc/judge/backend.env ] && [ -f /etc/judge/worker.env ] || fail "no /etc/judge/*.env: run deploy/install-wsl.sh first"
grep -q '^DATABASE_URL=postgresql' /etc/judge/backend.env && ! grep -q '^DATABASE_URL=.*CHANGE_ME' /etc/judge/backend.env \
  || fail "DATABASE_URL in /etc/judge/backend.env is not set: run deploy/setup-postgres.sh"
[ -d "$BLOBS" ] && [ -d "$CACHE" ] || fail "no $BLOBS or $CACHE: run deploy/install-wsl.sh first"

# 2. stop the services (they are connected to the database)
PHASE=stop
for u in $UNITS; do systemctl --no-pager stop "$u" || true; done
for u in $UNITS; do
  if systemctl is-active --quiet "$u"; then fail "$u is still active"; fi
done

# 3. the database: dropped and created again as on a fresh installation. Anything still connected (a public app
#    or a psql started by hand) stops it here, before any change (no FORCE, D-064)
PHASE=connections
busy=$(judge_db_connections "$DB")
[ -z "$busy" ] || fail "the database $DB has connections (pid | user | application | client | since):
$busy
stop them the normal way (a service: systemctl stop; an app: Ctrl-C in its terminal) and run again"
PHASE=recreate
judge_db_recreate "$DB"
PHASE=migrate
j /opt/judge/.venv/bin/alembic -c /opt/judge/alembic.ini upgrade head

# 4. the files: sources, answers and problem files of the old database mean nothing now
PHASE=files
find "$BLOBS" "$CACHE" -mindepth 1 -delete
left=$(find "$BLOBS" "$CACHE" -mindepth 1 | wc -l)
[ "$left" = 0 ] || fail "$left entries are left in $BLOBS or $CACHE"

# 5. a new worker token (the old one lived in the dropped database)
PHASE=token
f=/etc/judge/worker.env
judge_worker_token_new main "$f" j
PHASE=done

echo "done: database $DB recreated (owner and privileges as on a fresh installation) and migrated, $BLOBS and"
echo "$CACHE wiped, a new worker token in $f (not shown). Services are stopped. Next (docs/DEPLOY_WSL2.md):"
echo "  the first admin: judgectl admin add <login> --name <name> (§2 «Первый администратор»), then start the services (§4)"
