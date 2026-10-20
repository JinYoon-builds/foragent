---
name: fa-init
description: Set up a project folder for Foragent.
---

1. Check whether the current folder is a git work tree. If it is not, get the
   personal login with `gh api user --jq .login` and organization logins with
   `gh api user/orgs --jq '.[].login'`. Use AskUserQuestion (or the available
   question tool) to choose an owner (personal login recommended first, then
   organization logins) and visibility (private recommended first, then public).
   Run `git init --initial-branch=main`, create an initial commit, then run
   `gh repo create <owner>/<folder> --private|--public --source . --push` using
   the selected owner, folder name, and visibility flag.

2. Ask only for review mode and executor, using AskUserQuestion (or the available
   question tool), with recommendations first:
   - batch (recommended): group completed work for human review.
   - fast: merge passing work automatically unless a human review rule applies.
   - careful: require human review of each task before merging.
   - Executor: codex (recommended), or claude.

3. Run `fa init --review <mode> --executor <executor>` in the project folder and
   show its summary.

4. Tell the user the next step is `/fa-order`.
