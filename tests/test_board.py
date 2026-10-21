from dataclasses import replace
from types import SimpleNamespace

from foragent import board
from foragent.config import Config
from foragent.tracker import FakeTracker


def snapshot(tracker, identity):
    return tracker.graphql("""query($id: ID!) { node(id: $id) { ... on ProjectV2 {
      id title fields(first: 100) { nodes { ... on ProjectV2SingleSelectField { id name options { id name } } } }
      items(first: 100) { nodes { content { ... on Issue { number } }
        fieldValues(first: 100) { nodes { ... on ProjectV2ItemFieldSingleSelectValue {
          name field { ... on ProjectV2SingleSelectField { name } }
        } } }
      } }
    } } }""", {"id": identity})["data"]["node"]


def test_new_lane_preserves_existing_field_values_and_item_identity():
    tracker = FakeTracker()
    config = Config(lanes={"api": {}})
    identity = board.Board(tracker, "owner/repo", config).ensure()
    config = replace(config, board=identity)
    issue = tracker.add_issue(1, labels=["lane:api", "size:m"])
    board.add_issue(config, tracker, "owner/repo", issue)
    board.update_status(config, tracker, "owner/repo", issue, "Ready")
    before = snapshot(tracker, identity)
    status = next(field for field in before["fields"]["nodes"] if field["name"] == "Status")
    assert [option["name"] for option in status["options"]] == list(board.STATUSES)
    config = replace(config, lanes={"api": {}, "web": {}})
    assert board.Board(tracker, "owner/repo", config).ensure() == identity
    board.add_issue(config, tracker, "owner/repo", issue)
    board.add_issue(config, tracker, "owner/repo", tracker.add_issue(2, labels=["lane:web", "size:l"]))
    after = snapshot(tracker, identity)
    assert after["items"]["nodes"][0] == before["items"]["nodes"][0]
    assert len(after["items"]["nodes"]) == 2
    lane = next(field for field in after["fields"]["nodes"] if field["name"] == "Lane")
    assert {option["name"] for option in lane["options"]} == {"main", "api", "web"}


def test_board_lookup_paginates_and_is_scoped_to_repository_owner():
    tracker = FakeTracker()
    query = "mutation($input: CreateProjectV2Input!) { createProjectV2(input: $input) { projectV2 { id } } }"
    for index in range(100):
        tracker.graphql(query, {"input": {"ownerId": "OWNER_owner", "title": f"Other {index}"}})
    other_owner = board.Board(tracker, "someone-else/repo", Config()).ensure()
    identity = board.Board(tracker, "owner/repo", Config()).ensure()
    count = len(tracker.projects)
    assert board.Board(tracker, "owner/repo", Config()).ensure() == identity
    assert identity != other_owner and len(tracker.projects) == count
    assert any(variables.get("after") for _, variables in tracker.graphql_calls)


def test_unconfigured_board_does_not_call_transport():
    tracker = FakeTracker()
    issue = tracker.add_issue(1)
    ctx = SimpleNamespace(config=Config(), tracker=tracker, repo="owner/repo")
    board.sync_status(ctx, issue, "Ready")
    board.add_issue(ctx.config, tracker, ctx.repo, issue)
    assert tracker.graphql_calls == []
