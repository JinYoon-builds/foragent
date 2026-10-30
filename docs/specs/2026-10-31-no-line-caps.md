# Drop fixed source module line-count caps

## Problem and decision
The six historical split acceptance tests enforce source size as a proxy for avoiding file contention. Their frozen module inventories also pull later, unrelated modules into the checks. On the supplied main snapshot the runner cap already rejects capacity.py (706 > 600 lines) and run_review.py (607 > 600). Remove these six size checks and the architect instruction that recreates them. Split decisions belong to the architect, follow responsibilities, and require measured overlap waits or co-change facts from fa orch facts. No replacement size threshold or scripted architectural judgment is introduced.

## Compatibility and test identities
Keep every existing test function name, including the historical *_meet_line_limits names. src/foragent/ci_mode.py::comparison subtracts passing head node IDs from passing base node IDs; a previously passing test renamed or removed on the implementation head is a regression. src/foragent/ci_local.py::fallback_comparison also runs baseline node selectors directly, so a removed selector fails. The renamed-test handling in run_baseline.py::collect_protected_ids and tests/acceptance/test_baseline_renamed_tests.py concerns tests that disappeared on a newly advanced base; it does not justify dropping a selector only on the head.

The historical cap functions now check the frozen facade bindings. Preserve every existing importability, public-method and facade patch propagation assertion, as well as run_publish's imported-split-module check and watch's module-naming assertion. Keep the exclusive file reservation in the architect instruction. Amend the locked tests through the complete six acceptance_files values, byte for byte equal to the amended files.

## Ownership and scope
One size-s issue uses the implicit main lane. None of its seven paths matches any lane paths in .foragent.yml: orch owns scheduler/runner source and its own skill, view owns cli/watch source, and the skills lane owns selected skill-management source, not these acceptance tests or skills/fa-architect/SKILL.md. No new lane is needed. The issue Files list contains exactly the six split acceptance files and the architect skill. Source implementations, other locked tests, SKILL.md length limits, baseline/merge policy, and publication are out of scope.

## Verification
Run only the six split files and the three locked skill files in Done when, serially without xdist; check uptime and require the one-minute load average below 8 before every pytest invocation. Use the existing offline fixtures and keep TMPDIR inside this workspace. The initial targeted run was 42 passed and 2 failed: the runner line cap and test_architect_references, whose required ignored .claude/skills/testing-strategy/SKILL.md is absent from this supplied snapshot. Do not fabricate that dependency or weaken its test.

Break-it in a workspace scratch copy of the supplied main sources: append 400 comment lines to scheduler_recovery.py (300 -> 700), prove the original scheduler cap rejects it, then run all six amended split files and require them to pass. Remove scheduler.has_machine_check in that scratch copy and require both its historical selector and its original facade-name test to fail with the missing binding. Leave main source bytes unchanged. Check the plan with read_plan and validate_plan locally, without GitHub or publication.

## Observed results
The amended targeted run completed with 43 passed and the same single pre-existing missing-testing-strategy failure (6.05 s). In the scratch copy, the original scheduler selector failed at scheduler_recovery.py = 700 > 300 lines; all six amended split files passed (30 passed, 5.71 s); deleting scheduler.has_machine_check then failed both scheduler facade-name selectors with that binding reported missing (2 expected failures, 0.59 s). No main source file was mutated.

Local read_plan/validate_plan checks passed. The plan contains one main-lane size-s issue, exactly seven Files, and six acceptance_files equal to the amended file bytes. AST comparison confirmed all 28 test function names survived and all 83 assertions in unchanged functions are identical. The non-size run_publish import and watch naming assertions remain. All 293 source Python module hashes stayed unchanged across verification. Every pytest launch was serial with plugin autoload disabled and an uptime one-minute load average below 8 (observed range 3.23-4.14). See verification.txt for commands and captured output.

## Assumptions
The supplied repo/ snapshot is the main source baseline. Historical selector names and unrelated structural/reservation assertions remain for compatibility and minimal scope. The missing ignored testing-strategy skill is an environment deficiency that predates this order, not an instruction to change another skill or its locked test.
