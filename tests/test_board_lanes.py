"""Unit checks for the gate's lane label and board Lane reconciliation (#1230)."""

import json

import pytest
from types import SimpleNamespace

from foragent import board, board_lanes
from foragent.tracker import FakeTracker, GhTracker, LabelSpec, TrackerError


def body(*files):
    return "## Goal\nWork.\n## Files\n" + "".join(f"- {name}\n" for name in files) + "## Done when\n- done\n"


def iso(tag):
    """Listing tags t0, t1, ... as the store's ISO times."""
    return f"2026-01-01T00:{int(tag[1:]):02d}:00+00:00"


class Store:
    """Rows are GitHub as of the latest listing, unless a test freezes an older snapshot."""

    def __init__(self, root, tracker, runs=()):
        self.root, self.tracker, self.runs = root, tracker, list(runs)
        self.as_of, self.hidden, self.frozen, self.outbox = "t0", set(), None, []

    def local_issues(self):
        if self.frozen is not None:
            return [dict(row) for row in self.frozen]
        return [{"number": i.number, "title": i.title, "body": i.body, "labels": sorted(i.labels), "state": i.state,
                 "observed_at": iso(self.as_of)}
                for i in self.tracker.issues.values() if i.number not in self.hidden]

    def freeze(self):
        self.frozen = self.local_issues()

    def issues_observation(self):
        return {"as_of": iso(self.as_of)}

    def github_outbox(self):
        return list(self.outbox)

    def active_runs(self):
        return [SimpleNamespace(number=n) for n in self.runs]

    def _write_json(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")


class Tracker(FakeTracker):
    def __init__(self):
        super().__init__()
        self.writes = []
        self.fail_board_writes = 0

    def ensure_labels(self, specs):
        self.writes.append(("ensure", tuple(spec.name for spec in specs)))
        return super().ensure_labels(specs)

    def add_labels(self, number, labels):
        self.writes.append(("add", number, tuple(labels)))
        return super().add_labels(number, labels)

    def remove_labels(self, number, labels):
        self.writes.append(("remove", number, tuple(labels)))
        return super().remove_labels(number, labels)

    def graphql(self, query, variables):
        if query.lstrip().startswith("mutation"):
            if "ForagentSetBoardValue" in query and self.fail_board_writes:
                self.fail_board_writes -= 1
                raise TrackerError("board write failed", returncode=1)
            self.writes.append(("board", query.split("(")[0].split()[-1]))
        return super().graphql(query, variables)


def case(tmp_path, lanes, *, board_id=None, runs=()):
    tracker = Tracker()
    tracker.ensure_labels([LabelSpec("lane:old", "123456", "Kept"), LabelSpec("lane:new", "123456", "Kept")])
    config = SimpleNamespace(lanes=lanes, board=board_id, board_map=None)
    ctx = SimpleNamespace(tracker=tracker, repo="example/project", config=config,
                          store=Store(tmp_path, tracker, runs), case_sensitive=True)
    tracker.writes.clear()
    return ctx


SPLIT = {"old": {"paths": ["service/**"]}, "new": {"paths": ["product/**"]}}
JOINED = {"old": {"paths": ["service/**", "product/**"]}, "new": {"paths": ["unrelated/**"]}}


def test_first_tick_aligns_like_a_change_and_waits_for_a_newer_listing(tmp_path):
    ctx = case(tmp_path, SPLIT)
    ctx.tracker.add_issue(1, "One", body("product/a.py"), labels=["lane:old"])
    board_lanes.reconcile(ctx)
    assert ctx.tracker.get_issue(1).labels == {"lane:new"}, "No recorded state: nothing is known converged"
    assert ctx.tracker.writes == [("add", 1, ("lane:new",)), ("remove", 1, ("lane:old",))]
    assert json.loads((tmp_path / board_lanes.STATE_FILE).read_text())["confirmed"] is False


CLAIMS_SPLIT = '{"lanes": {"new": ["product/**"], "old": ["service/**"]}, "board": null, "confirmed": true, '


@pytest.mark.parametrize("content", ["not json", "[]", '{"lanes": null}', CLAIMS_SPLIT + '"pending": ["x"]}'],
                         ids=["garbage", "list", "no-lanes", "bad-pending"])
def test_unreadable_state_realigns_instead_of_trusting_it(tmp_path, content):
    ctx = case(tmp_path, JOINED)
    ctx.tracker.add_issue(1, "One", body("product/a.py"), labels=["lane:old"])
    board_lanes.reconcile(ctx)
    ctx.store.as_of = "t1"
    board_lanes.reconcile(ctx)
    ctx.config.lanes = SPLIT
    (tmp_path / board_lanes.STATE_FILE).write_text(content, encoding="utf-8")
    board_lanes.reconcile(ctx)
    assert ctx.tracker.get_issue(1).labels == {"lane:new"}


def test_board_unreadable_at_first_tick_then_paths_change_still_aligns(tmp_path):
    ctx = case(tmp_path, JOINED)
    project = board.ensure(ctx.tracker, ctx.repo, ["old", "new"])
    ctx.config.board = project
    ctx.tracker.add_issue(1, "One", body("product/a.py"), labels=["lane:old"])
    board.add_issue(ctx.tracker, ctx.repo, project, 1, "old", "s")
    graphql = ctx.tracker.graphql

    def offline(*args):
        raise OSError("offline project")

    ctx.tracker.graphql = offline
    board_lanes.reconcile(ctx)
    ctx.tracker.graphql = graphql
    ctx.config.lanes = SPLIT  # The paths change before the retry.
    board_lanes.reconcile(ctx)
    from test_board import item_values
    assert ctx.tracker.get_issue(1).labels == {"lane:new"}
    assert item_values(ctx.tracker, project, 1)["Lane"] == "new"
    ctx.store.as_of = "t1"
    board_lanes.reconcile(ctx)
    assert json.loads((tmp_path / board_lanes.STATE_FILE).read_text())["confirmed"] is True


def test_waiting_issue_missing_from_the_store_moves_after_the_next_listing(tmp_path):
    ctx = case(tmp_path, JOINED, runs=[1])
    ctx.tracker.add_issue(1, "One", body("product/a.py"), labels=["lane:old"])
    board_lanes.reconcile(ctx)
    ctx.store.as_of = "t1"
    board_lanes.reconcile(ctx)
    ctx.config.lanes = SPLIT
    board_lanes.reconcile(ctx)
    ctx.store.as_of = "t2"
    board_lanes.reconcile(ctx)  # Confirmed while the run still holds its lane.
    state = json.loads((tmp_path / board_lanes.STATE_FILE).read_text())
    assert state["pending"] == [1] and state["confirmed"] is True
    ctx.store.runs, ctx.store.hidden = [], {1}  # The run ended; its row is not in the store yet.
    board_lanes.reconcile(ctx)
    assert ctx.tracker.get_issue(1).labels == {"lane:old"}
    assert json.loads((tmp_path / board_lanes.STATE_FILE).read_text())["confirmed"] is False
    ctx.store.hidden = set()
    board_lanes.reconcile(ctx)
    assert ctx.tracker.get_issue(1).labels == {"lane:old"}, "Only a newer complete listing decides it"
    ctx.store.as_of = "t3"  # The next complete listing has it.
    board_lanes.reconcile(ctx)
    assert ctx.tracker.get_issue(1).labels == {"lane:new"}


def test_only_running_issues_waiting_reads_nothing(tmp_path):
    ctx = case(tmp_path, JOINED, runs=[1])
    ctx.tracker.add_issue(1, "One", body("product/a.py"), labels=["lane:old"])
    board_lanes.reconcile(ctx)
    ctx.config.lanes = SPLIT
    board_lanes.reconcile(ctx)
    calls = []
    ctx.store.local_issues = lambda: calls.append("local_issues") or []
    ctx.tracker.label_names = lambda: calls.append("label_names") or set()
    board_lanes.reconcile(ctx)
    assert calls == []


def test_board_option_deleted_before_confirmation_is_added_again(tmp_path):
    ctx = case(tmp_path, JOINED)
    project = board.ensure(ctx.tracker, ctx.repo, ["old", "new"])
    ctx.config.board = project
    ctx.tracker.add_issue(1, "One", body("product/a.py"), labels=["lane:old"])
    board.add_issue(ctx.tracker, ctx.repo, project, 1, "old", "s")
    board_lanes.reconcile(ctx)
    ctx.config.lanes = SPLIT
    board_lanes.reconcile(ctx)
    field = board.Project(ctx.tracker, ctx.repo, project).fields()["Lane"]
    ctx.tracker.graphql("""mutation ForagentUpdateBoardField($input: UpdateProjectV2FieldInput!) {
      updateProjectV2Field(input: $input) { projectV2Field { id } } }""", {"input": {
        "fieldId": field["id"], "singleSelectOptions": [o for o in field["options"] if o["name"] != "new"]}})
    ctx.store.as_of = "t1"
    board_lanes.reconcile(ctx)
    from test_board import item_values
    options = board.Project(ctx.tracker, ctx.repo, project).fields()["Lane"]["options"]
    assert "new" in {o["name"] for o in options}
    assert item_values(ctx.tracker, project, 1)["Lane"] == "new"
    assert json.loads((tmp_path / board_lanes.STATE_FILE).read_text())["confirmed"] is True


def test_label_deleted_after_the_last_listing_is_created_on_the_change_tick(tmp_path):
    ctx = case(tmp_path, SPLIT)
    ctx.tracker.add_issue(1, "One", body("product/a.py"), labels=["lane:new"])
    ctx.tracker.add_issue(2, "Two", body("service/b.py"), labels=["lane:old"])
    board_lanes.reconcile(ctx)
    ctx.store.as_of = "t1"
    board_lanes.reconcile(ctx)
    ctx.store.freeze()  # The store still shows the last listing, with lane:new on #1.
    del ctx.tracker.labels["lane:new"]  # Deleting a label on GitHub removes it from its issues.
    ctx.tracker.remove_labels(1, ["lane:new"])
    ctx.tracker.writes.clear()
    ctx.config.lanes = JOINED  # Only paths change, before any refresh.
    board_lanes.reconcile(ctx)
    assert ("ensure", ("lane:new",)) in ctx.tracker.writes, "Cached issue labels prove no label exists"
    assert "lane:new" in ctx.tracker.labels


def test_label_list_is_read_for_a_change_and_before_applying_a_label_only(tmp_path):
    ctx = case(tmp_path, SPLIT)
    ctx.tracker.add_issue(1, "One", body("product/a.py"), labels=["lane:new"])
    ctx.tracker.add_issue(2, "Two", body("service/b.py"), labels=["lane:old"])
    calls = []
    label_names = ctx.tracker.label_names
    ctx.tracker.label_names = lambda: calls.append("label_names") or label_names()
    board_lanes.reconcile(ctx)  # No recorded state and nothing to move: no label work.
    assert calls == []
    (tmp_path / board_lanes.STATE_FILE).unlink()
    ctx.tracker.add_issue(3, "Three", body("product/c.py"), labels=["lane:old"])
    board_lanes.reconcile(ctx)  # Moving #3 first checks that lane:new exists.
    assert calls == ["label_names"] and ctx.tracker.get_issue(3).labels == {"lane:new"}
    calls.clear()
    ctx.config.lanes = {**SPLIT, "extra": {"paths": ["extra/**"]}}
    board_lanes.reconcile(ctx)
    assert calls == ["label_names"], "A recorded change always reads the label list once"
    assert ("ensure", ("lane:extra",)) in ctx.tracker.writes


def test_only_missing_labels_are_created(tmp_path):
    ctx = case(tmp_path, JOINED)
    board_lanes.reconcile(ctx)
    ctx.config.lanes = {**SPLIT, "extra": {"paths": ["extra/**"]}}
    board_lanes.reconcile(ctx)
    assert ("ensure", ("lane:extra",)) in ctx.tracker.writes
    assert ctx.tracker.labels["lane:old"].color == "123456"


def test_ambiguous_and_unmatched_files_keep_their_lane(tmp_path):
    ctx = case(tmp_path, JOINED)
    ctx.tracker.add_issue(1, "Both", body("product/a.py", "service/b.py"), labels=["lane:old"])
    ctx.tracker.add_issue(2, "None", body("docs/x.md"), labels=["lane:old"])
    ctx.tracker.add_issue(3, "One", body("product/a.py"), labels=["lane:old", "custom"])
    board_lanes.reconcile(ctx)
    ctx.config.lanes = SPLIT
    board_lanes.reconcile(ctx)
    assert ctx.tracker.get_issue(1).labels == {"lane:old"}
    assert ctx.tracker.get_issue(2).labels == {"lane:old"}
    assert ctx.tracker.get_issue(3).labels == {"lane:new", "custom"}


def test_same_configuration_reads_nothing(tmp_path):
    ctx = case(tmp_path, JOINED)
    board_lanes.reconcile(ctx)
    calls = []
    ctx.tracker.label_names = lambda: calls.append("label_names") or set()
    ctx.store.local_issues = lambda: calls.append("local_issues") or []
    board_lanes.reconcile(ctx)
    ctx.config.lanes = SPLIT
    board_lanes.reconcile(ctx)
    calls.clear()
    ctx.store.as_of = "t1"  # A newer listing confirms the change once.
    board_lanes.reconcile(ctx)
    assert calls == ["local_issues"]
    calls.clear()
    board_lanes.reconcile(ctx)
    ctx.store.as_of = "t2"
    board_lanes.reconcile(ctx)
    assert calls == [], "A confirmed configuration reads nothing, whatever the listings"


def test_programming_errors_are_not_deferred(tmp_path):
    ctx = case(tmp_path, JOINED)
    board_lanes.reconcile(ctx)
    ctx.config.lanes = SPLIT

    def broken():
        raise AttributeError("bug")

    ctx.tracker.label_names = broken
    with pytest.raises(AttributeError):
        board_lanes.reconcile(ctx)


def test_issue_missing_from_the_store_at_the_change_moves_after_the_next_listing(tmp_path):
    ctx = case(tmp_path, JOINED)
    ctx.tracker.add_issue(1, "Seen", body("product/a.py"), labels=["lane:old"])
    ctx.tracker.add_issue(2, "Unseen", body("product/b.py"), labels=["lane:old"])
    ctx.store.hidden = {2}
    board_lanes.reconcile(ctx)
    ctx.config.lanes = SPLIT
    board_lanes.reconcile(ctx)
    assert ctx.tracker.get_issue(1).labels == {"lane:new"}
    ctx.store.hidden = set()  # A later per-issue read stores it, but no complete listing yet.
    board_lanes.reconcile(ctx)
    assert ctx.tracker.get_issue(2).labels == {"lane:old"}, "Only a newer complete listing confirms"
    ctx.store.as_of = "t1"
    board_lanes.reconcile(ctx)
    assert ctx.tracker.get_issue(2).labels == {"lane:new"}
    assert json.loads((tmp_path / board_lanes.STATE_FILE).read_text())["confirmed"] is True


def test_stale_card_lane_is_corrected_when_the_label_is_already_right(tmp_path):
    ctx = case(tmp_path, SPLIT)
    project = board.ensure(ctx.tracker, ctx.repo, ["old", "new"])
    ctx.config.board = project
    ctx.tracker.add_issue(1, "One", body("product/a.py"), labels=["lane:new"])
    board.add_issue(ctx.tracker, ctx.repo, project, 1, "old", "s")
    ctx.tracker.writes.clear()
    board_lanes.reconcile(ctx)
    from test_board import item_values
    assert item_values(ctx.tracker, project, 1)["Lane"] == "new"
    assert not [w for w in ctx.tracker.writes if w[0] in {"add", "remove"}]
    ctx.tracker.writes.clear()
    ctx.store.as_of = "t1"
    board_lanes.reconcile(ctx)
    assert ctx.tracker.writes == [], "A card already on its Lane is not written again"


def test_unreadable_board_keeps_the_change_unconverged(tmp_path):
    ctx = case(tmp_path, JOINED)
    project = board.ensure(ctx.tracker, ctx.repo, ["old", "new"])
    ctx.config.board = project
    ctx.tracker.add_issue(1, "One", body("product/a.py"), labels=["lane:old"])
    board.add_issue(ctx.tracker, ctx.repo, project, 1, "old", "s")
    board_lanes.reconcile(ctx)
    ctx.config.lanes = SPLIT
    graphql = ctx.tracker.graphql

    def offline(*args):
        raise OSError("offline project")

    ctx.tracker.graphql = offline
    board_lanes.reconcile(ctx)
    assert ctx.tracker.get_issue(1).labels == {"lane:old"}, "A label waits until its board option exists"
    ctx.tracker.graphql = graphql
    board_lanes.reconcile(ctx)
    from test_board import item_values
    assert ctx.tracker.get_issue(1).labels == {"lane:new"}
    assert item_values(ctx.tracker, project, 1)["Lane"] == "new"


def test_failed_card_write_is_retried_next_tick(tmp_path):
    ctx = case(tmp_path, JOINED)
    project = board.ensure(ctx.tracker, ctx.repo, ["old", "new"])
    ctx.config.board = project
    ctx.tracker.add_issue(1, "One", body("product/a.py"), labels=["lane:old"])
    board.add_issue(ctx.tracker, ctx.repo, project, 1, "old", "s")
    board_lanes.reconcile(ctx)
    ctx.config.lanes = SPLIT
    ctx.tracker.fail_board_writes = 1
    board_lanes.reconcile(ctx)
    assert ctx.tracker.get_issue(1).labels == {"lane:new"}
    assert json.loads((tmp_path / board_lanes.STATE_FILE).read_text())["pending"] == [1]
    board_lanes.reconcile(ctx)
    from test_board import item_values
    assert item_values(ctx.tracker, project, 1)["Lane"] == "new"
    assert json.loads((tmp_path / board_lanes.STATE_FILE).read_text())["pending"] == []


def test_running_issue_waits_then_moves(tmp_path):
    ctx = case(tmp_path, JOINED, runs=[1])
    ctx.tracker.add_issue(1, "One", body("product/a.py"), labels=["lane:old"])
    board_lanes.reconcile(ctx)
    ctx.config.lanes = SPLIT
    board_lanes.reconcile(ctx)
    assert ctx.tracker.get_issue(1).labels == {"lane:old"}
    ctx.store.runs = []
    board_lanes.reconcile(ctx)
    assert ctx.tracker.get_issue(1).labels == {"lane:new"}


def test_gh_label_names_reads_every_page():
    tracker = GhTracker.__new__(GhTracker)
    tracker._path = "repos/example/project"
    calls = []
    tracker._call = lambda args, **kw: calls.append(args) or '[{"name": "a"}][{"name": "lane:b"}]'
    assert tracker.label_names() == {"a", "lane:b"}
    assert calls == [["api", "--paginate", "repos/example/project/labels?per_page=100"]]
