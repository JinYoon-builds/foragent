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

2. At first use, ask only these configuration questions: merge (review) mode, then Claude quota
   mode. Use AskUserQuestion (or the available question tool), recommendation first:
   - batch (recommended): group completed work for human review.
   - fast: merge passing work automatically unless a human review rule applies.
   - careful: require human review of each task before merging.
   On a re-run, skip a question whose key is already set in its own scope file
   ([settings](references/settings.md)). Leave the executor to the config default.

3. Run `fa init --review <mode>` in the project folder (no `--executor`, never
   `--claude-quota`), so the answer lands in init's own commit, and show its
   summary. Only if Claude is available (`claude auth status` logged in, or
   `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` set), then ask the quota mode:
   keep (recommended: leave part of Claude's limit for you) or burn (use all of
   it), and save it with `fa settings set claude_quota.mode <mode>`. Both can be
   changed later with `fa settings set review <mode>` and `fa settings set claude_quota.mode <mode>`. New boards get five Status columns: 🗂 Backlog, 🌰 Up next,
   🐿 In progress, ✋ Your turn, 📦 Done. A blocked card stays in 🐿 In progress;
   its `blocked` label is the ⚠. If init stops because an existing team
   board needs a mapping, show its column names and ask once which column each
   group uses (Backlog, Up next, In progress, Your turn, Done). Then rerun
   `fa init ... --board-map 'Backlog=<col>,Up next=<col>,In progress=<col>,Your turn=<col>,Done=<col>'`.
   An existing board that already has these five columns is mapped without asking.
   The mapping is saved as `board_map` in `.foragent.yml`; later inits do not ask again.
   `fa init` offers one optional account-wide step: wrapping
   Claude Code's statusline with `fa statusline`, which records Claude's rate
   limits and still runs the person's current statusline unchanged. When the
   command cannot prompt (no terminal), ask this once with AskUserQuestion (or the
   available question tool): Yes (recommended) or No, then rerun with
   `--statusline yes|no`. Either answer is remembered and never asked again; skip the question if the summary already reports a decision.
   `fa init` also offers one optional project step: adding the single rule
   `Bash(fa:*)` to this project's `.claude/settings.local.json`, so Claude can
   run fa commands here without a prompt each time. It covers this project
   only, never the account, and adds nothing else. fa commands do not expose
   permanent deletion, money transactions, sending to others, or widening
   permissions. Add it only after the person explicitly accepts; never answer
   for them. The CLI asks directly only when the project already has
   `.claude/settings.json` or `.claude/settings.local.json`. In a new project,
   the summary says `FA commands permission: not yet decided`. Every time that
   note appears, or the command says it could not ask because there is no
   terminal, ask the person with AskUserQuestion (or the available question tool):
   - Yes: add only `Bash(fa:*)`, for this project only.
   - No: keep approving commands individually.
   Use no recommended default. Then run `fa init --fa-allow yes` or
   `fa init --fa-allow no` with their explicit answer. If they have not answered,
   leave it undecided and ask again on the next setup run. Either answer is
   remembered for this project, so skip a decided offer. A dry run neither asks
   nor grants, and must never be followed by an automatic grant.

   When `.foragent.yml` has no `checks` key, `fa init` chooses and saves `ci` for
   public repositories (hosted Actions minutes are free), or `local` for private
   repositories (to avoid paid Actions minutes). Unreadable visibility selects
   `local` with a note. Init prints one line naming the mode, why it chose it,
   and how to change `checks` in `.foragent.yml`. An existing key is preserved.
   CI acceptance measurement requires pytest; other detected or configured test
   commands select `local` with a note. An existing `checks: ci` configuration
   with an unsupported command uses the local comparison path at runtime.
   A private repository can avoid measurement minutes with its own runner:
   set `ci_runs_on: [self-hosted, linux]` in `.foragent.yml`. Init mentions this
   option without asking for a runner label. Use self-hosted runners only for
   private repositories; GitHub advises against using them on public ones.
   Selecting `ci` generates `.github/workflows/foragent-acceptance.yml` and
   includes it with `.foragent.yml` in the init commit. Measurement uses the base
   configuration with read-only permissions. The runner reads the exact head's
   measurement artifacts directly and verifies every configured shard.
   `ci_shards` defaults to
   4 and `ci_runs_on` to `ubuntu-latest`. After changing these settings, run
   `fa ci workflow --repo-root .` to rewrite the generated workflow and include
   it in a separate configuration commit. `checks_fallback: local`
   optionally runs a local acceptance comparison when CI evidence is unusable.

   Team readiness: every `fa init` reads the signed-in GitHub account again and
   prints `GitHub account: <login>`. This factory works only issues labelled
   `by:<login>` (stamped when that person publishes an order), plus unlabelled
   issues when `legacy_owner` in `.foragent.yml` names this login or is unset.
   Show the account line to the person. If it says unknown, ask them to run
   `gh auth login` (their own account, not an App or bot token) and rerun
   `fa init`; nothing can be published until it works. After `gh auth switch`,
   rerun `fa init` so the factory uses the new account. If the summary reports
   that team setup is missing, another person's orders already exist without a
   shared `legacy_owner`: before this factory starts, the original factory adds
   `legacy_owner: <its login>` through a normal PR. Before a second person joins,
   every running `fa` must be stopped or upgraded (an old gate ignores `by:`
   labels), and each person runs one gate per repository. Each person needs write
   access including labels; a publication whose owner label GitHub drops stops
   and closes that issue. `by:` labels are trusted: to hand work over or fix a
   wrong label, stop the affected factories, keep their unfinished work, agree on
   the new owner, change the single `by:` label (and `legacy_owner` if needed),
   then restart only the owning factory. README "Working as a team" has details.

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
   for human follow-up. Also show each `collect_only` finding (workflow `file` and
   suggested `add` command): a collection step lists every test without running
   it, so it catches import errors and module name clashes. Collection steps must
   keep the acceptance directory included; report them, do not insert them. If the answer is no or CI already excludes
   the acceptance directory, leave CI unchanged and create no CI commit.

9. Run `fa order context --json` and read the project's code and docs to determine
   whether it has a screen: web UI and terminal UI (TUI) both count. A nonempty
   `ui.frameworks` is evidence of a screen, but an empty list does not rule out a
   TUI or a planned screen. For a project with no screen, skip the design direction
   question. Otherwise, propose a design lane covering the project's actual UI and
   design foundation paths, reusing existing lanes where paths overlap.
   Run `fa skills catalog --json` and ask only which style skill should set the
   design direction (for example, `frontend-design`), using AskUserQuestion (or the
   available question tool) with the recommended choice first. Offer skills with
   role `style` from the existing catalog; if none fits, report that and leave the
   style unset. Show the paths and style choice for approval, create approved new
   lanes with `fa lanes add <name> --paths '<path>/**'`, then fix only the chosen
   style with `fa lanes skills <name> <style-id>` on the relevant new or reused lane.
   Each lane agent chooses tool, review, and domain skills per issue as needed;
   examples include `design-system`, `design-critique`, `accessibility-review`,
   `ux-copy`, and `webapp-testing`. These are examples, not a fixed design lane
   skill list. Design interviews and foundation issues belong to the planning interview (`/fa-order`, which people
   open from `/fa`) (D33).

10. Tell the user the next step is `/fa` (it opens the planning interview): for an existing project, normalize
   existing open issues into the Foragent format with the user's approval; for
   a new project, order the first work.
