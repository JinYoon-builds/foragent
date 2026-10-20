# P7b spec: try cards, prepared results, language setting

Why: in the first real `/fa-inspect` the user's reaction was "so where am I supposed to look?". The inbox listed items but never said what changed, how to try it, or what a correct result looks like. Read `docs/decisions.md` D16 and D17, `docs/inspect.md`, and `src/foragent/{runner,inspect_data,inspect_actions,cli,config}.py`. Offline tests only.

## 1. `language` setting (D17)
- New config key `language` (default `en`; any short BCP-47-ish string like `ko`, `en`, `ja`). Global default via `~/.config/foragent/config.yml`, repo override in `.foragent.yml`. `fa init --language` writes it when given.
- Machine-read formats stay English always: contract headings, labels, reason codes, markers, TRY.md headings below.
- Human prose follows `language`: the runner prompt tells the executor to write QUESTION.md and TRY.md prose in that language; gate needs-human summaries stay English (short, fixed strings) but the skill renders everything to the user in `language`.

## 2. TRY.md from the implementer (runner)
Add to the implementer prompt (every attempt), after the existing rules:
```
- When the Done when commands would pass, also write .foragent/TRY.md so a person can check your work in about a minute.
  Write the prose in <language>. Keep these English headings exactly:
  ## What changed
  (one or two sentences, user-visible terms, no code jargon)
  ## Try it
  kind: cli            (or: web)
  ```sh
  <commands a person runs; for cli, commands that print something meaningful>
  ```
  ## Expect
  (what a correct result looks like, concretely)
  For kind: web, also add:
  ## Web
  start: <command that starts the app>
  port: <port>
  path: <path to open>
  steps:
  - <what to click or type>
```
Also extend the QUESTION.md rule: "Write it in <language>. Include an `## Evidence` section quoting the exact conflicting lines (issue sentence; file:line and the code line)."

Runner after a successful run (checks + review pass): read `.foragent/TRY.md` if present (never committed — `.foragent/` is already excluded), store it at `store.root/try/<n>.md`, and put it verbatim in the PR body under `## Try it` (if missing: `## Try it` + `_not provided by the implementer_`). Missing TRY.md never fails a run.

## 3. Parsing (`src/foragent/trycard.py`)
`parse_try(text) -> TryCard(what_changed, kind, commands: list[str], expect, web: WebSpec | None)` where `WebSpec(start, port: int, path, steps: list[str])`. Tolerant: missing parts become empty; unknown kind → `cli`. Unit tests with Korean prose.

## 4. inspect: prepared results
- `Item` gains `try_card: TryCard | None` (from the store copy if present, else parsed from the merged PR body `## Try it` section) and `prepared: Prepared | None`.
- `fa inspect prepare [--repo-root]`: for each `try` item with `kind: cli`: create a temporary worktree of `origin/<base>` under `store.root/inspect/wt`, run each command with `/bin/sh -c`, timeout 60 s each, env scrubbed like the runner, capture combined output (last 3000 chars) and exit code, save to `store.root/inspect/prepared/<n>.json` with the base SHA; remove the worktree. Re-running is idempotent (skip when the stored base SHA equals the current one). Never touches GitHub.
- `fa inspect serve N [--repo-root]` (web kind): worktree of `origin/<base>` under `store.root/inspect/serve-<n>`, run `web.start` detached in its own process group with `PORT=<port>` in env, wait up to 90 s for the TCP port, record pid/port in `store.root/inspect/served.json`, print the URLs: `http://localhost:<port><path>` and, when `tailscale status --json` works, `http://<Self.DNSName without trailing dot><:port><path>`. `fa inspect stop [--repo-root]` kills all served process groups and removes their worktrees. `mark-seen` also calls stop.
- `fa inspect --json` includes `try_card` and `prepared` per item, and a top-level `summary` object: counts per group and `estimated_minutes` = decision×1 + reapprove×1 + review×3 + human-check×1 + try×1 + tech-failure×1.
- Text output: header line `Decision 2 (~2 min) · Try 2 (prepared)`; try items render What changed / Try it / Expect / prepared output (or "not prepared").

## 5. `skills/fa-inspect/SKILL.md` update (keep under 90 lines)
- Read `language` from `fa inspect --json` (add `language` to the JSON top level) and talk to the user in that language.
- Start: run `fa inspect prepare`, then `fa inspect --json`. Show one summary line with the estimated minutes.
- decision: show the question, then the `## Evidence` quotes side by side, then options (recommended first) with AskUserQuestion + "Later".
- try (cli): show What changed, the exact command, Expect, and the prepared output; ask "Does this look right?" (Yes / Not what I wanted → feedback text / Later).
- try (web): run `fa inspect serve N`, show the URLs (the tailnet one works from phone and laptop), list the steps and Expect; if a headless browser skill or tool is available, open the page, follow the steps and show screenshots; then ask the same question. Run `fa inspect stop` when done.
- End: `fa inspect mark-seen` **always**, even if the user stops early (tell the user it is done). One-line recap.

## Tests
Prompt contains the TRY.md and Evidence rules with the configured language; TRY.md stored and placed in PR body; missing TRY.md tolerated; parse_try variants; prepare runs commands in a temp worktree, idempotent by base SHA, timeouts, env scrubbed, never writes GitHub; serve/stop with an injected port waiter and process launcher; JSON shape incl. summary/language; text rendering; mark-seen stops served apps.

## Done criteria
`uv run pytest -q` passes. Report what you built and any ambiguity.
