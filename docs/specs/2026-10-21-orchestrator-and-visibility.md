# Order 2026-10-21: orchestrator first response, visibility, skills, design

Source decisions: `docs/decisions.md` D31, D33, D36, D37, D39, D40, D41, D43–D48. Each section is one issue: plan (Goal, Out of scope) and the interfaces the locked acceptance test checks. Acceptance tests live in `tests/acceptance/test_<key>.py` and must use only the public interfaces named here (CLI via `foragent.cli.main([...])` and the named functions), with `FakeTracker` and temporary git repos — no network.

Shared conventions for all issues:
- New labels: `blocked` (added to `labels.LABELS`, gate/runner-owned).
- Orchestrator events file: `<state root>/orch-events.jsonl`, one JSON object per line: `{"at": ISO8601, "issue": int|null, "kind": str, "summary": str}`.
- Lane config gains optional `title` (str) and `emoji` (str).

---

## Lane orch (🧭 오케스트레이터; paths: src/foragent/orch*.py, src/foragent/scheduler.py, src/foragent/runner.py, src/foragent/reasons.py, skills/fa-orchestrator/**)

### orch-blocked — D45 gate and runner raise `blocked`, only the orchestrator raises `needs-human` (size m)
Goal: Every place where gate or runner currently adds `needs-human` (the shared `flag` helper) adds `blocked` instead, with the same reason-code comment but first line `<!-- foragent:blocked reason=<code> -->`. `needs-human` is added only through `foragent.orch.escalate(...)` (see orch-verbs) and by the timeout rule: in `tick`, an issue labelled `blocked` whose latest blocked comment is older than config `orchestrator_timeout_minutes` (new key, default 30) gets `needs-human` plus a comment starting `<!-- foragent:needs-human reason=orchestrator-timeout -->`. The scheduler treats `blocked` like `needs-human` (never promotes or starts). `fa inspect` lists only `needs-human` items (blocked items are not shown to the human).
Out of scope: the orchestrator verbs themselves (orch-verbs).
Test checks: after a spec-question run the issue has `blocked` not `needs-human`; timeout rule adds `needs-human` after the configured minutes (injected clock); scheduler skips `blocked`; `inspect_data.collect` ignores `blocked`-only issues.

### orch-review-handoff — D44 unusable reviews go to the implementer, rebuttals are recorded (size m, blocked by orch-blocked)
Goal: When the reviewer's result is not a clear PASS/FAIL (verdict UNUSABLE), the runner does not hold the issue. It treats the raw review text as findings and starts the next implementer attempt with `A reviewer said (verbatim):` + the raw text, consuming one attempt like FAIL. The implementer prompt adds: "For each reviewer point either fix it or write `.foragent/REBUTTAL.md` with `- <point>: <why it is out of scope or not a defect>`". If REBUTTAL.md exists after a session, its content is (a) appended to the PR body under `## Rebuttals` and (b) included in the next review's scope file (CLAUDE.local.md) under `Implementer rebuttals (judge them):`. REBUTTAL.md is never committed. If the same finding text (normalized) is reported in two consecutive rounds after a rebuttal, the runner raises `blocked` with reason `review-dispute`.
Out of scope: changing the reviewer command.
Test checks (fake executors): UNUSABLE → next attempt prompt contains the raw text and no hold; rebuttal reaches PR body and next scope file and is not committed; repeated finding after rebuttal → `blocked` reason `review-dispute`.

### orch-verbs — D43 the orchestrator's verbs and event log (size l, blocked by orch-blocked)
Goal: Module `foragent/orch.py` and CLI `fa orch`:
- `fa orch list [--json]` — blocked items: number, title, lane, reason code, blocked_at, the blocked comment details, raw review text if any (from `<state>/runs/issue-<n>/review-*.txt`), last 60 log lines path.
- `fa orch rerun-review N` — keeps the existing PR, runs only the review step again on the PR head (runner entry `review_only(n)`), then continues the normal merge decision; removes `blocked`.
- `fa orch retry N [--hint TEXT]` — closes the issue's open Foragent PR if any, removes `blocked`, stores the hint at `<state>/hints/<n>.txt`; the runner prepends `Orchestrator hint:` + hint to the next implementer prompt and deletes the file.
- `fa orch resolve N --verdict pass|fail --note TEXT` — records an orchestrator review verdict for the current PR head (comment `<!-- foragent:orch-verdict ... -->`), removes `blocked`; `pass` lets the runner's merge decision proceed (Done when and CI are still required), `fail` behaves like `retry` with the note as hint.
- `fa orch escalate N --summary TEXT --tried TEXT [--option TEXT ...] [--recommend INDEX]` — the only way to add `needs-human`; comment first line `<!-- foragent:needs-human reason=escalated -->` then summary, what was tried, options with the recommended one marked.
- `fa orch log` — prints the last 20 events; every verb above appends an event to `orch-events.jsonl` and posts a short issue comment `<!-- foragent:orch -->` with the same summary.
Forbidden by construction (tests check): no verb adds `approved`; `resolve --verdict pass` never merges when Done when or CI failed.
Out of scope: the orchestrator skill (orch-skill), merges (orch-merge).
Test checks: each verb's label/comment/event effects with FakeTracker; hint reaches next prompt once; escalate is the only `needs-human` path.

### orch-merge — D46 pre-merge check and human-approved merge (size m, blocked by orch-verbs)
Goal: `fa orch premerge PR [--json]` checks a batch or careful PR without writing to GitHub: in a temporary worktree of the PR head it runs the full test command (config `test_command`, default detected like `fa order context`), test-merges the PR base into it and reports conflicts, and reads CI status. Output: `{"pr":, "tests": "pass"|"fail", "conflicts": [paths], "ci": "pass"|"fail"|"pending"|"none", "ready": bool}`. `fa orch merge PR --approved-by LOGIN [--at ISO]` refuses unless premerge is ready, posts `Approved by <login> in the orchestrator (<at>). Pre-merge: tests pass, no conflicts, CI <ci>.` and merges with `--match-head-commit`. Appends an event.
Out of scope: resolving conflicts automatically.
Test checks: premerge JSON for pass / failing tests / conflict / CI pending; merge refuses when not ready; approval comment text; event appended.

### orch-skill — D36 D43 D48 the orchestrator session skill and change watcher (size m, blocked by orch-verbs)
Goal: (1) `fa orch watch [--interval SEC]` prints one JSON line whenever the set of blocked/needs-human items, merged issues, or the queue-empty state changes (state kept in `<state>/orch-watch.json`), and exits after printing (one change per invocation, suitable for a Monitor until-loop) or `--follow` keeps printing. (2) `skills/fa-orchestrator/SKILL.md` (English, ≤150 lines) instructs a long-running Claude session to: run under `/goal` with the condition "all approved issues are merged or only items waiting for a human decision remain; no blocked items; print `fa gate status --json` each turn as evidence"; wake on `fa orch watch` output; handle blocked items first with `fa orch list` + the verbs (diagnose from raw logs and reviewer text; same technical problem twice → `fa orch escalate`); for human decisions use AskUserQuestion with the recommendation first and record answers; send a push via the configured notifier (`fa notify`) for needs-human and queue-empty; answer "what is happening" from `fa gate status`; classify every user message (status question / answer to a pending decision / new work or change → accumulate an order draft and, when clear, propose one card with new issues by lane, new lanes, [add] [edit] [later] and publish via `fa order publish --approve` / change to a running issue → propose an edit, which needs re-approval / thought → record only); never add `approved` except through `fa order publish` after the user chose it; never merge except `fa orch merge` after the user chose merge in AskUserQuestion.
Out of scope: the push implementation (view-notify).
Test checks: watch emits on each kind of change and not otherwise; the skill file exists, has frontmatter `name: fa-orchestrator`, and contains the goal condition sentence, the five message kinds, and the words "fa orch escalate", "fa orch merge", "AskUserQuestion".

---

## Lane view (🖥️ 화면; paths: src/foragent/watch*.py, src/foragent/status*.py, src/foragent/cli.py, src/foragent/notify*.py, src/foragent/i18n*.py)

### view-names — D47 lane slug and human name (size s)
Goal: Lane config accepts `title` and `emoji`. `fa lanes add NAME --title TEXT --emoji E` stores them. Helper `foragent.labels.lane_display(config, slug, language) -> str` returns `"<emoji> <title>"`, falling back to `"🧰 공통"` (ko) / `"🧰 Common"` (en) for `main`, and to the slug for unknown lanes. `fa gate status` and `fa inspect` text output use the display name; JSON keeps the slug plus `lane_display`.
Test checks: display name rules per language, fallbacks, CLI storing title/emoji, status text uses display names.

### view-status — D40 phases and three states (size m)
Goal: The runner writes `phase` (`implementing|checking|reviewing|ci|merging|resolving-conflict`) and `phase_started_at` to the run record at each step, and `last_activity_at` whenever the executor log grows. `fa gate status --json` adds `lanes`: per lane `{slug, state: working|blocked|waiting-human|queued|idle, issue, title, phase, elapsed_seconds, last_activity_seconds, waiting_for}` (`waiting_for` = blocker issue numbers when queued) and a top-level `summary: {working, blocked, waiting_human, queued}`. Text output starts with one line `working N · waiting for you N · queued N` (localized by `language`).
Test checks: phases recorded in order by a fake run; JSON lanes/summary for working, blocked, needs-human, queued-behind-blocker and idle lanes; text first line localized.

### view-watch — D41 the lane conveyor TUI (size l, blocked by view-names, view-status, orch-verbs)
Goal: `fa gate watch [--repo-root] [--once]` renders the conveyor described in D41 and the mock `docs/impl/conveyor_mock.py`, from `fa gate status --json` data and `orch-events.jsonl`: orchestrator box (state word, order progress bar done/total for the open parent issue, waiting-for-you count, ETA), one road per lane (display name, white queue blocks, active block that flashes on the pickaxe hit frame, executor badge ✳/✺, "<lane title> 담당" box, "✓ N done · just now: <title>"), "now doing" table (lane, issue title, phase, ● or "○ looks stalled N min" after 10 min without activity), "handled by the orchestrator" panel (last 5 events). `--once` prints a single frame as plain text (for tests). Number keys 1..N toggle an agent detail panel (lane, paths, executor, model, skills, done count, first 3 lines of the lane agent memory file). Strings come from `foragent/i18n.py` keyed by `language` (ko and en).
Test checks: `--once` output for a fixture state contains display names, queue numbers, "✓ 2", the stalled marker for an old activity, the last event summary, and Korean or English words per language; detail panel text from a render helper.

### view-notify — D36 pluggable notifications (size s)
Goal: `foragent/notify.py` with `send(config, title, line, link)` and CLI `fa notify --title T --line L [--link U]`. Config `notify` values: `claude-push` (append to `<state>/push-outbox.jsonl` for the orchestrator session to deliver; default), `slack:<webhook url>` (POST JSON `{"text": ...}`), `ntfy:<topic>` (POST to `https://ntfy.sh/<topic>`), `command:<path>` (run with env FA_TITLE, FA_LINE, FA_LINK), `macos` (osascript), `none`. The old inspect notifier uses this module; macOS is no longer the default. Failures never raise to the caller (logged).
Test checks: each target with injected transport/runner; default writes the outbox; command env vars; failures swallowed.

### view-version — operations: version shows the build commit (size s)
Goal: `fa --version` prints `foragent <version> (<git short sha>)` where the sha is captured at build/install time into `foragent/_build.py` (fallback `unknown`), so an installed copy can be compared with the source in one line.
Test checks: version string format with a monkeypatched build module and with the fallback.

---

## Lane board (📌 보드; paths: src/foragent/board*.py, src/foragent/init.py, src/foragent/order.py, src/foragent/tracker.py)

### board-projects — D31 GitHub Projects board as the view (size l)
Goal: `foragent/board.py`. `fa init` (unless `--no-board`) creates or finds a Projects v2 board titled `Foragent: <repo>` owned by the repo owner, with single-select fields `Lane`, `Size` (s/m/l), `Status` (Approved, Ready, Running, Blocked, Needs human, PR open, Done), and stores its id in `.foragent.yml` `board`. `fa order publish` adds created issues to the board and sets Lane/Size. The scheduler and runner set Status on transitions through `board.set_status(issue, status)`; any board failure is logged and never fails a tick or run. Tracker gains the GraphQL calls needed (GhTracker via `gh api graphql`), with FakeTracker support.
Test checks (FakeTracker): init creates the board and fields once (idempotent), publish adds items with Lane/Size, a transition sets Status, a failing board call does not fail the tick.

---

## Lane skills (🧩 스킬; paths: src/foragent/skills.py, src/foragent/lanes.py, src/foragent/git.py)

### skills-local — D37 library skills stay local and are injected per run (size m)
Goal: `fa skills install` / `fa lanes add --skill` copy library skills into `.claude/skills/<name>/` and add that path to `.gitignore` (once) instead of committing it; the lane config keeps the skill list. The runner copies each lane skill from the local install (repo root `.claude/skills/<name>`, else the library cache) into the worktree before the executor starts, never staged. A non-open license prints a warning instead of refusing, unless `--commit` is given (then it refuses as today). User-made skills (not from a library) are untouched.
Test checks: install writes .gitignore and does not commit; runner worktree contains the skill and the commit does not; warning vs refusal with `--commit`.

### skills-review-scope — D39 optional review scope via the PR body (size s)
Goal: Config `review_scope: claude-local|pr-body` (default `claude-local`). With `pr-body`, the runner opens the PR before the review, writes a `## Review scope` section into the PR body (the three blocking rules and the note about other issues' locked acceptance tests), runs `/code-review <pr> <level>`, and keeps earlier review findings in PR comments instead of the PR body. With `claude-local`, behaviour is unchanged.
Test checks: argv for pr-body mode, PR body contains the scope section and no previous findings, default unchanged.

---

## Lane design (🎨 디자인; paths: src/foragent/design*.py, skills/fa-order/**, skills/fa-init/**)

### design-first — D33 design foundation support (size m)
Goal: (1) `fa design lint [--tokens PATH ...] [--glob GLOB ...]` reports raw color literals (`#rgb`, `#rrggbb`, `rgb(`, `hsl(`) in matched UI files outside the token files (defaults: tokens `**/tokens.css`, `**/tokens.ts`; globs `**/*.css`, `**/*.tsx`, `**/*.jsx`, `**/*.vue`, `**/*.svelte`, `**/*.html`), exit 1 when any are found, JSON with `--json` — usable as a Done when command. (2) `fa order context` adds `ui: {frameworks: [...], has_design_foundation: bool}` (frameworks detected from package.json dependencies react/vue/svelte/next/nuxt/solid-js and from html files; foundation = `docs/design/CONTRACT.md` exists). (3) `skills/fa-order/SKILL.md` gains a short "Design first" section implementing D33 (foundation issue first when UI work and no foundation; only visual work waits; taste from 2–3 reference apps; "this screen or the standard?" for visual feedback; exploratory throwaway prototypes are exempt). (4) `skills/fa-init/SKILL.md` proposes a design lane with skills frontend-design, design-system, design-critique, accessibility-review, ux-copy, webapp-testing when UI frameworks are detected.
Test checks: lint finds/ignores literals correctly and exit codes; context ui detection; both skill files contain the required sections and skill names.

---

## Interface notes fixed by the locked tests
- view-watch: `foragent.watch.render_detail(config, slug, repo_root, done_count=...) -> str`; the frame test imports the `status_case` helper from `tests/acceptance/test_view_status.py`; `fa gate watch --once` prints plain text without ANSI escapes.
- board-projects: `FakeTracker.graphql(query, variables)` and the matching `GhTracker.graphql` are the board's transport.
- orch-merge: config key `test_command` (default: detected like `fa order context`).
- orch-skill: `fa orch watch` emits an initial snapshot on the first call.
- orch-verbs: `--recommend` indices start at 1.
