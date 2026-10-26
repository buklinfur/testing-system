#!/usr/bin/env bash
# Wipes the test data of the module 3 stabilisation from the production installation:
# stops the judge services, recreates the `judge` database through Alembic (downgrade base + upgrade head),
# deletes the stored blobs and the worker cache, and creates a fresh worker token in /etc/judge/worker.env
# (the old one lived in the wiped database). Destructive and NOT reversible. Not part of judge-dev / sudoers:
# run it yourself, with your password, from the repository root:
#   sudo ./deploy/dev/reset-production.sh --yes-wipe-production
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
[ "${1:-}" = --yes-wipe-production ] || { echo "usage: $0 --yes-wipe-production"; exit 2; }
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

j() {  # as judge with the backend environment
  env -i PATH=/usr/bin:/bin HOME=/var/lib/judge LANG=C.UTF-8 runuser -u judge -- /bin/bash -c \
    'set -a; . /etc/judge/backend.env; set +a; cd /opt/judge; export PYTHONPATH=/opt/judge:/opt/judge/backend; exec "$@"' _ "$@"
}

for u in judge-worker judge-reaper judge-api-internal; do systemctl --no-pager stop "$u" || true; done
j /opt/judge/.venv/bin/alembic -c /opt/judge/alembic.ini downgrade base
j /opt/judge/.venv/bin/alembic -c /opt/judge/alembic.ini upgrade head
find /var/lib/judge/blobs -mindepth 1 -delete
find /var/cache/judge/worker -mindepth 1 -delete

token=$(j /opt/judge/.venv/bin/python -m app.admin_cli create-worker-token main | tail -1)
[[ $token =~ ^jw_[A-Za-z0-9_-]{20,}$ ]] || { echo "unexpected token output"; exit 1; }
f=/etc/judge/worker.env
TOKEN="$token" awk 'BEGIN{d=0} /^JUDGE_WORKER_TOKEN=/{print "JUDGE_WORKER_TOKEN=" ENVIRON["TOKEN"]; d=1; next} {print}
                    END{if(!d) print "JUDGE_WORKER_TOKEN=" ENVIRON["TOKEN"]}' "$f" > "$f.new"
chown root:judge "$f.new"; chmod 640 "$f.new"; mv "$f.new" "$f"
echo "done: database recreated, blobs and worker cache wiped, new worker token in $f; services are stopped"
