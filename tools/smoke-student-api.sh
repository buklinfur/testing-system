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
# The class (4b.2, D-104): an assignment and two materials made by the admin; the feed, the page with its link to the
# memo, a draft of a PNG, a PDF and a HEIC photo (the PNG inline, the HEIC only as a download), a repeated name and
# an SVG 422, a file deleted, the hand-in by the ids of the draft, a return and the hand-in again through from-last,
# a hand-in over a grade not released (the teacher sees it handed in again, that grade never goes out), a released
# grade closes the hand-in, the own works; the shared and the own private thread, the teacher's answer, the read mark,
# deleting one's own, the comments rate (429 rate_limited + Retry-After, if JUDGE_COMMENT_RATE_PER_MIN is at most
# 14); the library, its search, a material and its file; another student's draft file, hand-in file, private
# comment and up_to 404, their shared comment 403, a material in no library 404.
# At the end the admin archives both contests, the assignment, the materials and the group and disables both
# students (they have submissions, so they cannot be deleted).
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

echo "--- the class (4b.2): the feed, a draft, hand-ins, a return, from-last, a grade not released, comments, materials"
WHO=admin
mup() {  # mup <label> <material id> <name> <file>: a file of a new version of the material (the admin API)
  c "$1" 201 POST "/api/admin/materials/$2/attachments?name=$3" -H "$O" -H @"$D/admin.cookie" -H @"$D/admin.csrf" \
    -H 'Content-Type: application/octet-stream' --data-binary @"$4"
}
dup() {  # dup <label> <want> <percent-encoded name> <file>: one more file of the draft of $WHO on $ITEM
  c "$WHO: $1" "$2" POST "/api/feed/$ITEM/draft/files?name=$3" -H "$O" -H @"$D/$WHO.cookie" -H @"$D/$WHO.csrf" \
    -H 'Content-Type: application/octet-stream' --data-binary @"$4"
}
python3 -c 'import sys; open(sys.argv[1], "wb").write(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 40)' "$D/pic.png"
python3 -c 'import sys; open(sys.argv[1], "wb").write(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64)' "$D/photo.heic"
printf '%%PDF-1.7\n%% smoke %s\n' "$STAMP" > "$D/report.pdf"
printf '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>' > "$D/x.svg"
printf 'for i in range(3):\n    print(i)\n' > "$D/loops.py"
send "a material in the library for every student"    201 POST /api/admin/materials \
  "{'slug': '$SLUG-memo', 'title': 'Памятка $STAMP', 'body_md': 'Циклы: for и while', 'library': 'all_students'}"
MAT=$(val 'd["id"]' 2>/dev/null || echo 0)
mup "its file"                                        "$MAT" loops.py "$D/loops.py"
send "a material in no library"                       201 POST /api/admin/materials \
  "{'slug': '$SLUG-hidden', 'title': 'Ответы $STAMP', 'body_md': 'для учителя'}"
HIDDEN=$(val 'd["id"]' 2>/dev/null || echo 0)
send "an assignment for the class: files or text, points released by hand, comments on" 201 POST /api/admin/items \
  "{'kind': 'assignment', 'title': 'Проект $STAMP', 'body_md': 'Сделай. См. [[памятка:$SLUG-memo]]', 'grading': 'points',
    'max_points': 10, 'grades_release': 'manual', 'answer_kind': 'text_or_files', 'due_at': '2099-01-01 00:00',
    'comments_enabled': True, 'group_ids': [$GID]}"
ITEM=$(val 'd["id"]' 2>/dev/null || echo 0)
send "publish it"                                     200 POST "/api/admin/items/$ITEM/publish"

WHO=s1
get "the feed"                                        200 /api/feed
check "the assignment, its own deadline in UTC"       "any(i['id'] == $ITEM and i['assignment']['due_at'].endswith('Z') and i['assignment']['status'] is None for i in d['items'])"
get "the page of the assignment"                      200 "/api/feed/$ITEM"
check "the statement, the memo link open, can hand in, no draft" \
      "d['body_md'] and d['links'] == [{'slug': '$SLUG-memo', 'available': True, 'id': $MAT, 'title': 'Памятка $STAMP'}] and d['can_submit']['allowed'] and d['draft'] == []"
dup "a PNG under a Cyrillic name"                     201 "%D1%81%D1%85%D0%B5%D0%BC%D0%B0.png" "$D/pic.png"
check "the file, and the draft with expires_at"       "d['file']['name'] == 'схема.png' and len(d['draft']) == 1 and d['draft'][0]['expires_at'].endswith('Z') and 'sha256' not in d['file']"
DPNG=$(val 'd["file"]["id"]' 2>/dev/null || echo 0)
dup "a PDF"                                           201 report.pdf "$D/report.pdf"
DPDF=$(val 'd["file"]["id"]' 2>/dev/null || echo 0)
dup "a HEIC photo from a phone"                       201 photo.heic "$D/photo.heic"
check "three files in the draft"                      "[f['name'] for f in d['draft']] == ['схема.png', 'report.pdf', 'photo.heic']"
DHEIC=$(val 'd["file"]["id"]' 2>/dev/null || echo 0)
dup "the same name again"                             422 REPORT.PDF "$D/report.pdf"
check "duplicate_name"                                "d['detail'] == 'duplicate_name'"
dup "an SVG"                                          422 x.svg "$D/x.svg"
check "file_type_not_allowed"                         "d['detail'] == 'file_type_not_allowed'"
get "the PNG of the draft"                            200 "/api/feed/$ITEM/draft/files/$DPNG"
header "a picture: image/png"                      '^content-type: image/png'
header "inline"                                       '^content-disposition: inline'
cmp -s "$D/body" "$D/pic.png" && echo "OK   the same bytes" || fail "the PNG of the draft differs"
get "the HEIC of the draft"                           200 "/api/feed/$ITEM/draft/files/$DHEIC"
header "HEIC: never inline, a download"               '^content-disposition: attachment'
header "nosniff"                                      '^x-content-type-options: nosniff'
send "delete the PDF from the draft"                  204 POST "/api/feed/$ITEM/draft/files/$DPDF/delete"
get "the draft"                                       200 "/api/feed/$ITEM/draft"
check "the PNG and the HEIC are left"                 "[f['name'] for f in d['draft']] == ['схема.png', 'photo.heic']"

WHO=s2
get "s1's draft file, as s2"                          404 "/api/feed/$ITEM/draft/files/$DPNG"
check "draft_file_not_found"                          "d['detail'] == 'draft_file_not_found'"
send "s1's draft file in s2's hand-in"                404 POST "/api/feed/$ITEM/work" "{'text': 'x', 'files': [$DPNG]}"
check "draft_file_not_found"                          "d['detail'] == 'draft_file_not_found'"

WHO=s1
send "hand in the text and two files"                 201 POST "/api/feed/$ITEM/work" "{'text': 'Мой проект', 'files': [$DPNG, $DHEIC]}"
check "submitted, not late"                           "d['work']['status'] == 'submitted' and d['submission']['late'] is False"
get "the page: the hand-in, the draft empty"          200 "/api/feed/$ITEM"
check "one hand-in with both files"                   "len(d['hand_ins']) == 1 and [f['name'] for f in d['hand_ins'][0]['files']] == ['схема.png', 'photo.heic'] and d['draft'] == []"
WFILE=$(val 'd["hand_ins"][0]["files"][0]["id"]' 2>/dev/null || echo 0)
get "a file of the own hand-in"                       200 "/api/feed/$ITEM/work/files/$WFILE"
cmp -s "$D/body" "$D/pic.png" && echo "OK   the same bytes" || fail "the file of the hand-in differs"
WHO=s2
get "s1's file of the hand-in, as s2"                 404 "/api/feed/$ITEM/work/files/$WFILE"
check "file_not_found"                                "d['detail'] == 'file_not_found'"

WHO=admin
send "return s1's work"                               200 POST "/api/admin/items/$ITEM/works/$S1ID/return" "{'comment': 'добавь вывод'}"
WHO=s1
get "the page after the return"                       200 "/api/feed/$ITEM"
check "returned, with the comment"                    "d['assignment']['status'] == 'returned' and d['assignment']['returned_comment'] == 'добавь вывод'"
send "from-last: the files of the hand-in back"       200 POST "/api/feed/$ITEM/draft/from-last"
check "the PNG and the HEIC in the draft again"       "[f['name'] for f in d['draft']] == ['схема.png', 'photo.heic']"
FILES=$(val '[f["id"] for f in d["draft"]]' 2>/dev/null || echo '[]')
send "hand in again with them"                        201 POST "/api/feed/$ITEM/work" "{'text': 'Мой проект, с выводом', 'files': $FILES}"
check "submitted again"                               "d['work']['status'] == 'submitted' and d['work']['submissions'] == 2"

WHO=admin
send "grade s1: 7, not released"                      200 POST "/api/admin/items/$ITEM/works/$S1ID/grade" "{'score': 7}"
WHO=s1
get "the page with a grade not released"              200 "/api/feed/$ITEM"
check "no grade seen, still can hand in"              "d['assignment']['status'] == 'submitted' and d['assignment']['grade'] is None and d['can_submit']['allowed']"
send "hand in over the grade not released"            201 POST "/api/feed/$ITEM/work" "{'text': 'Исправил ещё'}"
check "submitted, 3 hand-ins"                         "d['work']['status'] == 'submitted' and d['work']['submissions'] == 3"
WHO=admin
get "the teacher's card"                              200 "/api/admin/items/$ITEM/works/$S1ID"
check "handed in again after a grade; the grade for the earlier hand-in" \
      "d['resubmitted_after_grade'] and [e['superseded'] for e in d['events'] if e.get('action') == 'graded'] == [True]"
send "release: that grade never goes out"            200 POST "/api/admin/items/$ITEM/release" "{'user_ids': [$S1ID]}"
check "nothing_to_release"                            "[r['result'] for r in d['results']] == ['nothing_to_release']"
send "grade s1: 8"                                    200 POST "/api/admin/items/$ITEM/works/$S1ID/grade" "{'score': 8}"
send "release it"                                     200 POST "/api/admin/items/$ITEM/release" "{'user_ids': [$S1ID]}"
check "released"                                      "[r['result'] for r in d['results']] == ['released']"
WHO=s1
get "the page with the released grade"                200 "/api/feed/$ITEM"
check "graded 8.00"                                   "d['assignment']['status'] == 'graded' and d['assignment']['grade']['score'] == '8.00'"
send "hand in over a released grade"                  409 POST "/api/feed/$ITEM/work" "{'text': 'ещё'}"
check "already_graded"                                "d['detail'] == 'already_graded'"
get "my works"                                        200 /api/works
check "this one graded 8.00, 3 hand-ins"              "any(w['item']['id'] == $ITEM and w['grade']['score'] == '8.00' and w['submissions'] == 3 for w in d['items'])"

echo "--- comments: the shared thread, the own private thread, others' comments, the rate"
send "a comment in the shared thread"                 201 POST "/api/feed/$ITEM/comments" "{'body': 'Вопрос всем $STAMP'}"
SHARED=$(val 'd["id"]' 2>/dev/null || echo 0)
send "a question to the teacher"                      201 POST "/api/feed/$ITEM/my-thread" "{'body': 'Можно сдать позже?'}"
PRIV=$(val 'd["id"]' 2>/dev/null || echo 0)
WHO=admin
send "the teacher answers s1"                         201 POST "/api/admin/items/$ITEM/students/$S1ID/comments" "{'body': 'Да'}"
WHO=s1
get "the own private thread"                          200 "/api/feed/$ITEM/my-thread"
check "the question and the answer, 1 unread, no login" \
      "[c['body'] for c in d['comments']] == ['Можно сдать позже?', 'Да'] and d['unread'] == 1 and d['comments'][1]['author']['teacher'] and 'login' not in d['comments'][1]['author']"
send "mark it read"                                   200 POST "/api/feed/$ITEM/my-thread/read"
check "unread 0"                                      "d == {'unread': 0}"
WHO=s2
get "s2's own private thread"                         200 "/api/feed/$ITEM/my-thread"
check "nothing of s1's thread"                        "d['comments'] == [] and d['thread_id'] is None"
send "up_to from s1's thread"                         404 POST "/api/feed/$ITEM/my-thread/read" "{'up_to': $PRIV}"
check "comment_not_found"                             "d['detail'] == 'comment_not_found'"
send "delete s1's private comment"                    404 POST "/api/comments/$PRIV/delete"
check "comment_not_found"                             "d['detail'] == 'comment_not_found'"
send "delete s1's shared comment"                     403 POST "/api/comments/$SHARED/delete"
check "not_your_comment"                              "d['detail'] == 'not_your_comment'"
WHO=s1
send "delete the own shared comment"                  204 POST "/api/comments/$SHARED/delete"
WHO=s2
limited=0
for i in $(seq 1 15); do
  code=$(curl -s --noproxy '*' --max-time 10 -D "$D/hdr" -o "$D/body" -w '%{http_code}' -X POST "$BASE/api/feed/$ITEM/comments" \
         -H "$O" -H @"$D/s2.cookie" -H @"$D/s2.csrf" -H "$J" -d "{\"body\": \"burst $i\"}")
  if [ "$code" = 429 ]; then limited=$i; break; fi
  [ "$code" = 201 ] || { fail "comment $i of the burst: $code $(head -c 200 "$D/body")"; break; }
done
if [ "$limited" != 0 ]; then
  echo "OK   429 after $((limited - 1)) comments"
  check "rate_limited with retry_after_s"             "d['detail'] == 'rate_limited' and 1 <= d['retry_after_s'] <= 60"
  header "Retry-After header"                         '^retry-after: [0-9]+$'
else
  echo "SKIP comments rate_limited: 15 were accepted (JUDGE_COMMENT_RATE_PER_MIN is 0 or above 15 here)"; SKIPS=$((SKIPS + 1))
fi

echo "--- materials and «Справочник»"
WHO=s1
get "the library"                                     200 /api/library
check "the memo is there, the hidden one is not"      "'$SLUG-memo' in [m['slug'] for m in d['unsorted']] and '$SLUG-hidden' not in str(d)"
get "search in lower case Cyrillic"                   200 /api/library -G --data-urlencode "q=памятка $STAMP"
check "finds the memo"                                "[m['slug'] for m in d['materials']] == ['$SLUG-memo']"
get "the memo"                                        200 "/api/materials/$MAT"
check "its text and its file, no sha256"              "d['body_md'] == 'Циклы: for и while' and [a['name'] for a in d['attachments']] == ['loops.py'] and 'sha256' not in str(d)"
MATT=$(val 'd["attachments"][0]["id"]' 2>/dev/null || echo 0)
get "its file"                                        200 "/api/materials/$MAT/attachments/$MATT"
cmp -s "$D/body" "$D/loops.py" && echo "OK   the same bytes" || fail "the file of the memo differs"
get "a material in no library"                        404 "/api/materials/$HIDDEN"
check "material_not_found, as no material"            "d['detail'] == 'material_not_found'"
get "a file of the memo under the hidden one"         404 "/api/materials/$HIDDEN/attachments/$MATT"
get "an item of nobody"                               404 /api/feed/2147483647
check "item_not_found"                                "d['detail'] == 'item_not_found'"

WHO=admin
send "archive the assignment"                         200 POST "/api/admin/items/$ITEM/archive"
send "archive the memo (a material is never deleted)" 200 POST "/api/admin/materials/$MAT/archive"
send "archive the hidden one"                         200 POST "/api/admin/materials/$HIDDEN/archive"

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
