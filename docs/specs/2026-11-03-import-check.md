# Refuse an order whose changed test files break other passing tests

## Why
On 2026-11-03 an order commit (afd1b174) changed the shared acceptance file
`tests/acceptance/test_review_perspectives.py` (its `NAMES`/`CHECKLIST`). The order's own new
tests were red on main, as new locks should be. But the unit test
`tests/test_review_conflict_paths.py` imports `CHECKLIST` and `perspective_case` from that file
and passed on main. After the order landed, 7 of its tests broke. Main CI was red for about
50 minutes and every PR's CI failed with it. Restoring the earlier file (cea2961a) fixed it.

The check before publication (#1523, made slot-free by #1717) only collects the order's own
acceptance files. A value change in a shared file still collects, so nothing caught this.

## What changes
When an order changes or adds a locked test file, the check before publication also runs the
other test files that import it. It runs them on the current base and with the order's files.
A test that passes on the base and fails or errors with the order's files breaks the order:

- `fa order publish` (plain, `--approve`, `--dry-run`, `--defer`) and `fa order preread` refuse
  before any write and before the preread model. The refusal names each broken test (file and
  test name) and shows why it broke.
- An order whose changed files keep every such test passing publishes as before.
- A test that already fails on the base is not a reason to refuse and is not named.
- The order's own new or changed locks failing on main is expected and is not checked here.
- Like the collection check (#1717), this does not wait for lane test runs that hold every
  machine test slot.

## Out of scope
- Tests that reach the changed file only through another module (indirect importers).
- Running the whole suite, CI changes, and the collection rules of #1523.
- Re-checking importers when a deferred order is flushed later on a moved base (allowed, not
  required).

## Done when
- `uv run pytest -q tests/acceptance/test_publish_collect_check.py`: the new cases build
  today's case in a small fixture repository (a shared acceptance file, an importer that passes
  on main, an importer red on main). Changing the shared file the way afd1b174 did is refused
  and names `tests/test_review_conflict_paths.py`; removing a name the importer needs is
  refused; a change that keeps importers passing publishes; the refusal does not wait for lane
  test runs. #1523's and #1717's cases in the same file keep passing.
- `uv run pytest -q tests/test_order_collect.py` and
  `uv run pytest -q tests/acceptance/test_order_preread.py` keep passing.

## Notes for the lane (facts, not a design)
- `tests/conftest.py` turns `order_collect.check` off outside `COLLECTION_FILES`. Many locked
  publication tests publish from fake Git, a scrubbed PATH or no test command. A new check that
  runs outside that switch reaches all of them.
- The collection budget is 30 seconds per order. Running importers takes longer than collecting.
  The budget for this run is the lane's choice.
- A scratch prototype (`prototype.patch`, about 100 lines in `order_collect.py`) passed all
  three Done when files. It found importers by a text search for `import <stem>` /
  `from <stem> import` and compared per-test JUnit results of the base copy and the order copy.
  It is evidence that the locks are satisfiable, not the design.
