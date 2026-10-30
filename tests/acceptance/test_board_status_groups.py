"""Acceptance test for board-status-groups. Locked by Foragent; do not edit."""

import json
import logging

import pytest

from test_board_projects import board_snapshot, initialized_case
from test_skills_autosync import cli_exit, isolated_home


COLUMNS = ["🗂 Backlog", "🌰 Up next", "🐿 In progress", "✋ Your turn", "📦 Done"]
TEAM_MAP = {"Backlog": "Todo", "Up next": "Todo", "In progress": "Doing",
            "Your turn": "Review", "Done": "Done"}
MAP_FLAG = "Backlog=Todo,Up next=Todo,In progress=Doing,Your turn=Review,Done=Done"


@pytest.fixture(autouse=True)
def offline_github(monkeypatch, isolated_home):
    from test_local_view import record_gh

    calls = record_gh(monkeypatch)
    yield
    assert calls == [], "Board tests must use the injected offline gh runner"


def record_board_gh(tracker, monkeypatch):
    """Exercise GhTracker's public GraphQL transport against the fake board."""
    from foragent.tracker import GhTracker

    graphql = tracker.graphql
    calls = []

    def run(argv, input=None):
        assert argv == ["api", "graphql", "--input", "-"]
        payload = json.loads(input)
        calls.append(payload)
        return json.dumps(graphql(payload["query"], payload["variables"]))

    monkeypatch.setattr(tracker, "graphql", GhTracker("owner/repo", run=run).graphql)
    return calls


def column_updates(calls):
    return [call for call in calls if "updateProjectV2ItemFieldValue(" in call["query"]]


def status_field(tracker, board_id):
    from foragent.board import Project

    return Project(tracker, "owner/repo", board_id).fields()["Status"]


def team_columns(tracker, board_id):
    """Seed a team's existing options through GraphQL, without Foragent repairs."""
    tracker.graphql("""mutation ForagentUpdateBoardField($input: UpdateProjectV2FieldInput!) {
      updateProjectV2Field(input: $input) { projectV2Field {
        ... on ProjectV2SingleSelectField { id name options { id name } }
      } }
    }""", {"input": {"fieldId": status_field(tracker, board_id)["id"],
                     "singleSelectOptions": [{"name": name, "color": "GRAY", "description": ""}
                                             for name in ("Todo", "Doing", "Review", "Done")]}})


def seed_card(tracker, board_id, number, column):
    """Seed existing card membership and value at the fake GraphQL boundary."""
    field = status_field(tracker, board_id)
    option = next(option for option in field["options"] if option["name"] == column)
    item = tracker.graphql("""mutation ForagentAddBoardItem($input: AddProjectV2ItemByIdInput!) {
      addProjectV2ItemById(input: $input) { item { id } }
    }""", {"input": {"projectId": board_id, "contentId": f"I_owner/repo#{number}"}})
    item_id = item["data"]["addProjectV2ItemById"]["item"]["id"]
    tracker.graphql("""mutation ForagentSetBoardValue($input: UpdateProjectV2ItemFieldValueInput!) {
      updateProjectV2ItemFieldValue(input: $input) { projectV2Item { id } }
    }""", {"input": {"projectId": board_id, "itemId": item_id, "fieldId": field["id"],
                     "value": {"singleSelectOptionId": option["id"]}}})


def card_column(tracker, board_id, number):
    project = board_snapshot(tracker, board_id)
    item = next(item for item in project["items"]["nodes"] if item["content"]["number"] == number)
    return next(value["name"] for value in item["fieldValues"]["nodes"]
                if value["field"]["name"] == "Status")


def test_board_status_groups_public_mapping_and_blocked_warning():
    from foragent.board import board_column, card_warning

    expected = {"unapproved": COLUMNS[0], "Approved": COLUMNS[1], "Ready": COLUMNS[1],
                "Running": COLUMNS[2], "PR open": COLUMNS[2], "Blocked": COLUMNS[2],
                "Needs human": COLUMNS[3], "Done": COLUMNS[4],
                "merged": COLUMNS[4], "closed": COLUMNS[4]}
    for state, column in expected.items():
        assert board_column(state) == column, state
        assert card_warning(state) is (state == "Blocked"), state


def test_board_status_groups_init_creates_exactly_five_ordered_options(
    git_history, tmp_path, monkeypatch,
):
    case, main, argv, config = initialized_case(git_history, tmp_path, monkeypatch)
    field = status_field(case.tracker, config["board"])
    assert [option["name"] for option in field["options"]] == COLUMNS
    assert main(argv) == 0
    assert status_field(case.tracker, config["board"]) == field


def test_board_status_groups_existing_team_board_saves_mapping_once_and_uses_it(
    git_history, tmp_path, monkeypatch, isolated_home,
):
    import yaml

    from foragent.board import ProjectContext, sync_status
    from foragent.config import load_config

    case, main, argv, config = initialized_case(git_history, tmp_path, monkeypatch)
    board_id = config["board"]
    team_columns(case.tracker, board_id)
    case.tracker.add_issue(20, "Existing team card")
    seed_card(case.tracker, board_id, 20, "Review")
    original = status_field(case.tracker, board_id)
    user_config = isolated_home / ".config/foragent/config.yml"
    user_before = user_config.read_bytes()

    def no_board_question(prompt="", *args):
        # fa init may ask once about upstream reports (a personal setting); the
        # board mapping itself must never be asked again.
        if "upstream report" in str(prompt).lower():
            return "n"
        pytest.fail("--board-map supplies the answer; persisted mappings must not be asked again")

    monkeypatch.setattr("builtins.input", no_board_question)

    assert cli_exit(main, [*argv, "--board-map", MAP_FLAG]) == 0, "fa init must accept --board-map"
    saved = yaml.safe_load((case.root / ".foragent.yml").read_text())
    assert saved["board"] == board_id and saved["board_map"] == TEAM_MAP
    assert main(argv) == 0
    assert yaml.safe_load((case.root / ".foragent.yml").read_text()) == saved
    def team_view(raw):
        # The personal upstream-report answer may be saved; nothing else may change.
        values = yaml.safe_load(raw) or {}
        values.pop("upstream_reports", None)
        return values

    assert team_view(user_config.read_bytes()) == team_view(user_before), "board_map belongs to the team"
    assert status_field(case.tracker, board_id) == original
    assert card_column(case.tracker, board_id, 20) == "Review"

    # Reload the saved configuration so an init-only, in-memory map cannot pass.
    context = ProjectContext(case.tracker, "owner/repo", load_config(case.root))
    for state, column in (("unapproved", "Todo"), ("Approved", "Todo"), ("Ready", "Todo"),
                          ("Running", "Doing"), ("PR open", "Doing"), ("Blocked", "Doing"),
                          ("Needs human", "Review"), ("Done", "Done")):
        issue = case.tracker.get_issue(1)
        sync_status(context, issue, state)
        assert card_column(case.tracker, board_id, 1) == column, state
        assert case.tracker.get_issue(1) == issue, "Board grouping must not change gate labels"
    assert status_field(case.tracker, board_id) == original
    assert card_column(case.tracker, board_id, 20) == "Review"


def test_board_status_groups_ticks_write_only_when_the_human_column_changes(
    git_history, tmp_path, monkeypatch,
):
    from dataclasses import replace
    from datetime import timedelta

    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context

    case, main, argv, config = initialized_case(git_history, tmp_path, monkeypatch)
    board_id = config["board"]
    case.ctx.config = replace(case.config, board=board_id)
    case.store.finish_run(1, outcome="blocked")
    case.tracker.remove_labels(1, ["running", "approved"])
    case.tracker.record_label(1, "approved", "human")
    calls = record_board_gh(case.tracker, monkeypatch)
    context = tick_context(case, start_runs=False)
    assert tick(context).skipped is None  # Establish the initial Ready card once.
    columns = [card_column(case.tracker, board_id, 1)]
    assert case.tracker.get_issue(1).labels == {"approved", "ready"}
    observed = {}

    calls.clear()
    for _ in range(2):
        assert tick(context).skipped is None
    observed["Ready repeats"] = len(column_updates(calls))

    calls.clear()
    # A fake launcher returning no PID avoids probing or starting real processes.
    assert tick(replace(context, start_runs=True, launcher=lambda number: None)).skipped is None
    observed["Ready to Running"] = len(column_updates(calls))
    columns.append(card_column(case.tracker, board_id, 1))
    assert case.tracker.get_issue(1).labels == {"approved", "running"}

    calls.clear()
    for _ in range(2):
        assert tick(context).skipped is None
    observed["Running repeats"] = len(column_updates(calls))

    pr = case.tracker.create_pr(head="foragent/issue-1", base="main", title="Feature", body="Closes #1")
    case.store.finish_run(1, outcome="pr-open")
    case.tracker.remove_labels(1, ["running"])
    case.clock[0] += timedelta(minutes=5, seconds=1)
    calls.clear()
    assert tick(context).skipped is None
    observed["Running to PR open"] = len(column_updates(calls))
    columns.append(card_column(case.tracker, board_id, 1))
    assert case.tracker.get_issue(1).labels == {"approved"}
    assert case.tracker.find_pr("foragent/issue-1") == pr

    assert observed == {"Ready repeats": 0, "Ready to Running": 1,
                        "Running repeats": 0, "Running to PR open": 0}
    assert columns == [COLUMNS[1], COLUMNS[2], COLUMNS[2]]


def test_board_status_groups_missing_custom_group_warns_once_and_skips_the_card(
    monkeypatch, caplog, capsys,
):
    from types import SimpleNamespace

    from foragent.board import ProjectContext, ensure, sync_status
    from foragent.tracker import FakeTracker

    tracker = FakeTracker()
    tracker.add_issue(1, "Working card", labels=["approved", "running"])
    board_id = ensure(tracker, "owner/repo")
    team_columns(tracker, board_id)
    seed_card(tracker, board_id, 1, "Todo")
    before = board_snapshot(tracker, board_id)
    # Exercise the public BoardContext protocol independently of config parsing.
    config = SimpleNamespace(board=board_id, board_map={key: value for key, value in TEAM_MAP.items()
                                                      if key != "In progress"})
    context = ProjectContext(tracker, "owner/repo", config)
    calls = record_board_gh(tracker, monkeypatch)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        sync_status(context, tracker.get_issue(1), "Running")

    assert column_updates(calls) == [], "An unmapped group must not update the board"
    assert board_snapshot(tracker, board_id) == before
    assert tracker.get_issue(1).labels == {"approved", "running"}
    warnings = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]
    warnings += capsys.readouterr().err.splitlines()
    assert len(warnings) == 1 and len(warnings[0].splitlines()) == 1
    assert "In progress" in warnings[0] or "Running" in warnings[0]
    assert all(record.exc_info is None for record in caplog.records)
