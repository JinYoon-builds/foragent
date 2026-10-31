"""Locked acceptance: the gate owns lane labels derived from Files and lane paths.

Break-it: reject missing lane labels/options, stale board Lane values, changed
bodies/approvals, early movement of running work, and repeated tracker writes.
Only Git, process, launcher and machine sensor boundaries are faked.
"""

from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from hashlib import sha1
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from foragent import board, capacity, cli, gh, launchd, processes, scheduler
from foragent.config import load_config
from foragent.git import GitRepo
from foragent.state import StateStore
from foragent.tracker import FakeTracker, LabelSpec


class Writes(FakeTracker):
    """Record attempted writes, including redundant no-op label requests."""

    def __init__(self, **kwargs):
        super().__init__(strict=True, **kwargs)
        self.writes = []
        self.lane_board = None

    def add_labels(self, number, labels):
        labels = tuple(labels)
        for label in labels:
            if label.startswith("lane:"):
                assert label in self.labels, f"Create the GitHub label before applying {label}"
                if self.lane_board:
                    options = board.Project(self, "example/project", self.lane_board).fields()["Lane"]["options"]
                    assert label[5:] in {option["name"] for option in options}, (
                        f"Create the board Lane option before applying {label}"
                    )
        self.writes.append(("add", number, labels))
        return super().add_labels(number, labels)

    def remove_labels(self, number, labels):
        labels = tuple(labels)
        self.writes.append(("remove", number, labels))
        return super().remove_labels(number, labels)

    def comment(self, number, body):
        self.writes.append(("comment", number, body))
        return super().comment(number, body)

    def update_issue(self, number, **kwargs):
        self.writes.append(("body", number, kwargs))
        return super().update_issue(number, **kwargs)

    def ensure_labels(self, specs):
        self.writes.append(("ensure_labels", tuple(specs)))
        return super().ensure_labels(specs)

    def graphql(self, query, variables):
        if query.lstrip().startswith("mutation"):
            self.writes.append(("board", query, deepcopy(variables)))
        return super().graphql(query, variables)


@pytest.fixture
def gate(tmp_path, monkeypatch):
    home, root = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    root.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(home / name.lower()))
    for name in ("FORAGENT_VIEW", "FORAGENT_ORCH_SPAWN", "FORAGENT_UPDATE_CHECK", "FORAGENT_CPU_BUSY"):
        monkeypatch.setenv(name, "off")
    monkeypatch.delenv("FORAGENT_SESSION_ID", raising=False)
    monkeypatch.delenv("FORAGENT_ROLE", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail(f"Acceptance must stay offline and launch no real process: {args}")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(processes, "list_processes", lambda: processes.ProcessTable([]))
    monkeypatch.setattr(launchd, "refresh_from_tick", lambda *args: None)
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.0))
    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path / "machine")
    monkeypatch.setattr(capacity, "slept_seconds", lambda *args: 0.0)
    clock = [datetime(2026, 10, 31, 9, tzinfo=UTC)]
    tracker = Writes(now=lambda: clock[0], login="human")
    tracker.ensure_labels([LabelSpec(f"lane:{name}", "c5def5", "Existing lane")
                           for name in ("old", "new")])
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    settings = {
        "repo": "example/project", "approvers": ["human"], "checks": "ci",
        "skill_libraries": [], "lanes": {
            "old": {"paths": ["product/**", "service/**"]},
            "new": {"paths": ["unrelated/**"]},
        },
    }

    def configure(lanes, *, board_id=None):
        settings["lanes"] = lanes
        if board_id is not None:
            settings["board"] = board_id
        (root / ".foragent.yml").write_text(yaml.safe_dump(settings), encoding="utf-8")

    configure(settings["lanes"])

    def git_read(args):
        if args[0] == "fetch":
            return ""
        if args[0] == "rev-parse":
            if args[-1].endswith(":.foragent.yml"):
                return sha1((root / ".foragent.yml").read_bytes()).hexdigest()
            return "acceptance-blob"
        if args[:2] == ["cat-file", "blob"]:
            return (root / ".foragent.yml").read_text(encoding="utf-8")
        if args[0] == "rev-list":
            return "approved-base"
        if args[0] == "ls-tree":
            return "product/page.py\0product/task.py\0service/api.py\0tests/acceptance/test_feature.py\0"
        pytest.fail(f"Unexpected Git boundary read: {args}")

    context = scheduler.TickContext(
        repo="example/project", repo_root=root, tracker=tracker, store=store,
        config=load_config(root), git=GitRepo(root, git_read), now=lambda: clock[0],
        launcher=forbidden, start_runs=False, committed_config=True,
        runner_process_exists=lambda number: False, kill_runner=forbidden,
        baseline_launcher=forbidden,
    )
    monkeypatch.setattr(cli, "kick_tick", lambda *args: None)

    def step():
        clock[0] += timedelta(seconds=1)
        report = scheduler.tick(context)
        assert report.skipped is None, report
        return report

    def issue(number, files, *, state="open", approved=True):
        body = ("<!-- foragent:v1 -->\n## Goal\nShip the chosen work.\n"
                "## Out of scope\nOther work.\n## Files\n"
                + "".join(f"- {path}\n" for path in files)
                + "## Done when\n```sh\nuv run pytest tests/acceptance/test_feature.py\n```\n")
        tracker.add_issue(number, f"Work {number}", body,
                          labels=["lane:old", "size:s", "by:human", "custom:keep"], state=state)
        if approved:
            tracker.record_label(number, "approved", "human")
        return body

    gh.clear_recorder()
    yield SimpleNamespace(ctx=context, root=root, tracker=tracker, store=store,
                          clock=clock, configure=configure, step=step, issue=issue)
    gh.clear_recorder()


def lane_labels(case, number):
    return {label for label in case.tracker.get_issue(number).labels if label.startswith("lane:")}


def split(case):
    case.configure({"old": {"paths": ["service/**"]},
                    "new": {"paths": ["product/**"]}})


@pytest.mark.parametrize("files", [["product/page.py"], ["product/**"]], ids=["path", "glob"])
def test_changed_paths_relabel_next_tick_without_changing_approval(gate, files):
    case = gate
    body = case.issue(1, files)
    case.issue(2, ["service/api.py"])
    case.issue(3, files, state="closed", approved=False)
    case.issue(4, files, approved=False)
    case.step()
    approval = asdict(case.store.load_approval(1))
    approval_events = [event for event in case.tracker.label_events(1) if event.label == "approved"]
    other_labels = case.tracker.get_issue(1).labels - {"lane:old"}
    comments = deepcopy(case.tracker.comments)
    case.tracker.writes.clear()
    split(case)
    case.step()  # Less than the normal GitHub listing interval: config is sufficient.
    assert lane_labels(case, 1) == {"lane:new"}, "Next tick must compute the new lane from Files"
    assert lane_labels(case, 4) == {"lane:new"}, "Unapproved open work also follows path ownership"
    assert lane_labels(case, 2) == {"lane:old"}
    assert lane_labels(case, 3) == {"lane:old"}, "Closed work is not rewritten"
    assert case.tracker.get_issue(1).body == body
    assert case.tracker.get_issue(1).labels - {"lane:new"} == other_labels
    assert asdict(case.store.load_approval(1)) == approval
    assert [event for event in case.tracker.label_events(1) if event.label == "approved"] == approval_events
    assert case.tracker.comments == comments
    assert not any(write[0] in {"body", "comment"} for write in case.tracker.writes)
    label_writes = [write for write in case.tracker.writes if write[0] in {"add", "remove"}]
    assert len(label_writes) == len(set(label_writes)), "Do not issue the same label write twice"


def test_running_work_moves_only_after_its_run_ends(gate):
    case = gate
    body = case.issue(1, ["product/task.py"])
    case.step()
    approval = asdict(case.store.load_approval(1))
    case.store.start_run(1, lane="old", branch="foragent/issue-1", worktree=case.root / "worker")
    case.tracker.add_labels(1, ["running"])
    split(case)
    case.step()
    assert lane_labels(case, 1) == {"lane:old"}, "A running issue must keep its current lane"
    assert case.store.get_run(1).status == "running"
    assert case.store.get_run(1).lane == "old"
    case.ctx.store = StateStore(case.store.root, now=lambda: case.clock[0])
    case.store.finish_run(1, "merged")
    case.step()
    assert lane_labels(case, 1) == {"lane:new"}, "Deferred lane work must survive until run completion"
    assert case.tracker.get_issue(1).body == body
    assert {"approved", "size:s", "by:human", "custom:keep"} <= case.tracker.get_issue(1).labels
    assert asdict(case.store.load_approval(1)) == approval


def test_unchanged_config_writes_nothing_after_relabel_even_after_reload(gate):
    case = gate
    case.issue(1, ["product/task.py"])
    case.step()
    split(case)
    case.step()
    assert lane_labels(case, 1) == {"lane:new"}, "Establish convergence before checking idempotence"
    case.tracker.writes.clear()
    case.step()
    case.ctx.store = StateStore(case.store.root, now=lambda: case.clock[0])
    case.clock[0] += timedelta(minutes=6)  # Include a full tracker refresh after reload.
    case.step()
    assert case.tracker.writes == [], "The same configuration must attempt no tracker writes"


def board_snapshot(case, project_id):
    """Read remote board values through the fake tracker's public GraphQL API."""
    return case.tracker.graphql("""query($id: ID!) { node(id: $id) { ... on ProjectV2 {
      id title fields(first: 100) { nodes { ... on ProjectV2SingleSelectField { id name options { id name } } } }
      items(first: 100) { nodes { content { ... on Issue { number } }
        fieldValues(first: 100) { nodes { ... on ProjectV2ItemFieldSingleSelectValue {
          name field { ... on ProjectV2SingleSelectField { name } }
        } } }
      } }
    } } }""", {"id": project_id})["data"]["node"]


def card_values(snapshot, number):
    card = next(item for item in snapshot["items"]["nodes"] if item["content"]["number"] == number)
    return {value["field"]["name"]: value["name"] for value in card["fieldValues"]["nodes"]}


@pytest.mark.parametrize("added_lane", ["review-engine", "roles"])
def test_new_lane_creates_label_and_board_option_then_aligns_once(gate, added_lane):
    case = gate
    body = case.issue(1, ["product/task.py"])
    case.issue(2, ["service/api.py"])
    lanes = deepcopy(case.ctx.config.lanes)
    project_id = board.ensure(case.tracker, case.ctx.repo, lanes)
    assert project_id, "Seed an existing board through the public board API"
    case.configure(lanes, board_id=project_id)
    for number in (1, 2):
        board.add_issue(case.tracker, case.ctx.repo, project_id, number, "old", "s")
    case.step()
    approval = asdict(case.store.load_approval(1))
    approval_events = [event for event in case.tracker.label_events(1) if event.label == "approved"]
    labels_before = case.tracker.get_issue(1).labels
    comments_before = deepcopy(case.tracker.comments)
    fields_before = board.Project(case.tracker, case.ctx.repo, project_id).fields()
    values_before = card_values(board_snapshot(case, project_id), 1)
    assert f"lane:{added_lane}" not in case.tracker.labels
    assert added_lane not in {option["name"] for option in fields_before["Lane"]["options"]}

    lanes["old"]["paths"] = ["service/**"]
    lanes[added_lane] = {"paths": ["product/**"]}
    case.configure(lanes)
    case.tracker.lane_board = project_id
    case.tracker.writes.clear()
    case.step()  # New config alone must trigger provisioning before the normal refresh is due.

    assert f"lane:{added_lane}" in case.tracker.labels, "The next tick must create the configured lane's GitHub label"
    fields_after = board.Project(case.tracker, case.ctx.repo, project_id).fields()
    options = fields_after["Lane"]["options"]
    assert sum(option["name"] == added_lane for option in options) == 1, "Create one board Lane option"
    assert all(option in options for option in fields_before["Lane"]["options"]), "Keep existing option identities"
    assert fields_after["Status"] == fields_before["Status"]
    assert fields_after["Size"] == fields_before["Size"]
    label_creates = [spec for write in case.tracker.writes if write[0] == "ensure_labels"
                     for spec in write[1] if spec.name == f"lane:{added_lane}"]
    assert len(label_creates) == 1, "Provision the missing lane label once"
    option_writes = [write for write in case.tracker.writes if write[0] == "board"
                     and any(option["name"] == added_lane
                             for option in write[2].get("input", {}).get("singleSelectOptions", []))]
    assert len(option_writes) == 1, "Provision the missing board Lane option once"
    assert lane_labels(case, 1) == {f"lane:{added_lane}"}
    snapshot = board_snapshot(case, project_id)
    assert card_values(snapshot, 1) == {**values_before, "Lane": added_lane}, "Align the remote board Lane value"
    assert card_values(snapshot, 2)["Lane"] == "old" and lane_labels(case, 2) == {"lane:old"}
    assert case.tracker.get_issue(1).body == body
    assert case.tracker.get_issue(1).labels - {f"lane:{added_lane}"} == labels_before - {"lane:old"}
    assert asdict(case.store.load_approval(1)) == approval
    assert [event for event in case.tracker.label_events(1) if event.label == "approved"] == approval_events
    assert case.tracker.comments == comments_before

    labels_after = deepcopy(case.tracker.labels)
    case.tracker.writes.clear()
    case.step()
    assert case.tracker.labels == labels_after
    assert board.Project(case.tracker, case.ctx.repo, project_id).fields() == fields_after
    assert board_snapshot(case, project_id) == snapshot
    assert case.tracker.writes == [], "The second tick must not recreate or rewrite labels, options or card values"
