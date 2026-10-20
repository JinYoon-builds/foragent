#!/usr/bin/env bash
# Reset a Foragent trial sandbox to its trial-base state so a trial can be re-run.
# Destructive on purpose: force-resets main, deletes branches/PRs/comments made by trials.
# Refuses to run on any repo whose name does not end in "-sandbox".
set -euo pipefail

REPO="${1:-JinYoon-builds/foragent-sandbox}"
ISSUES="${2:-1 2 3 4}"
LOCAL="${3:-$HOME/Dev/foragent-sandbox}"

case "$REPO" in
  *-sandbox) ;;
  *) echo "refusing: $REPO is not a *-sandbox repo" >&2; exit 1 ;;
esac

echo "== closing open PRs"
for pr in $(gh pr list -R "$REPO" --state open --json number --jq '.[].number'); do
  gh pr close "$pr" -R "$REPO" --delete-branch >/dev/null
  echo "closed PR #$pr"
done

echo "== resetting main to trial-base"
git -C "$LOCAL" fetch -q origin --tags --prune
git -C "$LOCAL" checkout -q main
git -C "$LOCAL" reset -q --hard trial-base
git -C "$LOCAL" push -q --force origin main

echo "== deleting remote branches other than main"
for b in $(gh api "repos/$REPO/branches" --paginate --jq '.[].name'); do
  [ "$b" = main ] && continue
  gh api -X DELETE "repos/$REPO/git/refs/heads/$b" >/dev/null && echo "deleted $b"
done

echo "== resetting issues"
for n in $ISSUES; do
  gh issue reopen "$n" -R "$REPO" >/dev/null 2>&1 || true
  for l in approved ready running needs-human feedback; do
    gh issue edit "$n" -R "$REPO" --remove-label "$l" >/dev/null 2>&1 || true
  done
  for c in $(gh api "repos/$REPO/issues/$n/comments" --paginate --jq '.[] | select(.body | test("foragent:")) | .id'); do
    gh api -X DELETE "repos/$REPO/issues/comments/$c" >/dev/null
  done
  echo "reset #$n"
done

echo "== clearing local Foragent state and worktrees"
STATE="$HOME/.local/share/foragent/${REPO/\//__}"
rm -rf "$STATE"
git -C "$LOCAL" worktree prune
echo "done"
