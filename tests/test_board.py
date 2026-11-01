from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import subprocess
from types import SimpleNamespace

import pytest

from foragent import board
from foragent.config import Config
from foragent.state import StateStore
from foragent.tracker import FakeTracker, GhTracker, RecordingTracker, TrackerError


@pytest.fixture(params=["fake", "gh"])
def recording_board(tmp_path, request):
    clock = [datetime(2026, 10, 23, tzinfo=UTC)]
    remote = FakeTracker(now=lambda: clock[0])
    issue = remote.add_issue(1, "Feature", labels=["blocked"])
    project_id = board.ensure(remote, "owner/repo")
    transport = remote
    if request.param == "gh":
        def run(args, input=None):
            payload = json.loads(input)
            return json.dumps(remote.graphql(payload["query"], payload["variables"]))

        transport = GhTracker("owner/repo", run)
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.record_issue(issue, {})
    ctx = SimpleNamespace(store=store, config=Config(board=project_id), repo="owner/repo")
    ctx.tracker = RecordingTracker(transport, ctx)
    return ctx, remote, clock


def test_board_field_writes_record_confirmed_values_and_timestamp(recording_board):
    ctx, remote, clock = recording_board
    clock[0] += timedelta(seconds=1)
    board.sync_status(ctx, 1, "PR open")
    row, = StateStore(ctx.store.root).local_issues()
    assert row.get("board_values") == {ctx.config.board: {"Status": board.board_column("PR open")}}
    assert row["updated_at"] == clock[0].isoformat()
    clock[0] += timedelta(seconds=1)
    board.add_issue(ctx.tracker, ctx.repo, ctx.config.board, 1, "main", "m")
    row, = StateStore(ctx.store.root).local_issues()
    assert row["board_values"][ctx.config.board] == item_values(remote, ctx.config.board, 1)
    assert row["board_values"][ctx.config.board] == {"Status": board.board_column("PR open"),
                                                     "Lane": "main", "Size": "m"}
    assert row["updated_at"] == clock[0].isoformat() and row["labels"] == ["blocked"]


@pytest.mark.parametrize("fail_at", [1, 2])
def test_board_field_failure_retains_only_confirmed_values(recording_board, monkeypatch, fail_at):
    ctx, remote, clock = recording_board
    before = ctx.store.local_issues()
    clock[0] += timedelta(seconds=1)
    graphql = remote.graphql
    writes = 0

    def fail(query, variables):
        nonlocal writes
        if "ForagentSetBoardValue" in query:
            writes += 1
            if writes == fail_at:
                return {"errors": [{"message": "Field update failed"}]}
        return graphql(query, variables)

    monkeypatch.setattr(remote, "graphql", fail)
    with pytest.raises(TrackerError, match="Field update failed"):
        board.add_issue(ctx.tracker, ctx.repo, ctx.config.board, 1, "main", "m")
    rows = StateStore(ctx.store.root).local_issues()
    if fail_at == 1:
        assert rows == before
    else:
        assert rows[0].get("board_values") == {ctx.config.board: {"Lane": "main"}}
        assert rows[0]["updated_at"] == clock[0].isoformat()


def snapshot(tracker, project_id):
    return tracker.graphql("""query($id: ID!) { node(id: $id) { ... on ProjectV2 {
      id title fields(first: 100) { nodes { ... on ProjectV2SingleSelectField { id name options { id name } } } }
      items(first: 100) { nodes { content { ... on Issue { number } }
        fieldValues(first: 100) { nodes { ... on ProjectV2ItemFieldSingleSelectValue {
          name field { ... on ProjectV2SingleSelectField { name } }
        } ... on ProjectV2ItemFieldTextValue {
          text field { ... on ProjectV2Field { name } }
        } } }
      } }
    } } }""", {"id": project_id})["data"]["node"]


def item_values(tracker, project_id, number):
    item = next(item for item in snapshot(tracker, project_id)["items"]["nodes"]
                if item["content"]["number"] == number)
    return {value["field"]["name"]: value["name"] for value in item["fieldValues"]["nodes"]}


def test_reuses_owner_board_and_preserves_option_ids_and_item_values_when_adding_lane():
    tracker = FakeTracker()
    tracker.add_issue(1)
    project_id = board.ensure(tracker, "team/repo", ["api"])
    context = board.ProjectContext(tracker, "team/repo", Config(board=project_id))
    board.add_issue(tracker, "team/repo", project_id, 1, "api", "m")
    board.sync_status(context, 1, "Ready")
    original = board.Project(tracker, "team/repo", project_id).fields()
    assert [option["name"] for option in original["Status"]["options"]] == list(board.COLUMNS)
    assert board.ensure(tracker, "team/repo", ["api", "web"]) == project_id
    assert board.ensure(tracker, "other/repo") != project_id
    updated = board.Project(tracker, "team/repo", project_id).fields()
    assert updated["Lane"]["options"][:2] == original["Lane"]["options"]
    assert updated["Status"] == original["Status"]
    board.add_issue(tracker, "team/repo", project_id, 1, "web", "l")
    assert len(snapshot(tracker, project_id)["items"]["nodes"]) == 1
    assert item_values(tracker, project_id, 1) == {"Lane": "web", "Size": "l", "Status": "🌰 Up next"}


@pytest.mark.parametrize("operation", ["discover", "saved_id", "set_status"])
def test_existing_status_options_and_card_values_survive_field_repairs(operation):
    tracker = FakeTracker()
    project_id = tracker.graphql("""mutation ForagentCreateBoard($input: CreateProjectV2Input!) {
      createProjectV2(input: $input) { projectV2 { id } }
    }""", {"input": {"ownerId": "O_team", "title": "Foragent: repo"}})["data"]["createProjectV2"]["projectV2"]["id"]
    project = board.Project(tracker, "team/repo", project_id)
    status = project.fields()["Status"]
    options = [*status["options"], {"name": "Paused", "color": "ORANGE", "description": "Waiting for review"}]
    status = tracker.graphql("""mutation ForagentUpdateBoardField($input: UpdateProjectV2FieldInput!) {
      updateProjectV2Field(input: $input) { projectV2Field {
        ... on ProjectV2SingleSelectField { id name options { id name color description } }
      } }
    }""", {"input": {"fieldId": status["id"], "singleSelectOptions": options}})["data"]["updateProjectV2Field"]["projectV2Field"]
    # Seed cards through GraphQL, before Foragent ever configures this board.
    for number, option in enumerate(status["options"], 1):
        tracker.add_issue(number)
        content = tracker.graphql("""query ForagentBoardIssue($owner: String!, $name: String!, $number: Int!) {
          repository(owner: $owner, name: $name) { issue(number: $number) { id } }
        }""", {"owner": "team", "name": "repo", "number": number})["data"]["repository"]["issue"]
        item = tracker.graphql("""mutation ForagentAddBoardItem($input: AddProjectV2ItemByIdInput!) {
          addProjectV2ItemById(input: $input) { item { id } }
        }""", {"input": {"projectId": project_id, "contentId": content["id"]}})["data"]["addProjectV2ItemById"]["item"]
        tracker.graphql("""mutation ForagentSetBoardValue($input: UpdateProjectV2ItemFieldValueInput!) {
          updateProjectV2ItemFieldValue(input: $input) { projectV2Item { id } }
        }""", {"input": {"projectId": project_id, "itemId": item["id"], "fieldId": status["id"],
                         "value": {"singleSelectOptionId": option["id"]}}})

    tracker.add_issue(10)
    context = board.ProjectContext(tracker, "team/repo", Config(board=project_id, board_map={"In progress": "In Progress"}))
    previous = None
    for _ in range(2):
        if operation == "set_status":
            board.sync_status(context, 10, "Running")
            assert item_values(tracker, project_id, 10) == {"Status": "In Progress"}
        else:
            saved_id = project_id if operation == "saved_id" else None
            assert board.ensure(tracker, "team/repo", ["api"], saved_id) == project_id
        updated = project.fields()["Status"]
        by_name = {option["name"]: option for option in updated["options"]}
        assert updated == status
        for number, option in enumerate(status["options"], 1):
            assert by_name[option["name"]] == option
            assert item_values(tracker, project_id, number) == {"Status": option["name"]}
        if previous is not None:
            assert updated == previous
        previous = updated


def test_owner_board_search_paginates_instead_of_creating_duplicate():
    tracker = FakeTracker()
    for number in range(101):
        tracker.graphql("""mutation ForagentCreateBoard($input: CreateProjectV2Input!) {
          createProjectV2(input: $input) { projectV2 { id } }
        }""", {"input": {"ownerId": "O_team", "title": f"Foragent: repo-{number}"}})
    calls = []
    graphql = tracker.graphql

    def recorded(query, variables):
        calls.append((query, variables))
        return graphql(query, variables)

    tracker.graphql = recorded
    project_id = board.ensure(tracker, "team/repo-100")
    assert snapshot(tracker, project_id)["title"] == "Foragent: repo-100"
    assert not any("mutation ForagentCreateBoard(" in query for query, _ in calls)
    assert any(variables.get("cursor") == "100" for _, variables in calls)


def test_partial_field_failure_keeps_board_identity_and_next_ensure_repairs(caplog):
    tracker = FakeTracker()
    graphql = tracker.graphql

    def fail_field(query, variables):
        if "mutation ForagentCreateBoardField" in query:
            raise OSError("project scope unavailable")
        return graphql(query, variables)

    tracker.graphql = fail_field
    project_id = board.ensure(tracker, "owner/repo")
    assert project_id and "Could not configure board" in caplog.text
    tracker.graphql = graphql
    assert board.ensure(tracker, "owner/repo", project_id=project_id) == project_id
    assert set(board.Project(tracker, "owner/repo", project_id).fields()) == {"Lane", "Size", "Status"}


def test_unconfigured_board_makes_no_transport_calls():
    tracker = FakeTracker()
    tracker.graphql = lambda *a: pytest.fail("board must be disabled")
    board.sync_status(board.ProjectContext(tracker, "owner/repo", Config()), 1, "Ready")
    board.add_issue(tracker, "owner/repo", None, 1, "main", "s")


def test_status_reads_persisted_card_and_repairs_manual_moves_without_duplicate_adds():
    tracker = FakeTracker()
    tracker.add_issue(1)
    project_id = board.ensure(tracker, "owner/repo")
    context = board.ProjectContext(tracker, "owner/repo", Config(board=project_id))
    board.add_issue(tracker, "owner/repo", project_id, 1)
    assert item_values(tracker, project_id, 1)["Status"] == board.board_column("unapproved")
    board.sync_status(context, 1, "Running")
    graphql = tracker.graphql
    calls = []

    def recorded(query, variables):
        calls.append(query)
        return graphql(query, variables)

    tracker.graphql = recorded
    # A fresh context simulates the next scheduler process, with no local cache.
    board.sync_status(replace(context), 1, "Blocked")
    assert not any("mutation" in query for query in calls)
    board.Project(tracker, "owner/repo", project_id).set_values(1, {"Status": board.board_column("Done")})
    calls.clear()
    board.sync_status(context, 1, "Running")
    assert sum("ForagentSetBoardValue" in query for query in calls) == 1
    assert not any("ForagentAddBoardItem" in query for query in calls)
    assert item_values(tracker, project_id, 1)["Status"] == board.board_column("Running")


def test_issue_project_membership_paginates():
    tracker = FakeTracker()
    tracker.add_issue(1)
    for index in range(101):
        project_id = board.ensure(tracker, f"owner/repo-{index}")
        # Use the same issue repository, with a different target project each time.
        board.add_issue(tracker, "owner/repo", project_id, 1)
    context = board.ProjectContext(tracker, "owner/repo", Config(board=project_id))
    board.sync_status(context, 1, "Running")
    assert item_values(tracker, project_id, 1)["Status"] == board.board_column("Running")


def test_missing_target_column_warns_without_changing_the_board(caplog):
    tracker = FakeTracker()
    tracker.add_issue(1)
    project_id = board.ensure(tracker, "owner/repo")
    board.add_issue(tracker, "owner/repo", project_id, 1)
    before = snapshot(tracker, project_id)
    config = Config(board=project_id, board_map={"In progress": "Removed column"})
    board.sync_status(board.ProjectContext(tracker, "owner/repo", config), 1, "Running")
    assert snapshot(tracker, project_id) == before
    assert len(caplog.records) == 1 and caplog.records[0].exc_info is None


def test_status_context_is_restored_after_failure_and_between_repositories(monkeypatch, caplog):
    tracker = FakeTracker()
    tracker.add_issue(1)
    first = board.ensure(tracker, "owner/one")
    second = board.ensure(tracker, "owner/two")
    context = board.ProjectContext(tracker, "owner/one", Config(board=first))
    original = board.set_status

    def failed(issue, status):
        raise OSError("offline")

    monkeypatch.setattr(board, "set_status", failed)
    board.sync_status(context, 1, "Ready")
    assert "Could not set board Status" in caplog.text
    monkeypatch.setattr(board, "set_status", original)
    board.sync_status(replace(context, repo="owner/two", config=Config(board=second)), 1, "Running")
    board.sync_status(context, 1, "Done")
    assert item_values(tracker, first, 1)["Status"] == "📦 Done"
    assert item_values(tracker, second, 1)["Status"] == "🐿 In progress"
    board.set_status(1, "Blocked")  # No repository context is left behind.
    assert item_values(tracker, first, 1)["Status"] == "📦 Done"


def test_gh_graphql_sends_typed_variables_over_stdin():
    calls = []
    payload = {"query": "query($n: Int!) { node(id: $n) { id } }", "variables": {"n": 7, "cursor": None}}

    def run(args, input=None):
        calls.append((args, json.loads(input)))
        return '{"data": {"node": {"id": "node-7"}}}'

    result = GhTracker("owner/repo", run).graphql(**payload)
    assert result == {"data": {"node": {"id": "node-7"}}}
    assert calls == [(["api", "graphql", "--input", "-"], payload)]


@pytest.mark.parametrize("response, message", [
    ('{"errors": [{"message": "missing project scope"}]}', "missing project scope"),
    ('{"data": {"node": null}, "errors": [{"message": "permission denied"}]}', "permission denied"),
    ('[]', "expected an object"), ('{}', "missing data"), ('not JSON', "invalid GraphQL JSON"),
])
def test_gh_graphql_rejects_errors_and_invalid_payloads(response, message):
    with pytest.raises(TrackerError, match=message):
        GhTracker("owner/repo", lambda *a, **kw: response).graphql("query { node { id } }", {})


@pytest.mark.parametrize("transport", ["gh", "graphql"])
def test_duplicate_board_addition_is_silent_success(transport, caplog):
    tracker = FakeTracker()
    tracker.add_issue(1)
    project_id = board.ensure(tracker, "owner/repo")
    graphql = tracker.graphql
    message = "Content already exists in this project"

    def run(args, input=None):
        payload = json.loads(input)
        query, variables = payload["query"], payload["variables"]
        result = graphql(query, variables)
        if "addProjectV2ItemById" in query:
            if transport == "gh":
                raise subprocess.CalledProcessError(1, args, stderr=f"gh: {message}")
            return json.dumps({"errors": [{"message": message}]})
        return json.dumps(result)

    tracker.graphql = GhTracker("owner/repo", run).graphql
    board.add_issue(tracker, "owner/repo", project_id, 1, "main", "s")
    assert len(snapshot(tracker, project_id)["items"]["nodes"]) == 1
    assert not caplog.records


@pytest.mark.parametrize("operation, message", [
    ("addProjectV2ItemById", "Resource not accessible (HTTP 403)"),
    ("ForagentBoardIssue", "Content already exists in this project"),
    ("ForagentSetBoardValue", "Content already exists in this project"),
])
def test_board_addition_propagates_other_github_errors(operation, message):
    tracker = FakeTracker()
    tracker.add_issue(1)
    project_id = board.ensure(tracker, "owner/repo")
    graphql = tracker.graphql

    def fail(query, variables):
        if operation in query:
            raise TrackerError(message)
        return graphql(query, variables)

    tracker.graphql = fail
    with pytest.raises(TrackerError) as error:
        board.add_issue(tracker, "owner/repo", project_id, 1, "main", "s")
    assert str(error.value) == message


def test_board_calls_only_when_the_wanted_card_value_differs(recording_board, monkeypatch):
    ctx, remote, clock = recording_board
    board.sync_status(ctx, 1, "Approved")
    calls = []
    graphql = remote.graphql

    def counted(query, variables):
        calls.append(query)
        return graphql(query, variables)

    monkeypatch.setattr(remote, "graphql", counted)
    board.sync_status(ctx, 1, "Approved")
    assert calls == []
    board.sync_status(ctx, 1, "Ready")  # Same 🌰 Up next column: the card stays.
    assert not any("ForagentSetBoardValue" in query for query in calls)
    board.sync_status(ctx, 1, "Running")
    assert any("ForagentSetBoardValue" in query for query in calls)
    assert item_values(remote, ctx.config.board, 1)["Status"] == board.board_column("Running")


def test_known_only_sync_moves_recorded_cards_and_never_adds_cards(recording_board, monkeypatch):
    ctx, remote, clock = recording_board
    calls = []
    graphql = remote.graphql

    def counted(query, variables):
        calls.append(query)
        return graphql(query, variables)

    monkeypatch.setattr(remote, "graphql", counted)
    board.sync_status(ctx, 1, "closed", existing_only=True)
    assert not any("ForagentAddBoardItem" in query or "ForagentSetBoardValue" in query for query in calls)
    assert snapshot(remote, ctx.config.board)["items"]["nodes"] == []
    calls.clear()
    board.sync_status(ctx, 1, "merged", existing_only=True)  # Same column, already checked.
    assert calls == [], "an issue checked for this column needs no further board call"
    board.sync_status(ctx, 1, "Ready")
    calls.clear()
    board.sync_status(ctx, 1, "closed", existing_only=True)
    assert item_values(remote, ctx.config.board, 1)["Status"] == board.board_column("closed")
    assert sum("ForagentSetBoardValue" in query for query in calls) == 1
    calls.clear()
    board.sync_status(ctx, 1, "merged", existing_only=True)  # Same 📦 Done column.
    assert calls == []


def test_existing_only_sync_adopts_cards_without_a_local_record(recording_board, monkeypatch):
    ctx, remote, clock = recording_board
    board.sync_status(ctx, 1, "Ready")
    ctx.store.update_local_issue(1, board_values={})  # A card placed before local records existed.
    writes = []
    graphql = remote.graphql

    def counted(query, variables):
        if "ForagentSetBoardValue" in query:
            writes.append(variables)
        return graphql(query, variables)

    monkeypatch.setattr(remote, "graphql", counted)
    board.sync_status(ctx, 1, "unapproved", existing_only=True)
    assert item_values(remote, ctx.config.board, 1)["Status"] == board.board_column("unapproved")
    assert len(writes) == 1
    board.sync_status(ctx, 1, "unapproved", existing_only=True)
    assert len(writes) == 1, "the moved card is recorded; a repeat sync writes nothing"
    board.sync_status(ctx, 1, "closed", existing_only=True)
    assert item_values(remote, ctx.config.board, 1)["Status"] == board.board_column("closed")


def test_confirmed_unchanged_card_is_recorded_so_the_next_sync_needs_no_call(recording_board, monkeypatch):
    ctx, remote, clock = recording_board
    board.sync_status(ctx, 1, "Ready")
    ctx.store.update_local_issue(1, board_values={})  # Forget the local record only.
    calls = []
    graphql = remote.graphql

    def counted(query, variables):
        calls.append(query)
        return graphql(query, variables)

    monkeypatch.setattr(remote, "graphql", counted)
    board.sync_status(ctx, 1, "Approved")  # Same 🌰 Up next column: read, no write.
    assert calls and not any("ForagentSetBoardValue" in query for query in calls)
    calls.clear()
    board.sync_status(ctx, 1, "Ready")
    assert calls == []


def test_refresh_cards_records_every_card_and_marks_issues_without_one(recording_board):
    ctx, remote, clock = recording_board
    remote.add_issue(2, "No card")
    ctx.store.record_issue(remote.get_issue(2), {})
    board.sync_status(ctx, 1, "Running")
    ctx.store.update_local_issue(1, board_values={})
    board.refresh_cards(ctx)
    assert ctx.tracker.board_value(1, ctx.config.board, "Status") == board.board_column("Running")
    assert ctx.tracker.board_value(2, ctx.config.board, "Status") == board.NO_CARD


def test_sync_status_shows_person_waits_in_your_turn_except_final_states(tmp_path, monkeypatch):
    """A waiting-person label or open question wins over work state; Done and closed do not change."""
    from foragent import orch_inbox

    store = StateStore(tmp_path / "state")
    monkeypatch.setattr(store, "local_issues", lambda: [
        {"number": 1, "state": "open", "labels": ["approved", "running", "waiting-person"]},
        {"number": 2, "state": "open", "labels": []},
        {"number": 3, "state": "open", "labels": ["approved", "ready"]},
    ])
    orch_inbox.ask(store, "Which one?", ["A", "B"], 1, [2])
    written = []
    monkeypatch.setattr(board, "set_status", lambda issue, status: written.append((issue, status)))
    ctx = SimpleNamespace(tracker=SimpleNamespace(), repo="owner/repo",
                          config=SimpleNamespace(board="PVT_1", board_map=None), store=store)
    for number, status in ((1, "Running"), (2, "Ready"), (3, "Ready"), (1, "Done"), (2, "closed")):
        board.sync_status(ctx, number, status)
    assert written == [(1, "Needs human"), (2, "Needs human"), (3, "Ready"), (1, "Done"), (2, "closed")]


WAITING = ("Reason: Locale choice pending\nNext condition: Record the locale on this issue\n"
           "Responsible role: orchestrator")


def waiting_value(remote, project_id, number=1):
    item = next(item for item in snapshot(remote, project_id)["items"]["nodes"]
                if item["content"]["number"] == number)
    return next((entry["text"] for entry in item["fieldValues"]["nodes"]
                 if entry["field"]["name"] == "Waiting"), None)


def record_wait(ctx, remote, body, *, closed=False):
    remote.update_issue(1, title="Feature", body=body)
    if closed:
        remote.close_issue(1)
    ctx.store.record_issue(remote.get_issue(1), {})


@pytest.mark.parametrize("closed", [False, True])
def test_waiting_card_projects_current_section_and_clears_when_removed_or_closed(recording_board, closed):
    ctx, remote, _ = recording_board
    board.add_issue(ctx.tracker, ctx.repo, ctx.config.board, 1)
    record_wait(ctx, remote, "## Goal\nShip\n## Waiting\n" + WAITING + "\n## Files\nsrc/app.py\n")
    board.refresh_cards(ctx)
    assert waiting_value(remote, ctx.config.board) == WAITING
    assert ctx.tracker.board_value(1, ctx.config.board, "Waiting") == WAITING
    changed = WAITING.replace("Locale choice pending", "Catalog owner deciding")
    record_wait(ctx, remote, "## Waiting\n" + changed)
    board.refresh_cards(ctx)
    assert waiting_value(remote, ctx.config.board) == changed
    record_wait(ctx, remote, "## Waiting\n" + changed if closed else "## Goal\nReady", closed=closed)
    board.refresh_cards(ctx)
    assert waiting_value(remote, ctx.config.board) is None
    assert ctx.tracker.board_value(1, ctx.config.board, "Waiting") == ""


def test_waiting_projection_is_lazy_existing_only_and_unchanged_read_costs_one_call(recording_board, monkeypatch):
    ctx, remote, _ = recording_board
    record_wait(ctx, remote, "## Waiting\n" + WAITING)
    board.refresh_cards(ctx)  # A wait alone never adds a card or field.
    assert snapshot(remote, ctx.config.board)["items"]["nodes"] == []
    assert "Waiting" not in board.Project(remote, ctx.repo, ctx.config.board).fields()
    board.add_issue(ctx.tracker, ctx.repo, ctx.config.board, 1)
    other = remote.add_issue(2, "Other", "## Waiting\n" + WAITING)
    ctx.store.record_issue(other, {})
    board.add_issue(ctx.tracker, ctx.repo, ctx.config.board, 2)
    calls = []
    graphql = remote.graphql

    def counted(query, variables):
        calls.append(query)
        return graphql(query, variables)

    monkeypatch.setattr(remote, "graphql", counted)
    board.refresh_cards(ctx)
    assert sum("ForagentCreateBoardField" in query for query in calls) == 1
    assert sum("ForagentBoardFields" in query for query in calls) == 1
    assert sum("ForagentSetBoardValue" in query for query in calls) == 2
    calls.clear()
    board.refresh_cards(ctx)
    assert len(calls) == 1 and "ForagentBoardCards" in calls[0]
    assert waiting_value(remote, ctx.config.board) == WAITING
    assert waiting_value(remote, ctx.config.board, 2) == WAITING


@pytest.mark.parametrize("rate_limited", [False, True])
@pytest.mark.parametrize("clear", [False, True])
def test_waiting_failed_writes_are_not_recorded_and_other_cards_continue(
    recording_board, monkeypatch, caplog, rate_limited, clear,
):
    ctx, remote, _ = recording_board
    board.add_issue(ctx.tracker, ctx.repo, ctx.config.board, 1)
    record_wait(ctx, remote, "## Waiting\n" + WAITING)
    if clear:
        board.refresh_cards(ctx)
        record_wait(ctx, remote, "## Goal\nReady")
    other = remote.add_issue(2, "Other", "## Waiting\n" + WAITING)
    ctx.store.record_issue(other, {})
    board.add_issue(ctx.tracker, ctx.repo, ctx.config.board, 2)
    graphql = remote.graphql
    error = TrackerError("API rate limit exceeded" if rate_limited else "Field update failed")

    def fail(query, variables):
        if ("ForagentClearBoardValue" if clear else "ForagentSetBoardValue") in query:
            raise error
        return graphql(query, variables)

    monkeypatch.setattr(remote, "graphql", fail)
    if rate_limited:
        with pytest.raises(TrackerError):
            board.refresh_cards(ctx)
    else:
        board.refresh_cards(ctx)
        assert "Could not set board Waiting" in caplog.text
    assert ctx.tracker.board_value(1, ctx.config.board, "Waiting") == (WAITING if clear else None)
    assert waiting_value(remote, ctx.config.board) == (WAITING if clear else None)
    if clear and not rate_limited:
        assert waiting_value(remote, ctx.config.board, 2) == WAITING
    monkeypatch.setattr(remote, "graphql", graphql)
    board.refresh_cards(ctx)
    assert waiting_value(remote, ctx.config.board) == (None if clear else WAITING)


def test_waiting_refresh_repairs_stale_board_after_projection_cache_is_lost(recording_board):
    ctx, remote, _ = recording_board
    board.add_issue(ctx.tracker, ctx.repo, ctx.config.board, 1)
    record_wait(ctx, remote, "## Waiting\n" + WAITING)
    board.refresh_cards(ctx)
    record_wait(ctx, remote, "## Goal\nReady")
    ctx.store.update_local_issue(1, board_values={})
    board.refresh_cards(ctx)
    assert waiting_value(remote, ctx.config.board) is None
    assert ctx.tracker.board_value(1, ctx.config.board, "Waiting") == ""


def test_waiting_refresh_uses_issue_body_to_repair_a_manual_board_edit(recording_board):
    ctx, remote, _ = recording_board
    board.add_issue(ctx.tracker, ctx.repo, ctx.config.board, 1)
    record_wait(ctx, remote, "## Waiting\n" + WAITING)
    board.refresh_cards(ctx)
    project = board.Project(remote, ctx.repo, ctx.config.board)
    field = project.fields()["Waiting"]
    item, = snapshot(remote, ctx.config.board)["items"]["nodes"]
    remote.graphql("""mutation ForagentSetBoardValue($input: UpdateProjectV2ItemFieldValueInput!) {
      updateProjectV2ItemFieldValue(input: $input) { projectV2Item { id } }
    }""", {"input": {"projectId": project.id, "itemId": item["id"], "fieldId": field["id"],
                     "value": {"text": "obsolete board-only reason"}}})
    assert ctx.tracker.board_value(1, project.id, "Waiting") == WAITING
    board.refresh_cards(ctx)
    assert waiting_value(remote, project.id) == WAITING


def test_moving_an_existing_card_takes_two_calls_with_a_retry_that_fits_one_bounded_tick(monkeypatch):
    """A third call would never fit two four-second calls per tick, and a retry starts over."""
    tracker = FakeTracker()
    tracker.add_issue(1)
    project_id = board.ensure(tracker, "owner/repo")
    board.add_issue(tracker, "owner/repo", project_id, 1)
    project = board.Project(tracker, "owner/repo", project_id)
    project.set_values(1, {"Status": board.board_column("Ready")})
    calls = []
    graphql = tracker.graphql

    def counted(query, variables):
        calls.append(query)
        return graphql(query, variables)

    monkeypatch.setattr(tracker, "graphql", counted)
    column = board.board_column("Running")
    project.set_values(1, {"Status": column})
    assert len(calls) == 2 and "ForagentBoardIssue" in calls[0] and "ForagentSetBoardValue" in calls[1]
    assert any(value.get("name") == column for value in
               snapshot(tracker, project_id)["items"]["nodes"][0]["fieldValues"]["nodes"])


def test_moving_a_card_reads_all_fields_when_one_read_cannot_carry_them(monkeypatch):
    tracker = FakeTracker()
    tracker.add_issue(1)
    project_id = board.ensure(tracker, "owner/repo")
    board.add_issue(tracker, "owner/repo", project_id, 1)
    graphql = tracker.graphql
    calls = []

    def more_fields(query, variables):
        calls.append(query)
        result = graphql(query, variables)
        if "ForagentBoardIssue" in query:
            result["data"]["node"]["fields"]["pageInfo"]["hasNextPage"] = True
            result["data"]["node"]["fields"]["nodes"] = []
        return result

    monkeypatch.setattr(tracker, "graphql", more_fields)
    column = board.board_column("Running")
    board.Project(tracker, "owner/repo", project_id).set_values(1, {"Status": column})
    assert any("ForagentBoardFields" in query for query in calls)
    assert any(value.get("name") == column for value in
               snapshot(tracker, project_id)["items"]["nodes"][0]["fieldValues"]["nodes"])


def test_adding_a_card_keeps_its_progress_when_the_budget_ends_before_the_status_write(monkeypatch):
    tracker = FakeTracker()
    tracker.add_issue(1)
    project_id = board.ensure(tracker, "owner/repo")
    graphql = tracker.graphql
    calls = []

    def out_of_time(query, variables):
        calls.append(query)
        if "ForagentSetBoardValue" in query:
            raise TrackerError("gh time budget exhausted")
        return graphql(query, variables)

    monkeypatch.setattr(tracker, "graphql", out_of_time)
    column = board.board_column("Running")
    with pytest.raises(TrackerError):
        board.Project(tracker, "owner/repo", project_id).set_values(1, {"Status": column})
    assert len(calls) == 3 and "ForagentAddBoardItem" in calls[1]
    calls.clear()
    monkeypatch.setattr(tracker, "graphql", lambda query, variables: (calls.append(query), graphql(query, variables))[1])
    board.Project(tracker, "owner/repo", project_id).set_values(1, {"Status": column})
    assert len(calls) == 2 and not any("ForagentAddBoardItem" in query for query in calls), "The card stays added"


def _paged_card_tracker(tmp_path):
    from foragent.tracker import RecordingTracker

    remote = FakeTracker()
    remote.add_issue(1)
    project_id = board.ensure(remote, "owner/repo")
    board.add_issue(remote, "owner/repo", project_id, 1)
    store = StateStore(tmp_path / "state")
    context = SimpleNamespace(store=store, config=SimpleNamespace(lanes={}), now=lambda: datetime.now(UTC))
    tracker = RecordingTracker(remote, context)
    tracker.get_issue(1)
    return remote, tracker, store, project_id


def _second_page(graphql, calls):
    def paged(query, variables):
        calls.append(query)
        if "ForagentBoardIssue" not in query:
            return graphql(query, variables)
        if variables.get("cursor") is None:
            result = graphql(query, variables)
            result["data"]["repository"]["issue"]["projectItems"] = {
                "nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "2"}}
            return result
        return graphql(query, {**variables, "cursor": None})
    return paged


def test_card_on_a_later_page_is_kept_only_after_extra_reads(monkeypatch, tmp_path):
    remote, tracker, store, project_id = _paged_card_tracker(tmp_path)
    project = board.Project(tracker, "owner/repo", project_id)
    project.set_values(1, {"Status": board.board_column("Ready")})
    assert store.local_board_value(1, project_id, board.CARD_ITEM) is None, "A first-page card keeps nothing"
    calls = []
    monkeypatch.setattr(remote, "graphql", _second_page(remote.graphql, calls))
    project.set_values(1, {"Status": board.board_column("Running")})
    assert len(calls) == 3 and store.local_board_value(1, project_id, board.CARD_ITEM)
    calls.clear()
    column = board.board_column("Needs human")
    project.set_values(1, {"Status": column})
    assert len(calls) == 2 and "ForagentSetBoardValue" in calls[1], "The retry skips the extra read"
    assert any(value.get("name") == column for value in
               snapshot(remote, project_id)["items"]["nodes"][0]["fieldValues"]["nodes"])


def test_board_read_corrects_a_kept_card(tmp_path):
    remote, tracker, store, project_id = _paged_card_tracker(tmp_path)
    ctx = SimpleNamespace(tracker=tracker, repo="owner/repo", store=store, config=SimpleNamespace(board=project_id))
    store.record_board_value(1, project_id, board.CARD_ITEM, "PVTI_old")
    board.refresh_cards(ctx)
    current = store.local_board_value(1, project_id, board.CARD_ITEM)
    assert current not in (None, "PVTI_old"), "The board read shows the card's real id"
    remote._projects.projects[project_id]["items"].clear()
    board.refresh_cards(ctx)
    assert store.local_board_value(1, project_id, board.CARD_ITEM) is None, "A removed card is forgotten"


def test_search_continues_from_the_page_it_reached_and_a_board_read_ends_it(monkeypatch, tmp_path):
    remote, tracker, store, project_id = _paged_card_tracker(tmp_path)
    project = board.Project(tracker, "owner/repo", project_id)
    graphql, seen = remote.graphql, []

    def paged(query, variables):
        if "ForagentBoardIssue" not in query:
            return graphql(query, variables)
        seen.append(variables.get("cursor"))
        if variables.get("cursor") == "3":
            raise TrackerError("gh time budget exhausted")
        result = graphql(query, {**variables, "cursor": None})
        reached = int(variables.get("cursor") or 1)
        result["data"]["repository"]["issue"]["projectItems"] = {
            "nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": str(reached + 1)}}
        return result

    monkeypatch.setattr(remote, "graphql", paged)
    for _ in range(2):
        with pytest.raises(TrackerError):
            project.set_values(1, {"Status": board.board_column("Running")})
    assert seen == [None, "2", "3", "3"], "The retry starts where the search stopped"
    assert store.local_board_value(1, project_id, board.CARD_CURSOR) == "3"
    monkeypatch.setattr(remote, "graphql", graphql)
    ctx = SimpleNamespace(tracker=tracker, repo="owner/repo", store=store, config=SimpleNamespace(board=project_id))
    board.refresh_cards(ctx)
    assert store.local_board_value(1, project_id, board.CARD_CURSOR) is None
    assert store.local_board_value(1, project_id, board.CARD_ITEM), "The board read answered the search"


def test_finished_search_keeps_the_card_and_leaves_no_position(monkeypatch, tmp_path):
    remote, tracker, store, project_id = _paged_card_tracker(tmp_path)
    monkeypatch.setattr(remote, "graphql", _second_page(remote.graphql, []))
    board.Project(tracker, "owner/repo", project_id).set_values(1, {"Status": board.board_column("Running")})
    assert store.local_board_value(1, project_id, board.CARD_CURSOR) is None
    assert store.local_board_value(1, project_id, board.CARD_ITEM)


def test_adding_a_card_after_a_long_search_keeps_it_and_ends_the_search(monkeypatch, tmp_path):
    remote, tracker, store, project_id = _paged_card_tracker(tmp_path)
    remote._projects.projects[project_id]["items"].clear()
    graphql = remote.graphql

    def paged(query, variables):
        result = graphql(query, {**variables, "cursor": None} if "ForagentBoardIssue" in query else variables)
        if "ForagentBoardIssue" in query and variables.get("cursor") is None:
            result["data"]["repository"]["issue"]["projectItems"]["pageInfo"] = {"hasNextPage": True, "endCursor": "2"}
        return result

    monkeypatch.setattr(remote, "graphql", paged)
    board.Project(tracker, "owner/repo", project_id).set_values(1, {"Status": board.board_column("Running")})
    items = remote._projects.projects[project_id]["items"]
    assert list(items) == [store.local_board_value(1, project_id, board.CARD_ITEM)], "The added card is kept"
    assert store.local_board_value(1, project_id, board.CARD_CURSOR) is None

