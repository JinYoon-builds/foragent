# The publication collect check does not wait for lane test runs

## Defect (measured 2026-11-03, five refusals)

`fa order publish` (plain, `--approve`, `--dry-run`, `--defer`), `fa order preread` and
`fa order flush` run a collect-only check of the order's acceptance files first (#1523).
That check waits for a machine-wide test slot inside its deadline. When lane test runs hold
every test slot, no slot frees in time and the order is refused:

> 수집 검사를 할 수 없어서 결과가 없어요 … 원인: 수집 마감 전에 기계 시험 칸이 비지 않았어요

A plain retry minutes later passes. The check is cheap (collect only, no test runs), so
the wait is the fault, not the load.

Reproduced on main eb765758: with one real `fa test` run holding the only test slot,
`publish --dry-run` and `preread --json` refuse after about 30 s with
"no machine test slot became free before the collection deadline".

## Wanted result

While lane test runs hold every test slot, the collect check before publish, preread and
flush finishes within its deadline and gives its normal answer: a collectable order passes,
and an order whose locked file does not collect is still refused with the collection error.
It does not wait for lane test runs to finish.

How (not taking a slot, going to the front of the queue, or something else) is the lane's
choice. Messages, tests or code paths that only served the old wait may go.

## Out of scope

- The collection deadline itself and what the check collects (#1523's contract stays).
- Slot admission for real test runs (`fa test`, gates, Done when).

## Done when

- `uv run pytest -q tests/acceptance/test_publish_collect_check.py` passes, including the
  existing #1523 locks and these new locks:
  - `test_collect_check_passes_while_lane_tests_hold_every_slot`
  - `test_uncollectable_file_is_still_refused_while_lane_tests_hold_every_slot[publish-options0]` (`--defer`)
  - `test_uncollectable_file_is_still_refused_while_lane_tests_hold_every_slot[publish-options1]` (`--dry-run`)
  - `test_uncollectable_file_is_still_refused_while_lane_tests_hold_every_slot[preread-options2]` (`--json`)
- `uv run pytest -q tests/test_order_collect.py` passes.
- `uv run pytest -q tests/acceptance/test_order_preread.py` passes.

The new locks hold every slot with a real `fa test` run of a lane test that keeps going until
the test releases it (the project allows one test slot). They also check that this run is
still going when the order command returns, so a fix that waits for it fails.
