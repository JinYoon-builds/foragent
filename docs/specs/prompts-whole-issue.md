# Preserve authored requirements through implementation and review

An issue's approved requirements must reach the people doing and reviewing the work. Today the first implementation prompt and the textual review prompt keep only Goal, Out of scope and executable Done when commands. Done when checklist lines reach only the PR body, and arbitrary authored sections such as Contracts disappear. Resumed sessions intentionally receive only issue-body changes, so their unchanged requirements must already have arrived in their first prompt.

The implementer's first attempt, every fresh retry and the review prompt must carry all authored content of the current approved issue. Preserve checklist lines, arbitrary authored headings and their contents, and the existing Goal, Out of scope and Done when commands. Use the approved snapshot, as today; live unapproved issue edits do not replace its requirements.

A resumed attempt receives only changed authored issue content and its existing feedback. Changed checklist lines and changed lines in Contracts or any other authored section count as specification changes and must appear. Do not repeat unchanged Goal, Out of scope, commands, checklist items, Contracts lines or other authored content. A resume with no issue edits keeps its current feedback-only behavior. Preserve tests/acceptance/test_resume_after_question.py unchanged, including its no-repeated-unchanged-body assertions at lines 187-188.

Gate-generated Waiting sections and machine markers are excluded from both complete prompts and resume changes, while authored sections following Waiting remain present when they belong in that prompt. Prompt size budgets must not silently remove relevant authored requirements: complete content for first attempts, fresh retries and reviews, and changed content for resumes. This includes authored issues exceeding 60,000 characters. This does not change how Done when commands execute. Keep checklist lines in the PR body's human-check section exactly as today, including the existing handling of checked items. An ordinary issue with no checklist or extra authored section keeps its existing execution, review and PR behavior.

Close when the locked offline tests observe the actual executor prompts through run_issue: first attempts, fresh retries and review receive the whole authored issue; a resumed session receives changed checklist and Contracts lines without repeating unchanged ones; the first prompt supplies the unchanged requirements that session retains; gate bookkeeping stays out; long content and the ordinary-issue control retain their required behavior. Existing named locked regression tests stay green.

## Out of scope

Publishing this draft or editing production code during order preparation; changing approval policy or preread invalidation policy; repeating unchanged issue content on resume; editing or replacing tests/acceptance/test_resume_after_question.py; classifying checklist lines as human-only or machine-only; moving or removing checklist lines from PR bodies; changing command execution, review verdict rules, model selection, or executor transport; adding semantic judgment rules to scripts; unrelated prompt rewrites.

## Files

- src/foragent/issue.py
- src/foragent/run_prompts.py
- src/foragent/run_implementation.py
- src/foragent/run_review.py
- src/foragent/review_single.py
- tests/test_issue.py
- tests/test_run_prompts.py
- tests/acceptance/test_prompts_whole_issue.py

## Verification

The order supplies one locked acceptance file. It exercises the public run_issue path with local Git histories, FakeTracker, fake implementer/reviewer executors and isolated state. No prompt builder, parser, session selection or approval reader is replaced. The two resume cases run a real first attempt that pauses with a question, reapprove changed checklist and Contracts lines, and resume the saved session. They require those changed lines to appear while unchanged issue text stays absent. They also require the first prompt to have delivered the retained unchanged content; that first-prompt behavior is currently missing. One resume case additionally changes gate metadata and requires its exclusion. Every new node remains a regression failure on current source.

- `uv run pytest -q tests/acceptance/test_prompts_whole_issue.py`
- `uv run pytest -q tests/acceptance/test_issue_files.py::test_issue_files_public_body_builder_and_parser_preserve_paths_and_globs`
- `uv run pytest -q tests/acceptance/test_order_preread.py::test_order_preread_reads_each_issue_with_implementer_in_read_only_mode`
- `uv run pytest -q tests/acceptance/test_order_preread.py::test_order_preread_plan_content_change_requires_new_preread`
- `uv run pytest -q tests/acceptance/test_review_rework_prompt.py::test_rework_prompt_requires_class_wide_repairs_and_a_searched_list_in_the_pr`
- `uv run pytest -q tests/acceptance/test_review_what_how.py::test_public_review_prompt_defines_failure_and_accepts_valid_alternative_approaches`
- `uv run pytest -q tests/acceptance/test_session_resume_prompt.py::test_resume_failure_logs_cli_error_counts_each_failure_and_still_starts_fresh`
- `uv run pytest -q tests/acceptance/test_claude_goal_prompt.py::test_long_claude_goal_separates_full_prompt_and_preserves_normal_codex_argv`
- `uv run pytest -q tests/acceptance/test_resume_after_question.py`

## Existing locked contracts

Keep tests/acceptance/test_resume_after_question.py unchanged, including assert_resumed_attempt lines 187-188: a resumed attempt must not repeat the unchanged issue body or original goal. Its existing parameterized node IDs are preserved. No existing locked test requires replacement. Keep the delta-only expectations in tests/test_worker_sessions.py unchanged as well. The prior proposal to restate the whole issue on resume was a planning judgment error caused by missing this locked-contract context, not a tool defect. This revision supplies that context in the Goal, acceptance cases and Done when without adding script judgment rules.
