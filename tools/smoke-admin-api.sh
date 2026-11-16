#!/usr/bin/env bash
# Smoke test of the admin API (docs/ADMIN_API.md) with curl, against a running public app: the check scenario of
# session 4a.3a through the API: a class from CSV with a class column, a contest for a group, adding and removing a
# student, deadlines with the policies late and no, an extension for one student, submissions and a rejudge in two
# steps, the rollback of the import (K3), the profile time zone, system, audit, languages; and of session 4a.4:
# uploaded problems are checked by a worker (it waits for ready, up to 120 s), a package whose checker does not
# compile (tests/problems/check-broken-checker) ends failed and cannot be pinned, a recheck runs once and ends ready;
# and of session 4v.1b (the class feed, docs/ADMIN_API.md §5.8): an assignment for a group, hand-ins of the students, an
# extension, grades, a return, a hand-in again, a regrade, a release per student, the filters of the review queue,
# archiving the group answers its feed items.
# And of session 4b.0 (docs/FILES.md): a statement picture under a Cyrillic name, a PNG under .jpg and HTML under .png
# are served by content and name together, with ETag and 304; the import warns about the fake picture.
# And of session 4v.2b (comments, docs/ADMIN_API.md §5.9): a student refused while comments are off, then a shared and
# a private comment of the students, the inbox awaiting reply, untrusted text byte for byte,
# a teacher's answer, the read mark, soft deletion and again; NUL and a lone surrogate are 422 (a student's 429 is in
# tools/smoke-student-api.sh).
# And of session 4v.3b (files and links, docs/ADMIN_API.md §5.10): attachments of a teacher (a Cyrillic name, a
# picture, a link), SVG / HTML under .png / .exe / a name not in UTF-8 / a repeated name are 422, another
# Content-Type 415; downloads with nosniff, the sandbox CSP and 304; the order and removal; a hand-in with files and
# a link (the draft uploaded file by file), the files of the work downloaded, another student's file 404.
# And of session 4v.4b (materials, docs/ADMIN_API.md §5.11): a material linking a slug nobody has (a warning), a
# new version linking another material, the same text gives no version, another slug is 422 slug_immutable, an
# assignment linking a material and an unknown slug (a warning, the draft deleted), resolve, a section with a
# subsection, the search in lower case Cyrillic, a file of a version (downloaded; not a file of the version before),
# archive (twice: changed false), the sections deleted. The materials smoke-<time>-memo / -loops stay, archived:
# a material is never deleted.
# A worker with the capability problem_check must be running (judgectl worker list: CAPS).
#   tools/smoke-admin-api.sh [BASE_URL] [ORIGIN]       defaults: http://127.0.0.1:8000 and $JUDGE_PUBLIC_ORIGIN
# It asks for an admin (login, password, a TOTP code from the phone); ADMIN_LOGIN, ADMIN_PASSWORD and TOTP_CODE
# from the environment are used instead (named, never shown). Secrets reach curl only through files in a private
# temporary directory and are never printed.
#
# The students: the admin creates two of them (their passwords go into files of the private directory, never shown)
# and they act through the student API of 4b (docs/STUDENT_API.md §6): the entry and the submissions of the contest,
# the hand-ins with their draft files, the comments (4b.2, D-104; no dev_seed any more). They have submissions, so at
# the end they are disabled, not deleted.
#
# Everything it makes is named smoke-<time> (contest, groups, students); the contest is archived at the end, the
# imported students are rolled back, the two students of the run disabled, the problem check-broken-checker is
# archived (and unarchived on the next run).
# Every request gives up after 10 s. Exit code = number of failures.
set -uo pipefail
BASE=${1:-http://127.0.0.1:8000}; ORIGIN=${2:-${JUDGE_PUBLIC_ORIGIN:-}}
[ -n "$ORIGIN" ] || { echo "usage: $0 [BASE_URL] ORIGIN   (ORIGIN = JUDGE_PUBLIC_ORIGIN of the server)"; exit 2; }
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(dirname "$HERE")
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT; chmod 700 "$D"
FAILS=0; SKIPS=0
STAMP=$(date +%m%d%H%M%S); SLUG="smoke-$STAMP"; G1="smoke $STAMP"; G2="smoke $STAMP b"

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
  code=$(curl -s --noproxy '*' --max-time 10 -D "$D/hdr" -o "$D/body" -w '%{http_code}' -X "$method" "$BASE$path" "$@")
  [[ $code =~ ^($want)$ ]] || { mark=FAIL; FAILS=$((FAILS + 1)); }
  printf '%-4s %-56s %s (want %s)  %s\n' "$mark" "$label" "$code" "$want" \
    "$(python3 -c 'import json, sys
try: d = json.load(open(sys.argv[1]))
except Exception: sys.exit()
if isinstance(d, dict):
    d.pop("csrf_token", None)
    d.pop("password", None)
    for u in d.get("users", []) if isinstance(d.get("users"), list) else []:
        u.pop("password", None)
print(json.dumps(d, ensure_ascii=False)[:140])' "$D/body")"
}
check() {  # check <label> <python expression over d = the last JSON body>
  if python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); sys.exit(0 if eval(sys.argv[2]) else 1)' \
      "$D/body" "$2" 2>/dev/null; then echo "OK   $1"; else fail "$1"; fi
}
poll() {  # poll <label> <path> <python condition over d> [timeout s]: GET quietly until it holds
  local label=$1 path=$2 cond=$3 limit=${4:-120} start=$SECONDS
  while :; do
    curl -s --noproxy '*' --max-time 10 -o "$D/body" -H @"$D/cookie" "$BASE$path"
    if python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); sys.exit(0 if eval(sys.argv[2]) else 1)' \
        "$D/body" "$cond" 2>/dev/null; then echo "OK   $label ($((SECONDS - start)) s)"; return 0; fi
    [ $((SECONDS - start)) -ge "$limit" ] && { fail "$label: not within $limit s: $(head -c 200 "$D/body")"; return 1; }
    sleep 2
  done
}
val() { python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); print(eval(sys.argv[2]))' "$D/body" "$1"; }
json() { python3 -c 'import json, sys; print(json.dumps(eval(sys.argv[1]), ensure_ascii=False))' "$1" > "$D/req"; }
keep_cookie() {
  local v
  v=$(sed -n 's/^set-cookie: __Host-session=\([^;]*\);.*/\1/Ip' "$D/hdr" | tr -d '\r' | tail -1)
  [ -n "$v" ] || fail "no session cookie in the answer"
  printf 'Cookie: __Host-session=%s\n' "$v" > "$D/cookie"
}
keep_csrf() {
  python3 -c 'import json, sys; print("X-CSRF-Token: " + json.load(open(sys.argv[1]))["csrf_token"])' "$D/body" \
    > "$D/csrf" 2>/dev/null || { fail "no csrf_token in the answer"; : > "$D/csrf"; }
}
J='Content-Type: application/json'; O="Origin: $ORIGIN"
get() { local label=$1 want=$2 path=$3; shift 3; c "$label" "$want" GET "$path" -H @"$D/cookie" "$@"; }
send() {  # send <label> <want> <method> <path> [python literal of the JSON body]
  local label=$1 want=$2 method=$3 path=$4
  if [ $# -ge 5 ]; then json "$5"; set -- -H "$J" -d @"$D/req"; else set --; fi
  c "$label" "$want" "$method" "$path" -H "$O" -H @"$D/cookie" -H @"$D/csrf" "$@"
}
student_login() {  # student_login <i>: a session of student i from the login and password kept in $D/s<i>.login
  c "s$1 logs in (a student has no 2FA)"              200 POST /api/auth/login -H "$J" -H "$O" -d @"$D/s$1.login"
  sed -n 's/^set-cookie: __Host-session=\([^;]*\);.*/Cookie: __Host-session=\1/Ip' "$D/hdr" | tr -d '\r' | tail -1 > "$D/s$1.cookie"
  [ -s "$D/s$1.cookie" ] || fail "no session cookie of s$1"
  python3 -c 'import json, sys; print("X-CSRF-Token: " + json.load(open(sys.argv[1]))["csrf_token"])' "$D/body" \
    > "$D/s$1.csrf" 2>/dev/null || { fail "no csrf_token of s$1"; : > "$D/s$1.csrf"; }
}
as_s() {  # as_s <i> <label> <want> <method> <path> [python literal of the JSON body]: a request of student i
  local i=$1 label=$2 want=$3 method=$4 path=$5
  if [ $# -ge 6 ]; then json "$6"; set -- -H "$J" -d @"$D/req"; else set --; fi
  c "s$i: $label" "$want" "$method" "$path" -H "$O" -H @"$D/s$i.cookie" -H @"$D/s$i.csrf" "$@"
}
s_get() { local i=$1 label=$2 want=$3 path=$4; shift 4; c "s$i: $label" "$want" GET "$path" -H @"$D/s$i.cookie" "$@"; }
s_submit() {  # s_submit <i> <label> <want> <problem label> <file>: a Python source to the contest $SLUG
  c "s$1: $2" "$3" POST "/api/contests/$SLUG/problems/$4/submissions?language=python" -H "$O" -H @"$D/s$1.cookie" \
    -H @"$D/s$1.csrf" -H 'Content-Type: text/plain' --data-binary @"$5"
}
s_up() {  # s_up <i> <label> <want> <item> <percent-encoded name> <file>: one more file of student i's draft
  c "s$1: $2" "$3" POST "/api/feed/$4/draft/files?name=$5" -H "$O" -H @"$D/s$1.cookie" -H @"$D/s$1.csrf" \
    -H 'Content-Type: application/octet-stream' --data-binary @"$6"
}

[ "$(curl -s --noproxy '*' --max-time 5 -o /dev/null -w '%{http_code}' "$BASE/api/auth/me")" = 401 ] || {
  echo "the public app does not answer at $BASE (start it: j .venv/bin/python -m app.public_main)"; exit 2; }
ask ADMIN_LOGIN "admin login"; ask ADMIN_PASSWORD "admin password" silent
[ ${#FROM_ENV[@]} = 0 ] || echo "from the environment (not asked): ${FROM_ENV[*]}"
python3 -c 'import json, os; print(json.dumps({"login": os.environ["ADMIN_LOGIN"], "password": os.environ["ADMIN_PASSWORD"]}))' > "$D/login"
echo "everything is named $SLUG / \"$G1\""

echo "--- admin login"
c "password"                                          200 POST /api/auth/login -H "$J" -H "$O" -d @"$D/login"
keep_cookie; keep_csrf
ask TOTP_CODE "TOTP code from the phone (now)"
python3 -c 'import json, os; print(json.dumps({"code": os.environ["TOTP_CODE"]}))' > "$D/totp"
c "TOTP code"                                         200 POST /api/auth/totp -H "$J" -H "$O" -H @"$D/cookie" -H @"$D/csrf" -d @"$D/totp"
keep_cookie; keep_csrf
get "admin me"                                        200 /api/admin/me
c "groups without a session"                          401 GET /api/admin/groups

echo "--- two students of this run: they act through the student API"
for i in 1 2; do
  send "create student $i"                            201 POST /api/admin/users "{'surname': 'Смоук', 'first_name': ['Первый', 'Второй'][$i - 1]}"
  python3 -c 'import json, sys
d = json.load(open(sys.argv[1]))
json.dump({"login": d["user"]["login"], "password": d["password"]}, open(sys.argv[2], "w"))' "$D/body" "$D/s$i.login" \
    2>/dev/null || fail "the password of student $i"
  printf -v "U$i" %s "$(val 'd["user"]["id"]' 2>/dev/null || echo 0)"
  printf -v "L$i" %s "$(val 'd["user"]["login"]' 2>/dev/null || echo none)"
done
echo "the students: $L1 ($U1), $L2 ($U2)"
student_login 1; student_login 2

echo "--- a class from CSV with the column Класс (В4): accounts and two groups"
printf 'Фамилия;Имя;Класс\nСмоук;Анна;%s\nСмоук;Борис;%s\nСмоук;Вера;%s\n' "$G1" "$G1" "$G2" > "$D/class.csv"
c "import the class"                                  201 POST /api/admin/users/import -H "$O" -H @"$D/cookie" -H @"$D/csrf" \
                                                          -H 'Content-Type: text/csv' --data-binary @"$D/class.csv"
check "3 students, each with their class"             "len(d['users']) == 3 and sorted({u['group'] for u in d['users']}) == sorted(['$G1', '$G2'])"
IMPORT_ID=$(val 'd["import_id"]' 2>/dev/null || echo 0)
get "find the groups"                                 200 "/api/admin/groups?q=smoke%20$STAMP"
G1ID=$(val "next(g['id'] for g in d['items'] if g['name'] == '$G1')" 2>/dev/null || echo 0)
G2ID=$(val "next(g['id'] for g in d['items'] if g['name'] == '$G2')" 2>/dev/null || echo 0)
get "group $G1"                                       200 "/api/admin/groups/$G1ID"
check "2 students in it"                              "len(d['students']) == 2"

echo "--- problems (the same content again: 200 exists), checked by a worker; a contest only for the group"
for p in sum-of-two pair-with-sum; do
  "$HERE/pack-problem.sh" "$ROOT/problems-examples/$p" > "$D/$p.zip" || fail "pack $p"
  c "upload $p"                                       '200|201' POST /api/admin/problems -H "$O" -H @"$D/cookie" -H @"$D/csrf" \
                                                          -H 'Content-Type: application/zip' --data-binary @"$D/$p.zip"
  V=$(val 'd["version"]' 2>/dev/null || echo 0)
  eval "V_${p//-/_}=$V"
  poll "$p v$V is ready (the worker's check)"         "/api/admin/problems/$p/versions/$V" "d['status'] == 'ready'"
done
CONTEST="{'format_version': 1, 'slug': '$SLUG', 'title': 'Smoke $STAMP', 'starts_at': '2026-01-01 00:00',
          'ends_at': '2099-01-01 00:00', 'submit_cooldown_sec': 0, 'access': {'mode': 'groups', 'groups': ['$G1']},
          'problems': [{'label': 'A', 'problem': 'sum-of-two', 'due_at': '2026-01-02 00:00:00', 'after_due': {'submit': 'late'}},
                       {'label': 'B', 'problem': 'pair-with-sum', 'due_at': '2026-01-02 00:00:00', 'after_due': {'submit': 'no'}}]}"
send "create the contest (PUT contest.yaml as JSON)"  201 PUT "/api/admin/contests/$SLUG" "$CONTEST"
check "no warnings"                                   "d['warnings'] == []"
send "the same again changes nothing"                 200 PUT "/api/admin/contests/$SLUG" "$CONTEST"
check "no changes"                                    "d['changes'] == []"
send "publish"                                        200 POST "/api/admin/contests/$SLUG/publish"
get "the contest"                                     200 "/api/admin/contests/$SLUG"
check "access groups [$G1], no invite code"           "d['spec']['access'] == {'mode': 'groups', 'groups': ['$G1']} and d['invite_code'] is None"
check "deadlines are in UTC with Z"                   "all(p['due_at'].endswith('Z') for p in d['spec']['problems'])"
get "the group knows its contest"                     200 "/api/admin/groups/$G1ID"
check "contests of $G1 = [$SLUG]"                     "[c['slug'] for c in d['contests']] == ['$SLUG']"

echo "--- students: not in the group -> no access; added later -> access at once"
SUM=$ROOT/problems-examples/sum-of-two/solutions/ok.py; PAIR=$ROOT/problems-examples/pair-with-sum/solutions/ok.py
s_get 1 "the contest, not in the group"               404 "/api/contests/$SLUG"
check "contest_not_found: one answer for not yours"   "d['detail'] == 'contest_not_found'"
as_s 2 "enter, not in the group"                      404 POST "/api/contests/$SLUG/enter"
send "add both students to the group"                 200 POST "/api/admin/groups/$G1ID/members" "{'user_ids': [$U1, $U2]}"
check "both added"                                    "len(d['added']) == 2"
for i in 1 2; do
  as_s $i "enter without a code"                      200 POST "/api/contests/$SLUG/enter"
  check "came in through the group"                   "d['entered'] == 'ok' and d['joined_via'] == 'group'"
done
s_submit 1 "A after due_at, policy late: accepted"    201 A "$SUM"
check "counted and late"                              "d['counted'] and d['late']"
s_submit 1 "B after due_at, policy no: refused"       409 B "$PAIR"
check "deadline_passed"                               "d['detail'] == 'deadline_passed'"
send "extension of B for s1 only"                     201 POST "/api/admin/contests/$SLUG/problems/B/extensions" \
                                                          "{'user_id': $U1, 'due_at': '2098-12-31 23:59:59', 'reason': 'smoke'}"
send "the same extension again replaces it"           200 POST "/api/admin/contests/$SLUG/problems/B/extensions" \
                                                          "{'user_id': $U1, 'due_at': '2098-12-31 23:59:58'}"
check "replaced is the old due"                       "d['replaced'].startswith('2098-12-31')"
s_submit 1 "B with the extension: accepted"           201 B "$PAIR"
check "not late"                                      "d['late'] is False"
s_submit 2 "B without an extension: still refused"    409 B "$PAIR"
check "deadline_passed"                               "d['detail'] == 'deadline_passed'"
get "participants"                                    200 "/api/admin/contests/$SLUG/participants"
check "s1 came in through the group, has access"      "any(p['login'] == '$L1' and p['joined_via'] == 'group' and p['access_now'] for p in d['items'])"

echo "--- removed from the group: loses access, keeps the submissions"
send "remove s1"                                      200 POST "/api/admin/groups/$G1ID/members/remove" "{'user_ids': [$U1]}"
s_submit 1 "A after the removal: refused"             403 A "$SUM"
check "no_access"                                     "d['detail'] == 'no_access'"
get "participants"                                    200 "/api/admin/contests/$SLUG/participants"
check "s1 has no access now"                          "all(not p['access_now'] for p in d['items'] if p['login'] == '$L1')"
get "submissions of the contest"                      200 "/api/admin/submissions?contest=$SLUG"
N=$(val 'len(d["items"])' 2>/dev/null || echo 0)
SUB=$(val 'd["items"][0]["id"] if d["items"] else 0' 2>/dev/null || echo 0)
check "2 submissions, both of s1 (kept)"              "len(d['items']) == 2 and {i['user']['login'] for i in d['items']} == {'$L1'}"
get "late ones"                                       200 "/api/admin/submissions?contest=$SLUG&late=true"
check "1 late (A)"                                    "[i['label'] for i in d['items']] == ['A']"
get "of the group (its current members)"              200 "/api/admin/submissions?contest=$SLUG&group_id=$G1ID"
check "none: s1 is not in it any more"               "d['items'] == []"
if [ "$SUB" != 0 ]; then
  get "one submission, the teacher's view"            200 "/api/admin/submissions/$SUB"
  check "source text and the history"                 "'text' in d['source'] and len(d['judgements']) >= 1"
  get "its source"                                    200 "/api/admin/submissions/$SUB/source"
  grep -qi '^content-disposition: attachment' "$D/hdr" && grep -qi '^x-content-type-options: nosniff' "$D/hdr" \
    && grep -qi "^content-security-policy: sandbox" "$D/hdr" && grep -qi '^content-type: text/plain' "$D/hdr" \
    && echo "OK   text/plain attachment, nosniff, sandbox CSP" || fail "headers of the source"
  send "rejudge A: dry run"                           200 POST /api/admin/submissions/rejudge "{'contest': '$SLUG', 'label': 'A', 'dry_run': True}"
  check "affected 1"                                  "d['affected'] == 1"
  send "rejudge A with a wrong expect"                409 POST /api/admin/submissions/rejudge "{'contest': '$SLUG', 'label': 'A', 'expect': 5}"
  send "rejudge A with expect 1"                      200 POST /api/admin/submissions/rejudge "{'contest': '$SLUG', 'label': 'A', 'expect': 1}"
  check "1 rejudge queued or retargeted"              "d['created'] + d['retargeted'] + d['already_running'] == 1"
else
  echo "SKIP the teacher's view, the source and the rejudge (no submissions: $N)"; SKIPS=$((SKIPS + 1))
fi
send "rejudge without saying what"                    422 POST /api/admin/submissions/rejudge "{'dry_run': True}"

echo "--- rollback of the import (K3): the group used by the contest stays, the other one goes"
send "roll back the import"                           200 POST "/api/admin/users/imports/$IMPORT_ID/rollback" "{'confirm_import_id': $IMPORT_ID}"
check "3 deleted; $G2 deleted; $G1 kept (contest)"    "len(d['deleted']) == 3 and d['groups_deleted'] == ['$G2'] and any(k.startswith('$G1:') for k in d['groups_kept'])"
get "$G2 is gone"                                     404 "/api/admin/groups/$G2ID"

echo "--- checks of problem versions (4a.4): a broken checker fails, cannot be pinned; a recheck"
BROKEN=check-broken-checker
c "unarchive $BROKEN if an earlier run archived it"   '200|404' POST "/api/admin/problems/$BROKEN/unarchive" -H "$O" -H @"$D/cookie" -H @"$D/csrf"
"$HERE/pack-problem.sh" "$ROOT/tests/problems/$BROKEN" > "$D/broken.zip" || fail "pack $BROKEN"
c "upload $BROKEN"                                    '200|201' POST /api/admin/problems -H "$O" -H @"$D/cookie" -H @"$D/csrf" \
                                                          -H 'Content-Type: application/zip' --data-binary @"$D/broken.zip"
check "pending_check with a check, or already there"  "d['result'] == 'exists' or (d['status'] == 'pending_check' and d['check_id'])"
VB=$(val 'd["version"]' 2>/dev/null || echo 0)
poll "$BROKEN v$VB ends failed"                       "/api/admin/problems/$BROKEN/versions/$VB" "d['status'] == 'failed'"
check "report: checker_compile_failed with the compiler's log, no solution run" \
      "d['check_report']['reason'] == 'checker_compile_failed' and not d['check_report']['programs'][0]['ok'] and 'error' in d['check_report']['programs'][0]['log'] and d['check_report']['solutions'] == [] and d['checks'][0]['outcome'] == 'failed'"
send "pin the failed version: refused"                409 PUT "/api/admin/contests/$SLUG-checks" \
     "{'format_version': 1, 'slug': '$SLUG-checks', 'title': 'Smoke checks', 'starts_at': '2026-01-01 00:00', 'problems': [{'label': 'A', 'problem': '$BROKEN', 'version': $VB}]}"
check "the status and where the report is"             "'v$VB is failed' in d['problems'][0] and 'judgectl problem show $BROKEN@$VB' in d['problems'][0]"
send "recheck sum-of-two v$V_sum_of_two"              '202|200' POST "/api/admin/problems/sum-of-two/versions/$V_sum_of_two/recheck"
check "queued (or one already queued / running); the version stays ready" "d['result'] in ('created', 'already_queued', 'already_running') and d['status'] == 'ready'"
RC=$(val 'd["check"]["id"]' 2>/dev/null || echo 0)
send "recheck again at once"                          200 POST "/api/admin/problems/sum-of-two/versions/$V_sum_of_two/recheck"
check "nothing new: the same check"                   "d['result'] in ('already_queued', 'already_running') and d['check']['id'] == $RC"
poll "the recheck ends ready"                         "/api/admin/problems/sum-of-two/versions/$V_sum_of_two" \
     "d['status'] == 'ready' and any(c['id'] == $RC and c['outcome'] == 'ready' for c in d['checks'])"
send "archive $BROKEN (test data)"                    200 POST "/api/admin/problems/$BROKEN/archive"

echo "--- the class feed (4v.1b): an assignment for a group, hand-ins, grades, a return, a release, the queue"
G3="smoke $STAMP feed"
send "a group for the feed"                           201 POST /api/admin/groups "{'name': '$G3'}"
G3ID=$(val 'd["id"]' 2>/dev/null || echo 0)
TXT=$D/answer-text
printf '%s\n' "<script>alert(\"smoke\")</script> ответ $STAMP" > "$TXT"
TITLE="<script>alert(1)</script> Блок-схема $STAMP"
ITEM_BODY="{'kind': 'assignment', 'title': '$TITLE', 'body_md': '**Нарисуй** <img src=x onerror=alert(1)>',
            'grading': 'points', 'max_points': 10, 'due_at': '2026-01-02 00:00', 'after_due_submit': 'late',
            'group_ids': [$G3ID]}"
send "create an assignment for $G3 (a draft)"         201 POST /api/admin/items "$ITEM_BODY"
ITEM=$(val 'd["id"]' 2>/dev/null || echo 0)
check "untrusted title back byte for byte, due in UTC with Z" \
      "d['title'] == '$TITLE' and d['due_at'].endswith('Z') and d['state'] == 'draft' and d['max_points'] == '10.00'"
send "publish it"                                     200 POST "/api/admin/items/$ITEM/publish"
check "published"                                     "d['item']['state'] == 'published' and d['changed']"
get "the feed of $G3"                                 200 "/api/admin/groups/$G3ID/feed"
check "the assignment is in it"                       "[i['id'] for i in d['items']] == [$ITEM]"
get "an unknown student in the path"                  404 "/api/admin/items/$ITEM/works/999999999"
send "s1 and s2 into $G3"                              200 POST "/api/admin/groups/$G3ID/members" "{'user_ids': [$U1, $U2]}"
get "the card of an addressee without work"           200 "/api/admin/items/$ITEM/works/$U1"
check "assigned, no events, missing (past due)"       "d['status'] == 'assigned' and d['events'] == [] and d['missing']"
as_s 1 "hands in after due: late"                     201 POST "/api/feed/$ITEM/work" "{'text': open('$TXT').read()}"
check "late"                                          "d['submission']['late'] is True and d['work']['status'] == 'submitted'"
send "extension for s2"                               201 POST "/api/admin/items/$ITEM/extensions" \
                                                          "{'user_id': $U2, 'due_at': '2098-12-31 23:59:59', 'reason': '<b>smoke</b>'}"
as_s 2 "with the extension: on time"                  201 POST "/api/feed/$ITEM/work" "{'text': open('$TXT').read()}"
check "not late"                                      "d['submission']['late'] is False"
get "the review queue of the item"                    200 "/api/admin/works?item_id=$ITEM"
check "2 hand-ins; s1 late, s2 not" \
    "sorted((r['login'], r['status'], r['late']) for r in d['items']) == sorted([('$L1', 'submitted', True), ('$L2', 'submitted', False)])"
get "late ones of $G3 across items"                   200 "/api/admin/works?late=true&group_id=$G3ID"
check "of this item: s1 only (earlier runs have works too)" \
    "[r['login'] for r in d['items'] if r['item'] == $ITEM] == ['$L1']"
get "missing without an item"                         422 "/api/admin/works?missing=true"
get "the card of s1"                                  200 "/api/admin/items/$ITEM/works/$U1"
check "the answer byte for byte"                      "d['events'][0]['text'].startswith('<script>alert(\"smoke\")</script>')"
send "a score with three decimals"                    422 POST "/api/admin/items/$ITEM/works/$U1/grade" "{'score': '8.555'}"
check "score_invalid"                                 "d['detail'] == 'score_invalid'"
send "grade s1: 8.5"                                  200 POST "/api/admin/items/$ITEM/works/$U1/grade" "{'score': '8.5', 'comment': 'хорошо'}"
check "graded, not released"                          "d['result']['status'] == 'graded' and not d['result']['released'] and d['work']['student_sees']['grade'] is None"
send "return s2"                                      200 POST "/api/admin/items/$ITEM/works/$U2/return" "{'comment': 'доделай <i>схему</i>'}"
check "returned, seen at once"                        "d['work']['student_sees']['status'] == 'returned'"
as_s 2 "hands in again"                               201 POST "/api/feed/$ITEM/work" "{'text': open('$TXT').read()}"
check "submitted"                                     "d['work']['status'] == 'submitted'"
send "regrade s1: 9"                                  200 POST "/api/admin/items/$ITEM/works/$U1/grade" "{'score': 9}"
send "release unknown students: nothing changes"      404 POST "/api/admin/items/$ITEM/release" "{'user_ids': [$U1, 999999999]}"
send "release the grades"                             200 POST "/api/admin/items/$ITEM/release" "{}"
check "per student: s1 released, s2 nothing_to_release" \
    "{r['login']: r['result'] for r in d['results']} == {'$L1': 'released', '$L2': 'nothing_to_release'}"
get "s1 now sees 9"                                   200 "/api/admin/items/$ITEM/works/$U1"
check "the released grade is the regrade"             "d['student_sees']['grade']['score'] == '9.00'"
s_get 1 "the own page of the item"                    200 "/api/feed/$ITEM"
check "graded 9.00, as the teacher released it"       "d['assignment']['status'] == 'graded' and d['assignment']['grade']['score'] == '9.00'"
get "submitted ones of $G3"                           200 "/api/admin/works?group_id=$G3ID&status=submitted"
check "of this item: s2 (handed in again)"            "[r['login'] for r in d['items'] if r['item'] == $ITEM] == ['$L2']"
send "archive the assignment"                         200 POST "/api/admin/items/$ITEM/archive"
send "archive $G3: answers its feed items"            200 POST "/api/admin/groups/$G3ID/archive"
check "feed_items = [the assignment], no contests; the old fields stay" \
      "set(d) == {'group', 'contests', 'feed_items'} and d['contests'] == [] and [i['id'] for i in d['feed_items']] == [$ITEM]"

echo "--- statement pictures and file headers (4b.0): content and name together, ETag, 304, the import warning"
PIC="smoke-pic-$STAMP"
PICDIR=$(mktemp -d); trap 'rm -rf "$D" "$PICDIR"' EXIT
cp -r "$ROOT/problems-examples/sum-of-two" "$PICDIR/pkg"
sed -i "s/^slug: .*/slug: $PIC/" "$PICDIR/pkg/problem.yaml"
mkdir -p "$PICDIR/pkg/statement/images"
python3 -c 'import sys; open(sys.argv[1], "wb").write(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 40)' \
  "$PICDIR/pkg/statement/images/схема.png"
cp "$PICDIR/pkg/statement/images/схема.png" "$PICDIR/pkg/statement/images/photo.jpg"   # a PNG under .jpg
printf '<html><script>alert(1)</script></html>' > "$PICDIR/pkg/statement/images/fake.png"
"$HERE/pack-problem.sh" "$PICDIR/pkg" > "$D/pic.zip" || fail "pack $PIC"
c "upload $PIC (a fake picture inside)"              201 POST /api/admin/problems -H "$O" -H @"$D/cookie" -H @"$D/csrf" \
                                                          -H 'Content-Type: application/zip' --data-binary @"$D/pic.zip"
check "imported, one warning: fake.png, save it as PNG" \
      "[w for w in d['warnings'] if w.startswith('statement/images/')] == [w for w in d['warnings'] if w.startswith('statement/images/fake.png') and 'save it as PNG' in w] and len([w for w in d['warnings'] if w.startswith('statement/images/')]) == 1"
PV=$(val 'd["version"]' 2>/dev/null || echo 1)
poll "$PIC v$PV is ready (its check, so that no check is left waiting)" "/api/admin/problems/$PIC/versions/$PV" "d['status'] == 'ready'"
FILES="/api/admin/problems/$PIC/versions/$PV/files/statement/images"
get "the Cyrillic picture"                            200 "$FILES/%D1%81%D1%85%D0%B5%D0%BC%D0%B0.png"
hdr() { grep -i "^$1:" "$D/hdr" | tr -d '\r' | cut -d' ' -f2-; }
[ "$(hdr content-type)" = image/png ] && echo "OK   image/png" || fail "content-type: $(hdr content-type)"
[ "$(hdr content-disposition)" = "inline; filename=\"skhema.png\"; filename*=UTF-8''%D1%81%D1%85%D0%B5%D0%BC%D0%B0.png" ] \
  && echo "OK   inline, the Cyrillic name in filename*, a transliteration in filename" || fail "disposition: $(hdr content-disposition)"
[ "$(hdr x-content-type-options)" = nosniff ] && [ "$(hdr content-security-policy)" = "sandbox; default-src 'none'" ] \
  && [ "$(hdr cache-control)" = "private, no-cache" ] && echo "OK   nosniff, sandbox CSP, private no-cache" || fail "safety headers"
ETAG=$(hdr etag)
[[ $ETAG =~ ^\"[0-9a-f]{64}\"$ ]] && echo "OK   ETag \"<sha256>\"" || fail "etag: $ETAG"
get "the same with If-None-Match"                     304 "$FILES/%D1%81%D1%85%D0%B5%D0%BC%D0%B0.png" -H "If-None-Match: $ETAG"
[ "$(hdr x-content-type-options)" = nosniff ] && [ "$(hdr cache-control)" = "private, no-cache" ] && [ "$(hdr etag)" = "$ETAG" ] \
  && echo "OK   304 carries the same headers" || fail "304 headers"
get "a PNG under .jpg"                                200 "$FILES/photo.jpg"
[ "$(hdr content-type)" = image/png ] && echo "OK   the type of the bytes" || fail "photo.jpg: $(hdr content-type)"
get "HTML under .png"                                 200 "$FILES/fake.png"
[ "$(hdr content-type)" = application/octet-stream ] && [[ $(hdr content-disposition) == attachment* ]] \
  && echo "OK   not a picture: a download" || fail "fake.png: $(hdr content-type) $(hdr content-disposition)"
c "no session: If-None-Match changes nothing"         401 GET "$FILES/%D1%81%D1%85%D0%B5%D0%BC%D0%B0.png" -H "If-None-Match: $ETAG"
send "archive $PIC (test data)"                       200 POST "/api/admin/problems/$PIC/archive"

echo "--- comments (4v.2): comments off and on, a shared and a private thread, read marks, deletion, the inbox"
get "who am I"                                        200 /api/admin/me
ME=$(val 'd["user"]["id"]' 2>/dev/null || echo 0)
G4="smoke $STAMP comments"
send "a group for the comments"                       201 POST /api/admin/groups "{'name': '$G4'}"
G4ID=$(val 'd["id"]' 2>/dev/null || echo 0)
send "an assignment for $G4, comments off"            201 POST /api/admin/items \
     "{'kind': 'assignment', 'title': 'Вопросы $STAMP', 'grading': 'none', 'group_ids': [$G4ID]}"
CITEM=$(val 'd["id"]' 2>/dev/null || echo 0)
check "comments_enabled false by default"             "d['comments_enabled'] is False"
send "a comment in a draft: refused"                  409 POST "/api/admin/items/$CITEM/comments" "{'body': 'рано'}"
send "publish it"                                     200 POST "/api/admin/items/$CITEM/publish"
get "an admin in the path of a private thread"        404 "/api/admin/items/$CITEM/students/$ME/comments"
check "thread_not_found"                              "d['detail'] == 'thread_not_found'"
send "delete a comment that does not exist"           404 POST /api/admin/comments/999999999/delete "{}"
send "a body with NUL"                                422 POST "/api/admin/items/$CITEM/comments" "{'body': 'a\\x00'}"
check "comment_invalid"                               "d['detail'] == 'comment_invalid'"
c "a lone surrogate in the JSON body"                 422 POST "/api/admin/items/$CITEM/comments" -H "$O" -H @"$D/cookie" \
                                                          -H @"$D/csrf" -H "$J" --data-binary '{"body": "a\ud800"}'
check "invalid_request, not a 500"                    "d['detail'] == 'invalid_request'"
send "a contest item (a draft) for the contest"       201 POST /api/admin/items "{'kind': 'contest', 'title': 'К', 'contest': '$SLUG'}"
CLINK=$(val 'd["id"]' 2>/dev/null || echo 0)
send "a private thread of a contest item"             409 POST "/api/admin/items/$CLINK/students/$ME/comments" "{'body': 'x'}"
check "not_an_assignment"                             "d['detail'] == 'not_an_assignment'"
send "delete that draft"                              204 POST "/api/admin/items/$CLINK/delete" "{'confirm_id': $CLINK}"
send "s1 and s2 into $G4"                             200 POST "/api/admin/groups/$G4ID/members" "{'user_ids': [$U1, $U2]}"
as_s 1 "a comment while comments are off"             409 POST "/api/feed/$CITEM/comments" "{'body': 'можно?'}"
check "comments_disabled"                             "d['detail'] == 'comments_disabled'"
send "turn comments on"                               200 PUT "/api/admin/items/$CITEM" "{'comments_enabled': True}"
check "changed: comments_enabled"                     "d['changed'] == ['comments_enabled'] and d['item']['comments_enabled']"
as_s 1 "a comment in the shared thread"               201 POST "/api/feed/$CITEM/comments" "{'body': 'вопрос $STAMP'}"
check "shared"                                        "d['kind'] == 'shared' and d['mine']"
as_s 2 "privately: <script> and ESC"                  201 POST "/api/feed/$CITEM/my-thread" \
                                                          "{'body': '<script>alert(1)</script> \\x1b[2J ответ $STAMP'}"
check "private"                                       "d['kind'] == 'private'"
get "the inbox: awaiting reply"                       200 "/api/admin/comments/threads?awaiting=true&item_id=$CITEM"
check "both threads await, newest first"              "[(t['kind'], t['awaiting_reply']) for t in d['items']] == [('private', True), ('shared', True)] and all(t['last']['at'].endswith('Z') for t in d['items'])"
get "the private thread of s2"                        200 "/api/admin/items/$CITEM/students/$U2/comments"
grep -qF '<script>alert(1)</script>' "$D/body" && echo "OK   <script> in the JSON as it is" || fail "<script> changed in the JSON"
check "the body byte for byte (ESC in it), unread 1"   "d['comments'][0]['body'] == '<script>alert(1)</script> \\x1b[2J ответ $STAMP' and d['unread'] == 1 and d['comments'][0]['at'].endswith('Z')"
get "the same again: GET sets no mark"                200 "/api/admin/items/$CITEM/students/$U2/comments"
check "still unread 1"                                "d['unread'] == 1"
send "the teacher answers s2"                         201 POST "/api/admin/items/$CITEM/students/$U2/comments" "{'body': 'Отвечаю <b>тут</b>'}"
REPLY=$(val 'd["id"]' 2>/dev/null || echo 0)
check "a private comment of the item"                 "d['kind'] == 'private' and d['item'] == $CITEM and not d['deleted']"
get "the inbox: awaiting reply"                       200 "/api/admin/comments/threads?awaiting=true&item_id=$CITEM"
check "only the shared thread waits"                  "[t['kind'] for t in d['items']] == ['shared']"
send "mark the private thread read"                   200 POST "/api/admin/items/$CITEM/students/$U2/comments/read" "{}"
check "unread 0"                                      "d == {'unread': 0}"
send "delete the answer, with a reason"               200 POST "/api/admin/comments/$REPLY/delete" "{'reason': 'не туда'}"
check "deleted, the reason and the text kept"         "d['deleted'] and d['delete_reason'] == 'не туда' and d['body'] == 'Отвечаю <b>тут</b>' and d['deleted_at'].endswith('Z')"
send "delete it again"                                409 POST "/api/admin/comments/$REPLY/delete" "{}"
check "already_deleted"                               "d['detail'] == 'already_deleted'"
get "the inbox: awaiting reply"                       200 "/api/admin/comments/threads?awaiting=true&item_id=$CITEM"
check "the private thread waits again"                "[t['kind'] for t in d['items']] == ['private', 'shared']"
send "archive the assignment"                         200 POST "/api/admin/items/$CITEM/archive"
send "archive $G4"                                    200 POST "/api/admin/groups/$G4ID/archive"

echo "--- files and links (4v.3): attachments, refused types, downloads with headers and 304, files of a work"
up() {  # up <label> <want> <item> <percent-encoded name> <file>: an upload of the raw body
  c "$1" "$2" POST "/api/admin/items/$3/attachments?name=$4" -H "$O" -H @"$D/cookie" -H @"$D/csrf" \
    -H 'Content-Type: application/octet-stream' --data-binary @"$5"
}
FD=$D/files; mkdir "$FD"
printf '%%PDF-1.7\n%% smoke %s\n' "$STAMP" > "$FD/report.pdf"
python3 -c 'import sys; open(sys.argv[1], "wb").write(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 40)' "$FD/pic.png"
printf '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>' > "$FD/x.svg"
printf '<html><script>alert(1)</script></html>' > "$FD/fake.png"
printf 'MZ\x90\x00' > "$FD/run.exe"
printf 'print("smoke %s")\n' "$STAMP" > "$FD/main.py"
printf 'Готово %s\n' "$STAMP" > "$FD/answer.txt"
G5="smoke $STAMP files"
send "a group for the files"                          201 POST /api/admin/groups "{'name': '$G5'}"
G5ID=$(val 'd["id"]' 2>/dev/null || echo 0)
send "an assignment with files or text for $G5"      201 POST /api/admin/items \
     "{'kind': 'assignment', 'title': 'Проект $STAMP', 'grading': 'pass_fail', 'answer_kind': 'text_or_files', 'group_ids': [$G5ID]}"
FITEM=$(val 'd["id"]' 2>/dev/null || echo 0)
up "attach a PDF under a Cyrillic name"               201 "$FITEM" "%D0%A3%D1%81%D0%BB%D0%BE%D0%B2%D0%B8%D0%B5.pdf" "$FD/report.pdf"
check "a file, the name decoded, its size"            "d['kind'] == 'file' and d['name'] == 'Условие.pdf' and d['size'] > 0 and len(d['sha256']) == 64"
ATT=$(val 'd["id"]' 2>/dev/null || echo 0)
up "attach a picture"                                 201 "$FITEM" "pic.png" "$FD/pic.png"
PICATT=$(val 'd["id"]' 2>/dev/null || echo 0)
up "SVG"                                              422 "$FITEM" "x.svg" "$FD/x.svg"
check "file_type_not_allowed"                         "d['detail'] == 'file_type_not_allowed'"
up "HTML under .png"                                  422 "$FITEM" "fake.png" "$FD/fake.png"
check "file_content_mismatch"                         "d['detail'] == 'file_content_mismatch'"
up ".exe"                                             422 "$FITEM" "run.exe" "$FD/run.exe"
check "file_type_not_allowed"                         "d['detail'] == 'file_type_not_allowed'"
up "a name that is not UTF-8"                         422 "$FITEM" "%FF.pdf" "$FD/report.pdf"
check "invalid_request, not a 500"                    "d['detail'] == 'invalid_request'"
up "the same name in another case"                    422 "$FITEM" "%D0%A3%D0%A1%D0%9B%D0%9E%D0%92%D0%98%D0%95.PDF" "$FD/report.pdf"
check "duplicate_name"                                "d['detail'] == 'duplicate_name'"
c "another Content-Type"                              415 POST "/api/admin/items/$FITEM/attachments?name=a.pdf" -H "$O" \
                                                          -H @"$D/cookie" -H @"$D/csrf" -H 'Content-Type: application/pdf' --data-binary @"$FD/report.pdf"
send "a link with an untrusted title"                 201 POST "/api/admin/items/$FITEM/links" "{'url': 'https://scratch.mit.edu/projects/1', 'title': '<b>пример</b>'}"
check "stored as sent"                                "d['kind'] == 'link' and d['title'] == '<b>пример</b>'"
LINK=$(val 'd["id"]' 2>/dev/null || echo 0)
send "a javascript: link"                             422 POST "/api/admin/items/$FITEM/links" "{'url': 'javascript:alert(1)'}"
check "link_invalid"                                  "d['detail'] == 'link_invalid'"
get "download the PDF"                                200 "/api/admin/items/$FITEM/attachments/$ATT"
cmp -s "$D/body" "$FD/report.pdf" && echo "OK   the same bytes" || fail "the PDF differs"
[ "$(hdr content-type)" = application/octet-stream ] && [[ $(hdr content-disposition) == "attachment; filename=\"Uslovie.pdf\"; filename*=UTF-8''%D0%A3%D1%81%D0%BB%D0%BE%D0%B2%D0%B8%D0%B5.pdf" ]] \
  && echo "OK   a download, the Cyrillic name in filename*" || fail "pdf: $(hdr content-type) $(hdr content-disposition)"
[ "$(hdr x-content-type-options)" = nosniff ] && [ "$(hdr content-security-policy)" = "sandbox; default-src 'none'" ] \
  && echo "OK   nosniff, sandbox CSP" || fail "safety headers of an attachment"
ETAG=$(hdr etag)
get "the same with If-None-Match"                     304 "/api/admin/items/$FITEM/attachments/$ATT" -H "If-None-Match: $ETAG"
get "the picture"                                     200 "/api/admin/items/$FITEM/attachments/$PICATT"
[ "$(hdr content-type)" = image/png ] && [[ $(hdr content-disposition) == inline* ]] && echo "OK   image/png inline" \
  || fail "picture: $(hdr content-type) $(hdr content-disposition)"
get "a link is not a file"                            404 "/api/admin/items/$FITEM/attachments/$LINK"
check "attachment_not_found"                          "d['detail'] == 'attachment_not_found'"
send "the new order"                                  200 PUT "/api/admin/items/$FITEM/attachments/order" "{'ids': [$LINK, $ATT, $PICATT]}"
check "link first"                                    "[a['id'] for a in d['items']] == [$LINK, $ATT, $PICATT]"
send "an order without the picture"                   422 PUT "/api/admin/items/$FITEM/attachments/order" "{'ids': [$LINK, $ATT]}"
check "attachment_order_invalid, missing named"       "d['detail'] == 'attachment_order_invalid' and d['missing'] == [$PICATT]"
send "remove the picture"                             204 POST "/api/admin/items/$FITEM/attachments/$PICATT/delete"
get "the attachments"                                 200 "/api/admin/items/$FITEM/attachments"
check "the link and the PDF are left"                 "[a['id'] for a in d['items']] == [$LINK, $ATT]"
send "publish it"                                     200 POST "/api/admin/items/$FITEM/publish"
send "s1 and s2 into $G5"                              200 POST "/api/admin/groups/$G5ID/members" "{'user_ids': [$U1, $U2]}"
s_up 1 ".exe into the draft: refused"                 422 "$FITEM" run.exe "$FD/run.exe"
check "file_type_not_allowed"                         "d['detail'] == 'file_type_not_allowed'"
s_up 1 "main.py into the draft"                       201 "$FITEM" main.py "$FD/main.py"
DPY=$(val 'd["file"]["id"]' 2>/dev/null || echo 0)
s_up 1 "report.pdf into the draft"                    201 "$FITEM" report.pdf "$FD/report.pdf"
DPDF=$(val 'd["file"]["id"]' 2>/dev/null || echo 0)
as_s 1 "hand in the text, both files and a link"      201 POST "/api/feed/$FITEM/work" \
     "{'text': open('$FD/answer.txt').read(), 'files': [$DPY, $DPDF], 'links': [{'url': 'https://github.com/smoke/x', 'title': 'репозиторий'}]}"
check "submitted"                                     "d['work']['status'] == 'submitted'"
s_get 1 "the draft is empty now"                      200 "/api/feed/$FITEM/draft"
check "no files left"                                 "d['draft'] == []"
get "the card of the work lists the files"            200 "/api/admin/items/$FITEM/works/$U1"
check "answer.md, main.py, report.pdf and the link"   "[f['name'] for f in d['events'][0]['files']] == ['answer.md', 'main.py', 'report.pdf'] and [l['title'] for l in d['events'][0]['links']] == ['репозиторий']"
PYID=$(val '[f["id"] for f in d["events"][0]["files"] if f["name"] == "main.py"][0]' 2>/dev/null || echo 0)
ANSID=$(val '[f["id"] for f in d["events"][0]["files"] if f["kind"] == "text"][0]' 2>/dev/null || echo 0)
get "download main.py of the work"                    200 "/api/admin/items/$FITEM/works/$U1/files/$PYID"
cmp -s "$D/body" "$FD/main.py" && echo "OK   the same bytes" || fail "main.py differs"
[[ $(hdr content-type) == text/plain* ]] && [ "$(hdr x-content-type-options)" = nosniff ] && echo "OK   text, nosniff" \
  || fail "main.py: $(hdr content-type)"
get "the same with If-None-Match"                     304 "/api/admin/items/$FITEM/works/$U1/files/$PYID" -H "If-None-Match: $(hdr etag)"
get "download answer.md"                              200 "/api/admin/items/$FITEM/works/$U1/files/$ANSID"
cmp -s "$D/body" "$FD/answer.txt" && echo "OK   the text answer as handed in" || fail "answer.md differs"
get "the same file as another student's"              404 "/api/admin/items/$FITEM/works/$U2/files/$PYID"
check "file_not_found"                                "d['detail'] == 'file_not_found'"
get "the same file under another item"                404 "/api/admin/items/$ITEM/works/$U1/files/$PYID"
check "file_not_found"                                "d['detail'] == 'file_not_found'"
s_get 2 "s1's file of the work, as s2"                404 "/api/feed/$FITEM/work/files/$PYID"
check "file_not_found"                                "d['detail'] == 'file_not_found'"
send "archive the assignment"                         200 POST "/api/admin/items/$FITEM/archive"
up "an archived item is read only"                    409 "$FITEM" "late.pdf" "$FD/report.pdf"
check "item_archived"                                 "d['detail'] == 'item_archived'"
send "archive $G5"                                    200 POST "/api/admin/groups/$G5ID/archive"

echo "--- materials (4v.4): versions, links and their warnings, the fixed slug, sections, search, files, archive"
MS="smoke-$STAMP"
mup() {  # mup <label> <want> <material id> <percent-encoded name> <file>: a file of a new version, the raw body
  c "$1" "$2" POST "/api/admin/materials/$3/attachments?name=$4" -H "$O" -H @"$D/cookie" -H @"$D/csrf" \
    -H 'Content-Type: application/octet-stream' --data-binary @"$5"
}
send "a material linking a slug nobody has yet"       201 POST /api/admin/materials \
     "{'slug': '$MS-memo', 'title': 'Памятка $STAMP', 'body_md': 'См. [[памятка:$MS-later]]'}"
check "version 1, one warning, the link unknown"      "d['version'] == 1 and len(d['warnings']) == 1 and d['links'] == [{'slug': '$MS-later', 'status': 'unknown'}]"
MEMO=$(val 'd["id"]' 2>/dev/null || echo 0)
send "a material for every student"                  201 POST /api/admin/materials \
     "{'slug': '$MS-loops', 'title': 'Циклы $STAMP', 'library': 'all_students'}"
LOOPS=$(val 'd["id"]' 2>/dev/null || echo 0)
send "a new text linking it"                          200 POST "/api/admin/materials/$MEMO/versions" "{'body_md': 'См. [[памятка:$MS-loops|циклы]]'}"
check "version 2, no warnings"                        "d['version'] == 2 and d['created'] and d['warnings'] == []"
send "the same text again"                            200 POST "/api/admin/materials/$MEMO/versions" "{'body_md': 'См. [[памятка:$MS-loops|циклы]]'}"
check "no new version"                                "d['version'] == 2 and d['created'] is False"
send "another slug"                                   422 PATCH "/api/admin/materials/$MEMO" "{'slug': '$MS-other'}"
check "slug_immutable"                                "d['detail'] == 'slug_immutable'"
send "a new title in place"                           200 PATCH "/api/admin/materials/$MEMO" "{'title': 'Памятка $STAMP (2)'}"
check "changed title, still version 2"                "d['changed'] == ['title'] and d['material']['versions'] == 2"
send "an assignment linking it and an unknown slug"   201 POST /api/admin/items \
     "{'kind': 'assignment', 'title': 'Памятки $STAMP', 'grading': 'none', 'body_md': '[[памятка:$MS-memo]] [[памятка:$MS-nope]]'}"
check "saved, one link warning"                       "len(d['link_warnings']) == 1 and '$MS-nope' in d['link_warnings'][0]"
MITEM=$(val 'd["id"]' 2>/dev/null || echo 0)
get "where the material is used"                      200 "/api/admin/materials/$MEMO/where"
check "the assignment links it"                       "[i['id'] for i in d['linking_items']] == [$MITEM]"
send "delete the draft assignment"                    204 POST "/api/admin/items/$MITEM/delete" "{'confirm_id': $MITEM}"
send "resolve a text"                                 200 POST /api/admin/materials/resolve "{'text': '[[памятка:$MS-memo]] [[памятка:$MS-nope]] [[памятка:]]'}"
check "ok and unknown, two warnings, nothing written" "[r['status'] for r in d['links']] == ['ok', 'unknown'] and len(d['warnings']) == 2"
send "a section"                                      201 POST /api/admin/library/sections "{'title': 'Smoke $STAMP'}"
SEC=$(val 'd["id"]' 2>/dev/null || echo 0)
send "a subsection"                                   201 POST /api/admin/library/sections "{'title': 'Циклы', 'parent_id': $SEC}"
SUB=$(val 'd["id"]' 2>/dev/null || echo 0)
send "no third level"                                 409 POST /api/admin/library/sections "{'title': 'Глубже', 'parent_id': $SUB}"
check "section_too_deep"                              "d['detail'] == 'section_too_deep'"
send "the material into the subsection"               200 POST "/api/admin/library/sections/$SUB/materials" "{'material_id': $LOOPS}"
get "the library"                                     200 /api/admin/library
check "the section, its subsection, the material"     "[[m['slug'] for m in x['materials']] for s in d['sections'] if s['id'] == $SEC for x in s['sections']] == [['$MS-loops']]"
get "search in lower case Cyrillic"                   200 /api/admin/library -G --data-urlencode "q=циклы $STAMP"
check "finds «Циклы $STAMP»"                          "[m['slug'] for m in d['materials']] == ['$MS-loops']"
mup "a PDF into a new version"                        201 "$MEMO" "%D0%BF%D0%B0%D0%BC%D1%8F%D1%82%D0%BA%D0%B0.pdf" "$FD/report.pdf"
check "version 3 with the file"                       "d['version'] == 3 and [a['name'] for a in d['attachments']] == ['памятка.pdf']"
MATT=$(val 'd["attachments"][0]["id"]' 2>/dev/null || echo 0)
mup "SVG"                                             422 "$MEMO" "x.svg" "$FD/x.svg"
check "file_type_not_allowed"                         "d['detail'] == 'file_type_not_allowed'"
get "download the file of the version"                200 "/api/admin/materials/$MEMO/attachments/$MATT"
cmp -s "$D/body" "$FD/report.pdf" && echo "OK   the same bytes" || fail "the file of the material differs"
[ "$(hdr x-content-type-options)" = nosniff ] && echo "OK   nosniff" || fail "nosniff of a material's file"
get "not a file of version 2"                         404 "/api/admin/materials/$MEMO/attachments/$MATT?version=2"
check "attachment_not_found"                          "d['detail'] == 'attachment_not_found'"
get "the history"                                     200 "/api/admin/materials/$MEMO/versions"
check "versions 3, 2, 1"                              "[v['version'] for v in d['items']] == [3, 2, 1]"
send "archive the material"                           200 POST "/api/admin/materials/$MEMO/archive"
check "changed"                                       "d['changed'] is True"
send "archive it again"                               200 POST "/api/admin/materials/$MEMO/archive"
check "not changed"                                   "d['changed'] is False"
mup "an archived material is read only"               409 "$MEMO" "late.pdf" "$FD/report.pdf"
check "material_archived"                             "d['detail'] == 'material_archived'"
send "archive the other one"                          200 POST "/api/admin/materials/$LOOPS/archive"
send "delete the subsection"                          200 POST "/api/admin/library/sections/$SUB/delete" "{'confirm_id': $SUB}"
send "delete the section"                             200 POST "/api/admin/library/sections/$SEC/delete" "{'confirm_id': $SEC}"

echo "--- the rest: archive, profile, system, audit, languages"
send "archive the contest"                            200 POST "/api/admin/contests/$SLUG/archive"
send "archive the group: answers its contests (K6)"    200 POST "/api/admin/groups/$G1ID/archive"
check "contests = [$SLUG]"                            "d['contests'] == ['$SLUG']"
send "own time zone"                                  200 PUT /api/auth/profile "{'timezone': 'Asia/Novosibirsk'}"
check "timezone set"                                  "d['user']['timezone'] == 'Asia/Novosibirsk'"
send "a field other than timezone"                    422 PUT /api/auth/profile "{'timezone': None, 'role': 'student'}"
send "clear it"                                       200 PUT /api/auth/profile "{'timezone': None}"
get "system"                                          200 /api/admin/system
check "the database is at the migration head"         "d['migration']['ok'] and 'blobs' not in d"
check "a checks section, no check waiting"             "d['checks']['queued'] == 0 and d['checks']['running'] == 0"
get "audit of groups"                                 200 "/api/admin/audit?action=group.&limit=5"
check "newest first, times with Z"                    "d['items'] and all(i['at'].endswith('Z') for i in d['items'])"
get "languages"                                       200 /api/admin/languages
for id in "$U1" "$U2"; do send "disable student $id (they have submissions)" 200 POST "/api/admin/users/$id/disable"; done
s_get 1 "a disabled student's session is gone"        401 /api/feed
send "logout"                                         204 POST /api/auth/logout

echo "failures: $FAILS, skipped: $SKIPS"
exit $FAILS
