# P2 spec: scheduler tick + CLI

Builds on P1 (`src/foragent/{issue,config,reasons,state,tracker}.py`, read them first). Product rules: `docs/gate.md` sections 3, 4, 8 and `docs/contract.md` (추가 6, 8-3, 8-4). No runner yet: the scheduler promotes issues and *launches* a runner through an injected launcher; the real runner arrives in P3.

English code/comments. Offline tests only (FakeTracker, temporary git repos created with the `git` binary, which is available).

## New modules

### `src/foragent/labels.py`
`LABELS: list[LabelSpec]` — `approved` (human approval), `ready`, `running`, `needs-human`, `feedback`, `risk:high`, each with a color and a one-line description. Constants `APPROVED, READY, RUNNING, NEEDS_HUMAN, FEEDBACK, RISK_HIGH`.

### `src/foragent/git.py`
`GitRepo(root: Path, run=None)` (injectable runner like GhTracker):
- `fetch(remote="origin", ref: str | None = None)`
- `blob_sha(ref: str, path: str) -> str | None` (`git rev-parse <ref>:<path>`; None when the path does not exist at that ref)
- `blob_shas(ref, paths) -> dict[str, str]` (missing paths map to `"missing"`)
- `remote_slug(remote="origin") -> str` parsing `git@github.com:o/r.git`, `https://github.com/o/r(.git)`, `ssh://git@github.com/o/r.git` → `o/r`.

### Config addition
Add `repo: None` to `DEFAULTS`/`Config` (string `owner/name`, optional; when None the CLI uses `GitRepo.remote_slug()`).

### `src/foragent/scheduler.py`
```python
@dataclass
class TickContext:
    repo: str; repo_root: Path; tracker: Tracker; store: StateStore; config: Config
    git: GitRepo; now: Callable[[], datetime]          # timezone-aware
    launcher: Callable[[int], int | None]              # starts the runner for issue n, returns pid
    start_runs: bool = True

@dataclass
class TickReport:
    skipped: str | None          # e.g. "another tick is running"
    actions: list[str]           # human-readable lines, e.g. "#2 promoted to ready"
```
`tick(ctx) -> TickReport`, run entirely inside `ctx.store.tick_lock()`; if the lock is not acquired return immediately with `skipped`.

Algorithm (one tick):
1. `git.fetch("origin", config.base_branch)` once. Base ref = `origin/<base_branch>`.
2. For each open issue, ascending number. Skip issues labelled `needs-human` entirely (a human must remove that label to resume).
3. **Approval.** The valid approval is the latest `labeled approved` event whose actor is in `config.approvers` and which is not followed by an `unlabeled approved` event. Labels added by anyone else are ignored (log an action line once per tick, no comment). No valid approval → skip.
4. **Snapshot.** If the store has no approval for the issue or its `approved_at` differs from the event time: parse the *current* body, compute `body_hash`, compute acceptance blobs (`acceptance_paths(done_when_commands, config.acceptance_dir)` → `git.blob_shas(base_ref, ...)`), `store.save_approval(...)`, and post `format_approval_comment(...)`.
5. **Integrity (every tick, for every approved issue including ready/running ones).** If the current body hash or the current acceptance blobs differ from the snapshot → flag `hash-mismatch` (details say which: body and/or which files), remove `approved`, `ready`, `running`, clear the stored approval. If the issue was running, the runner is not killed in P2 (P3 adds that); just record it in the report.
6. Issues already `ready` or `running` need no promotion → continue.
7. **Promotion.** Parse the *snapshot* body. No machine check → flag `no-machine-check`. Any blocked-by dependency still open → stay. Otherwise add `ready`.
8. **Start.** If `store.active_runs()` is non-empty → no start (one run per project). Else if `start_runs` and `in_run_hours(now().astimezone(), config.run_hours)`: take the lowest-numbered issue labelled `ready` (use labels as updated during this tick), re-run step 5's integrity check against a freshly fetched issue (contract: check right before running); if it passes, swap `ready`→`running`, `store.start_run(n, branch=f"foragent/issue-{n}", worktree=store.worktree_path(n))`, `pid = launcher(n)`, `store.update_run(n, pid=pid)`.

`flag(ctx, n, reason, summary, details=None)`: add `needs-human`, remove `ready` and `running`, post `format_comment(reason, summary, details)`. Write clear, specific English summaries (e.g. "Approval invalidated: the issue body changed after approval. Re-approve to run it.").

Never close issues, never touch PRs, never edit issue bodies in this phase.

### `src/foragent/launcher.py`
`spawn_runner(repo_root: Path, store: StateStore) -> Callable[[int], int]`: returns a launcher that starts `[sys.executable, "-m", "foragent.cli", "gate", "run-issue", str(n), "--repo-root", str(repo_root)]` with `start_new_session=True`, stdout/stderr appended to `store.log_path(n)`, and returns the pid without waiting.

### `src/foragent/cli.py` (argparse; `main(argv) -> int`; also make `python -m foragent.cli` work)
- `fa gate tick [--repo-root PATH] [--no-start] [--json]`: build GitRepo, Config (`load_config`), repo slug (`config.repo` or remote), `GhTracker`, `StateStore.for_repo`, launcher; run `tick`; print the report (plain lines, or JSON with `--json`). Exit 0 on success including "skipped".
- `fa gate status [--repo-root PATH] [--json]`: open issues grouped by state (needs-human with its latest reason code, running, ready, approved-waiting, unapproved), active runs, last 10 history entries.
- `fa gate run-issue N --repo-root PATH`: placeholder for P3 — finish the run with outcome `not-implemented`, remove `running`, add `ready` back, exit 0.
- `fa --version`.
Errors (ConfigError, TrackerError) print one clear line to stderr and exit 1.

## Tests
- scheduler with FakeTracker + a real temporary git repo (bare "origin" + clone, acceptance files committed): approval by approver vs non-approver; approval removed then re-added (new snapshot); body edited after approval → hash-mismatch flag and labels; acceptance file changed on origin/main after approval → hash-mismatch; no machine check → flag once (second tick does nothing because of needs-human); blocked-by open stays approved, becomes ready after the blocker closes; one-run-per-project; start picks the lowest number; run_hours outside → no start; `start_runs=False`; tick lock held → skipped; launcher receives the issue number and pid is stored; integrity re-check right before start.
- git: remote_slug forms, blob_sha present/missing.
- cli: `gate tick --no-start --json` with monkeypatched GhTracker/GitRepo; error exit codes.

## Done criteria
`uv run pytest -q` passes (all P1 tests too). Report what you built and any ambiguity.
