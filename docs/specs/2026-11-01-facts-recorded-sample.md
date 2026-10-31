# Facts tests use a recorded gate sample

Date: 2026-11-01. Choice-free correction of the 2026-10-31 defect.

## Goal

Facts acceptance tests read a recorded gate resource sample from their isolated machine root. The same commit and records produce identical JSON and human facts on repeated observation, even while live host load changes. Production facts retain their existing resource-reading behavior, including the live reader when no gate sample exists.

## Evidence and root cause

The locked case `tests/acceptance/test_facts_baseline_trend.py::test_facts_baseline_trend_repeated_json_and_human_observation_is_read_only` failed 2 of 7 runs on one HEAD and blocked #1295 twice as a baseline regression. The reported human output changed from `load 1.03 per CPU` to `load 1.00 per CPU`.

The shared `facts_case` isolates the clock, repository and machine root but supplies no gate resource sample. `orch_facts_machine.freeze_sample` therefore falls back to `capacity.machine_load` under its read-only observation context. Host load is legitimate changing production input; it is missing recorded test input here. Reusing the existing recorded-sample path fixes the test boundary without changing production facts.

## Required behavior

- The shared fixture supplies a recorded gate sample in the isolated machine root. Resource-specific cases retain their distinct inputs and assertions using recorded samples.
- Run the original baseline-trend read-only case ten consecutive times with a live reader that would return a different load on every call. All 30 JSON and 20 human observations must agree, retain baseline assertions and existing file-byte/mtime, process and offline guards, and make zero live-reader calls.
- A facts observation with no recorded gate sample still calls the live reader on every observation and reports its changing values in both JSON and human output without writing files.
- Keep every existing function name, signature, pytest parameter value and parameter ID. The orchestrator owns the corrected locked files in this order. Full contents are in `acceptance_files`; lane ownership is `roles` per `.foragent.yml` for `src/foragent/orch_facts*.py`.

## Out of scope

Production behavior or policy changes, output normalization or resource-field removal, retry-based suppression, changes to unrelated tests, and actual publication.

## Locked checks and break-it evidence

Main and HEAD were both `a1d5fedccbbac2380efac23e233dab0c6f9e180b`. The new locked check was run against main's original source and fixtures, with the live reader monkeypatched to change on each call. The production fallback case passed; the repeated observation case failed deterministically:

```text
tests/acceptance/test_facts_baseline_trend.py:246: in test_facts_baseline_trend_repeated_json_and_human_observation_is_read_only
    assert case.read() == first
E   AssertionError: Recorded facts must have identical JSON and human output across all ten runs
FAILED order-out/tests/acceptance/test_facts_recorded_sample.py::test_facts_recorded_sample_repeats_original_read_only_case_ten_times
1 failed, 1 passed in 1.53s
```

Reproduction from the draft directory uses the repository's original fixture imports, rather than the proposed sibling fixture:

```sh
PYTHONPATH=src:tests:tests/acceptance uv run pytest -p conftest --import-mode=importlib -p no:cacheprovider order-out/tests/acceptance/test_facts_recorded_sample.py -q --tb=short
```

A separate main archive with only the two proposed locked files overlaid passed all six targeted files: `52 passed in 16.37s`. Its production source was unchanged. Removing the sample re-exposes the deterministic failure above; freezing or omitting the live value fails the no-record production check. No sleeps or retries hide output changes, and no pytest-xdist is used.

Two mechanical runtime faults were also injected against the proposed files without editing production source. Both were rejected by the production fallback check:

```text
# Skip the live read and return a fixed load:
E   assert 9.9 == 1.03
1 failed in 1.13s
# Perform the live read twice for one observation:
E   assert 1.0 == 1.03
1 failed in 1.16s
```

## Done when

```sh
uv run pytest -q tests/acceptance/test_facts_recorded_sample.py tests/acceptance/test_facts_baseline_trend.py tests/acceptance/test_orch_facts.py tests/acceptance/test_resource_facts.py tests/acceptance/test_facts_repeated_work.py tests/acceptance/test_overlap_facts.py
```

## Assumptions

"Production still reads the live host" preserves the existing no-record fallback; it does not replace the existing preference for recorded gate samples. This is a local order draft only; network operations and publication are outside this run.

## Local preview

`fa order publish --dry-run order-out/plan.json` succeeded with an empty offline `FakeTracker` at the CLI boundary and network connections disabled. This validates the real CLI plan schema, locked file paths and roles/size:s issue rendering. GitHub duplicate-order discovery was not checked.
