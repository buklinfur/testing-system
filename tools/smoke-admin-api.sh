#!/usr/bin/env bash
# Smoke test of the admin API (docs/ADMIN_API.md) with curl, against a running public app: the check scenario of
# session 4a.3a through the API: a class from CSV with a class column, a contest for a group, adding and removing a
# student, deadlines with the policies late and no, an extension for one student, submissions and a rejudge in two
# steps, the rollback of the import (K3), the profile time zone, system, audit, languages; and of session 4a.4:
# uploaded problems are checked by a worker (it waits for ready, up to 120 s), a package whose checker does not
# compile (tests/problems/check-broken-checker) ends failed and cannot be pinned, a recheck runs once and ends ready;
# and of session 4v.1b (the class feed, docs/ADMIN_API.md §5.8): an assignment for a group, hand-ins through SEED_CMD
# (dev_seed --item), an extension, grades, a return, a hand-in again, a regrade, a release per student, the filters of
# the review queue, archiving the group answers its feed items.
# And of session 4b.0 (docs/FILES.md): a statement picture under a Cyrillic name, a PNG under .jpg and HTML under .png
# are served by content and name together, with ETag and 304; the import warns about the fake picture.
# And of session 4v.2b (comments, docs/ADMIN_API.md §5.9): a student refused while comments are off, then a shared and
# a private comment through SEED_CMD (dev_seed --comment), the inbox awaiting reply, untrusted text byte for byte,
# a teacher's answer, the read mark, soft deletion and again; NUL and a lone surrogate are 422. rate_limited is not
# checked (it depends on JUDGE_COMMENT_RATE_PER_MIN of the installation).
# A worker with the capability problem_check must be running (judgectl worker list: CAPS).
#   tools/smoke-admin-api.sh [BASE_URL] [ORIGIN]       defaults: http://127.0.0.1:8000 and $JUDGE_PUBLIC_ORIGIN
# It asks for an admin (login, password, a TOTP code from the phone); ADMIN_LOGIN, ADMIN_PASSWORD and TOTP_CODE
# from the environment are used instead (named, never shown). Secrets reach curl only through files in a private
# temporary directory and are never printed.
#
# Submissions: the student API (4b) does not exist yet, so a student "submits" with dev_seed:
#   SEED_CMD=tools/seed-as-judge.sh tools/smoke-admin-api.sh ...
# (dev only; it runs dev_seed as judge on this database, as dev-student-1 and dev-student-2). Without SEED_CMD
# the submission checks are skipped (SKIP) and each line names the dev_seed command and the result it would
# check; that is a description only: the contest is archived and the import rolled back by the end of the run,
# so the deadlines and the access through the group are really checked only with SEED_CMD.
#
# Everything it makes is named smoke-<time> (contest, groups, students); the contest is archived at the end, the
# imported students are rolled back, the problem check-broken-checker is archived (and unarchived on the next run).
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
seed() {  # seed <label> <want exit 0|1> <grep in output or -> dev_seed args...; output in $D/seed.out / .err
  local label=$1 want=$2 needle=$3; shift 3
  if [ -z "${SEED_CMD:-}" ]; then
    echo "SKIP $label   ->  dev_seed --contest $SLUG $*   (expect exit $want${needle:+, \"$needle\"})"
    SKIPS=$((SKIPS + 1)); return 1
  fi
  local code
  $SEED_CMD --contest "$SLUG" "$@" > "$D/seed.out" 2> "$D/seed.err"; code=$?
  if [ "$code" = "$want" ] && { [ "$needle" = - ] || grep -q -- "$needle" "$D/seed.out" "$D/seed.err"; }; then
    echo "OK   $label (exit $code)"
  else
    fail "$label: exit $code, want $want $needle: $(tail -c 200 "$D/seed.err" | tr '\n' ' ')"
  fi
  cp "$D/seed.out" "$D/body" 2>/dev/null
}

seed_item() {  # seed_item <label> <want exit> <grep or -> dev_seed args...: a hand-in on the assignment $ITEM
  local label=$1 want=$2 needle=$3; shift 3
  if [ -z "${SEED_CMD:-}" ]; then
    echo "SKIP $label   ->  dev_seed --item $ITEM $*   (expect exit $want${needle:+, \"$needle\"})"
    SKIPS=$((SKIPS + 1)); return 1
  fi
  local code
  $SEED_CMD --item "$ITEM" "$@" > "$D/seed.out" 2> "$D/seed.err"; code=$?
  if [ "$code" = "$want" ] && { [ "$needle" = - ] || grep -q -- "$needle" "$D/seed.out" "$D/seed.err"; }; then
    echo "OK   $label (exit $code)"
  else
    fail "$label: exit $code, want $want $needle: $(tail -c 200 "$D/seed.err" | tr '\n' ' ')"
  fi
}

[ "$(curl -s --noproxy '*' --max-time 5 -o /dev/null -w '%{http_code}' "$BASE/api/auth/me")" = 401 ] || {
  echo "the public app does not answer at $BASE (start it: j .venv/bin/python -m app.public_main)"; exit 2; }
ask ADMIN_LOGIN "admin login"; ask ADMIN_PASSWORD "admin password" silent
[ ${#FROM_ENV[@]} = 0 ] || echo "from the environment (not asked): ${FROM_ENV[*]}"
python3 -c 'import json, os; print(json.dumps({"login": os.environ["ADMIN_LOGIN"], "password": os.environ["ADMIN_PASSWORD"]}))' > "$D/login"
[ -n "${SEED_CMD:-}" ] && echo "submissions through SEED_CMD=$SEED_CMD" || echo "no SEED_CMD: submission checks are printed, not run"
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
seed "dev-student-1, not in the group: refused"       1 no_access --label A --user 1
seed "dev-student-2, not in the group: refused"       1 no_access --label A --user 2
get "find dev-student-1"                              200 "/api/admin/users?q=dev-student-1"
U1=$(val "next((u['id'] for u in d['items'] if u['login'] == 'dev-student-1'), 0)" 2>/dev/null || echo 0)
get "find dev-student-2"                              200 "/api/admin/users?q=dev-student-2"
U2=$(val "next((u['id'] for u in d['items'] if u['login'] == 'dev-student-2'), 0)" 2>/dev/null || echo 0)
if [ "$U1" != 0 ] && [ "$U2" != 0 ]; then
  send "add dev-student-1 and -2 to the group"        200 POST "/api/admin/groups/$G1ID/members" "{'user_ids': [$U1, $U2]}"
  check "both added"                                  "len(d['added']) + len(d['already']) == 2"
else
  echo "SKIP adding dev-student-1/-2 (they do not exist yet: run the dev_seed lines above, then this script again)"
  SKIPS=$((SKIPS + 1))
fi
if seed "A after due_at, policy late: accepted, late"  0 '"late": true' --label A --user 1; then
  check "counted and late"                            "d[0]['counted'] and d[0]['late']"
fi
seed "B after due_at, policy no: refused"             1 deadline_passed --label B --user 1
send "extension of B for dev-student-1 only"          201 POST "/api/admin/contests/$SLUG/problems/B/extensions" \
                                                          "{'user_id': $U1, 'due_at': '2098-12-31 23:59:59', 'reason': 'smoke'}"
send "the same extension again replaces it"           200 POST "/api/admin/contests/$SLUG/problems/B/extensions" \
                                                          "{'user_id': $U1, 'due_at': '2098-12-31 23:59:58'}"
check "replaced is the old due"                       "d['replaced'].startswith('2098-12-31')"
seed "B with the extension: accepted, not late"       0 '"late": false' --label B --user 1
seed "B for dev-student-2: still refused"             1 deadline_passed --label B --user 2
get "participants"                                    200 "/api/admin/contests/$SLUG/participants"
check "dev-student-1 came in through the group, has access" \
      "any(p['login'] == 'dev-student-1' and p['joined_via'] == 'group' and p['access_now'] for p in d['items']) or not '${SEED_CMD:-}'"

echo "--- removed from the group: loses access, keeps the submissions"
send "remove dev-student-1"                           200 POST "/api/admin/groups/$G1ID/members/remove" "{'user_ids': [$U1]}"
seed "A after the removal: refused"                   1 no_access --label A --user 1
get "participants"                                    200 "/api/admin/contests/$SLUG/participants"
check "dev-student-1 has no access now"               "all(not p['access_now'] for p in d['items'] if p['login'] == 'dev-student-1')"
get "submissions of the contest"                      200 "/api/admin/submissions?contest=$SLUG"
N=$(val 'len(d["items"])' 2>/dev/null || echo 0)
SUB=$(val 'd["items"][0]["id"] if d["items"] else 0' 2>/dev/null || echo 0)
[ -z "${SEED_CMD:-}" ] || check "2 submissions, both of dev-student-1 (kept)" \
                               "len(d['items']) == 2 and {i['user']['login'] for i in d['items']} == {'dev-student-1'}"
get "late ones"                                       200 "/api/admin/submissions?contest=$SLUG&late=true"
[ -z "${SEED_CMD:-}" ] || check "1 late (A)"           "[i['label'] for i in d['items']] == ['A']"
get "of the group (its current members)"              200 "/api/admin/submissions?contest=$SLUG&group_id=$G1ID"
check "none: dev-student-1 is not in it any more"     "d['items'] == []"
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
# the answer is read by dev_seed as judge (SEED_CMD): a file in /tmp anyone may read, not in the private $D
TXT=$(mktemp); chmod 644 "$TXT"; trap 'rm -rf "$D" "$TXT"' EXIT
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
if [ "$U1" != 0 ] && [ "$U2" != 0 ]; then
  send "dev-student-1 and -2 into $G3"                200 POST "/api/admin/groups/$G3ID/members" "{'user_ids': [$U1, $U2]}"
  get "the card of an addressee without work"         200 "/api/admin/items/$ITEM/works/$U1"
  check "assigned, no events, missing (past due)"     "d['status'] == 'assigned' and d['events'] == [] and d['missing']"
  seed_item "dev-student-1 hands in after due: late"  0 '"late": true' --user 1 --text-file "$TXT"
  send "extension for dev-student-2"                  201 POST "/api/admin/items/$ITEM/extensions" \
                                                          "{'user_id': $U2, 'due_at': '2098-12-31 23:59:59', 'reason': '<b>smoke</b>'}"
  seed_item "dev-student-2 with the extension: on time" 0 '"late": false' --user 2 --text-file "$TXT"
  get "the review queue of the item"                  200 "/api/admin/works?item_id=$ITEM"
  [ -z "${SEED_CMD:-}" ] || check "2 hand-ins; dev-student-1 late, dev-student-2 not" \
      "sorted((r['login'], r['status'], r['late']) for r in d['items']) == [('dev-student-1', 'submitted', True), ('dev-student-2', 'submitted', False)]"
  get "late ones of $G3 across items"                 200 "/api/admin/works?late=true&group_id=$G3ID"
  [ -z "${SEED_CMD:-}" ] || check "of this item: dev-student-1 only (earlier runs have works too)" \
      "[r['login'] for r in d['items'] if r['item'] == $ITEM] == ['dev-student-1']"
  get "missing without an item"                       422 "/api/admin/works?missing=true"
  if [ -n "${SEED_CMD:-}" ]; then
    get "the card of dev-student-1"                   200 "/api/admin/items/$ITEM/works/$U1"
    check "the answer byte for byte"                  "d['events'][0]['text'].startswith('<script>alert(\"smoke\")</script>')"
  fi
  send "a score with three decimals"                  422 POST "/api/admin/items/$ITEM/works/$U1/grade" "{'score': '8.555'}"
  check "score_invalid"                               "d['detail'] == 'score_invalid'"
  send "grade dev-student-1: 8.5"                     200 POST "/api/admin/items/$ITEM/works/$U1/grade" "{'score': '8.5', 'comment': 'хорошо'}"
  check "graded, not released"                        "d['result']['status'] == 'graded' and not d['result']['released'] and d['work']['student_sees']['grade'] is None"
  if [ -n "${SEED_CMD:-}" ]; then
    send "return dev-student-2"                       200 POST "/api/admin/items/$ITEM/works/$U2/return" "{'comment': 'доделай <i>схему</i>'}"
    check "returned, seen at once"                    "d['work']['student_sees']['status'] == 'returned'"
    seed_item "dev-student-2 hands in again"          0 '"status": "submitted"' --user 2 --text-file "$TXT"
  else
    echo "SKIP return and hand in again   ->  needs a hand-in of dev-student-2 (SEED_CMD)"; SKIPS=$((SKIPS + 1))
  fi
  send "regrade dev-student-1: 9"                     200 POST "/api/admin/items/$ITEM/works/$U1/grade" "{'score': 9}"
  send "release unknown students: nothing changes"    404 POST "/api/admin/items/$ITEM/release" "{'user_ids': [$U1, 999999999]}"
  send "release the grades"                           200 POST "/api/admin/items/$ITEM/release" "{}"
  check "per student: dev-student-1 released, dev-student-2 nothing_to_release" \
      "{r['login']: r['result'] for r in d['results']} == {'dev-student-1': 'released', 'dev-student-2': 'nothing_to_release'} or not '${SEED_CMD:-}'"
  get "dev-student-1 now sees 9"                      200 "/api/admin/items/$ITEM/works/$U1"
  check "the released grade is the regrade"           "d['student_sees']['grade']['score'] == '9.00'"
  get "submitted ones of $G3"                         200 "/api/admin/works?group_id=$G3ID&status=submitted"
  [ -z "${SEED_CMD:-}" ] || check "of this item: dev-student-2 (handed in again)" \
      "[r['login'] for r in d['items'] if r['item'] == $ITEM] == ['dev-student-2']"
else
  echo "SKIP the hand-ins and grades of the feed (dev-student-1/-2 do not exist yet: run with SEED_CMD twice)"
  SKIPS=$((SKIPS + 1))
fi
send "archive the assignment"                         200 POST "/api/admin/items/$ITEM/archive"
send "archive $G3: answers its feed items"            200 POST "/api/admin/groups/$G3ID/archive"
check "feed_items = [the assignment], no contests; the old fields stay" \
      "set(d) == {'group', 'contests', 'feed_items'} and d['contests'] == [] and [i['id'] for i in d['feed_items']] == [$ITEM]"

echo "--- statement pictures and file headers (4b.0): content and name together, ETag, 304, the import warning"
PIC="smoke-pic-$STAMP"
PICDIR=$(mktemp -d); trap 'rm -rf "$D" "${TXT:-}" "$PICDIR"' EXIT
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
seed_comment() {  # seed_comment <label> <want exit> <grep or -> dev_seed args...: a student's comment on $CITEM
  local label=$1 want=$2 needle=$3; shift 3
  if [ -z "${SEED_CMD:-}" ]; then
    echo "SKIP $label   ->  dev_seed --item $CITEM $*   (expect exit $want${needle:+, \"$needle\"})"
    SKIPS=$((SKIPS + 1)); return 1
  fi
  local code
  $SEED_CMD --item "$CITEM" "$@" > "$D/seed.out" 2> "$D/seed.err"; code=$?
  if [ "$code" = "$want" ] && { [ "$needle" = - ] || grep -q -- "$needle" "$D/seed.out" "$D/seed.err"; }; then
    echo "OK   $label (exit $code)"
  else
    fail "$label: exit $code, want $want $needle: $(tail -c 200 "$D/seed.err" | tr '\n' ' ')"
  fi
}
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
if [ "$U1" != 0 ] && [ "$U2" != 0 ]; then
  HOSTILE_C=$(printf '<script>alert(1)</script> \033[2J ответ %s' "$STAMP")
  send "dev-student-1 and -2 into $G4"                200 POST "/api/admin/groups/$G4ID/members" "{'user_ids': [$U1, $U2]}"
  seed_comment "dev-student-1, comments off: refused" 1 comments_disabled --user 1 --comment "можно?"
  send "turn comments on"                             200 PUT "/api/admin/items/$CITEM" "{'comments_enabled': True}"
  check "changed: comments_enabled"                   "d['changed'] == ['comments_enabled'] and d['item']['comments_enabled']"
  seed_comment "dev-student-1 in the shared thread"   0 '"kind": "shared"' --user 1 --comment "вопрос $STAMP"
  seed_comment "dev-student-2 privately: <script> and ESC" 0 '"kind": "private"' --user 2 --private --comment "$HOSTILE_C"
  get "the inbox: awaiting reply"                     200 "/api/admin/comments/threads?awaiting=true&item_id=$CITEM"
  check "both threads await, newest first"            "[(t['kind'], t['awaiting_reply']) for t in d['items']] == [('private', True), ('shared', True)] and all(t['last']['at'].endswith('Z') for t in d['items'])"
  get "the private thread of dev-student-2"           200 "/api/admin/items/$CITEM/students/$U2/comments"
  grep -qF '<script>alert(1)</script>' "$D/body" && echo "OK   <script> in the JSON as it is" || fail "<script> changed in the JSON"
  check "the body byte for byte (ESC in it), unread 1" "d['comments'][0]['body'] == '<script>alert(1)</script> \\x1b[2J ответ $STAMP' and d['unread'] == 1 and d['comments'][0]['at'].endswith('Z')"
  get "the same again: GET sets no mark"              200 "/api/admin/items/$CITEM/students/$U2/comments"
  check "still unread 1"                              "d['unread'] == 1"
  send "the teacher answers dev-student-2"            201 POST "/api/admin/items/$CITEM/students/$U2/comments" "{'body': 'Отвечаю <b>тут</b>'}"
  REPLY=$(val 'd["id"]' 2>/dev/null || echo 0)
  check "a private comment of the item"               "d['kind'] == 'private' and d['item'] == $CITEM and not d['deleted']"
  get "the inbox: awaiting reply"                     200 "/api/admin/comments/threads?awaiting=true&item_id=$CITEM"
  check "only the shared thread waits"                "[t['kind'] for t in d['items']] == ['shared']"
  send "mark the private thread read"                 200 POST "/api/admin/items/$CITEM/students/$U2/comments/read" "{}"
  check "unread 0"                                    "d == {'unread': 0}"
  send "delete the answer, with a reason"             200 POST "/api/admin/comments/$REPLY/delete" "{'reason': 'не туда'}"
  check "deleted, the reason and the text kept"       "d['deleted'] and d['delete_reason'] == 'не туда' and d['body'] == 'Отвечаю <b>тут</b>' and d['deleted_at'].endswith('Z')"
  send "delete it again"                              409 POST "/api/admin/comments/$REPLY/delete" "{}"
  check "already_deleted"                             "d['detail'] == 'already_deleted'"
  get "the inbox: awaiting reply"                     200 "/api/admin/comments/threads?awaiting=true&item_id=$CITEM"
  check "the private thread waits again"              "[t['kind'] for t in d['items']] == ['private', 'shared']"
else
  echo "SKIP the students' comments (dev-student-1/-2 do not exist yet: run with SEED_CMD twice)"
  SKIPS=$((SKIPS + 1))
fi
send "archive the assignment"                         200 POST "/api/admin/items/$CITEM/archive"
send "archive $G4"                                    200 POST "/api/admin/groups/$G4ID/archive"

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
send "logout"                                         204 POST /api/auth/logout

echo "failures: $FAILS, skipped: $SKIPS"
exit $FAILS
