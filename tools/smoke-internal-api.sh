#!/usr/bin/env bash
# Smoke test of the internal worker API (docs/WORKER_PROTOCOL.md) with curl, against a running server.
# DEVELOPMENT DATABASE ONLY: it claims and finishes real judgements. Needs 3 queued submissions:
#   JUDGE_DEV=1 python -m app.dev_seed --count 3
#   python -m app.admin_cli create-worker-token smoke | tail -1 > /path/to/token && chmod 600 /path/to/token
#   tools/smoke-internal-api.sh /path/to/token [http://127.0.0.1:8001]
# The token is passed to curl through a header file, never on the command line. Exit code = number of failures.
set -uo pipefail
[ -r "${1:-}" ] || { echo "usage: $0 TOKEN_FILE [BASE_URL]"; exit 2; }
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
ROOT=${2:-http://127.0.0.1:8001}; B=$ROOT/internal/worker/v1
FAILS=0; NAME=smoke-$(date +%s)
(umask 077; printf 'Authorization: Bearer %s\n' "$(tr -d '[:space:]' < "$1")" > $D/h_auth)
printf 'Authorization: Bearer %s\n' "not-a-real-token" > $D/h_bad
c() {  # c <label> <expected status> <method> <path> [curl args...]
  local label=$1 want=$2 method=$3 path=$4; shift 4
  local out code
  out=$(curl -s --noproxy '*' -o $D/body -w '%{http_code}' -X "$method" "$B$path" "$@"); code=$out
  local mark=OK; [ "$code" = "$want" ] || { mark=FAIL; FAILS=$((FAILS + 1)); }
  printf '%-4s %-48s %s (want %s)  %s\n' "$mark" "$label" "$code" "$want" "$(head -c 160 $D/body | tr -d '\n')"
}
J='Content-Type: application/json'
jget() { python3 -c 'import json,sys
v=json.loads(sys.argv[1])
for k in sys.argv[2].split("."): v=v[k]
print(v)' "$1" "$2"; }
reg() { curl -s --noproxy '*' -X POST "$B/register" -H @$D/h_auth -H "$J" -d "{\"name\":\"$1\",\"capabilities\":[\"cpu\"],\"info\":{}}"; }
wh() { printf 'X-Worker-Id: %s\nX-Worker-Instance: %s\n' "$(jget "$1" worker_id)" "$(jget "$1" instance)"; }

echo "--- auth"
c "claim without token"                 401 POST /claim
c "register without token"              401 POST /register -H "$J" -d '{"name":"w0"}'
c "register with a wrong token"         401 POST /register -H @$D/h_bad -H "$J" -d '{"name":"w0"}'
c "register, bad name"                  422 POST /register -H @$D/h_auth -H "$J" -d '{"name":"w 0/.."}'

echo "--- normal cycle"
R1=$(reg $NAME); echo "     register -> $R1"; wh "$R1" > $D/h_w1
c "claim without X-Worker-Id"           400 POST /claim -H @$D/h_auth
c "claim"                               200 POST /claim -H @$D/h_auth -H @$D/h_w1
JOB=$(cat $D/body); JID=$(jget "$JOB" job.judgement_id); ATT=$(jget "$JOB" job.attempt)
PV=$(jget "$JOB" job.problem_version_id); SRC=$(jget "$JOB" job.source_sha256)
c "heartbeat (running 1/2)"             200 POST /heartbeat -H @$D/h_auth -H @$D/h_w1 -H "$J" \
   -d "{\"state\":\"busy\",\"judgement_id\":$JID,\"attempt\":$ATT,\"phase\":\"running\",\"tests_done\":1,\"tests_total\":2}"
c "manifest"                            200 GET "/problem-versions/$PV/manifest" -H @$D/h_auth -H @$D/h_w1
MAN=$(cat $D/body)
c "manifest of a missing version"       404 GET "/problem-versions/999999/manifest" -H @$D/h_auth -H @$D/h_w1
c "blob (source)"                       200 GET "/blobs/$SRC" -H @$D/h_auth -H @$D/h_w1
[ "$(sha256sum < $D/body | cut -d' ' -f1)" = "$SRC" ] && echo "OK   blob content matches its sha256" || { echo "FAIL blob sha256 mismatch"; FAILS=$((FAILS + 1)); }
c "blob, bad sha"                       400 GET "/blobs/zzz" -H @$D/h_auth -H @$D/h_w1
c "blob, unknown sha"                   404 GET "/blobs/$(printf 'f%.0s' $(seq 64))" -H @$D/h_auth -H @$D/h_w1
TESTS=$(python3 -c 'import json,sys; m=json.loads(sys.argv[1])["manifest"]; print(json.dumps([{"test_case_id": t["id"], "verdict": "OK", "time_ms": 10, "memory_kb": 1000, "visible_to_student": False} for g in m["groups"] for t in g["tests"]]))' "$MAN")
result() { echo "{\"attempt\":$1,\"verdict\":\"OK\",\"score\":\"100.00\",\"max_score\":\"100.00\",\"summary\":{\"groups\":[]},\"tests\":$TESTS}"; }

echo "--- fencing: superseded instance and stale attempt"
R2=$(reg $NAME); echo "     re-register $NAME -> new instance (the held job goes back to the queue)"; wh "$R2" > $D/h_w2
c "old instance heartbeat -> superseded" 409 POST /heartbeat -H @$D/h_auth -H @$D/h_w1 -H "$J" -d '{"state":"idle"}'
c "claim by new instance"               200 POST /claim -H @$D/h_auth -H @$D/h_w2
J2=$(jget "$(cat $D/body)" job.judgement_id); A2=$(jget "$(cat $D/body)" job.attempt)
echo "     new instance got judgement $J2 attempt $A2 (first was $JID attempt $ATT)"
c "result with stale attempt -> 409"     409 POST "/judgements/$J2/result" -H @$D/h_auth -H @$D/h_w2 -H "$J" -d "$(result $((A2 - 1)))"
c "heartbeat with stale attempt"        200 POST /heartbeat -H @$D/h_auth -H @$D/h_w2 -H "$J" \
   -d "{\"state\":\"busy\",\"judgement_id\":$J2,\"attempt\":$((A2 - 1)),\"phase\":\"running\"}"
[ "$(jget "$(cat $D/body)" cancel)" = True ] && echo "OK   stale heartbeat is told to cancel" || { echo "FAIL stale heartbeat not cancelled"; FAILS=$((FAILS + 1)); }
c "result with wrong test ids -> 422"    422 POST "/judgements/$J2/result" -H @$D/h_auth -H @$D/h_w2 -H "$J" \
   -d "{\"attempt\":$A2,\"verdict\":\"OK\",\"tests\":[{\"test_case_id\":999999,\"verdict\":\"OK\"}]}"
c "result with current attempt"          200 POST "/judgements/$J2/result" -H @$D/h_auth -H @$D/h_w2 -H "$J" -d "$(result $A2)"
c "same result again (idempotent)"       200 POST "/judgements/$J2/result" -H @$D/h_auth -H @$D/h_w2 -H "$J" -d "$(result $A2)"

echo "--- release"
c "claim next"                          200 POST /claim -H @$D/h_auth -H @$D/h_w2
J3=$(jget "$(cat $D/body)" job.judgement_id); A3=$(jget "$(cat $D/body)" job.attempt)
c "release"                             200 POST "/judgements/$J3/release" -H @$D/h_auth -H @$D/h_w2 -H "$J" -d "{\"attempt\":$A3,\"reason\":\"shutdown\"}"
c "result after release -> 409"         409 POST "/judgements/$J3/result" -H @$D/h_auth -H @$D/h_w2 -H "$J" -d "$(result $A3)"

echo "--- tunnel / proxy headers"
for h in 'cf-ray: 1' 'cf-connecting-ip: 1.2.3.4'; do
  code=$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' "$ROOT/internal/health" -H "$h")
  [ "$code" = 403 ] && echo "OK   health with '$h' -> 403" || { echo "FAIL health with '$h' -> $code"; FAILS=$((FAILS + 1)); }
done
echo "--- status"
curl -s --noproxy '*' "$ROOT/internal/status" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps({"load": d["load"], "workers": [{k: w[k] for k in ("name", "state", "judgement_id")} for w in d["workers"]]}))'
c "deregister"                          200 POST /deregister -H @$D/h_auth -H @$D/h_w2
echo "failures: $FAILS"; exit $FAILS
