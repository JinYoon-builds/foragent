from datetime import UTC, datetime, timedelta
import json

import pytest

from foragent import orch_plan, orch_queue
from foragent.state import StateStore

NOW = datetime(2026, 10, 27, 9, tzinfo=UTC)


def store_at(tmp_path, clock):
    return StateStore(tmp_path, now=lambda: clock[0])


def events(tmp_path, kind):
    path = tmp_path / "orch-events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [row for row in rows if row["kind"] == kind]


def test_legacy_lane_queues_migrate_once_as_human(tmp_path):
    (tmp_path / "lane-queues.json").write_text(json.dumps({"orch": [7, 3], "view": []}))
    store = store_at(tmp_path, [NOW])
    assert orch_queue.get(store, "orch") == [7, 3]
    assert not (tmp_path / "plan.json").exists(), "Readers never write the plan"
    orch_queue.set_order(store, "view", [5])  # The first command write moves the old queues in.
    assert not (tmp_path / "lane-queues.json").exists()
    stored = json.loads((tmp_path / "plan.json").read_text())
    assert stored["queues"] == {"orch": [7, 3], "view": [5]}
    assert stored["authors"]["queues"] == {"orch": "human", "view": "human"}
    assert stored["written_by"] == "human"
    with pytest.raises(orch_plan.PlanRefused, match="human"):
        orch_queue.set_order(store, "orch", [], by="orchestrator")


def test_orchestrator_edits_are_all_or_nothing(tmp_path):
    clock = [NOW]
    store = store_at(tmp_path, clock)
    orch_plan.set_plan(store, {"orch": 2}, None, "human")
    before = (tmp_path / "plan.json").read_text()
    with pytest.raises(orch_plan.PlanRefused, match="human"):
        orch_plan.set_plan(store, {"view": 3, "orch": None}, None, "orchestrator")
    assert (tmp_path / "plan.json").read_text() == before
    assert len(events(tmp_path, "plan-width")) == 1
    orch_plan.set_plan(store, {}, NOW + timedelta(hours=1), "human")
    with pytest.raises(orch_plan.PlanRefused, match="human"):
        orch_plan.set_plan(store, {}, NOW + timedelta(hours=2), "orchestrator")


def test_person_pause_replaces_orchestrator_pause_and_takes_authorship(tmp_path):
    store = store_at(tmp_path, [NOW])
    orch_plan.pause(store, "orch", NOW + timedelta(hours=1), "first", "orchestrator")
    orch_plan.pause(store, "orch", NOW + timedelta(hours=2), "again", "orchestrator")
    orch_plan.pause(store, "orch", NOW + timedelta(hours=3), "mine", "human")
    with pytest.raises(orch_plan.PlanRefused):
        orch_plan.pause(store, "orch", NOW + timedelta(hours=4), "theirs", "orchestrator")
    plan = orch_plan.load(store)
    assert plan["paused"]["orch"]["by"] == "human" and plan["paused"]["orch"]["why"] == "mine"
    assert len(events(tmp_path, "plan-pause")) == 3
    with pytest.raises(orch_plan.PlanRefused, match="past"):
        orch_plan.pause(store, "view", NOW - timedelta(seconds=1), "late", "human")


def test_lapsed_pause_drops_silently_and_expiry_writes_one_event(tmp_path):
    clock = [NOW]
    store = store_at(tmp_path, clock)
    orch_plan.pause(store, "orch", NOW + timedelta(minutes=10), "short", "human")
    orch_plan.set_plan(store, {"orch": 1}, NOW + timedelta(hours=1), "orchestrator")
    clock[0] += timedelta(minutes=10)
    assert not orch_plan.lane_paused(store, "orch", clock[0])
    before = (tmp_path / "plan.json").read_text()
    orch_plan.record_expiry(store, clock[0])
    assert (tmp_path / "plan.json").read_text() == before, "Only the commands write the plan"
    assert "orch" not in orch_plan.load(store)["paused"]
    assert events(tmp_path, "plan-resume") == []

    clock[0] += timedelta(hours=1)
    assert orch_plan.view(orch_plan.load(store))["written_by"] is None  # Read only: no event yet.
    assert events(tmp_path, "plan-expired") == []
    for _ in range(2):
        orch_plan.record_expiry(store, clock[0])
    assert len(events(tmp_path, "plan-expired")) == 1
    assert (tmp_path / "plan.json").read_text() == before
    shown = orch_plan.view(orch_plan.load(store))
    assert (shown["queues"], shown["paused"], shown["width"], shown["expires_at"], shown["written_by"]) == (
        {}, {}, {}, None, None)
    orch_plan.set_plan(store, {"orch": 2}, None, "orchestrator")
    assert orch_plan.load(store)["expires_at"] is None
    assert json.loads((tmp_path / "plan.json").read_text())["paused"] == {}
    orch_plan.set_plan(store, {}, clock[0] + timedelta(hours=1), "orchestrator")
    clock[0] += timedelta(hours=2)
    orch_plan.record_expiry(store, clock[0])
    assert len(events(tmp_path, "plan-expired")) == 2, "A later plan that expires again writes another"


def test_plan_cli_validates_lanes_widths_and_times(tmp_path, monkeypatch, capsys):
    from foragent import cli
    from foragent.config import Config

    monkeypatch.setattr(cli, "load_config", lambda root: Config(repo="o/r", lanes={"orch": {"paths": ["x/**"]}}))
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kwargs: cls(tmp_path)))

    def run(*argv):
        try:
            return cli.main(["orch", *argv, "--repo-root", str(tmp_path)])
        except SystemExit as exc:
            return exc.code

    assert run("plan", "--width", "orch=0") == 1
    assert run("plan", "--width", "nope=2", "--width", "orch=2") == 1
    assert "nope" in capsys.readouterr().err
    assert run("pause", "orch", "--until", "2000-01-01T00:00", "--why", "x") == 1
    assert run("plan", "--width", "main=2", "--width", "orch=3") == 0
    capsys.readouterr()
    assert run("plan") == 0
    out = capsys.readouterr().out
    assert "lane:orch" in out and "lane:main" in out
    assert out.index("lane:orch") < out.index("lane:main")
    assert run("plan", "--width", "orch=off", "--json") == 0
    assert json.loads(capsys.readouterr().out)["width"] == {"main": 2}


def test_refused_edit_after_expiry_keeps_the_expired_event(tmp_path):
    clock = [NOW]
    store = store_at(tmp_path, clock)
    orch_plan.set_plan(store, {"orch": 2}, NOW + timedelta(hours=1), "human")
    clock[0] += timedelta(hours=1, seconds=1)
    with pytest.raises(orch_plan.PlanRefused, match="past"):
        orch_plan.pause(store, "orch", NOW, "late", "human")
    assert len(events(tmp_path, "plan-expired")) == 1
    assert json.loads((tmp_path / "plan.json").read_text())["width"] == {"orch": 2}, "A refused edit writes nothing"
    assert orch_plan.load(store)["width"] == {}
    orch_plan.record_expiry(store, clock[0])
    assert len(events(tmp_path, "plan-expired")) == 1
