---
name: project-533-review-panel
description: #533 parallel review panel — run_review._panel_review, orch_reviewers state override, perspective shares, limit handling, test fixture imports
metadata:
  type: project
---

- `run_review._review()` branches to `_panel_review()` only when risk notes exist and `review_perspectives.shares(config, orch_reviewers.effective(...))` has >1 share (cap = number of perspectives). Signature of `_review` is fixed by the acceptance test.
- Panel: ThreadPoolExecutor runs `_available_executor(reviewer=True)` per share on the prompt path (never the Claude scope file), then one merge call via `build_review_prompt(..., panel_findings=...)`. Files: `review-<round>-<k>.txt` per reviewer (ignored by `_next_review_round` because not digit), `review-<round>.txt` for merge/unusable.
- Concurrent executor calls must not share `log_path`: ClaudeExecutor writes `log_path.with_suffix(".stdout")` and truncates it per call. Panel reviewers use `issue-N-review-<round>-<k>.log`; `_run_executor` tracks progress from whichever log it is given, so stall detection still works.
- Parse every reviewer with `parse_review` before merging; a reviewer with no VERDICT makes the round UNUSABLE (review caught this).
- A thread's `ExecutorStopped` (limit-wait) is re-raised after all threads join; nothing is saved so the round reruns.
- Override: `<state>/review-reviewers.json`; CLI `fa orch reviewers <issue> <N>|--clear` is in `INBOX_ACTIONS` (no GitHub). Text in `messages/orch_reviewers.py`.
- Unit tests in `tests/` that reuse acceptance helpers must `sys.path.insert` tests/acceptance and import the autouse fixtures by name (`offline` needs `isolated_home` from test_skills_autosync).

**Why:** design decisions from the issue text; kept for follow-ups (facts per perspective after #333).
**How to apply:** extend the panel here, not in the single-review path.
