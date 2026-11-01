# Stop condition the implementer can prove

## Problem and root cause
The executor receives a condition requiring proof that "no files outside this repository are changed". That describes all filesystem activity rather than the implementer's own edits. Done when tooling creates pytest temporary folders and shared test-slot files outside the repository, so successful checks can themselves contradict that condition. The implementer lacks an actor-scoped contract that distinguishes its edits from tooling output. It also needs an explicit turn-budget exit that does not ask the stop check again. More filesystem observation or a script that judges intent would not repair the contract.

The requester supplied evidence from 2026-10-27 through 2026-10-31: runs #1004, #1030, #1062, #1059, #1027, #995, #980, #745, #727 and #754 reached "A hook blocked the turn from ending 9 consecutive times". On #1062 a completed fix remained uncommitted and the issue was blocked with retries-exceeded. This order treats that choice-free evidence as supplied; no network lookup is needed.

## Required outcome
Give the implementer a stop condition it can verify: every Done when command exits 0 from the repository root, nothing under the configured acceptance directory is modified, and the implementer did not itself edit files outside the repository. Explicitly state that files written outside the repository by test tooling do not count as the implementer's edits. Reaching 40 turns ends the run independently of those checks, without repeating the stop check. Preserve the unchanged-acceptance-directory text, the configured path, and the existing 3,500-character length limit, truncating only an oversized acceptance path so every instruction survives.

## Scope
One issue, size s, on review-engine, the lane owning the prompt source in .foragent.yml. The change is to the implementer's observable stop-condition text and its regression coverage. The lane chooses the wording and implementation.

## Out of scope
Implementing this order during planning; changing the stop-hook algorithm, retry policy, executor protocol, test-slot handling, filesystem permissions, or acceptance locking; adding filesystem scans, semantic classifiers, keyword rules, or model decisions to scripts. The tooling exemption is limited to output produced by test tooling and does not excuse the implementer's own outside edits.

## Locked acceptance
Exercise the public goal builder already used by tests/acceptance/test_session_resume_prompt.py. tests/acceptance/test_claude_goal_prompt.py covers delivery of that goal to the executor and remains a regression check. The locked file observes the returned goal text, without scanning source or substituting a fake goal builder. It covers the default acceptance path, a custom path with spaces and non-ASCII characters, and an oversized path. It checks the own-edit scope, tooling exemption, preserved Done when and acceptance instructions, and an independent 40-turn exit without repeated stop checks. Oversized paths must leave those instructions intact within the existing 3,500-character cap.

Break-it rule: restoring the global outside-file claim, removing the own-edit scope or tooling exemption, dropping the Done when or unchanged-acceptance instruction, truncating instructions instead of only the long path, or turning the budget exit back into a repeated stop check must fail the locked test. Wording may vary within the observable contract; no private implementation name is required.

## Completion
Run only this issue's locked acceptance file and the related prompt regression files with uv run pytest. Tests for unrelated pending issues are outside this order. Existing editable unit expectations that preserve the defective global claim must agree with the corrected goal.
