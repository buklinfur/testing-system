#!/usr/bin/env bash
# Smoke test of the public login API (docs/AUTH.md) with curl, against a running public app:
#   tools/smoke-public-auth.sh [BASE_URL] [ORIGIN]       defaults: http://127.0.0.1:8000 and $JUDGE_PUBLIC_ORIGIN
# ORIGIN must equal JUDGE_PUBLIC_ORIGIN of the server. It asks for an admin (login, password, a TOTP code from the
# phone), a student (login, password) and a fresh QR token of another student or the same one (`judgectl user
# qr-token LOGIN --no-qr`). For scripts the answers can come from the environment instead: ADMIN_LOGIN,
# ADMIN_PASSWORD, TOTP_CODE, STUDENT_LOGIN, STUDENT_PASSWORD, QR_TOKEN.
# Secrets reach curl only through files in a private temporary directory (never the command line) and are
# never printed. It costs the student one failed login (wrong password) and uses up the QR token.
# Exit code = number of failures.
set -uo pipefail
BASE=${1:-http://127.0.0.1:8000}; ORIGIN=${2:-${JUDGE_PUBLIC_ORIGIN:-}}
[ -n "$ORIGIN" ] || { echo "usage: $0 [BASE_URL] ORIGIN   (ORIGIN = JUDGE_PUBLIC_ORIGIN of the server)"; exit 2; }
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT; chmod 700 "$D"
FAILS=0

ask() {  # ask VAR "prompt" [silent]: keeps a value already in the environment
  [ -n "${!1:-}" ] && return
  if [ "${3:-}" = silent ]; then read -rsp "$2: " "$1" < /dev/tty; echo; else read -rp "$2: " "$1" < /dev/tty; fi
  export "${1?}"
}
body() {  # body FILE KEY=ENVVAR ...: a JSON object from environment variables, written without echoing it
  local file=$1; shift
  python3 -c 'import json, os, sys
print(json.dumps({k: os.environ[v] for k, v in (a.split("=", 1) for a in sys.argv[1:])}))' "$@" > "$D/$file"
}
c() {  # c <label> <expected status> <method> <path> [curl args...]; headers -> $D/hdr, body -> $D/body
  local label=$1 want=$2 method=$3 path=$4; shift 4
  local code mark=OK
  code=$(curl -s --noproxy '*' -D "$D/hdr" -o "$D/body" -w '%{http_code}' -X "$method" "$BASE$path" "$@")
  [ "$code" = "$want" ] || { mark=FAIL; FAILS=$((FAILS + 1)); }
  printf '%-4s %-52s %s (want %s)  %s\n' "$mark" "$label" "$code" "$want" \
    "$(python3 -c 'import json, sys
try: d = json.load(open(sys.argv[1]))
except Exception: sys.exit()
if isinstance(d, dict) and "csrf_token" in d: d["csrf_token"] = "..."
print(json.dumps(d, ensure_ascii=False)[:150])' "$D/body")"
}
check() {  # check <label> <python expression over d = the last JSON body>
  if python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); sys.exit(0 if eval(sys.argv[2]) else 1)' \
      "$D/body" "$2" 2>/dev/null; then echo "OK   $1"; else echo "FAIL $1"; FAILS=$((FAILS + 1)); fi
}
keep_cookie() {  # keep_cookie NAME: the session cookie of the last answer -> header file $D/NAME
  local v
  v=$(sed -n 's/^set-cookie: __Host-session=\([^;]*\);.*/\1/Ip' "$D/hdr" | tr -d '\r' | tail -1)
  [ -n "$v" ] || { echo "FAIL no session cookie in the answer"; FAILS=$((FAILS + 1)); }
  printf 'Cookie: __Host-session=%s\n' "$v" > "$D/$1"
}
keep_csrf() {  # keep_csrf NAME: the CSRF token of the last answer -> header file $D/NAME
  python3 -c 'import json, sys; print("X-CSRF-Token: " + json.load(open(sys.argv[1]))["csrf_token"])' "$D/body" \
    > "$D/$1" 2>/dev/null || { echo "FAIL no csrf_token in the answer"; FAILS=$((FAILS + 1)); : > "$D/$1"; }
}
J='Content-Type: application/json'; O="Origin: $ORIGIN"

ask ADMIN_LOGIN "admin login"; ask ADMIN_PASSWORD "admin password" silent
ask STUDENT_LOGIN "student login"; ask STUDENT_PASSWORD "student password" silent
ask QR_TOKEN "QR token (judgectl user qr-token LOGIN --no-qr, the part after #)" silent
body admin login=ADMIN_LOGIN password=ADMIN_PASSWORD
body student login=STUDENT_LOGIN password=STUDENT_PASSWORD
body qr token=QR_TOKEN
printf '{"login": "%s", "password": "surely-not-the-password"}\n' "$STUDENT_LOGIN" > "$D/wrong"

echo "--- Origin (checked before anything else; not counted as a failed login)"
c "login without Origin"                          403 POST /api/auth/login -H "$J" -d @"$D/admin"
c "login with a foreign Origin"                   403 POST /api/auth/login -H "$J" -H "Origin: https://evil.example" -d @"$D/admin"

echo "--- admin: password -> TOTP -> me"
c "admin: password"                               200 POST /api/auth/login -H "$J" -H "$O" -d @"$D/admin"
check "state is mfa_pending, no user yet"         'd["state"] == "mfa_pending" and d["user"] is None'
grep -i '^set-cookie: __Host-session=' "$D/hdr" | grep -qi 'httponly' && grep -i '^set-cookie: __Host-session=' "$D/hdr" \
  | grep -qi 'secure' && grep -i '^set-cookie: __Host-session=' "$D/hdr" | grep -qi 'samesite=lax' \
  && grep -i '^set-cookie: __Host-session=' "$D/hdr" | grep -qi 'path=/' \
  && echo "OK   cookie is HttpOnly; Secure; SameSite=Lax; Path=/" || { echo "FAIL cookie flags"; FAILS=$((FAILS + 1)); }
keep_cookie a_pending; keep_csrf a_pending_csrf
c "me with the mfa_pending session"               401 GET /api/auth/me -H @"$D/a_pending"
c "totp without X-CSRF-Token"                     403 POST /api/auth/totp -H "$J" -H "$O" -H @"$D/a_pending" -d '{"code": "000000"}'
ask TOTP_CODE "TOTP code from the phone (now)"
body totp code=TOTP_CODE
c "admin: TOTP code"                              200 POST /api/auth/totp -H "$J" -H "$O" -H @"$D/a_pending" -H @"$D/a_pending_csrf" -d @"$D/totp"
check "state is active, role admin"               'd["state"] == "active" and d["user"]["role"] == "admin"'
keep_cookie a_active; keep_csrf a_csrf
c "the pre-TOTP cookie is dead"                   401 GET /api/auth/me -H @"$D/a_pending"
c "admin: me"                                     200 GET /api/auth/me -H @"$D/a_active"
check "me is the admin"                           "d['user']['login'] == '$ADMIN_LOGIN'.strip().lower()"
c "admin: logout without X-CSRF-Token"            403 POST /api/auth/logout -H "$O" -H @"$D/a_active"
c "admin: logout"                                 204 POST /api/auth/logout -H "$O" -H @"$D/a_active" -H @"$D/a_csrf"
c "admin: the logged-out cookie is dead"          401 GET /api/auth/me -H @"$D/a_active"

echo "--- student: password -> me"
c "student: wrong password"                       401 POST /api/auth/login -H "$J" -H "$O" -d @"$D/wrong"
c "student: password"                             200 POST /api/auth/login -H "$J" -H "$O" -d @"$D/student"
check "state is active, role student"             'd["state"] == "active" and d["user"]["role"] == "student"'
keep_cookie s_active; keep_csrf s_csrf
c "student: me"                                   200 GET /api/auth/me -H @"$D/s_active"
c "student: password change without X-CSRF-Token" 403 POST /api/auth/password -H "$J" -H "$O" -H @"$D/s_active" \
                                                      -d '{"current_password": "x", "new_password": "y"}'
c "student: logout"                               204 POST /api/auth/logout -H "$O" -H @"$D/s_active" -H @"$D/s_csrf"

echo "--- student: QR -> me; the same QR again is refused"
c "QR login"                                      200 POST /api/auth/qr -H "$J" -H "$O" -d @"$D/qr"
check "state is active, role student"             'd["state"] == "active" and d["user"]["role"] == "student"'
keep_cookie q_active; keep_csrf q_csrf
c "QR: me"                                        200 GET /api/auth/me -H @"$D/q_active"
c "the same QR token again"                       401 POST /api/auth/qr -H "$J" -H "$O" -d @"$D/qr"
c "QR: logout"                                    204 POST /api/auth/logout -H "$O" -H @"$D/q_active" -H @"$D/q_csrf"

echo "--- not part of the public app"
c "/internal/worker/v1/claim"                     404 POST /internal/worker/v1/claim -H "$O"
c "/docs"                                         404 GET /docs

echo "failures: $FAILS"
exit $FAILS
