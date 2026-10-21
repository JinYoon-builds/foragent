from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from foragent import board
from foragent.config import Config
from foragent.tracker import FakeTracker, GhTracker, TrackerError


SNAPSHOT = """query($id: ID!) { node(id: $id) { ... on ProjectV2 {
  id title fields(first: 100) { nodes { ... on ProjectV2SingleSelectField {
    id name options { id name color description }
  } } }
  items(first: 100) { nodes { content { ... on Issue { number } }
    fieldValues(first: 100) { nodes { ... on ProjectV2ItemFieldSingleSelectValue {
      name field { ... on ProjectV2SingleSelectField { name } }
    } } }
  } }
} } }"""


def snapshot(tracker, project_id):
    return tracker.graphql(SNAPSHOT, {"id": project_id})["data"]["node"]


def item_values(tracker, project_id, number=1):
    item = next(item for item in snapshot(tracker, project_id)["items"]["nodes"]
                if item["content"]["number"] == number)
    return {value["field"]["name"]: value["name"] for value in item["fieldValues"]["nodes"]}


@pytest.fixture
def project():
    tracker = FakeTracker()
    config = Config(lanes={"api": {"paths": ["api/**"]}})
    project_id = board.ensure_board(tracker, "owner/repo", config)
    config = replace(config, board=project_id)
    tracker.add_issue(1, labels=["lane:api", "size:s"])
    return SimpleNamespace(config=config, tracker=tracker, repo="owner/repo")


def test_owner_board_is_reused_even_without_a_saved_id_and_is_separate_for_other_owners(project):
    ctx = project
    assert board.ensure_board(ctx.tracker, ctx.repo, Config()) == ctx.config.board
    other = board.ensure_board(ctx.tracker, "organization/repo", Config())
    assert other != ctx.config.board
    created = [values["input"] for query, values in ctx.tracker.graphql_calls if "createProjectV2(" in query]
    assert [values["ownerId"] for values in created] == ["OWNER_owner", "OWNER_organization"]
    assert len({field["name"] for field in snapshot(ctx.tracker, ctx.config.board)["fields"]["nodes"]}) == 3


def test_new_lane_preserves_existing_item_values_and_option_ids(project):
    ctx = project
    board.add_issue(ctx.config, ctx.tracker, ctx.repo, 1, "api", "s")
    original = next(field for field in snapshot(ctx.tracker, ctx.config.board)["fields"]["nodes"]
                    if field["name"] == "Lane")["options"]
    ctx.tracker.add_issue(2)
    board.add_issue(ctx.config, ctx.tracker, ctx.repo, 2, "web", "l")
    board.add_issue(ctx.config, ctx.tracker, ctx.repo, 1, "api", "m")
    fields = snapshot(ctx.tracker, ctx.config.board)["fields"]["nodes"]
    options = next(field for field in fields if field["name"] == "Lane")["options"]
    assert options[:len(original)] == original
    assert item_values(ctx.tracker, ctx.config.board) == {"Lane": "api", "Size": "m"}
    assert item_values(ctx.tracker, ctx.config.board, 2) == {"Lane": "web", "Size": "l"}
    assert len(snapshot(ctx.tracker, ctx.config.board)["items"]["nodes"]) == 2


@pytest.mark.parametrize("status", board.STATUSES)
def test_status_values_round_trip_without_changing_issue_labels(project, status):
    ctx = project
    before = ctx.tracker.get_issue(1)
    board.sync_status(ctx, before, status)
    assert item_values(ctx.tracker, ctx.config.board)["Status"] == status
    assert ctx.tracker.get_issue(1) == before


def test_project_binding_does_not_leak_between_operations(project):
    ctx = project
    board.sync_status(ctx, 1, "Ready")
    board.set_status(1, "Running")  # No project bound outside an operation.
    assert item_values(ctx.tracker, ctx.config.board)["Status"] == "Ready"
    other = SimpleNamespace(config=Config(), tracker=ctx.tracker, repo=ctx.repo)
    calls = len(ctx.tracker.graphql_calls)
    board.sync_status(other, 1, "Done")
    board.add_issue(other.config, other.tracker, other.repo, 1, "main", "s")
    assert len(ctx.tracker.graphql_calls) == calls


def test_board_disappearing_logs_but_does_not_raise(project, caplog):
    ctx = project
    ctx.config = replace(ctx.config, board="deleted")
    board.sync_status(ctx, 1, "Running")
    board.add_issue(ctx.config, ctx.tracker, ctx.repo, 1, "api", "s")
    assert "Board status update failed" in caplog.text and "Board item update failed" in caplog.text


def test_board_and_field_queries_follow_cursors(project):
    ctx = project
    original = ctx.tracker.graphql
    cursors = []

    def paged(query, variables):
        if "repositoryOwner(" in query or "fields(first:" in query:
            cursors.append(variables.get("after"))
            if not variables.get("after"):
                connection = {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "later"}}
                if "repositoryOwner(" in query:
                    return {"data": {"repositoryOwner": {"id": "OWNER_owner", "projectsV2": connection}}}
                return {"data": {"node": {"fields": connection}}}
            variables = {**variables, "after": None}
        return original(query, variables)

    ctx.tracker.graphql = paged
    assert board.ensure_board(ctx.tracker, ctx.repo, Config()) == ctx.config.board
    assert cursors == [None, "later", None, "later"]


def test_gh_graphql_posts_typed_variables_and_multiline_query_as_json():
    calls = []
    query = "mutation($input: CreateProjectV2Input!) {\n createProjectV2(input: $input) { projectV2 { id } }\n}"
    variables = {"input": {"title": "Foragent: repo", "ownerId": "OWNER"}, "after": None, "number": 7}
    result = {"data": {"createProjectV2": {"projectV2": {"id": "PVT_1"}}}}

    def run(args, input=None):
        calls.append((args, json.loads(input)))
        return json.dumps(result)

    assert GhTracker("owner/repo", run).graphql(query, variables) == result
    assert calls == [(["api", "graphql", "--input", "-"], {"query": query, "variables": variables})]


@pytest.mark.parametrize("response", ["not JSON", "[]", '{"data": null}',
                                       '{"data": {}, "errors": [{"message": "Forbidden"}]}'])
def test_gh_graphql_rejects_errors_and_malformed_data(response):
    with pytest.raises(TrackerError, match="GraphQL"):
        GhTracker("owner/repo", lambda *a, **kw: response).graphql("query { viewer { id } }", {})
