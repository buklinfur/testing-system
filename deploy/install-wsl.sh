#!/usr/bin/env bash
# Installs or updates the judge services in WSL2. Run as root from the repository root. pip needs the
# Windows HTTP proxy (WSL DNS does not work), and sudo drops the proxy variables unless told to keep them:
#   sudo --preserve-env=HTTP_PROXY,HTTPS_PROXY,NO_PROXY,http_proxy,https_proxy,no_proxy ./deploy/install-wsl.sh
# What it does (docs/DEPLOY_WSL2.md):
#   * the `judge` system user, its data and cache directories, /etc/judge with env templates (kept if present);
#   * copies the code to /opt/judge, root-owned and writable only by root (removed files disappear too);
#   * the venv with the pinned requirements, bytecode compiled in advance (judge cannot write it);
#   * systemd units (not enabled or started);
#   * if /etc/judge/backend.env has a real DATABASE_URL: alembic upgrade head as `judge`, and a worker token
#     written into /etc/judge/worker.env if it still says CHANGE_ME (the token is never printed).
# It does not touch /etc/wsl.conf, /mnt/c, the proxy settings, PostgreSQL itself or isolate.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
[ -d runner ] && [ -d worker ] && [ -d backend ] && [ -f alembic.ini ] || { echo "run from the repository root"; exit 1; }
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

# 0. checks that save a half-done installation
command -v rsync >/dev/null || { echo "rsync is missing: apt install rsync"; exit 1; }
if [ -z "${HTTPS_PROXY:-${https_proxy:-}}" ]; then
  echo "warning: no HTTPS_PROXY in the environment; inside WSL pip usually needs the Windows proxy."
  echo "         Run with: sudo --preserve-env=HTTP_PROXY,HTTPS_PROXY,NO_PROXY,http_proxy,https_proxy,no_proxy $0"
fi
if ! python3 - <<'EOF'
import sys, urllib.request
try:
    urllib.request.urlopen("https://pypi.org/simple/pip/", timeout=10)
except Exception as exc:
    sys.exit(f"PyPI is not reachable ({exc}). Is the proxy on Windows (127.0.0.1:8899) running?")
EOF
then exit 1; fi
systemctl is-active --quiet isolate.service || echo "warning: isolate.service (cg-keeper) is not active; judge-worker requires it"

# 1. user and directories
id judge >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/judge --shell /usr/sbin/nologin judge
install -d -o judge -g judge /var/lib/judge/blobs /var/cache/judge/worker
install -d -o root -g judge -m 750 /etc/judge
install -d -o root -g root -m 755 /opt/judge

# 2. code: root-owned, no group/other write, stale files removed (.venv at the top level is not touched)
rsync -a --delete --chown=root:root --chmod=Dgo-w,Fgo-w --exclude=__pycache__ --exclude='*.pyc' \
  runner worker protocol backend requirements.txt alembic.ini /opt/judge/

# 3. venv with pinned requirements; bytecode compiled now because judge cannot write __pycache__
[ -d /opt/judge/.venv ] || python3 -m venv /opt/judge/.venv
/opt/judge/.venv/bin/pip install -q --disable-pip-version-check -r /opt/judge/requirements.txt
/opt/judge/.venv/bin/python -m compileall -q /opt/judge/runner /opt/judge/worker /opt/judge/protocol /opt/judge/backend
chown -R root:root /opt/judge
chmod -R go-w /opt/judge

# 4. env files (templates only if missing) and units
for f in backend worker; do
  [ -f /etc/judge/$f.env ] || { install -m 640 -o root -g judge deploy/env/$f.env.example /etc/judge/$f.env; echo "created /etc/judge/$f.env"; }
done
cp deploy/systemd/*.service /etc/systemd/system/
systemctl daemon-reload

# 5. database: migrations and the worker token, as judge
as_judge() {
  env -i PATH=/usr/bin:/bin HOME=/var/lib/judge LANG=C.UTF-8 /usr/sbin/runuser -u judge -- /bin/bash -c \
    'set -a; . /etc/judge/backend.env; set +a; cd /opt/judge; export PYTHONPATH=/opt/judge:/opt/judge/backend; exec "$@"' _ "$@"
}
if grep -q '^DATABASE_URL=.*CHANGE_ME' /etc/judge/backend.env; then
  echo "DATABASE_URL is not set yet: run deploy/setup-postgres.sh, then this script again"
else
  as_judge /opt/judge/.venv/bin/alembic -c /opt/judge/alembic.ini upgrade head
  if grep -q '^JUDGE_WORKER_TOKEN=CHANGE_ME' /etc/judge/worker.env; then
    name="$(hostname -s)-$(date +%Y%m%d%H%M%S)"
    token=$(as_judge /opt/judge/.venv/bin/python -m app.admin_cli create-worker-token "$name" | tail -1)
    [[ $token =~ ^jw_[A-Za-z0-9_-]{20,}$ ]] || { echo "unexpected token output"; exit 1; }
    TOKEN="$token" awk '/^JUDGE_WORKER_TOKEN=/{print "JUDGE_WORKER_TOKEN=" ENVIRON["TOKEN"]; next} {print}' \
      /etc/judge/worker.env > /etc/judge/worker.env.new
    chown root:judge /etc/judge/worker.env.new; chmod 640 /etc/judge/worker.env.new
    mv /etc/judge/worker.env.new /etc/judge/worker.env
    echo "worker token '$name' created and written to /etc/judge/worker.env (not shown)"
  fi
fi
echo "installed. Next: docs/DEPLOY_WSL2.md, section 'First start'."
