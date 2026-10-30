---
name: project-1016-review-what-how
description: #1016 D98 reviewer failure grounds and DECIDE-line context; frozen review text in test_risk_map and runner_support
metadata:
  type: project
---

- `tests/acceptance/test_risk_map.py` freezes `REVIEW_PROMPT` and the first 3 lines of `review_scope_rules()` verbatim (substring check). New review rules must be added after "Improvements beyond..." without editing those lines.
- `tests/runner_support.py::expected_review_rules` holds the full exact rules text; update it whenever `review_scope_rules()` changes (~22 test_run_review cases depend on it).
- Reviewer context for the lane's DECIDE lines: `run_review._lane_decisions()` reads `order.lane_proposal()` (from #1014) with the approval snapshot and the review worktree; `lane_decisions_section()` in run_prompts. It flows to codex prompt, claude-local `CLAUDE.local.md` scope, panel members and merge call.
- Reviewer-input paths to cover whenever reviewer context is added: codex prompt, claude-local `CLAUDE.local.md`, panel members + merge call, and claude `review_scope: pr-body` (prompt is empty; native review reads only the PR body). The pr-body path has two writers: `run_publish_pr._render_pr_body` (initial) and `review_only`'s refresh in run_review (re-review). #1016 attempt 1 missed pr-body; fix uses a marked block (`refresh_lane_decisions`, like `risks.refresh_notes`).
