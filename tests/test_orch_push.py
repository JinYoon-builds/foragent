"""Edge cases of the gate's blocked and outage pushes (#129)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from foragent import notify, orch_push
from foragent.config import Config
from foragent.orch_events import append_event
from foragent.state import StateStore
from foragent.tracker import Issue

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def case(tmp_path, monkeypatch):
    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.root.mkdir(parents=True)
    pushes, results = [], []

    def send(config, title, line, link=None):
        pushes.append({"line": line, "link": link})
        if results:
            outcome = results.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return True

    monkeypatch.setattr(notify, "send", send)
    monkeypatch.setattr("foragent.orch_spawn._session_alive", lambda store, root: False)
    ctx = SimpleNamespace(store=store, config=replace(Config(), repo="owner/repo", language="en"),
                          now=lambda: clock[0], repo_root=tmp_path)
    heartbeat(store, NOW)
    return SimpleNamespace(ctx=ctx, store=store, pushes=pushes, results=results, clock=clock)


def heartbeat(store, at):
    (store.root / "orch-heartbeat.json").write_text(json.dumps({"at": at.isoformat()}))


def label(case, n, *labels, state="open"):
    case.store.record_issue(Issue(n, f"Issue {n}", "", frozenset(labels), state), {})


def kinds(store):
    path = store.root / "orch-events.jsonl"
    if not path.exists():
        return []
    return [(e["kind"], e["issue"]) for e in map(json.loads, path.read_text().splitlines())]


def test_first_tick_sends_one_combined_push_with_lowest_issue_link(case):
    label(case, 7, "blocked")
    label(case, 3, "needs-human")
    label(case, 5, "approved")
    label(case, 9, "blocked", state="closed")
    case.store.save_flag(7, "retries-exceeded")
    assert orch_push.after_tick(case.ctx, spawned=False) == ([3, 7], False)
    assert len(case.pushes) == 1
    line = case.pushes[0]["line"]
    assert "#3" in line and "#7" in line and "retries-exceeded" in line and "#5" not in line and "#9" not in line
    assert case.pushes[0]["link"] == "https://github.com/owner/repo/issues/3"
    assert kinds(case.store) == [("push-blocked", 3), ("push-blocked", 7)]
    orch_push.after_tick(case.ctx, spawned=False)
    assert len(case.pushes) == 1


def test_no_link_without_repo(case):
    case.ctx.config = replace(case.ctx.config, repo="")
    label(case, 2, "blocked")
    orch_push.after_tick(case.ctx, spawned=False)
    assert case.pushes[0]["link"] is None


def test_unblock_then_reblock_pushes_again(case):
    label(case, 4, "blocked")
    orch_push.after_tick(case.ctx, spawned=False)
    label(case, 4, "approved")
    orch_push.after_tick(case.ctx, spawned=False)
    assert len(case.pushes) == 1
    label(case, 4, "blocked")
    orch_push.after_tick(case.ctx, spawned=False)
    assert len(case.pushes) == 2


def test_reason_change_pushes_again(case):
    label(case, 4, "blocked")
    case.store.save_flag(4, "stalled")
    orch_push.after_tick(case.ctx, spawned=False)
    case.store.save_flag(4, "retries-exceeded")
    orch_push.after_tick(case.ctx, spawned=False)
    assert len(case.pushes) == 2 and "retries-exceeded" in case.pushes[1]["line"]


def test_needs_human_reason_is_newest_escalation(case):
    label(case, 6, "needs-human")
    orch_push.after_tick(case.ctx, spawned=False)
    append_event(case.store, NOW, 6, "escalate", "#6: escalated to human; pick a schema")
    orch_push.after_tick(case.ctx, spawned=False)
    assert len(case.pushes) == 2 and "pick a schema" in case.pushes[1]["line"]
    orch_push.after_tick(case.ctx, spawned=False)
    assert len(case.pushes) == 2


@pytest.mark.parametrize("outcome", [False, OSError("down")])
def test_failed_send_is_not_recorded_and_retried(case, outcome):
    label(case, 4, "blocked")
    case.results.append(outcome)
    assert orch_push.after_tick(case.ctx, spawned=False) == ([], False)
    assert kinds(case.store) == []
    orch_push.after_tick(case.ctx, spawned=False)
    assert len(case.pushes) == 2 and kinds(case.store) == [("push-blocked", 4)]


def test_failed_reblock_push_is_retried(case):
    label(case, 4, "blocked")
    orch_push.after_tick(case.ctx, spawned=False)
    label(case, 4, "approved")
    orch_push.after_tick(case.ctx, spawned=False)
    label(case, 4, "blocked")
    case.results.append(False)
    orch_push.after_tick(case.ctx, spawned=False)
    orch_push.after_tick(case.ctx, spawned=False)
    assert len(case.pushes) == 3 and kinds(case.store).count(("push-blocked", 4)) == 2


def test_spawn_suppresses_and_records_nothing(case):
    label(case, 4, "blocked")
    orch_push.after_tick(case.ctx, spawned=True)
    assert case.pushes == [] and kinds(case.store) == []
    orch_push.after_tick(case.ctx, spawned=False)
    assert len(case.pushes) == 1


def test_outage_and_blocked_combine_without_link(case):
    case.store.start_run(1, branch="b", worktree="w")
    heartbeat(case.store, NOW - timedelta(seconds=31))
    label(case, 4, "blocked")
    orch_push.after_tick(case.ctx, spawned=False)
    assert len(case.pushes) == 1 and "#4" in case.pushes[0]["line"]
    assert "orchestrator is off" in case.pushes[0]["line"] and case.pushes[0]["link"] is None
    assert ("push-orch-off", None) in kinds(case.store)


def test_outage_rearms_only_after_fresh_heartbeat(case):
    case.store.start_run(1, branch="b", worktree="w")
    (case.store.root / "orch-heartbeat.json").unlink()  # Missing counts as off.
    orch_push.after_tick(case.ctx, spawned=False)
    assert len(case.pushes) == 1
    case.store.finish_run(1, outcome="merged")
    orch_push.after_tick(case.ctx, spawned=False)  # Idle, still no heartbeat: no re-arm.
    case.store.start_run(2, branch="b", worktree="w")
    orch_push.after_tick(case.ctx, spawned=False)
    assert len(case.pushes) == 1


def test_live_session_without_heartbeat_is_not_off(case, monkeypatch):
    monkeypatch.setattr("foragent.orch_spawn._session_alive", lambda store, root: True)
    case.store.start_run(1, branch="b", worktree="w")
    heartbeat(case.store, NOW - timedelta(minutes=5))
    orch_push.after_tick(case.ctx, spawned=False)
    assert case.pushes == []


def _spawning(case, monkeypatch, tmp_path, *, send_raises=False, record_raises=False):
    """A tick context whose spawn decision opens an orchestrator (no heartbeat, approved work)."""
    import shutil
    import subprocess

    from foragent import orch_binding, orch_spawn, orch_stale_files

    (case.store.root / "orch-heartbeat.json").unlink()
    case.store.save_approval(4, body="b", body_sha="s", acceptance_blobs={}, approved_by="human",
                             approved_at=NOW.isoformat())
    launches = []
    monkeypatch.delenv("FORAGENT_ORCH_SPAWN", raising=False)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **k: launches.append(argv)
                        or subprocess.CompletedProcess(argv, 0))
    monkeypatch.setattr(shutil, "which", lambda name: f"/offline/bin/{name}" if name == "claude" else None)
    monkeypatch.setattr(orch_binding, "record_tick", lambda ctx: None)
    monkeypatch.setattr(orch_stale_files, "report", lambda ctx: None)
    if send_raises:
        case.results.extend([OSError("push down")] * 3)
    if record_raises:
        def broken(*args, **kwargs):
            raise OSError("disk full")
        monkeypatch.setattr(orch_spawn, "_record_session", broken)
    repo = tmp_path / "repo"
    repo.mkdir()
    case.ctx.repo_root = repo
    return launches


@pytest.mark.parametrize("failure", ["send_raises", "record_raises"])
def test_spawn_suppresses_pushes_even_when_its_bookkeeping_fails(case, monkeypatch, tmp_path, failure):
    from foragent import orch_spawn

    label(case, 4, "blocked")
    launches = _spawning(case, monkeypatch, tmp_path, **{failure: True})
    with orch_spawn.deciding(case.ctx) as keep:
        orch_push.refresh_waited(case.ctx, None)  # A completing tick.
        report = keep(SimpleNamespace(actions=[]))
    assert len(launches) == 1
    assert len(case.pushes) == 1  # Only the spawn push was tried.
    assert all("#4" not in push["line"] for push in case.pushes)
    assert kinds(case.store) == [] and any("orchestrator" in action for action in report.actions)
    case.results.clear()
    assert orch_push.after_tick(case.ctx, spawned=False) == ([4], False)


def test_one_failed_record_keeps_the_other_records_and_saved_state(case, monkeypatch):
    real = orch_push.append_event
    label(case, 3, "blocked")
    label(case, 4, "blocked")

    def flaky(store, at, issue, kind, summary, **fields):
        if issue == 3:
            raise OSError("disk full")
        return real(store, at, issue, kind, summary, **fields)

    monkeypatch.setattr(orch_push, "append_event", flaky)
    with pytest.raises(OSError):
        orch_push.after_tick(case.ctx, spawned=False)
    assert kinds(case.store) == [("push-blocked", 4)]
    saved = json.loads((case.store.root / "notify-blocked.json").read_text())
    assert saved["blocked"] == {"3": "", "4": ""}


def _deciding_report(case, monkeypatch):
    from foragent import orch_binding, orch_spawn, orch_stale_files

    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    monkeypatch.setattr(orch_binding, "record_tick", lambda ctx: None)
    monkeypatch.setattr(orch_stale_files, "report", lambda ctx: None)
    with orch_spawn.deciding(case.ctx) as keep:
        orch_push.refresh_waited(case.ctx, None)  # A completing tick.
        return keep(SimpleNamespace(actions=[]))


def test_tick_report_lines_follow_the_project_language(case, monkeypatch):
    case.ctx.config = replace(case.ctx.config, language="ko")
    case.store.start_run(1, branch="b", worktree="w")
    heartbeat(case.store, NOW - timedelta(seconds=31))
    label(case, 4, "blocked")
    assert _deciding_report(case, monkeypatch).actions == ["알림을 보냈어요: #4, 오케스트레이터 꺼짐"]
    case.ctx.config = replace(case.ctx.config, language="en")
    label(case, 5, "blocked")
    assert _deciding_report(case, monkeypatch).actions == ["Pushed: #5"]


def test_failed_push_report_line_follows_the_project_language(case, monkeypatch):
    def broken(ctx, spawned):
        raise OSError("disk full")

    monkeypatch.setattr(orch_push, "after_tick", broken)
    case.ctx.config = replace(case.ctx.config, language="ko")
    assert _deciding_report(case, monkeypatch).actions == ["막힌 일 알림을 보내지 못했어요: disk full"]


@pytest.mark.parametrize("rate_limited", [False, True])
def test_only_a_tick_past_its_wait_check_without_a_rate_limit_pushes(case, monkeypatch, rate_limited):
    from foragent import orch_binding, orch_spawn, orch_stale_files, scheduler, scheduler_start

    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    monkeypatch.setattr(orch_binding, "record_tick", lambda ctx: None)
    monkeypatch.setattr(orch_stale_files, "report", lambda ctx: None)
    monkeypatch.setattr(scheduler.orch_github, "pause", lambda ctx, exc: NOW)
    monkeypatch.setattr(scheduler, "_apply_budgets", lambda ctx, ready: None)
    label(case, 4, "blocked")
    case.ctx._push_ready = True  # A previous tick's mark must not leak into this one.
    with orch_spawn.deciding(case.ctx) as keep:
        keep(SimpleNamespace(actions=[]))
    assert case.pushes == []
    with orch_spawn.deciding(case.ctx) as keep:
        assert orch_push.refresh_waited(case.ctx, None) is None
        if rate_limited:
            scheduler_start._github_pause(case.ctx, OSError("rate limit"))
        keep(SimpleNamespace(actions=[]))
    assert len(case.pushes) == (0 if rate_limited else 1)


def test_a_waiting_tick_does_not_push(case, monkeypatch):
    from foragent import orch_binding, orch_spawn, orch_stale_files

    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    monkeypatch.setattr(orch_binding, "record_tick", lambda ctx: None)
    monkeypatch.setattr(orch_stale_files, "report", lambda ctx: None)
    label(case, 4, "blocked")
    with orch_spawn.deciding(case.ctx) as keep:
        assert orch_push.refresh_waited(case.ctx, "GitHub rate limit until later") == "GitHub rate limit until later"
        keep(SimpleNamespace(actions=[]))
    assert case.pushes == []
