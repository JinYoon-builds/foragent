---
name: project-226-review-notes
description: #226 reviewer NOTE lines (review_notes.py), break-it rule in prompts, SKILL.md 150-line budget pitfalls
metadata:
  type: project
---

- `foragent/review_notes.py` parses `NOTE:` lines; `run_review._finish_review(..., texts)` extracts notes from every raw text (single: raw; panel: member raws + merge raw) and journals `review-note` once per issue (dedup reads orch-events.jsonl under `_state_lock`, normalised whitespace+case). Works for UNUSABLE too.
- NOTE lines are stripped from findings, reviewer_text (UNUSABLE handoff) and `run_previous.saved_review_section`; PR body/PR comments/run log/raw review files keep them. FAIL whose findings are all NOTE → PASS.
- Reviewer rules (break-it, NOTE) live in `run_prompts.review_scope_rules` so codex prompt, claude-local scope and PR-body scope all get them; `tests/runner_support.expected_review_rules` mirrors that text and must be updated with it.
- SKILL.md limit is `len(text.splitlines()) <= 150` — a file without trailing newline counts one more than `wc -l`. `test_orch_check_loop` and `test_orch_repeat_hold` require the 2026-10-24 example and the four architect signals to stay IN SKILL.md, so they cannot be moved to references/; join lines instead (no width limit).
- Panel members get messages in concurrent order: compare recorded notes as normalised sets in tests.
- Review round 2 lesson: persist side effects (NOTE events) the moment the text exists, not at round finalization — panel limit/interrupt re-raises and merge-call errors exit before `_finish_review`. Also filter every implementer feedback channel (hint, reviewer_text, previous_attempt; fresh and resume builders), not only findings; keep `strip()` byte-identical when there is no NOTE so exact-prompt tests hold.
- Round 3: `_available_executor` discards results on limit/transient paths before returning; capture reviewer text with its `on_result` hook (called inside `execute()` on every executor result), not after the call returns.
