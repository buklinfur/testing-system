#!/usr/bin/env bash
# app.dev_seed on the installation, as `judge`, with /etc/judge/backend.env (dev only: JUDGE_DEV=1).
# Meant as SEED_CMD of tools/smoke-admin-api.sh until the student API of 4b can submit, from the repository:
#   SEED_CMD=tools/seed-as-judge.sh tools/smoke-admin-api.sh http://127.0.0.1:8000 https://<domain>
# It runs a constant script as judge through `sudo -u judge` (your own sudo, it may ask for your password), so
# judge never reads this file (it cannot read your home directory, and /opt/judge holds no tools/).
# Arguments go to dev_seed unchanged, e.g. --contest <slug> --label A --user 1.
set -euo pipefail
INNER='set -a; . /etc/judge/backend.env; set +a; cd /opt/judge
JUDGE_DEV=1 PYTHONPATH=/opt/judge:/opt/judge/backend exec .venv/bin/python -m app.dev_seed "$@"'
if [ "$(id -un)" = judge ]; then exec bash -c "$INNER" _ "$@"; fi
exec sudo -u judge bash -c "$INNER" _ "$@"
