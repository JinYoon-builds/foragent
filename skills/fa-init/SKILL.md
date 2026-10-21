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

4. Run `fa adopt survey --json` after setup. When `kind` is `existing`,
   complete steps 5–8 in order. When it is `empty`, go to step 9.

5. Show a short "What I read" list from the survey:
   - `hot_paths`: frequently changed paths and their commit counts.
   - `existing_setup`: settings already present that will NOT be touched,
     including existing CLAUDE.md, AGENTS.md, .claude/ files and skills.
   - `ci`: workflow files, detected test commands, and `runs_acceptance` findings.
   Preserve existing agent setup; the only CI edit is the approved step 8 change.

6. Propose lanes from `hot_paths`, showing each name and path globs. Reuse
   existing lanes where they fit and keep overlapping paths in the same lane.
   Use AskUserQuestion (or the available question tool): Approve all (recommended),
   Approve some, or Change lanes. Create only the approved new lanes with
   `fa lanes add <name> --paths '<path>/**'`, substituting the approved name and
   globs. This creates new lane definitions; it must not overwrite existing
   agent files or install or edit existing skills.

7. Run `fa adopt baseline --json`. Report `command`, `passed`, `exit_code`,
   and the already-broken tests in `failing`. Record these as the starting
   state rather than fixing them during setup. If no test command was found,
   report `skipped`; if the run timed out, report that explicitly. An empty
   failure list with a nonzero exit code is not a passing baseline.

8. If any CI entry has `runs_acceptance: true`, show the affected workflows and
   use AskUserQuestion (or the available question tool): Yes, exclude the
   acceptance directory (recommended), or No, keep CI as is. Only on an explicit
   yes, run `fa adopt ci --exclude-acceptance --json`, report the result, and
   commit only the workflow files listed in `changed`. Report `manual` entries
   for human follow-up. If the answer is no or CI already excludes the acceptance
   directory, leave CI unchanged and create no CI commit.

9. Run `fa order context --json`. When `ui.frameworks` is nonempty, propose a
   design lane covering the project's actual UI and design foundation paths, reusing
   existing lanes where paths overlap. Suggest the relevant skills from
   `frontend-design`, `design-system`, `design-critique`, `accessibility-review`,
   `ux-copy`, and `webapp-testing`; select at most three to attach (the lane limit).
   Show the paths and skill choices for approval before `fa lanes add`. Design
   interviews and foundation issues belong to `/fa-order` (D33).

10. Tell the user the next step is `/fa-order`: for an existing project, normalize
   existing open issues into the Foragent format with the user's approval; for
   a new project, order the first work.
