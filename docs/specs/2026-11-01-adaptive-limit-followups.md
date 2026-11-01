# Adaptive protected-check limit follow-ups

Date: 2026-11-01. These follow-ups apply after #1347 merges. They are drafted and tested against the supplied foragent/issue-1347 checkout. The supplied independent review reported 290 passing tests; this run uses only the targeted checks below.

## Outcome

The protected-check allowance comes from the measured duration of the same acceptance selection on this machine. Test-slot queue time is excluded. Legacy reusable cache records retain the timings recorded in their companion JUnit XML. Single-run and merge-group protected checks follow the adaptive contract already established by #1347: twice that measurement, a 900-second floor, a 3600-second cap, bounded by the run's remaining budget. This order does not change that contract.

## Scope and delivery

There are four independently verifiable size-s P2 issues, with one locked acceptance file per issue. No issue uses a statements field. No new lane is needed. The relevant ci_local.py and run_*.py paths, their proposed unit-test files, and run_baseline_limits.py match none of the lane paths in .foragent.yml; all four therefore belong to the implicit main lane. The orch lane explicitly owns runner.py, not every run_*.py module. No changes to runner.py or ownership are required by this order.

Publish only after #1347 is on main; #1347 is the common prerequisite, not new work in this order. Keep group-check-adaptive-limit as its own issue and deliver it first. Its merge is a release prerequisite for both #1141 and #1142. At publication, those two existing issues need GitHub blocked-by edges to the new group-check issue. This PLAN schema accepts blocked_by keys for children in the same order only, not existing issue numbers; no unavailable existing issue bodies are rewritten here. The four follow-ups have no code dependency on one another, so their blocked_by lists are empty.

## Observed causes, without prescribing an implementation

- CI: ci_local.capture_local_only returns the measured protected selection without its duration. The protected check falls back to repository history, or starts at 900 seconds with no history and retries at 1800 seconds.
- Legacy cache: run_baseline's cached-baseline conversion reads only the duration field. The same entry's JUnit timing remains available, but the protected check instead uses another selection's newer record.
- Queue: run_baseline measures around the check call, whose slot acquisition happens inside the call. Command execution and slot waiting are combined in the saved duration.
- Group: run_group_check.check gives the protected acceptance command a fixed 900-second allowance, independent of its current-base baseline measurement.

These are measurement provenance and timing-boundary defects, not missing semantic judgment. The lane owns the implementation. No keyword rules, agent step orders or LLM decisions in scripts are part of this order.

## Locked acceptance checks

The first three files exercise the public runner.run_issue entry point, with the repository's offline runner fixture. The cache and queue files also exercise public capture_acceptance_baseline. They fake command execution, slot acquisition, the implementer and final publication; the real capture, cache loading, selection, command timing, protected verification and timeout retry paths execute. Fake Git histories remain local. The group file exercises public capture_acceptance_baseline, run_merge_turn.join/seat and run_group_check.check, with fake Git and subprocesses and a fake clock. It checks the adaptive allowance, cap, remaining budget and floor, and requires passing group evidence. No real command sleeps or remote calls are needed.

Break-it rule: a wrong allowance fails an exact expected-limit assertion; skipping a capture or protected check fails the invocation and selection assertions; running a capture, protected check, implementer, publication or group suite twice fails the counts where that operation is in scope. Cache cases require one capture across fresh creation and reuse. Queue cases check both credited waiting and the duration retained in the cache. Group checks run twice with the same identity and require exactly one suite and successful evidence. These are behavioral checks, not source-text checks. Mutation testing against a fixed implementation was not run: implementation is outside this task.

The acceptance_files strings in this plan are the locked publication copies. The matching files under order-out/tests/acceptance are staging copies; no tests/ file is written by this planning run. Done when paths name their future published locations and every command uses uv run pytest.

## Reproduction on this checkout

The following environment was used with each targeted command. All temporary state and caches stayed inside the working directory. The existing Python environment was read with --no-sync and network access was disabled for uv.

```sh
export TMPDIR="$PWD/order-out/.runtime"
export UV_CACHE_DIR="$PWD/order-out/.runtime/uv-cache"
export UV_PROJECT_ENVIRONMENT=/Users/apple/Dev/foragent/.venv
export UV_OFFLINE=1
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$PWD/src:$PWD/tests"
```

```sh
uv run --no-sync pytest -q --tb=short -p no:cacheprovider -p conftest --basetemp="$PWD/order-out/.runtime/ci" order-out/tests/acceptance/test_limit_ci_duration.py
```

```text
E   AssertionError: CI protected selection must use its own 736s measurement: expected one 1472s allowance, got [900, 1800]
E   AssertionError: CI protected selection must use its own 1200s measurement: expected one 2400s allowance, got [900, 1800]
2 failed in 2.60s
```

```sh
uv run --no-sync pytest -q --tb=short -p no:cacheprovider -p conftest --basetemp="$PWD/order-out/.runtime/cache" order-out/tests/acceptance/test_limit_cache_timings.py
```

```text
E   AssertionError: Cached selection's JUnit records 736s; unrelated 100s selection must not replace its 1472s allowance: got [900]
E   AssertionError: Cached selection's JUnit records 810s; unrelated 2000s selection must not replace its 1620s allowance: got [3600]
2 failed in 2.95s
```

```sh
uv run --no-sync pytest -q --tb=short -p no:cacheprovider -p conftest --basetemp="$PWD/order-out/.runtime/wait" order-out/tests/acceptance/test_limit_exclude_slot_wait.py
```

```text
E   AssertionError: 736s of execution must give a 1472s protected limit even after 400s waiting for a slot: got [2272]
E   AssertionError: 810s of execution must give a 1620s protected limit even after 1600s waiting for a slot: got [3600]
2 failed in 2.93s
```

```sh
uv run --no-sync pytest -q --tb=short -p no:cacheprovider -p conftest --basetemp="$PWD/order-out/.runtime/group" order-out/tests/acceptance/test_group_check_adaptive_limit.py
```

```text
E   AssertionError: Group protected check must use the same adaptive allowance as a single run (736s measurement, 21600s remaining): expected 1472, got [900]
E   AssertionError: Group protected check must use the same adaptive allowance as a single run (2000s measurement, 21600s remaining): expected 3600, got [900]
E   AssertionError: Group protected check must use the same adaptive allowance as a single run (736s measurement, 1000s remaining): expected 1000, got [900]
3 failed, 1 passed in 0.51s
```

The one passing group case is the 900-second floor control. All nine failures are assertions about the reported defects, with no setup or collection errors in these final targeted runs. No full-suite run or pytest-xdist run was made.

## Dry-run evidence

The required native command ran with local src on PYTHONPATH, bytecode disabled, and HOME, GH_CONFIG_DIR and all XDG directories isolated under order-out/.runtime. GH_TOKEN and GITHUB_TOKEN were unset so this network-blocked run could not use account credentials. It exited 1 before a GitHub-backed preview:

```sh
fa order publish --dry-run order-out/plan.json
```

```text
foragent: To get started with GitHub CLI, please run: gh auth login Alternatively, populate the GH_TOKEN environment variable with a GitHub API authentication token.
```

The final plan is also checked through the same local CLI entry point with only GhTracker replaced by the existing FakeTracker, empty because no live issue snapshots were supplied. No publisher, parser, configuration or validation logic is replaced. This is an offline preview, not a successful live GitHub lookup. It exited 0 with this preview:

```text
Plan (dry run): Keep adaptive protected-check limits tied to their measurements
Files: docs/specs/2026-11-01-adaptive-limit-followups.md, tests/acceptance/test_group_check_adaptive_limit.py, tests/acceptance/test_limit_cache_timings.py, tests/acceptance/test_limit_ci_duration.py, tests/acceptance/test_limit_exclude_slot_wait.py

lane:main
| Number | Title | Size | Blocked by | Approved? | Interactive? |
| --- | --- | --- | --- | --- | --- |
| group-check-adaptive-limit | [P2] Use the adaptive protected-check allowance for merge groups | s | — | no | no |
| limit-ci-duration | [P2] Keep the CI local-only measurement with its protected selection | s | — | no | no |
| limit-cache-timings | [P2] Use a legacy cached selection’s recorded timings for its protected limit | s | — | no | no |
| limit-exclude-slot-wait | [P2] Exclude test-slot queue time from the protected selection’s duration | s | — | no | no |
```

Local schema validation, Python AST parsing and an exact comparison between each embedded acceptance file and its staging copy passed. No implementation files or existing tests are changed; all deliverables remain uncommitted.
