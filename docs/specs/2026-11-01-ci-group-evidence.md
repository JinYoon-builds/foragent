# CI-mode group evidence

Decision: Jin, 2026-11-01, architect r185 option A. Starts after #1141 merges (it provides the member reuse rule). The planning session adds the real dependency relation; this one-issue order does not fabricate a blocked_by reference.

## Root cause and existing capability

The group check currently exits as soon as CI mode is enabled for main. A combined-commit builder, durable group evidence with per-member merge provenance, and GitHub CI acceptance evidence import already exist. The missing capability is exposing the combined commit to GitHub CI and allowing its valid evidence to cover the members in the CI publication path. Merely deleting the early return would run local checks and would not supply the required CI evidence.

The r185 investigation observed zero group checks and only singleton turns under checks: ci. Per-member main refreshes cause fresh GitHub CI rounds. Reuse the existing combined-commit builder, group evidence record and CI evidence import. Preserve the existing CI evidence bar, including exact base/head SHAs, scope and complete configured shards. Reuse #1141's member rule after it merges. GitHub's native merge queue is unavailable for user-owned repositories, so it is outside this order.

## Observable result

With checks: ci, target main, merge_group_max at least 3 and three consecutive ready members, record a group check and publish their combined commit for one GitHub CI round. It contains current main plus every ready head in FIFO order. Import the CI artifacts for that exact combined commit. A complete passing comparison on current main allows each owner to merge its original covered PR head in FIFO order, without another CI round caused solely by earlier covered members merging. The final main tree equals the tree checked by the group CI round. Uncovered head/base changes retain the existing evidence invalidation and individual verification behavior.

Failed, void or timed-out group evidence is not a pass: every member uses the ordinary individual verification path. Only the leader writes the temporary group branch/PR and cleans it up after use or failure. No member writes another member's run record. merge_group_max: 1 disables grouping and retains the current per-member CI refresh behavior.

The lane chooses how GitHub sees the combined commit, such as a temporary PR accepted by the existing pull_request workflow or a suitable workflow trigger. This order does not prescribe new internal helpers or record formats.

## Ownership and scope

Lane: main. In this checkout's .foragent.yml, no named lane path matches the group-check/evidence or merge-publication files. They therefore use the implicit main lane. The orch lane's runner.py entry does not claim its sibling run_group or run_publish files.

One issue, size m. Out of scope: implementing #1141 here, reviving #1142, failure bisection, switching checks to local, changing CI shard/evidence policy, moving the repository to an organization, GitHub native merge queue, scorecard work, and unrelated scheduler changes. The implementing lane owns reversible implementation choices and selects its own tool/review skills.

## Locked acceptance

The acceptance file enters through runner.run_issue and its public merge-group/FIFO flow using the repository's offline Git, model and tracker fixtures. It loads a real configuration containing checks: ci, base_branch: main and merge_group_max: 3. GitHub is a fake external transport; no group, CI comparison, reuse, refresh or merge decisions are stubbed. Its reports are immutable all-shard CI evidence pinned to the base and head at publication.

The three member PRs have already completed their initial CI before becoming ready. The one-round count starts when all three ready entries exist and covers group publication through all three merges; evidence polls are not new CI rounds. Separate tests exercise passing combined evidence, a real base-pass/head-fail report, malformed evidence, missing evidence until timeout, and merge_group_max: 1 as the existing-behavior control.

Break-it assertions catch the CI early return (missing group record), replacing CI with local checks (no combined publication/import), partial combined trees, repeated CI rounds, follower head refreshes, incorrect merge order, duplicate merges, failed/void evidence reuse, cross-owner writes and abandoned temporary group resources.

Done when: `uv run pytest -q tests/acceptance/test_ci_group_evidence.py` passes after the feature is implemented. Run only targeted tests, without pytest-xdist and without network access.

## Planning verification on main

Tested against a local archive of main at e2cc9aa44c2d04b05799bee083957f849c6fe538, without changing this checkout's HEAD. Command: `uv run pytest -q -p no:cacheprovider --tb=short tests/acceptance/test_ci_group_evidence.py` (with an isolated pytest base directory). Result: exit 1, 4 failed and 1 passed. The passing control is merge_group_max: 1; each feature case failed at the missing group record, not at import, setup or fake CI parsing.

Failure line, repeated for the pass/failed/void/timeout group scenarios:

```text
E   AssertionError: checks: ci skipped the group check: no group-check record was written
4 failed, 1 passed in 50.64s
```

The order dry-run uses an empty offline GitHub issue inventory because network access is blocked. It validates the plan and publication preview, without checking live issues or adding the #1141 dependency.
