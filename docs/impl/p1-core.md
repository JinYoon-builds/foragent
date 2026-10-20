# P1 spec: Foragent core library

Repository: `~/Dev/foragent` (uv project, package `src/foragent`, Python >= 3.12, deps: PyYAML only; pytest is installed). Read `docs/contract.md` (sections 1–4, 추가 6, 추가 8) and `docs/gate.md` (sections 2, 3, 9) for the product rules. This phase builds pure, well-tested building blocks. No scheduler, no runner, no CLI yet.

Write code and comments in English. Keep modules small and plain. Use dataclasses and type hints. No network access is available in your sandbox: everything must be testable offline.

## Modules to create

### `src/foragent/issue.py`
- `MARKER = "<!-- foragent:v1 -->"`
- `@dataclass(frozen=True) class ParsedIssue`: `version: str | None` ("v1" when the marker is present, else None), `goal: str`, `out_of_scope: str`, `done_when_commands: list[str]`, `done_when_checklist: list[ChecklistItem]` where `ChecklistItem(text: str, checked: bool)`.
- `parse_body(body: str) -> ParsedIssue`
  - Sections start at lines exactly `## Goal`, `## Out of scope`, `## Done when` (English, case-sensitive, trailing spaces allowed) and end at the next line starting with `## ` outside code fences or end of body. Missing section → empty string / empty lists.
  - In `## Done when`: every fenced code block (``` or ```` with optional language tag such as `sh`, `bash`) is one command script; keep its inner text exactly (strip one trailing newline). Lines matching `- [ ] text` / `- [x] text` (also `*`, `X`) outside code blocks are checklist items.
  - Normalize CRLF to LF before parsing.
- `body_hash(body: str) -> str`: hex sha256 of the body after CRLF→LF and stripping leading/trailing whitespace of the whole body.
- `has_machine_check(parsed) -> bool`: at least one non-blank Done when command.
- `acceptance_paths(commands: list[str], acceptance_dir: str) -> list[str]`: whitespace/quote-split tokens of all commands; keep tokens that equal the acceptance dir or start with it (normalize `./` prefix and a missing trailing slash on the dir); strip `::test_name` suffixes (pytest node ids); return sorted unique repo-relative paths.

### `src/foragent/config.py`
- `DEFAULTS` dict exactly as `docs/gate.md` section 9 except: `review: "batch"`, `review_paths: {}`, `always_human: []`, `executor: "codex"`, `executor_model: None`, `reviewer: None` (meaning "the other executor"), `reviewer_model: None`, `approvers: []`, `acceptance_dir: "tests/acceptance/"`, `notify: None`, `run_hours: "always"`, `stall_minutes: 30`, `max_runtime_minutes: 90`, `max_retries: 2`, plus `base_branch: "main"`.
- `@dataclass(frozen=True) class Config` with those fields.
- `load_config(repo_root: Path, global_path: Path | None = None) -> Config`: merge DEFAULTS < global YAML (default `~/.config/foragent/config.yml`, missing file is fine) < `repo_root/.foragent.yml` (missing file is an error: `ConfigError("not a Foragent repo: .foragent.yml missing")`). Validate: `review` and every `review_paths` value in {fast, batch, careful}; `executor`/`reviewer` in {codex, claude, None for reviewer}; ints positive; `run_hours` is `always` or `HH-HH` (00–23). Unknown keys → `ConfigError` naming the key. 
- `resolved_reviewer(config) -> str`: `reviewer` if set, else the executor that is not `config.executor`.
- `in_run_hours(now: datetime, run_hours: str) -> bool`: `always` → True; `22-08` wraps midnight (22:00–07:59 true); `09-18` → 09:00–17:59; equal start and end → True all day.

### `src/foragent/reasons.py`
- Reason codes from contract 추가 8-4 as a `StrEnum`: `spec-question, hash-mismatch, always-human-path, acceptance-touched, risk-high, retries-exceeded, stalled, timeout, no-machine-check`.
- `GROUP: dict[Reason, str]` mapping to `decision | reapprove | review | tech-failure` exactly as the contract table (no-machine-check → decision).
- `format_comment(reason, summary: str, details: str | None = None) -> str`: first line `<!-- foragent:needs-human reason=<code> -->`, then a bold one-line summary, then optional details. English text.
- `parse_reason(comment_body: str) -> Reason | None` reads the first-line marker only.
- Also `APPROVAL_MARKER` helpers for the approval snapshot comment: `format_approval_comment(body_sha, acceptance_blobs: dict[str, str], approved_by, approved_at) -> str` with first line `<!-- foragent:approved sha256=<sha> by=<login> at=<iso> -->` followed by a short human sentence and a list of acceptance file blob hashes; `parse_approval_comment(body) -> dict | None`.

### `src/foragent/state.py`
`StateStore(root: Path)`; default root factory `StateStore.for_repo("owner/name")` → `~/.local/share/foragent/owner__name/`. All writes atomic (write temp file in the same dir, then `os.replace`). JSON files, UTF-8.
- Approvals: `save_approval(number, *, body, body_sha, acceptance_blobs, approved_by, approved_at)` writes `approvals/<n>.json` with metadata and the exact `body` in one atomic snapshot. `load_approval(n) -> Approval | None` (dataclass incl. body). `clear_approval(n)`.
- Runs: `start_run(number, *, branch, worktree, pid=None) -> Run`, `update_run(number, **fields)`, `get_run(number)`, `active_runs() -> list[Run]` (status == "running"), `finish_run(number, outcome: str, detail: str = "")` which appends one JSON line to `runs.jsonl` history before marking the run finished. `read_history() -> list[Run]` deduplicates by `(number, started_at)`, retaining the latest entry in each execution's original position. Updates to finished runs return the existing record. `Run` fields: number, branch, worktree, pid, started_at, last_progress_at, attempts, restarts, status, outcome, detail.
- Run mutations: `start_run`, `update_run`, and `finish_run` hold a blocking `fcntl.flock(LOCK_EX)` on `root/state.lock` throughout the operation.
- `worktree_path(number) -> Path` = `root/worktrees/issue-<n>`; `log_path(number) -> Path` = `root/logs/issue-<n>.log`.
- Tick lock: `tick_lock()` context manager using `fcntl.flock(LOCK_EX | LOCK_NB)` on `root/tick.lock`; yields True if acquired, False if another tick holds it (never blocks).
- Times are timezone-aware UTC ISO strings; accept an injectable `now` callable for tests.

### `src/foragent/tracker.py`
Data: `Issue(number, title, body, labels: frozenset[str], state: "open"|"closed", is_pull_request: bool=False)`, `LabelEvent(label, action: "labeled"|"unlabeled", actor, created_at)`, `Comment(id, author, body, created_at)`, `Dependency(number, state)`.

`class Tracker(Protocol)` with:
`list_open_issues() -> list[Issue]` (issues only, no PRs), `get_issue(n)`, `label_events(n) -> list[LabelEvent]` (chronological), `blocked_by(n) -> list[Dependency]`, `add_labels(n, labels)`, `remove_labels(n, labels)` (missing label is not an error), `comment(n, body) -> int` (comment id), `list_comments(n)`, `ensure_labels(specs: list[LabelSpec])` (create or update name/color/description), `close_issue(n, comment: str | None = None)`, `create_pr(*, head, base, title, body) -> int`, `pr_changed_files(pr) -> list[str]`, `pr_checks(pr) -> "pass"|"fail"|"pending"|"none"`, `merge_pr(pr, *, method="squash")`.

`FakeTracker`: in-memory implementation for tests of later phases. Helpers: `add_issue(...)`, `set_blocked_by(n, deps)`, `record_label(n, label, actor)` (appends a LabelEvent and applies it), `set_pr_files`, `set_pr_checks`, plus inspection of created PRs, merged PRs, comments. Label changes through `add_labels/remove_labels` record events with actor `"foragent-bot"` by default (configurable `actor` attribute) so tests can tell human vs gate actions.

`GhTracker(repo: str, run=None)`: implements the protocol by invoking the `gh` CLI via `run(args: list[str], input: str | None = None) -> str` (default: `subprocess.run(["gh", *args], check=True, capture_output=True, text=True)`). Use:
- issues: `api --paginate repos/{repo}/issues?state=open&per_page=100` (skip items having `pull_request`)
- events: `api --paginate repos/{repo}/issues/{n}/events?per_page=100` (keep `labeled`/`unlabeled`: `actor.login`, `label.name`, `created_at`)
- blocked by: `api --paginate repos/{repo}/issues/{n}/dependencies/blocked_by?per_page=100`
- labels: `api -X POST repos/{repo}/issues/{n}/labels -f labels[]=X`, `api -X DELETE repos/{repo}/issues/{n}/labels/{urlquoted}` (ignore 404)
- comments: `api -X POST repos/{repo}/issues/{n}/comments -f body=@-` with body on stdin is not supported by gh; instead pass `--input -` with JSON `{"body": ...}` on stdin. List with `api --paginate repos/{repo}/issues/{n}/comments?per_page=100`.
- ensure label: `api -X POST repos/{repo}/labels --input -`; on HTTP 422 (already exists) `api -X PATCH repos/{repo}/labels/{name} --input -`.
- close: `issue close {n} -R {repo}` (with `--comment` when given)
- PR: `pr create -R {repo} --head H --base B --title T --body-file -` (body on stdin), parse the number from the printed URL; files `api --paginate repos/{repo}/pulls/{pr}/files?per_page=100` → `filename`; checks `pr checks {pr} -R {repo} --json bucket` → all pass/skipping → pass, any fail/cancel → fail, any pending → pending, empty list or "no checks reported" error → none; merge `pr merge {pr} -R {repo} --squash --delete-branch`.
Raise `TrackerError` with the gh stderr on failures that are not the documented ignorable ones.

## Tests (`tests/`)
Thorough unit tests per module, offline only:
- issue: the four sandbox issue bodies in `docs/impl/fixtures/` (create these fixture files from the examples below), CRLF bodies, missing sections, multiple code blocks, checklist inside vs outside code blocks, acceptance_paths with `./`, `::node`, directory form, quoted paths, body_hash stability.
- config: merge order, every validation error, run_hours edge cases incl. wrap and boundaries, resolved_reviewer.
- reasons: round trips, GROUP covers every code.
- state: atomic write leaves no temp files, approvals and runs round trip, runs.jsonl append, tick_lock returns False while held (use a second process or second fd).
- tracker: FakeTracker behaviour; GhTracker builds the exact gh argument lists and parses sample JSON (use a fake `run`), checks-bucket mapping, 404/422 handling.

Example issue body (fixture `issue_a.md`):
```
<!-- foragent:v1 -->
## Goal
Add a `tally words FILE` command that prints the number of whitespace-separated words in the file.

## Out of scope
- Any other command or option.

## Done when
```sh
uv run pytest -q tests/acceptance/test_a_words.py
```
Acceptance test: [`tests/acceptance/test_a_words.py`](../blob/main/tests/acceptance/test_a_words.py)
```
Create fixtures for a similar body with two code blocks and a checklist, one without the marker, and one without any code block.

## Done criteria
`uv run pytest -q` passes. No other files outside `src/foragent/`, `tests/`, `docs/impl/fixtures/` changed. Report what you built and anything in this spec you found ambiguous.
