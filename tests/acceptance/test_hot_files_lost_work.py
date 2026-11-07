"""Acceptance test for hot-files-only-on-lost-work (architect r1032, 2026-11-07).

The hot_overlap_files fact is met for a file only when, in the 24-hour window, a run lost
work on it. Co-presence in overlap records alone (no rebuild, no conflict-budget stop) is
not lost work. Driven through the public `fa orch facts --json` command.
"""

from datetime import timedelta

from test_optimistic_overlap import CLI, OTHER, STATE, _overlap, _recorded, offline_overlap  # noqa: F401
from test_orch_facts import facts, facts_case
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401

KIND = "hot_overlap_files"


def _scalars(value):
    """Every key and leaf value under a JSON subtree, as strings."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _scalars(item)
    elif isinstance(value, list):
        for item in value:
            yield from _scalars(item)
    else:
        yield str(value)


def _counts(value):
    """(key, value) pairs for every int leaf under a JSON subtree."""
    if isinstance(value, dict):
        for key, item in value.items():
            if type(item) is int:
                yield str(key), item
            else:
                yield from _counts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _counts(item)


def _file_entry(numbers, path):
    """The subtree of a kind's numbers keyed by ``path``, at any depth."""
    if isinstance(numbers, dict):
        if path in numbers:
            return numbers[path]
        for item in numbers.values():
            if (found := _file_entry(item, path)) is not None:
                return found
    elif isinstance(numbers, list):
        for item in numbers:
            if isinstance(item, dict) and item.get("file") == path:
                return item
            if (found := _file_entry(item, path)) is not None:
                return found
    return None


def test_three_runs_naming_the_same_file_without_lost_work_do_not_meet_hot_overlap_files(
        tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)
    # Three, then four, runs in 24 hours whose overlap records name STATE, one even
    # conflicted and resolved in place, none rebuilt and none stopped on the budget.
    _recorded(case, 31, hours=6, declared=[STATE], changed=[STATE], overlaps=[_overlap(STATE, issues=(32,))])
    _recorded(case, 32, hours=5, declared=[STATE, CLI], changed=[STATE],
              overlaps=[_overlap(STATE, issues=(31, 33)), _overlap(CLI, issues=(33,))])
    _recorded(case, 33, hours=4, declared=[STATE, CLI], changed=[STATE, CLI],
              overlaps=[_overlap(STATE, conflict=True, seconds=40, issues=(32,)), _overlap(CLI, issues=(32,))])
    _recorded(case, 34, hours=3, declared=[STATE, CLI], changed=[STATE, CLI],
              overlaps=[_overlap(STATE, issues=(33,)), _overlap(CLI, issues=(33,))])

    result = facts(case, capsys)

    assert KIND not in result["kinds"], (
        "Files that merely appear in the overlap records of several runs are no lost work: "
        f"hot_overlap_files must stay unmet, got {result.get('kind_numbers', {}).get(KIND)}")
    assert KIND not in result.get("kind_numbers", {})
    # Co-presence and conflict counts stay informational in the facts.
    assert STATE in result["file_conflicts"], "Keep per-file co-presence and conflict counts as information"
    assert facts(case, capsys) == result, "Observing facts twice must be read-only and deterministic"


def test_a_run_that_rebuilt_on_a_file_meets_hot_overlap_files_naming_issue_and_rebuild_count(
        tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)
    # Issue 457 rebuilt twice after its conflict budget ran out on STATE. Its CLI record and
    # two more runs naming CLI are co-presence only; OTHER lost work outside the window.
    _recorded(case, 457, hours=2, declared=[STATE, CLI], changed=[STATE, CLI], overlaps=[
        _overlap(STATE, conflict=True, seconds=13, redo=2, issues=(458,)), _overlap(CLI, issues=(458,))])
    _recorded(case, 458, hours=3, declared=[CLI], changed=[CLI], overlaps=[_overlap(CLI, issues=(457,))])
    _recorded(case, 459, hours=4, declared=[CLI], changed=[CLI], overlaps=[_overlap(CLI, issues=(458,))])
    _recorded(case, 460, hours=30, declared=[OTHER], changed=[OTHER],
              overlaps=[_overlap(OTHER, conflict=True, seconds=7, redo=1, issues=(461,))])

    result = facts(case, capsys)

    assert KIND in result["kinds"], "A run whose overlap record has redo_count > 0 is lost work: the kind is met"
    numbers = result["kind_numbers"][KIND]
    entry = _file_entry(numbers, STATE)
    assert entry is not None, f"hot_overlap_files numbers must name the file that lost work: {numbers}"
    assert _file_entry(numbers, CLI) is None, f"Co-presence alone must not name a file: {numbers}"
    assert _file_entry(numbers, OTHER) is None, f"Lost work older than 24 hours must not count: {numbers}"
    texts = set(_scalars(entry))
    assert texts & {"457", "#457"}, f"Name the issue that lost work on the file: {entry}"
    assert not texts & {"459", "#459"}, f"Name only issues whose records are on the file: {entry}"
    rebuilds = {count for key, count in _counts(entry)
                if any(word in key.lower() for word in ("rebuild", "redo", "lost", "stop")) and count > 0}
    assert rebuilds == {2}, f"Show the file's rebuild count (2, from redo_count), not doubled: {entry}"
    assert facts(case, capsys) == result, "Observing facts twice must be read-only and deterministic"
