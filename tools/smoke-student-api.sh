#!/usr/bin/env bash
# Smoke test of the student API (docs/STUDENT_API.md §6) with curl, against a running public app with a worker:
# an admin makes a class of two students (CSV import, passwords kept in files, never shown), the problem
# sum-of-two, a contest for the class with a visible leaderboard and a contest with an invite code and a personal
# timer; then the students, through the student API only: their contests, a statement, a submission that passes
# (OK), one with a wrong answer (WA), one that does not compile (CE, with its log), the poll interval while queued,
# 415 for another body type, 413 for a source over the limit (with Content-Length and chunked), their own list and
# source, another student's submission and source 404, the entry by code (wrong code 403, then the right one),
# press start, the personal start, the contest's pause (429 cooldown + Retry-After), the per-minute rate (429
# rate_limited, if the server's JUDGE_SUBMIT_RATE_PER_MIN is at most 12), the leaderboard (whole and filtered by the
# group) with its time measured, a hidden leaderboard 403, the load indicator for a student and for an admin.
# At the end the admin archives both contests and the group and disables both students (they have submissions, so
# they cannot be deleted).
#
#   tools/smoke-student-api.sh [BASE_URL] [ORIGIN]       defaults: http://127.0.0.1:8000 and $JUDGE_PUBLIC_ORIGIN
# It asks for an admin (login, password, a TOTP code from the phone); ADMIN_LOGIN, ADMIN_PASSWORD and TOTP_CODE
# from the environment are used instead (named, never shown). Secrets (passwords of the students, the invite code)
# reach curl only through files in a private temporary directory and are never printed.
# A worker must be running (judgectl worker list), with the capability problem_check when sum-of-two is not
# uploaded yet. Everything it makes is named smoke-<time>. Every request gives up after 10 s (a submission: 30 s).
# Exit code = number of failures.
set -uo pipefail
BASE=${1:-http://127.0.0.1:8000}; ORIGIN=${2:-${JUDGE_PUBLIC_ORIGIN:-}}
[ -n "$ORIGIN" ] || { echo "usage: $0 [BASE_URL] ORIGIN   (ORIGIN = JUDGE_PUBLIC_ORIGIN of the server)"; exit 2; }
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(dirname "$HERE"); PKG=$ROOT/problems-examples/sum-of-two
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT; chmod 700 "$D"
FAILS=0; SKIPS=0
STAMP=$(date +%m%d%H%M%S); SLUG="smoke-$STAMP"; SLUG2="smoke-$STAMP-code"; G="smoke $STAMP pupils"
WHO=admin  # whose cookie and CSRF token the requests carry: admin | s1 | s2 | anon

FROM_ENV=()
ask() {  # ask VAR "prompt" [silent]: keeps a value already in the environment
  [ -n "${!1:-}" ] && { FROM_ENV+=("$1"); return; }
  if [ "${3:-}" = silent ]; then read -rsp "$2: " "$1" < /dev/tty; echo; else read -rp "$2: " "$1" < /dev/tty; fi
  export "${1?}"
}
fail() { echo "FAIL $1"; FAILS=$((FAILS + 1)); }
c() {  # c <label> <expected status regex> <method> <path> [curl args...]; headers -> $D/hdr, body -> $D/body
  local label=$1 want=$2 method=$3 path=$4; shift 4
  local code mark=OK
  code=$(curl -s --noproxy '*' --max-time "${MAXTIME:-10}" -D "$D/hdr" -o "$D/body" -w '%{http_code}' -X "$method" \
         "$BASE$path" "$@")
  [[ $code =~ ^($want)$ ]] || { mark=FAIL; FAILS=$((FAILS + 1)); }
  printf '%-4s %-56s %s (want %s)  %s\n' "$mark" "$label" "$code" "$want" \
    "$(python3 -c 'import json, sys
try: d = json.load(open(sys.argv[1]))
except Exception: sys.exit()
if isinstance(d, dict):
    for k in ("csrf_token", "invite_code"):
        d.pop(k, None)
    for u in d.get("users", []) if isinstance(d.get("users"), list) else []:
        u.pop("password", None)
print(json.dumps(d, ensure_ascii=False)[:140])' "$D/body")"
}
check() {  # check <label> <python expression over d = the last JSON body>
  if python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); sys.exit(0 if eval(sys.argv[2]) else 1)' \
      "$D/body" "$2" 2>/dev/null; then echo "OK   $1"; else fail "$1"; fi
}
header() {  # header <label> <regex over the headers of the last answer, case-insensitive>
  if tr -d '\r' < "$D/hdr" | grep -Eqi "$2"; then echo "OK   $1"; else fail "$1"; fi
}
val() { python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); print(eval(sys.argv[2]))' "$D/body" "$1"; }
json() { python3 -c 'import json, sys; print(json.dumps(eval(sys.argv[1]), ensure_ascii=False))' "$1" > "$D/req"; }
keep_cookie() {
  local v
  v=$(sed -n 's/^set-cookie: __Host-session=\([^;]*\);.*/\1/Ip' "$D/hdr" | tr -d '\r' | tail -1)
  [ -n "$v" ] || fail "no session cookie in the answer"
  printf 'Cookie: __Host-session=%s\n' "$v" > "$D/$WHO.cookie"
}
keep_csrf() {
  python3 -c 'import json, sys; print("X-CSRF-Token: " + json.load(open(sys.argv[1]))["csrf_token"])' "$D/body" \
    > "$D/$WHO.csrf" 2>/dev/null || { fail "no csrf_token in the answer"; : > "$D/$WHO.csrf"; }
}
: > "$D/anon.cookie"; : > "$D/anon.csrf"
J='Content-Type: application/json'; O="Origin: $ORIGIN"
get() { local label=$1 want=$2 path=$3; shift 3; c "$label" "$want" GET "$path" -H @"$D/$WHO.cookie" "$@"; }
send() {  # send <label> <want> <method> <path> [python literal of the JSON body]
  local label=$1 want=$2 method=$3 path=$4
  if [ $# -ge 5 ]; then json "$5"; set -- -H "$J" -d @"$D/req"; else set --; fi
  c "$label" "$want" "$method" "$path" -H "$O" -H @"$D/$WHO.cookie" -H @"$D/$WHO.csrf" "$@"
}
submit() {  # submit <label> <want> <slug> <label> <language> <file> [curl args...]: a source as text/plain
  local label=$1 want=$2 slug=$3 plabel=$4 lang=$5 file=$6; shift 6
  MAXTIME=30 c "$label" "$want" POST "/api/contests/$slug/problems/$plabel/submissions?language=$lang" -H "$O" \
    -H @"$D/$WHO.cookie" -H @"$D/$WHO.csrf" -H 'Content-Type: text/plain; charset=utf-8' --data-binary @"$file" "$@"
}
wait_done() {  # wait_done <label> <submission id> [timeout s]: poll as the API says (poll_after_s) until done
  local label=$1 id=$2 limit=${3:-120} start=$SECONDS after
  while :; do
    curl -s --noproxy '*' --max-time 10 -o "$D/body" -H @"$D/$WHO.cookie" "$BASE/api/submissions/$id"
    after=$(python3 -c 'import json, sys; d = json.load(open(sys.argv[1]))
print("done" if d.get("status") == "done" else min(int(d.get("poll_after_s") or 2), 10))' "$D/body" 2>/dev/null)
    [ "$after" = done ] && { echo "OK   $label: done ($((SECONDS - start)) s)"; return 0; }
    [ -n "$after" ] || { fail "$label: not a submission: $(head -c 200 "$D/body")"; return 1; }
    [ $((SECONDS - start)) -ge "$limit" ] && { fail "$label: not done within $limit s"; return 1; }
    sleep "$after"
  done
}
login_student() {  # login_student s1|s2: the password from the file the import answer was written into
  WHO=$1
  c "$1 logs in (password; a student has no 2FA)"      200 POST /api/auth/login -H "$J" -H "$O" -d @"$D/$1.login"
  keep_cookie; keep_csrf
  check "$1 has a full session"                       "d['state'] == 'active' and d['user']['role'] == 'student'"
}

[ "$(curl -s --noproxy '*' --max-time 5 -o /dev/null -w '%{http_code}' "$BASE/api/auth/me")" = 401 ] || {
  echo "the public app does not answer at $BASE (start it: j .venv/bin/python -m app.public_main)"; exit 2; }
ask ADMIN_LOGIN "admin login"; ask ADMIN_PASSWORD "admin password" silent
[ ${#FROM_ENV[@]} = 0 ] || echo "from the environment (not asked): ${FROM_ENV[*]}"
python3 -c 'import json, os; print(json.dumps({"login": os.environ["ADMIN_LOGIN"], "password": os.environ["ADMIN_PASSWORD"]}))' > "$D/login"
echo "everything is named $SLUG / $SLUG2 / \"$G\""

echo "--- admin: login, a class of two, the problem, two contests"
c "admin password"                                    200 POST /api/auth/login -H "$J" -H "$O" -d @"$D/login"
keep_cookie; keep_csrf
ask TOTP_CODE "TOTP code from the phone (now)"
python3 -c 'import json, os; print(json.dumps({"code": os.environ["TOTP_CODE"]}))' > "$D/totp"
c "admin TOTP code"                                   200 POST /api/auth/totp -H "$J" -H "$O" -H @"$D/admin.cookie" -H @"$D/admin.csrf" -d @"$D/totp"
keep_cookie; keep_csrf
printf 'Фамилия;Имя;Класс\nУченик;Первый;%s\nУченик;Второй;%s\n' "$G" "$G" > "$D/class.csv"
c "import the class"                                  201 POST /api/admin/users/import -H "$O" -H @"$D/admin.cookie" -H @"$D/admin.csrf" \
                                                          -H 'Content-Type: text/csv' --data-binary @"$D/class.csv"
check "2 students in $G"                              "len(d['users']) == 2 and {u['group'] for u in d['users']} == {'$G'}"
python3 -c 'import json, sys
d = json.load(open(sys.argv[1]))
for i, u in enumerate(d["users"][:2], 1):
    with open(f"{sys.argv[2]}/s{i}.login", "w") as f:
        json.dump({"login": u["login"], "password": u["password"]}, f)
    with open(f"{sys.argv[2]}/s{i}.name", "w") as f:
        f.write(u["login"])' "$D/body" "$D" 2>/dev/null || fail "the passwords of the import"
for i in 1 2; do  # the import answers logins and passwords, not ids
  name=$(cat "$D/s$i.name" 2>/dev/null)
  get "find student $i"                               200 "/api/admin/users?q=$name"
  printf -v "S${i}ID" %s "$(val "next(u['id'] for u in d['items'] if u['login'] == '$name')" 2>/dev/null || echo 0)"
done
"$HERE/pack-problem.sh" "$PKG" > "$D/p.zip" || fail "pack sum-of-two"
c "upload sum-of-two (or it exists)"                  '200|201' POST /api/admin/problems -H "$O" -H @"$D/admin.cookie" -H @"$D/admin.csrf" \
                                                          -H 'Content-Type: application/zip' --data-binary @"$D/p.zip"
V=$(val 'd["version"]' 2>/dev/null || echo 0)
start=$SECONDS
until get "sum-of-two v$V" 200 "/api/admin/problems/sum-of-two/versions/$V" > /dev/null \
      && python3 -c 'import json, sys; sys.exit(json.load(open(sys.argv[1]))["status"] != "ready")' "$D/body" 2>/dev/null; do
  [ $((SECONDS - start)) -ge 120 ] && { fail "sum-of-two v$V is not ready within 120 s (a worker with problem_check?)"; break; }
  sleep 2
done
[ $((SECONDS - start)) -lt 120 ] && echo "OK   sum-of-two v$V is ready"
send "contest for the group, leaderboard visible"    201 PUT "/api/admin/contests/$SLUG" \
  "{'format_version': 1, 'slug': '$SLUG', 'title': 'Smoke $STAMP', 'starts_at': '2026-01-01 00:00',
    'ends_at': '2099-01-01 00:00', 'submit_cooldown_sec': 0, 'leaderboard': 'visible',
    'access': {'mode': 'groups', 'groups': ['$G']}, 'problems': [{'label': 'A', 'problem': 'sum-of-two'}]}"
send "publish it"                                     200 POST "/api/admin/contests/$SLUG/publish"
send "contest by code, personal 2h, pause 600 s"      201 PUT "/api/admin/contests/$SLUG2" \
  "{'format_version': 1, 'slug': '$SLUG2', 'title': 'Smoke $STAMP code', 'starts_at': '2026-01-01 00:00',
    'ends_at': '2099-01-01 00:00', 'timing_mode': 'personal', 'personal_duration': '2h', 'submit_cooldown_sec': 600,
    'access': {'mode': 'code'}, 'problems': [{'label': 'A', 'problem': 'sum-of-two'}]}"
send "publish it"                                     200 POST "/api/admin/contests/$SLUG2/publish"
get "its invite code"                                 200 "/api/admin/contests/$SLUG2"
python3 -c 'import json, sys; print(json.dumps({"code": json.load(open(sys.argv[1]))["invite_code"]}))' "$D/body" \
  > "$D/code.json" 2>/dev/null || fail "no invite code"
get "an admin is not a student"                       403 "/api/contests"
check "student_required"                              "d['detail'] == 'student_required'"
get "the load for an admin"                           200 /api/load
check "no place of mine"                              "d['my_position'] is None and 'level' in d and d['server_time'].endswith('Z')"
WHO=anon
get "no session"                                      401 "/api/contests"

echo "--- student 1: the contest of the class, a statement, OK / WA / CE"
login_student s1
get "my contests"                                     200 "/api/contests"
check "$SLUG is there, with access"                   "any(r['slug'] == '$SLUG' and r['has_access'] for r in d['items'])"
get "the contest"                                     200 "/api/contests/$SLUG"
check "problem A, contest groups with $G"             "[p['label'] for p in d['problems']] == ['A'] and [g['name'] for g in d['groups']] == ['$G']"
GID=$(val "d['groups'][0]['id']" 2>/dev/null || echo 0)
get "statement of A before entering"                  409 "/api/contests/$SLUG/problems/A"
check "not_participant"                               "d['detail'] == 'not_participant'"
send "enter $SLUG (the group, no code)"               200 POST "/api/contests/$SLUG/enter"
check "entered ok through the group"                  "d['entered'] == 'ok' and d['joined_via'] == 'group'"
send "enter again"                                    409 POST "/api/contests/$SLUG/enter"
check "already_joined"                                "d['detail'] == 'already_joined'"
get "statement of A"                                  200 "/api/contests/$SLUG/problems/A"
check "a statement and open examples"                 "d['statements'] and d['examples'] and 'python' in [l['key'] if isinstance(l, dict) else l for l in d['languages']]"
cp "$PKG/solutions/ok.py" "$D/ok.py"
printf 'a, b = map(int, input().split())\nprint(a + b + 1)\n' > "$D/wa.py"
printf 'int main( {\n' > "$D/ce.cpp"
submit "OK solution"                                  201 "$SLUG" A python "$D/ok.py"
OKID=$(val 'd["id"]' 2>/dev/null || echo 0)
get "it right away"                                   200 "/api/submissions/$OKID"
check "queued/judging with poll_after_s, or done without" \
      "('poll_after_s' in d and 2 <= d['poll_after_s'] <= 10) if d['status'] != 'done' else 'poll_after_s' not in d"
submit "WA solution"                                  201 "$SLUG" A python "$D/wa.py"
WAID=$(val 'd["id"]' 2>/dev/null || echo 0)
submit "CE solution"                                  201 "$SLUG" A cpp "$D/ce.cpp"
CEID=$(val 'd["id"]' 2>/dev/null || echo 0)
wait_done "OK" "$OKID"; check "verdict OK, full score, no poll" "d['verdict'] == 'OK' and float(d['score']) == float(d['max_score']) and 'poll_after_s' not in d"
wait_done "WA" "$WAID"; check "verdict WA, no points"  "d['verdict'] == 'WA' and float(d['score']) == 0"
wait_done "CE" "$CEID"; check "verdict CE with its log" "d['verdict'] == 'CE' and d.get('compile_log')"
c "another body type"                                 415 POST "/api/contests/$SLUG/problems/A/submissions?language=python" -H "$O" \
                                                          -H @"$D/s1.cookie" -H @"$D/s1.csrf" -H 'Content-Type: image/png' --data-binary @"$D/ok.py"
check "unsupported_media_type"                        "d['detail'] == 'unsupported_media_type'"
python3 -c 'import sys; open(sys.argv[1], "w").write("# x\n" * (300 * 1024 // 4))' "$D/big.py"
submit "300 KB source with Content-Length"            413 "$SLUG" A python "$D/big.py"
check "source_too_large"                              "d['detail'] == 'source_too_large'"
submit "300 KB source, chunked"                       413 "$SLUG" A python "$D/big.py" -H 'Transfer-Encoding: chunked'
check "source_too_large"                              "d['detail'] == 'source_too_large'"
get "my submissions in $SLUG"                         200 "/api/submissions?contest=$SLUG"
check "exactly the three, newest first"               "[i['id'] for i in d['items']] == [$CEID, $WAID, $OKID]"
get "my source"                                       200 "/api/submissions/$OKID/source"
cmp -s "$D/body" "$D/ok.py" && echo "OK   the source byte for byte" || fail "the source differs"

echo "--- student 2: not another's submission; the contest by code, the personal start, the pause"
login_student s2
get "s1's submission"                                 404 "/api/submissions/$OKID"
check "submission_not_found"                          "d['detail'] == 'submission_not_found'"
get "s1's source"                                     404 "/api/submissions/$OKID/source"
send "enter $SLUG2 with a wrong code"                 403 POST "/api/contests/$SLUG2/enter" "{'code': 'not-the-code'}"
check "wrong_code"                                    "d['detail'] == 'wrong_code'"
c "enter with the right code"                         200 POST "/api/contests/$SLUG2/enter" -H "$O" -H @"$D/s2.cookie" -H @"$D/s2.csrf" \
                                                          -H "$J" -d @"$D/code.json"
check "entered ok, not started"                       "d['entered'] == 'ok' and d['started_at'] is None"
get "statement before the start"                      409 "/api/contests/$SLUG2/problems/A"
check "press_start"                                   "d['detail'] == 'press_start'"
send "start"                                          200 POST "/api/contests/$SLUG2/start"
check "started, the window is 2 h"                    "d['started_at'] and d['window']"
send "start again"                                    409 POST "/api/contests/$SLUG2/start"
submit "a submission"                                 201 "$SLUG2" A python "$D/ok.py"
submit "another one within the pause"                 429 "$SLUG2" A python "$D/ok.py"
check "cooldown with retry_after_s"                   "d['detail'] == 'cooldown' and 0 < d['retry_after_s'] <= 600"
header "Retry-After header"                           '^retry-after: [0-9]+$'
get "the hidden leaderboard of $SLUG2"                403 "/api/contests/$SLUG2/standings"
check "standings_closed"                              "d['detail'] == 'standings_closed'"

echo "--- the leaderboard of $SLUG (time measured), the load"
WHO=s1
for q in "" "?group=$GID"; do
  : > "$D/times"
  for _ in 1 2 3 4 5; do
    curl -s --noproxy '*' --max-time 10 -o "$D/body" -w '%{http_code} %{time_total}\n' -H @"$D/s1.cookie" \
      "$BASE/api/contests/$SLUG/standings$q" >> "$D/times"
  done
  python3 - "$D/times" "standings$q" <<'EOF' || FAILS=$((FAILS + 1))
import sys
rows = [line.split() for line in open(sys.argv[1])]
codes = {c for c, _ in rows}
ms = sorted(float(t) * 1000 for _, t in rows)
mark = "OK  " if codes == {"200"} and ms[2] < 2000 else "FAIL"
print(f"{mark} {sys.argv[2]}: 5 requests, ms min {ms[0]:.0f} / median {ms[2]:.0f} / max {ms[-1]:.0f} (FAIL above a 2000 ms median)")
sys.exit(mark != "OK  ")
EOF
  check "s1 is me, first, with the full points of A"  "any(r['me'] and r['place'] == 1 and float(r['score']) == 100 for r in d['rows'])"
done
get "a group that is not of this contest"            404 "/api/contests/$SLUG/standings?group=2147483647"
get "the load for a student"                          200 /api/load
check "level, queue, workers"                         "d['level'] in ('free', 'busy', 'overloaded', 'offline') and d['workers_online'] >= 1"

echo "--- the rate of submissions (JUDGE_SUBMIT_RATE_PER_MIN, 6 by default): 429 rate_limited"
limited=0
for i in $(seq 1 12); do
  code=$(curl -s --noproxy '*' --max-time 30 -D "$D/hdr" -o "$D/body" -w '%{http_code}' -X POST \
         "$BASE/api/contests/$SLUG/problems/A/submissions?language=python" -H "$O" -H @"$D/s1.cookie" -H @"$D/s1.csrf" \
         -H 'Content-Type: text/plain' --data-binary @"$D/ok.py")
  if [ "$code" = 429 ]; then limited=$i; break; fi
  [ "$code" = 201 ] || { fail "submission $i of the burst: $code $(head -c 200 "$D/body")"; break; }
done
if [ "$limited" != 0 ]; then
  echo "OK   429 after $((limited - 1)) more submissions (3 before them in this minute)"
  check "rate_limited with retry_after_s"             "d['detail'] == 'rate_limited' and d['retry_after_s'] > 0"
  header "Retry-After header"                         '^retry-after: [0-9]+$'
else
  echo "SKIP rate_limited: 12 more were accepted (JUDGE_SUBMIT_RATE_PER_MIN is 0 or above 15 here)"; SKIPS=$((SKIPS + 1))
fi

echo "--- clean-up (admin): the contests and the group archived, the students disabled"
WHO=admin
send "archive $SLUG"                                  200 POST "/api/admin/contests/$SLUG/archive"
send "archive $SLUG2"                                 200 POST "/api/admin/contests/$SLUG2/archive"
for id in "$S1ID" "$S2ID"; do send "disable student $id" 200 POST "/api/admin/users/$id/disable"; done
send "archive the group"                              200 POST "/api/admin/groups/$GID/archive"
WHO=s1
get "a disabled student's session is gone"            401 "/api/contests"

echo "failures: $FAILS, skipped: $SKIPS"
exit "$FAILS"
