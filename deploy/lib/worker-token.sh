#!/usr/bin/env bash
# How a worker token gets into /etc/judge/worker.env: sourced (never run) by deploy/install-wsl.sh and
# deploy/dev/reset-production.sh. One definition, so that both write it the same way (D-089):
#   * judgectl writes the token only into a new file (--out, 0600, O_EXCL) in a private directory (mktemp -d, 0700);
#   * the token never appears on a command line (ps and /proc/*/cmdline are readable by everyone), in a variable, in
#     the environment of a process, in the output or in an xtrace: awk reads it from that file itself;
#   * the new worker.env is made under umask 077 and is root:judge 0640 when it replaces the old one (judge reads it:
#     judge-dev worker, DEPLOY_WSL2.md §3; judge cannot write it);
#   * the directory is removed by judge_token_cleanup, which the caller's EXIT trap runs, also on a failure.
# Needs root.

if [ "${BASH_SOURCE[0]}" = "$0" ]; then echo "$0: a library, sourced by the deploy scripts; not run by itself" >&2; exit 2; fi

JUDGE_TOKEN_DIR=""

# judge_token_cleanup: remove the private directory of the token, if there is one (safe to call any number of times)
judge_token_cleanup() {
  if [ -n "$JUDGE_TOKEN_DIR" ]; then rm -rf -- "$JUDGE_TOKEN_DIR"; fi
  JUDGE_TOKEN_DIR=""
}

# judge_worker_token_new NAME ENVFILE RUN...: a new worker token NAME, written into ENVFILE as JUDGE_WORKER_TOKEN (the
# line replaced, or added when missing). RUN... runs a command as judge with the backend environment (the caller's
# as_judge / j); judgectl and python come after it.
judge_worker_token_new() {
  local name=$1 envfile=$2
  shift 2
  JUDGE_TOKEN_DIR=$(mktemp -d)  # 0700, root
  chown judge "$JUDGE_TOKEN_DIR"  # judgectl runs as judge; still 0700: nobody else can even list it
  "$@" /opt/judge/.venv/bin/python -m app.judgectl worker token create "$name" --out "$JUDGE_TOKEN_DIR/token"
  grep -Eqx 'jw_[A-Za-z0-9_-]{20,}' "$JUDGE_TOKEN_DIR/token" || { echo "unexpected token file" >&2; return 1; }
  (
    umask 077
    awk 'FNR == NR { token = $0; next }
         /^JUDGE_WORKER_TOKEN=/ { print "JUDGE_WORKER_TOKEN=" token; done = 1; next }
         { print }
         END { if (!done) print "JUDGE_WORKER_TOKEN=" token }' "$JUDGE_TOKEN_DIR/token" "$envfile" > "$envfile.new"
  )
  chown root:judge "$envfile.new"
  chmod 640 "$envfile.new"
  mv "$envfile.new" "$envfile"
  judge_token_cleanup
}
