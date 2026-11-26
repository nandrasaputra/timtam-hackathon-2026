#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="${DIR}/bin/mobox-glab"
ASSEMBLE="${DIR}/bin/assemble-prompt"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

for name in summary_format.md fe_vs_be.md mobile_gojek.md transport_context.md cross_check.md rca.md thread_read.md mr_review.md; do
  [[ -f "${DIR}/prompts/${name}" ]] || fail "missing prompts/${name}"
done
grep -q "HTTP 500" "${DIR}/prompts/fe_vs_be.md" || fail "FE vs BE rubric"
grep -q "Ask BE to send MoBox the response" "${DIR}/prompts/fe_vs_be.md" || fail "missing API response ask"
grep -q "Ask BE to send MoBox the response" "${DIR}/prompts/summary_format.md" || fail "action item must name the response"
grep -q "nullable to non-null" "${DIR}/prompts/fe_vs_be.md" || fail "contract-change crash"
grep -q "nullable to non-null" "${DIR}/prompts/mobile_gojek.md" || fail "mobile contract crash"
grep -q "crash data unavailable" "${DIR}/prompts/summary_format.md" || fail "impact line"
grep -q "Raise MR fix" "${DIR}/prompts/summary_format.md" || fail "MR offer"
grep -q "\*\*Change:\*\*" "${DIR}/prompts/summary_format.md" || fail "change plan"
grep -q "Raise MR?" "${DIR}/prompts/summary_format.md" || fail "change MR offer"
grep -q "check both projects" "${DIR}/prompts/investigate.md" || fail "missing platform checks both"
grep -q "discrepancy" "${DIR}/prompts/investigate.md" || fail "platform discrepancy"
grep -q "up to three hypotheses" "${DIR}/prompts/investigate.md" || fail "low confidence still helps"
grep -q "asks you to open or raise an MR" "${DIR}/prompts/investigate.md" || fail "typed MR request brings the card back"
grep -q "MR review" "${DIR}/prompts/investigate.md" || fail "mr review job"
grep -q "\*\*Verdict:\*\*" "${DIR}/prompts/mr_review.md" || fail "mr review verdict"
grep -q "Do not end with" "${DIR}/prompts/mr_review.md" || fail "mr review must not offer an MR"
if grep -q "transport only" "${DIR}/prompts/investigate.md"; then
  fail "still transport only"
fi

[[ -x "${ASSEMBLE}" ]] || fail "assemble-prompt"
prompt="$("${ASSEMBLE}")"
echo "${prompt}" | grep -q "\*\*Issue:\*\*" || fail "summary shape"
echo "${prompt}" | grep -q "\*\*FE or BE:\*\*" || fail "summary headings missing"
echo "${prompt}" | grep -q "\*\*Checked:\*\*" || fail "checked line"
echo "${prompt}" | grep -q "\*\*Hypothesis:\*\*" || fail "investigation headings dropped"
if grep -q "\*\*Hypothesis" "${DIR}/prompts/investigate.md"; then
  fail "second summary in investigate"
fi
grep -q "source_branch" "${DIR}/prompts/investigate.md" || fail "MR branch"
grep -q "Do not ask which branch" "${DIR}/prompts/investigate.md" || fail "MR link rule"
grep -q "is the release to search" "${DIR}/prompts/investigate.md" || fail "rc app version"
grep -q "Use the mobox_gitlab tool for all mobile source searches" "${DIR}/prompts/investigate.md" || fail "gitlab is the only code search"
grep -q "develop" "${DIR}/prompts/summary_format.md" || fail "default branch"
grep -q "plain sentences" "${DIR}/prompts/investigate.md" || fail "ask before triage"
grep -q "confidence is low" "${DIR}/prompts/investigate.md" || fail "low confidence asks"
if grep -q "Which branch?" "${DIR}/prompts/investigate.md"; then
  fail "still asks which branch"
fi
echo "${prompt}" | grep -q "Aloha" || fail "mobile skill missing"
echo "${prompt}" | grep -q "Do not restate the bug" || fail "rca skill missing"
echo "${prompt}" | grep -q "Do not write the summary until mobox-glab file has run" || fail "rca must read the file"
echo "${prompt}" | grep -q "plain standard English" || fail "standard english"
echo "${prompt}" | grep -q "Do not narrate the search" || fail "no narration"
echo "${prompt}" | grep -q "action item" || fail "action item"
echo "${prompt}" | grep -q "The last line is the request" || fail "thread read missing"
echo "${prompt}" | grep -q "\[linked message\]" || fail "linked card"
echo "${prompt}" | grep -q "\[linked record\]" || fail "linked record"
echo "${prompt}" | grep -q "continues this session" || fail "follow-up continues"
echo "${prompt}" | grep -q "first GoHost frame" || fail "android rca missing"
echo "${prompt}" | grep -q "force unwrap" || fail "ios rca missing"
echo "${prompt}" | grep -q "GoCar" || fail "transport skill missing"
grep -q "search only that project" "${DIR}/prompts/investigate.md" || fail "named platform stays on one repo"
grep -q "not a new code check" "${DIR}/prompts/investigate.md" || fail "conversation skips glab"
grep -q "form attachment, already sent" "${DIR}/prompts/investigate.md" || fail "form image is read"
grep -q "only project to search" "${DIR}/prompts/cross_check.md" || fail "cross-check rule"
grep -q "not found" "${DIR}/prompts/cross_check.md" || fail "missing other-platform result"
grep -q "same session" "${DIR}/prompts/cross_check.md" || fail "cross-check keeps the session"
if grep -q "new session" "${DIR}/prompts/cross_check.md"; then
  fail "cross-check starts a new session"
fi
if grep -q "search gohost only" "${DIR}/prompts/investigate.md"; then
  fail "old one-platform route"
fi
echo "${prompt}" | grep -q "only project to search" || fail "cross-check not in prompt"
echo "${prompt}" | grep -q "\*\*Verdict:\*\*" || fail "mr review not in prompt"

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
cat > "${tmp}/glab" << 'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "${GLAB_LOG}"
case "$*" in
  *'/repository/branches?search=rc%2F5.65'*)
    printf '[{"name":"rc/5.65.0"}]\n'
    ;;
  *'/repository/branches/missing')
    printf 'glab: 404 Branch Not Found (HTTP 404)\n' >&2
    exit 1
    ;;
  *'/repository/branches/blocked')
    printf 'glab: 403 Forbidden (HTTP 403)\n' >&2
    exit 1
    ;;
  *) printf '[]\n' ;;
esac
EOF
chmod +x "${tmp}/glab"

export GLAB_LOG="${tmp}/log"
export MOBOX_GLAB="${tmp}/glab"
export GITLAB_HOST="source.golabs.io"
export GITLAB_TOKEN="glpat-test"
export GOHOST_PROJECT="mobile/GoHost"
export IOS_PROJECT="mobile/consumer-ios"
: > "${GLAB_LOG}"

[[ "$("${BIN}" branches ios 'rc/5.65')" == 'rc/5.65.0' ]] || fail "branch suggestions"
grep -Fq 'projects/mobile%2Fconsumer-ios/repository/branches?search=rc%2F5.65' "${GLAB_LOG}" || fail "branch search encoding"
missing="$("${BIN}" search ios missing Activate 2>&1)" && fail "missing branch accepted"
[[ "$missing" == *"branch not found on mobile/consumer-ios: missing"* ]] || fail "missing branch error"
blocked="$("${BIN}" search ios blocked Activate 2>&1)" && fail "blocked branch accepted"
[[ "$blocked" == *"could not verify branch on mobile/consumer-ios: blocked"* ]] || fail "access failure mistaken for a missing branch"

"${BIN}" mrs gohost opened >/dev/null
grep -q "projects/mobile%2FGoHost/merge_requests?state=opened" "${GLAB_LOG}" || fail "mrs"

"${BIN}" mr ios 12 >/dev/null
grep -q "projects/mobile%2Fconsumer-ios/merge_requests/12" "${GLAB_LOG}" || fail "mr"

"${BIN}" diff gohost 3 >/dev/null
grep -q "merge_requests/3/diffs" "${GLAB_LOG}" || fail "diff"

"${BIN}" pipelines gohost rc/5.81.0_bm_develop >/dev/null
grep -q "pipelines?ref=rc%2F5.81.0_bm_develop" "${GLAB_LOG}" || fail "pipelines"

"${BIN}" trace ios 99 >/dev/null
grep -q "jobs/99/trace" "${GLAB_LOG}" || fail "trace"

"${BIN}" issues gohost >/dev/null
grep -q "projects/mobile%2FGoHost/issues?state=opened" "${GLAB_LOG}" || fail "issues"

"${BIN}" commits gohost develop FareViewModel.kt >/dev/null
grep -q "repository/commits?ref=develop&path=FareViewModel.kt" "${GLAB_LOG}" || fail "commits"

lines_before="$(wc -l < "${GLAB_LOG}" | tr -d ' ')"
"${BIN}" crash gohost | grep -q "crash data unavailable" || fail "crash text"
lines_after="$(wc -l < "${GLAB_LOG}" | tr -d ' ')"
[[ "${lines_before}" == "${lines_after}" ]] || fail "crash called gitlab"

if "${BIN}" search gohost aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa Fare >/dev/null 2>&1; then
  fail "sha branch accepted"
fi
if "${BIN}" mrs nope opened >/dev/null 2>&1; then
  fail "bad project accepted"
fi
if "${BIN}" mr gohost abc >/dev/null 2>&1; then
  fail "bad iid accepted"
fi

echo "ok"
