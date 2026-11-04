#!/usr/bin/env bash
# Smoke test of the admin API (docs/ADMIN_API.md) with curl, against a running public app: the check scenario of
# session 4a.3a through the API: a class from CSV with a class column, a contest for a group, adding and removing a
# student, deadlines with the policies late and no, an extension for one student, submissions and a rejudge in two
# steps, the rollback of the import (K3), the profile time zone, system, audit, languages.
#   tools/smoke-admin-api.sh [BASE_URL] [ORIGIN]       defaults: http://127.0.0.1:8000 and $JUDGE_PUBLIC_ORIGIN
# It asks for an admin (login, password, a TOTP code from the phone); ADMIN_LOGIN, ADMIN_PASSWORD and TOTP_CODE
# from the environment are used instead (named, never shown). Secrets reach curl only through files in a private
# temporary directory and are never printed.
#
# Submissions: the student API (4b) does not exist yet, so a student "submits" with dev_seed:
#   SEED_CMD=/opt/judge/tools/seed-as-judge.sh tools/smoke-admin-api.sh ...
# (dev only; it runs dev_seed as judge on this database, as dev-student-1 and dev-student-2). Without SEED_CMD
# the submission checks are skipped (SKIP) and each line names the dev_seed command and the result it would
# check; that is a description only: the contest is archived and the import rolled back by the end of the run,
# so the deadlines and the access through the group are really checked only with SEED_CMD.
#
# Everything it makes is named smoke-<time> (contest, groups, students); the contest is archived at the end, the
# imported students are rolled back. Every request gives up after 10 s. Exit code = number of failures.
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

echo "--- problems (the same content again: 200 exists) and a contest only for the group"
for p in sum-of-two pair-with-sum; do
  "$HERE/pack-problem.sh" "$ROOT/problems-examples/$p" > "$D/$p.zip" || fail "pack $p"
  c "upload $p"                                       '200|201' POST /api/admin/problems -H "$O" -H @"$D/cookie" -H @"$D/csrf" \
                                                          -H 'Content-Type: application/zip' --data-binary @"$D/$p.zip"
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
get "audit of groups"                                 200 "/api/admin/audit?action=group.&limit=5"
check "newest first, times with Z"                    "d['items'] and all(i['at'].endswith('Z') for i in d['items'])"
get "languages"                                       200 /api/admin/languages
send "logout"                                         204 POST /api/auth/logout

echo "failures: $FAILS, skipped: $SKIPS"
exit $FAILS
