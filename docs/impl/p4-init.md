# P4 spec: `fa init` + launchd install/uninstall + `fa-init` skill

Builds on P1–P3 (read `src/foragent/*.py`, `docs/contract.md` section 4, `docs/gate.md` sections 2, 8, 9, `docs/decisions.md` D12, D14, D15). Offline tests only: inject the gh/git/launchctl runners and use temporary directories (never write to the real `~/Library/LaunchAgents` in tests).

## `fa init`
```
fa init [--repo-root PATH] [--approver LOGIN ...] [--review fast|batch|careful]
        [--executor codex|claude] [--run-hours always|HH-HH] [--no-push] [--no-launchd] [--dry-run]
```
Steps (each idempotent; re-running init on an initialized repo only fills what is missing and never overwrites an existing `.foragent.yml` value):
1. Preconditions: `repo-root` is a git work tree with an `origin` remote on github.com; `gh auth status` succeeds. Clear error otherwise. Normalize `repo-root` with `git rev-parse --show-toplevel`; if a subdirectory was supplied, use the top-level directory and report the change.
2. Labels: `tracker.ensure_labels(LABELS)`.
3. Files (create only if missing):
   - `.foragent.yml` with `review`, `executor`, `approvers` (default: the authenticated login from `gh api user --jq .login`), `acceptance_dir: tests/acceptance/`, `run_hours: always`. Write `approvers` and `review`, plus every explicitly supplied option even if it equals a built-in default, so global configuration cannot override that choice. Omit other default values. Include a comment line pointing to the docs.
   - `.github/ISSUE_TEMPLATE/foragent-task.md`: a **Markdown issue template** (not an issue form: forms render `### ` headings, the contract needs `## `). Front matter `name: Foragent task`, `about: ...`, body exactly the contract format: marker line, `## Goal`, `## Out of scope`, `## Done when` with an empty ```sh block and a one-line hint that commands must run in CI-free shell and acceptance tests live in `tests/acceptance/`.
   - `tests/acceptance/.gitkeep` if the directory does not exist.
4. Commit created or uncommitted setup paths on the base branch (`.foragent.yml`, `.github/ISSUE_TEMPLATE/foragent-task.md`, `tests/acceptance/.gitkeep`; `git add` those paths only; commit message "Set up Foragent"; author from git config, fallback `foragent <foragent@localhost>`). Preserve unrelated staged changes. Push with the explicit `refs/heads/<base>:refs/heads/<base>` refspec unless `--no-push`. On rerun, retry staged or unstaged setup changes after a failed commit. When there are no pending paths, retry the push if local base is ahead of `origin/base` and those commits include the exact subject "Set up Foragent"; do not retry diverged branches. If the remote tracking branch does not yet exist, a local setup commit also qualifies for a push. Skip completed Git steps.
5. launchd: unless `--no-launchd`, call `install` below.
6. Print a short summary of what was created and what already existed. `--dry-run` prints the plan and changes nothing.

## launchd (`src/foragent/launchd.py`)
- Label: `dev.foragent.<sanitized owner>.<sanitized repo>.<hash>` (owner and repo lowercase, characters outside `[a-z0-9.-]` replaced by `-`; hash is the first 8 hexadecimal characters of SHA-1 of the original `owner/repo` string). Install and uninstall also boot out and remove an existing plist with the legacy label without the hash.
- Plist path: `~/Library/LaunchAgents/<label>.plist` (injectable base dir for tests).
- ProgramArguments: absolute path of the `fa` executable that is currently running (resolve `sys.argv[0]` / `shutil.which("fa")`; if it lives in a project `.venv`, use that absolute path), then `gate tick --repo-root <abs repo root>`.
- `StartInterval` 300, `RunAtLoad` true, `ProcessType` Background.
- EnvironmentVariables: `PATH` = `~/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin` (expanded), `HOME`, `LANG=en_US.UTF-8`.
- StandardOutPath/StandardErrorPath: `<state root>/launchd.out.log` / `launchd.err.log`.
- Generate with `plistlib` (not string templates).
- `install(repo_root)`: write plist (replace if content differs), then `launchctl bootout gui/<uid>/<label>` (ignore "not loaded") and `launchctl bootstrap gui/<uid> <plist>`. Return the label.
- `uninstall(repo_root)`: `launchctl bootout gui/<uid>/<label>` (ignore not loaded), remove the plist. Does not touch GitHub or the state dir.
- `status(repo_root)`: plist present? loaded? (`launchctl print gui/<uid>/<label>` exit code) last exit status if available.

## CLI
- `fa gate install [--repo-root]`, `fa gate uninstall [--repo-root]`, `fa gate launchd-status [--repo-root]`.
- `fa gate status` additionally prints one line: launchd loaded/not loaded. Reuse the resolved repository slug, including a configured `repo` when no `origin` remote exists.

## `skills/fa-init/SKILL.md`
A thin Claude Code / Codex skill (frontmatter `name: fa-init`, short `description` saying it sets up a project folder for Foragent). Instructions, in this order, short:
1. If the current folder is not a git repo: ask (AskUserQuestion, recommendation first) for owner (personal vs organization login from `gh api user/orgs`) and visibility (private recommended); then `git init`, an initial commit, `gh repo create <owner>/<folder> --private|--public --source . --push`.
2. Ask only for things `fa init` cannot default: review mode (recommend `batch`, explain fast/batch/careful in one line each) and executor (recommend codex).
3. Run `fa init` with those flags and show its summary.
4. Tell the user the next step is `/fa-order`.
Keep the skill under 60 lines. English.

## Tests
init on a temp repo with a bare origin: files created with exact contents, idempotent second run, existing `.foragent.yml` untouched, only Foragent paths committed when the tree is dirty, `--no-push`, `--dry-run` changes nothing, precondition errors. Cover failed commit/push recovery, explicit default options over global settings, branch push refspec overrides, and nested directory normalization. launchd: label sanitising and hash collisions, legacy cleanup, plist content via plistlib round trip, install/uninstall/status argv with fake runner, replace-if-different. CLI wiring, including status for a configured repository without origin.

## Done criteria
`uv run pytest -q` passes. Report what you built and any ambiguity.
