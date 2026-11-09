"""Regression controls for docs-signal-new-mechanism (pass on main; not part of Done when).

Run explicitly: uv run pytest -q tests/acceptance/guard_docs_signal_new_mechanism.py
"""

from datetime import timedelta

from docs_signal_world import (add_group_command, add_nested_command, add_place, command_paths,
                               docs_requests, docs_world, merge, tick)
from test_role_source import NOW
from test_skills_autosync import isolated_home  # noqa: F401 (fixture)

HOUR = timedelta(hours=1)


def test_a_merge_adding_neither_raises_no_docs_signal(tmp_path, monkeypatch):
    case = docs_world(tmp_path, monkeypatch)
    assert tick(case, NOW) == []
    merge(case, 1, NOW + HOUR, ["src/foragent/shelf.py", "src/foragent/commands/orch.py"])
    assert tick(case, NOW + HOUR + timedelta(minutes=5)) == []
    assert tick(case, NOW + 2 * HOUR) == []
    assert docs_requests(case) == []


def test_the_world_changes_the_place_list_and_command_tree_through_public_readers(tmp_path, monkeypatch):
    from foragent import shelf

    case = docs_world(tmp_path, monkeypatch)
    before = set(command_paths())
    assert "fa orch promise add" in before or "fa orch promise" in before, (
        "the command tree reader sees nested subcommands")
    add_place(case, "probeplaces")
    add_nested_command(case, "orch", "docsprobe")
    add_group_command(case, tmp_path, "docsprobegroup")
    try:
        assert "probeplaces" in [place.name for place in shelf.places()]
        assert set(command_paths()) - before == {"fa orch docsprobe", "fa docsprobegroup"} | {
            path for path in command_paths() if path.startswith("fa docsprobegroup ")}
    finally:
        case.forget()
