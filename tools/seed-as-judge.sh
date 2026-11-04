#!/usr/bin/env bash
# app.dev_seed on the installation, as `judge`, with /etc/judge/backend.env (dev only: JUDGE_DEV=1).
# Meant as SEED_CMD of tools/smoke-admin-api.sh until the student API of 4b can submit:
#   SEED_CMD=/opt/judge/tools/seed-as-judge.sh tools/smoke-admin-api.sh http://127.0.0.1:8000 https://<domain>
# Run by another user it re-runs itself through `sudo -u judge` (your own sudo, it may ask for your password).
# Arguments go to dev_seed unchanged, e.g. --contest <slug> --label A --user 1.
set -euo pipefail
if [ "$(id -un)" != judge ]; then exec sudo -u judge "$(readlink -f "$0")" "$@"; fi
set -a; . /etc/judge/backend.env; set +a
cd /opt/judge
JUDGE_DEV=1 PYTHONPATH=/opt/judge:/opt/judge/backend exec .venv/bin/python -m app.dev_seed "$@"
