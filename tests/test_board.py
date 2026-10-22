from dataclasses import replace
import json
import subprocess

import pytest

from foragent import board
from foragent.config import Config
from foragent.tracker import FakeTracker, GhTracker, TrackerError


def snapshot(tracker, project_id):
    return tracker.graphql("""query($id: ID!) { node(id: $id) { ... on ProjectV2 {
      id title fields(first: 100) { nodes { ... on ProjectV2SingleSelectField { id name options { id name } } } }
      items(first: 100) { nodes { content { ... on Issue { number } }
        fieldValues(first: 100) { nodes { ... on ProjectV2ItemFieldSingleSelectValue {
          name field { ... on ProjectV2SingleSelectField { name } }
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
    assert [option["name"] for option in original["Status"]["options"]] == list(board.STATUSES)
    assert board.ensure(tracker, "team/repo", ["api", "web"]) == project_id
    assert board.ensure(tracker, "other/repo") != project_id
    updated = board.Project(tracker, "team/repo", project_id).fields()
    assert updated["Lane"]["options"][:2] == original["Lane"]["options"]
    assert updated["Status"] == original["Status"]
    board.add_issue(tracker, "team/repo", project_id, 1, "web", "l")
    assert len(snapshot(tracker, project_id)["items"]["nodes"]) == 1
    assert item_values(tracker, project_id, 1) == {"Lane": "web", "Size": "l", "Status": "Ready"}


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
    context = board.ProjectContext(tracker, "team/repo", Config(board=project_id))
    previous = None
    for _ in range(2):
        if operation == "set_status":
            board.sync_status(context, 10, "Running")
            assert item_values(tracker, project_id, 10) == {"Status": "Running"}
        else:
            saved_id = project_id if operation == "saved_id" else None
            assert board.ensure(tracker, "team/repo", ["api"], saved_id) == project_id
        updated = project.fields()["Status"]
        by_name = {option["name"]: option for option in updated["options"]}
        assert [option["name"] for option in updated["options"][:7]] == list(board.STATUSES)
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
    assert item_values(tracker, first, 1)["Status"] == "Done"
    assert item_values(tracker, second, 1)["Status"] == "Running"
    board.set_status(1, "Blocked")  # No repository context is left behind.
    assert item_values(tracker, first, 1)["Status"] == "Done"


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
