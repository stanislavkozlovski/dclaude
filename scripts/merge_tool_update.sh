#!/usr/bin/env bash
set -euo pipefail

: "${GITHUB_REPOSITORY:?}" "${BASE_BRANCH:?}" "${HEAD_SHA:?}"

# Resolve the PR by branch, then verify its current head against the commit
# that actually passed all CI checks.
pr_number="$(gh pr list --repo "$GITHUB_REPOSITORY" --base "$BASE_BRANCH" \
  --head chore/tool-version-updates --state open --limit 2 --json number | \
  jq -er 'if length == 1 then .[0].number else error("expected exactly one open tool update PR") end')"

gh api "repos/$GITHUB_REPOSITORY/pulls/$pr_number" | jq -e \
  --arg repository "$GITHUB_REPOSITORY" \
  --arg base "$BASE_BRANCH" \
  --arg sha "$HEAD_SHA" '
    .state == "open" and .draft == false and
    .head.ref == "chore/tool-version-updates" and .head.sha == $sha and
    .head.repo.full_name == $repository and
    .base.repo.full_name == $repository and .base.ref == $base and
    (.title | startswith("chore: "))
  ' >/dev/null

gh pr merge "$pr_number" --repo "$GITHUB_REPOSITORY" --squash --delete-branch \
  --match-head-commit "$HEAD_SHA"

# A merge made with GITHUB_TOKEN does not trigger push CI or release creation.
gh workflow run ci.yml --repo "$GITHUB_REPOSITORY" --ref "$BASE_BRANCH"
