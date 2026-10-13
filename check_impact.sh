#!/usr/bin/env bash
# Exercise the /changed-literals-impact orchestration route live.
#
# Edit the placeholders below, then run: ./check_impact.sh
set -euo pipefail

BASE="https://apps.cloud-dogfood.domino.tech/apps/e2e-test-literals"
#BASE="http://127.0.0.1:8888"

TEST_REPO="https://github.com/cerebrotech/internal-e2e-tests-service"
TEST_REF="main"
LITERALS_REPO="https://github.com/cerebrotech/domino"
BASE_REF="18f72106469b17c5338f076ac588a95dd1ba6374"
UPDATED_REF="f58ba93730d887fafb610f6f3397d25de36a072a"
# Optional: a GitHub PR number of LITERALS_REPO, used instead of BASE_REF/UPDATED_REF.
PR_NUMBER="${PR_NUMBER:-}"
MIN_REMOVAL_CONFIDENCE="0.9"
# Optional tag substitution bounds; leave empty to keep plain testrail(N) tags.
EXTRA_CASES_ALLOWED="${EXTRA_CASES_ALLOWED:-}"
MISSING_CASES_ALLOWED="${MISSING_CASES_ALLOWED:-}"

echo "== Creating impact job ==" >&2
request_body=$(jq -n \
  --arg test_repo "$TEST_REPO" --arg test_ref "$TEST_REF" --arg literals_repo "$LITERALS_REPO" \
  --arg base_ref "$BASE_REF" --arg updated_ref "$UPDATED_REF" \
  --argjson min_removal_confidence "$MIN_REMOVAL_CONFIDENCE" \
  --arg extra "$EXTRA_CASES_ALLOWED" --arg missing "$MISSING_CASES_ALLOWED" --arg pr "$PR_NUMBER" \
  '{test_repo: $test_repo, test_ref: $test_ref, literals_repo: $literals_repo,
    min_removal_confidence: $min_removal_confidence}
   + (if $pr != "" then {pr_number: ($pr | tonumber)} else {base_ref: $base_ref, updated_ref: $updated_ref} end)
   + (if $extra != "" then {extra_cases_allowed: ($extra | tonumber)} else {} end)
   + (if $missing != "" then {missing_cases_allowed: ($missing | tonumber)} else {} end)')
create_resp=$(curl -sS -L -X POST -H "Authorization: Bearer $CLOUD_DOGFOOD_PAT" "$BASE/changed-literals-impact" \
  -H "Content-Type: application/json" -d "$request_body")
echo "$create_resp" | jq .

job_id=$(echo "$create_resp" | jq -r .job_id)
if [[ -z "$job_id" || "$job_id" == "null" ]]; then
  echo "Failed to create job (no job_id in response)" >&2
  exit 1
fi

echo "== Polling job $job_id ==" >&2
status="pending"
while [[ "$status" != "done" && "$status" != "error" ]]; do
  sleep 1
  status_resp=$(curl -sS -L -H "Authorization: Bearer $CLOUD_DOGFOOD_PAT" "$BASE/changed-literals-impact/jobs/$job_id")
  status=$(echo "$status_resp" | jq -r .status)
  echo "$status_resp" | jq . >&2
done

if [[ "$status" == "error" ]]; then
  echo "Job errored, see status above" >&2
  exit 1
fi

echo "== Fetching result ==" >&2
curl -sS -L -H "Authorization: Bearer $CLOUD_DOGFOOD_PAT" "$BASE/changed-literals-impact/jobs/$job_id/result" | jq .
