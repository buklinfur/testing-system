#!/usr/bin/env bash
# Installs the judge services into systemd inside WSL2. Run as root from the repository root:
#   sudo ./deploy/install-wsl.sh
# It does not start anything: create the tokens and env files first (docs/DEPLOY_WSL2.md).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
[ -d runner ] && [ -d worker ] && [ -d backend ] || { echo "run from the repository root"; exit 1; }

id judge >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/judge --shell /usr/sbin/nologin judge
install -d -o judge -g judge /var/lib/judge/blobs /var/cache/judge/worker
install -d -o root -g judge -m 750 /etc/judge
mkdir -p /opt/judge
cp -a runner worker protocol backend requirements.txt /opt/judge/
[ -d /opt/judge/.venv ] || python3 -m venv /opt/judge/.venv
/opt/judge/.venv/bin/pip install -q -r /opt/judge/requirements.txt
for f in backend worker; do
  [ -f /etc/judge/$f.env ] || { install -m 640 -o root -g judge deploy/env/$f.env.example /etc/judge/$f.env; echo "created /etc/judge/$f.env: edit it"; }
done
cp deploy/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
echo "installed. Next: docs/DEPLOY_WSL2.md, section 'First start'."
