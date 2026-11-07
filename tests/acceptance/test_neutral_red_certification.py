"""Acceptance (neutral-during-train, statement 4). Locked by Foragent; do not edit.

A publication's red certification is not re-run when main moves only in files
outside its tests and their imports; it is re-run when the move touches them.

Today the certification survives only a D111 move (role memory). Any other main
move, even a source file no test of the publication imports, measures the
publication again, and 8 of 21 requests re-ran on 2026-11-07.

Real order publication queue, off-owner checker and merge owner pass over real
Git history (tests/test_order.py offline_publication); measured test runs,
GitHub and preparation records are the fakes of tests/test_order_publications.py.
A red check measures through the fake test runner; its calls are counted, and a
check may measure more than once (its reviewed base is seeded first).
"""

from conftest import git_command
from foragent import merge_owner, order_prepare, order_publications
from foragent.scheduler import TickReport
from test_order import offline_publication  # noqa: F401  (fixture)
from test_order_publications import NAME, case, review_plan, submit  # noqa: F401  (fixture)

IMPORTED = "src/ledger.py"
TEST_TEXT = ("from ledger import parse\n\n\n"
             "def test_new():\n    assert parse('1') == 1\n")


def published_world(case, offline_publication):
    """The publication's test imports `ledger`; main has src/ledger.py and an unrelated file."""
    root, _config, _tracker, git, _calls, origin = offline_publication
    history = case.history
    history.update({IMPORTED: "def parse(text):\n    return None\n",
                    "src/unrelated_tool.py": "VALUE = 1\n",
                    "docs/guide.md": "# Guide\n"}, message="Base")
    case.plan["issues"][0]["acceptance_files"] = {NAME: TEST_TEXT}
    path = order_prepare._record_path(case.store, case.plan)
    record = order_prepare._read(path, case.plan)
    record["issues"]["new"]["written"] = {NAME: TEST_TEXT}
    order_prepare._write(path, record)
    review_plan(case)
    submit(case)
    git.fetch("origin", "main")
    case.ctx.git, case.ctx.repo_root = git, root
    return origin


def owner_pass(case):
    """What the gate does each tick: the off-owner checker, then one merge owner pass."""
    report = TickReport()
    order_publications.check_pending(case.ctx, report)
    merge_owner.tick(case.ctx, report)
    return report


def red_runs(case):
    return len(case.checks)


def status(case):
    row, = order_publications.requests(case.store)
    return row


def certified(case):
    """The off-owner checker certifies the request on the current main."""
    order_publications.check_pending(case.ctx, TickReport())
    row = status(case)
    assert row["red_check_commit"] == case.history.tip, "The checker certified on current main"
    assert red_runs(case) >= 1
    return row


def test_move_outside_tests_and_imports_keeps_the_certification(case, offline_publication, git_history):
    case.history = git_history
    origin = published_world(case, offline_publication)
    checked = certified(case)["red_check_commit"]
    before = red_runs(case)

    # Main moves twice, only in files the publication's test neither is nor imports.
    git_history.update({"src/unrelated_tool.py": "VALUE = 2\n"}, message="Unrelated source")
    git_history.update({"docs/guide.md": "# Guide\nMore.\n"}, message="Docs")
    owner_pass(case)

    row = status(case)
    assert red_runs(case) == before, (
        "A main move outside the publication's tests and their imports must not re-run its red check, "
        f"but it measured again: {[check[0] for check in case.checks[before:]]}")
    assert row["status"] == "published", f"The certified publication lands on the moved main: {row.get('reason')}"
    assert row["red_check_commit"] == checked, "The certification stays the one made before the move"
    assert git_command(origin, "show", f"main:{NAME}") == TEST_TEXT.rstrip("\n")
    assert git_command(origin, "show", "main:src/unrelated_tool.py") == "VALUE = 2"


def test_neutral_move_keeps_then_imported_file_move_re_runs_the_certification_once(
        case, offline_publication, git_history):
    case.history = git_history
    origin = published_world(case, offline_publication)
    checked = certified(case)["red_check_commit"]
    before = red_runs(case)

    # First main moves only in a docs file: the certification stays and nothing is measured.
    git_history.update({"docs/guide.md": "# Guide\nNeutral.\n"}, message="Docs")
    order_publications.check_pending(case.ctx, TickReport())
    assert red_runs(case) == before, (
        "A main move outside the publication's tests and their imports must not re-run its red check, "
        f"but it measured again: {[check[0] for check in case.checks[before:]]}")
    assert status(case)["red_check_commit"] == checked, "The certification survives the neutral move"

    # Then main moves in a module the publication's test imports.
    git_history.update({IMPORTED: "def parse(text):\n    return int(text)\n"}, message="Ledger change")
    moved = git_history.tip
    owner_pass(case)
    once = red_runs(case)
    owner_pass(case)

    row = status(case)
    assert once > before, "A move that touches a module the publication's test imports re-runs its red check"
    assert moved in [check[0] for check in case.checks[before:]], "The re-run measures the moved main"
    assert red_runs(case) == once, "The re-run happens once, not again on the next pass"
    assert row["status"] == "published"
    assert git_command(origin, "show", f"main:{NAME}") == TEST_TEXT.rstrip("\n")


def test_neutral_move_keeps_then_test_file_move_re_runs_the_certification(case, offline_publication, git_history):
    case.history = git_history
    published_world(case, offline_publication)
    checked = certified(case)["red_check_commit"]
    before = red_runs(case)

    # First main moves only in a source file the test does not import.
    git_history.update({"src/unrelated_tool.py": "VALUE = 4\n"}, message="Unrelated source")
    order_publications.check_pending(case.ctx, TickReport())
    assert red_runs(case) == before, (
        "A main move outside the publication's tests and their imports must not re-run its red check, "
        f"but it measured again: {[check[0] for check in case.checks[before:]]}")
    assert status(case)["red_check_commit"] == checked, "The certification survives the neutral move"

    # Another writer lands the same test path first: the publication's test itself moved.
    git_history.update({NAME: TEST_TEXT}, message="Same test landed elsewhere")
    moved = git_history.tip
    owner_pass(case)

    assert red_runs(case) > before, "A move that touches the publication's own test file re-runs its red check"
    assert moved in [check[0] for check in case.checks[before:]], "The re-run measures the moved main"


def test_unrelated_move_then_import_move_re_runs_only_for_the_import(case, offline_publication, git_history):
    """A doubled or skipped effect: two neutral moves add no run, one touching move adds exactly one."""
    case.history = git_history
    published_world(case, offline_publication)
    certified(case)
    before = red_runs(case)
    git_history.update({"src/unrelated_tool.py": "VALUE = 3\n"}, message="Unrelated")
    order_publications.check_pending(case.ctx, TickReport())
    assert red_runs(case) == before, "An unrelated move must not re-run the red check"
    git_history.update({IMPORTED: "def parse(text):\n    return int(text)\n"}, message="Ledger change")
    order_publications.check_pending(case.ctx, TickReport())
    once = red_runs(case)
    order_publications.check_pending(case.ctx, TickReport())
    assert once > before, "The move touching an import re-runs the red check"
    assert red_runs(case) == once, "It re-runs once, not on every later pass"
    assert status(case)["red_check_commit"] == git_history.tip
