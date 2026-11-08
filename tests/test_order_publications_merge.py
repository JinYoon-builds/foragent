"""The red check measures the bytes the owner lands after a three-way merge onto main (r1278)."""

from pathlib import Path
import sys

from conftest import git_command
from foragent import order_prepare, order_publications
from foragent.scheduler import TickReport
from test_order import offline_publication  # noqa: F401  (fixture)
from test_order_publications import NAME, case, review_plan, submit  # noqa: F401  (fixture)

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))
import publication_keeps_main_world as lock_world  # noqa: E402


def _text(check, value):
    gap = "".join(f"# unchanged line {n}\n" for n in range(1, 9))
    return f"def test_new():\n    assert {check}\n\n{gap}\nVALUE = {value}\n"


PREPARED = _text("False", 1)
ORDER = _text("False, 'sharper'", 1)
MAIN = _text("False", 2)
MERGED = _text("False, 'sharper'", 2)


def _statement_world(case, offline_publication, git_history):
    root, _config, _tracker, git, _calls, origin = offline_publication
    git_history.update({NAME: PREPARED}, message="Existing test")
    case.plan["issues"][0]["acceptance_files"] = {NAME: ORDER}
    path = order_prepare._record_path(case.store, case.plan)
    record = order_prepare._read(path, case.plan)
    record["issues"]["new"]["written"] = {NAME: ORDER}
    order_prepare._write(path, record)
    review_plan(case)
    submit(case)
    git.fetch("origin", "main")
    case.ctx.git, case.ctx.repo_root = git, root
    return origin


def test_statement_order_red_check_measures_the_merged_bytes_it_lands(case, offline_publication, git_history):
    origin = _statement_world(case, offline_publication, git_history)
    order_publications.check_pending(case.ctx, TickReport())
    row, = order_publications.requests(case.store)
    assert row["red_check_commit"] == git_history.tip, row.get("reason")

    git_history.update({NAME: MAIN}, message="Main changes another part")
    moved = len(case.checks)
    order_publications.check_pending(case.ctx, TickReport())
    order_publications.apply_next(case.ctx, TickReport())

    row, = order_publications.requests(case.store)
    assert row["status"] == "published", row.get("reason")
    landed = git_command(origin, "show", f"main:{NAME}") + "\n"
    assert landed == MERGED
    on_tip = [placed[NAME] for base, placed, _commands, _files in case.checks[moved:]
              if base == git_history.tip and NAME in placed]
    assert on_tip and all(data == MERGED for data in on_tip), (
        "The red check on the moved main must measure exactly the bytes that land: " + repr(on_tip))


def test_lock_correction_measures_the_merged_correction_on_moved_main(case, offline_publication, monkeypatch):
    world = lock_world.make_world(case, offline_publication, monkeypatch, {lock_world.LOCKED: lock_world.BASE})
    plan = lock_world.correction(world, {lock_world.LOCKED: lock_world.ORDER})
    lock_world.prepare(world, plan)
    lock_world.move_main(world, {lock_world.LOCKED: lock_world.MAIN_TAIL})
    before = len(world.runs)

    row = lock_world.land(world, plan)

    assert row["status"] == "published", row.get("reason")
    assert lock_world.origin_main(world, lock_world.LOCKED) == lock_world.BOTH
    # Corrections are measured on the issue head: the corrected bytes and main's previous ones.
    measured = {data for _base, name, data in world.runs[before:] if name == lock_world.LOCKED}
    assert lock_world.BOTH in measured, "The corrected measurement must hold the bytes that land"
    assert lock_world.ORDER not in measured, "The unmerged correction must not stand as the evidence"
