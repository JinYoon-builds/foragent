# P7c spec: use the native harness (goal, deny rule, Stop hook, /code-review)

Why: `docs/decisions.md` D18 and D20. Foragent must not re-implement what Claude Code already does; it hands each approved issue (plan = body, goal = Done when) to the native harness and keeps only independent verification, cross-model review, PR/merge and the human inbox. Read `src/foragent/{runner,executors,processes,inspect_runtime*,init}.py` first. Offline tests only (exact argv/settings content; fake executors).

## 1. Claude implementer uses native `/goal`, a deny rule and a Stop hook
When `executor == "claude"`:
- Prompt passed on stdin starts with `/goal <condition>` where condition (≤ 3500 chars) is:
  `Every command in the "Done when" list below exits 0 when run from the repository root, nothing under <acceptance_dir> is modified, and you did not edit files outside this repository. Files that test tooling writes outside the repository do not count as your edits. At 40 turns, end the run without repeating the stop check.`
  followed by a blank line and the existing implementer prompt (plan = snapshot Goal/Out of scope/Done when, rules, TRY.md rule). Remove from the prompt the sentence "Do not modify anything under <acceptance_dir>" (it is now enforced by structure, see below).
- Run `claude -p --permission-mode auto --output-format stream-json --verbose --settings <file>` (+ `--model`), stdin = prompt, cwd = worktree. stdout (stream-json lines) appended to the issue log; `last_message` = the final `result` event's `result` field; `full_message` same. Any stream line counts as progress (update `last_progress_at` through the existing progress hook).
- `--settings <file>` points to a per-run JSON written by the runner into `store.root/runs/issue-<n>/claude-settings.json` (outside the worktree) with:
  - `permissions.deny`: `["Edit(<acceptance_dir>**)", "Write(<acceptance_dir>**)", "Bash(git commit:*)", "Bash(git push:*)", "Bash(git checkout:*)", "Bash(git switch:*)"]` — paths relative to the worktree root exactly as Claude Code permission rules expect (`Edit(tests/acceptance/**)`).
  - `hooks.Stop`: one command hook running `<python> -m foragent.cli gate check-done --snapshot <store approval path> --worktree <path> --acceptance-dir <dir>`; the command exits 0 when every Done when command passes (and prints a one-line summary), otherwise exits 2 printing the failing command and the last 2000 chars of its output to stderr (exit code 2 is the Claude Code convention that blocks the stop and feeds stderr back). Put a hard cap: if the hook has blocked 8 times in this run (counter file next to the settings), exit 0 so the session can end and the runner's own verification decides.
- New CLI `fa gate check-done` implementing the above using the existing check runner (env scrub, 15-minute per-command timeout).
- After the session ends the runner still runs Done when itself (unchanged) — D18: independent verification stays.
- The runner's own retry loop remains as the fallback, but for the claude executor one native session is one attempt; if the runner's verification still fails, the existing retry path continues (same `max_retries`).

When `executor == "codex"`: unchanged behaviour (runner-level enforcement and retry loop). If `config.codex_goal` (new bool key, default false) is true, prefix the prompt with `/goal <same condition>` — documented as experimental; nothing else changes.

## 2. Review uses the bundled `/code-review` skill when the reviewer is claude
- Reviewer claude: `claude -p "/code-review <level>" --permission-mode auto --output-format json`, stdin `/dev/null`, cwd = worktree with the implementation **staged** (it reviews staged + unstaged changes). `<level>` from config key `review_level` (low|medium|high|xhigh|max, default `low`).
- Parse `result` from the JSON: lines matching `^- \S+:\d+ — ` (em dash, also accept ` - ` and ` -- `) are findings → FAIL with those lines; a result whose first non-empty line is `(none)` → PASS; anything else, non-zero exit, `is_error: true`, or unparsable JSON → no usable verdict (existing fail-closed path: rerun once, then hold `risk-high`).
- Before each Claude review, prepend a review scope section to `CLAUDE.local.md` containing the full approved snapshot body and rules limiting defects to unmet Goals, changes listed in Out of scope, or weakened checks. Ignore intentionally failing locked acceptance tests belonging to other issues and improvements beyond this issue's Goal. Restore the original file bytes after review, including failed or interrupted reviews, or remove the temporary file if none existed. Add `/CLAUDE.local.md` to the repository's `info/exclude` and explicitly exclude it from staging and commits. Note the reviewer in the PR "Review" section as `reviewer: /code-review <level>`.
- Reviewer codex uses the same three scope rules in its custom prompt.
- Keep the existing guard that reverts any reviewer edits/commits.

## 3. Small fixes from P7b QA
- `fa inspect prepare` and `serve` fetch `origin/<base>` (explicit refspec, same as the scheduler) before creating their worktrees.
- `fa init --language X` writes `language` even when `.foragent.yml` already exists **only if the key is absent** (never overwrite an existing value); the same rule for the other explicit options when the file exists: add missing keys, never change existing ones, and print which keys were added.

## Tests
Exact argv and settings JSON for the claude implementer (deny list, Stop hook command, stream-json flags, goal prefix and condition length cap), check-done exit codes 0/2 and the block cap, stream-json progress parsing and final result extraction, codex unchanged + codex_goal prefix, /code-review argv and verdict parsing (findings, `(none)`, empty diff text, garbage, is_error, non-zero exit), prepare/serve fetch, init add-missing-keys.

## Done criteria
`uv run pytest -q` passes. Report what you built and any ambiguity.
