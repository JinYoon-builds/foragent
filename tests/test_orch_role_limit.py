"""Unit tests for orch_role_limit (#2308): reset parsing and gate-run role sessions."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from foragent import notify, orch_inbox, orch_role_limit
from foragent.orch_events import append_event
from foragent.state import StateStore

SENT = datetime(2026, 11, 6, 2, 31, tzinfo=UTC)  # 11:31 KST


def test_reset_time_reads_clock_zone_and_epoch():
    reset, zone = orch_role_limit.reset_time("You've hit your 5-hour limit · resets 12:10pm (Asia/Seoul)", SENT)
    assert (reset, zone) == (datetime(2026, 11, 6, 3, 10, tzinfo=UTC), "Asia/Seoul")
    reset, _ = orch_role_limit.reset_time("You've hit your limit · resets 9am (Asia/Seoul)", SENT)
    assert reset == datetime(2026, 11, 7, 0, 0, tzinfo=UTC)  # 09:00 KST already passed today.
    reset, zone = orch_role_limit.reset_time("Claude AI usage limit reached|1762398600", SENT)
    assert reset == datetime.fromtimestamp(1762398600, UTC) and zone is None
    assert orch_role_limit.reset_time("You've hit your usage limit", SENT) == (None, None)
    assert orch_role_limit.reset_time("resets 25:00", SENT) == (None, None)


def test_role_session_limit_alarms_once_wakes_once_and_clears(tmp_path, monkeypatch):
    clock = [SENT]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    pushes = []
    monkeypatch.setattr(notify, "send", lambda config, title, line, link=None: pushes.append(line) or True)
    ctx = SimpleNamespace(store=store, config=SimpleNamespace(language="en"), repo_root=tmp_path,
                          now=lambda: clock[0])

    def done(message):
        done.calls += 1
        append_event(store, clock[0], None, "architect-done", "exit code 1", started_at=f"s{done.calls}",
                     exit_code=1, message=message)

    done.calls = 0

    def wakes():
        return [row for row in orch_inbox.requests(store) if row.get("wake") == "usage-limit"]

    done("All good.")
    orch_role_limit.tick(ctx)
    assert pushes == [] and orch_role_limit.active(store) == []

    done("You've hit your 5-hour limit · resets 12:10pm (Asia/Seoul)")
    for minutes in (1, 10):
        clock[0] = SENT + timedelta(minutes=minutes)
        orch_role_limit.tick(ctx)
    assert len(pushes) == 1 and "12:10" in pushes[0] and wakes() == []
    assert [row["role"] for row in orch_role_limit.active(store)] == ["architect"]

    for minutes in (40, 45):
        clock[0] = SENT + timedelta(minutes=minutes)
        orch_role_limit.tick(ctx)
    assert len(wakes()) == 1 and "architect" in wakes()[0]["text"]

    done("Proposal filed.")
    clock[0] = SENT + timedelta(minutes=50)
    orch_role_limit.tick(ctx)
    assert orch_role_limit.active(store) == [] and len(pushes) == 1


def _limited_role(tmp_path, monkeypatch, send):
    clock = [SENT]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    monkeypatch.setattr(notify, "send", send)
    ctx = SimpleNamespace(store=store, config=SimpleNamespace(language="en"), repo_root=tmp_path,
                          now=lambda: clock[0])
    append_event(store, clock[0], None, "architect-done", "exit code 1", started_at="s1", exit_code=1,
                 message="You've hit your 5-hour limit · resets 12:10pm (Asia/Seoul)")
    return store, ctx, clock


def test_failed_push_is_attempted_once_per_episode(tmp_path, monkeypatch):
    """notify.deliver already keeps a failed push as a PM notice; a retry would add one per tick."""
    attempts = []

    def failing(config, title, line, link=None):
        attempts.append(line)
        return False

    store, ctx, clock = _limited_role(tmp_path, monkeypatch, failing)
    for minutes in (1, 2, 3):
        clock[0] = SENT + timedelta(minutes=minutes)
        orch_role_limit.tick(ctx)
    assert len(attempts) == 1
    events = [line for line in (store.root / "orch-events.jsonl").read_text().splitlines() if '"usage-limit"' in line]
    assert len(events) == 1 and '"error"' in events[0]


def test_wake_is_not_repeated_when_its_record_write_fails(tmp_path, monkeypatch):
    store, ctx, clock = _limited_role(tmp_path, monkeypatch, lambda *args, **kwargs: True)
    orch_role_limit.tick(ctx)
    write = orch_role_limit._write

    def failing_wake_write(store, role, record):
        if record.get("woken_at"):
            raise OSError("disk full")
        write(store, role, record)

    monkeypatch.setattr(orch_role_limit, "_write", failing_wake_write)
    clock[0] = SENT + timedelta(minutes=40)
    try:
        orch_role_limit.tick(ctx)
    except OSError:
        pass
    monkeypatch.setattr(orch_role_limit, "_write", write)
    for minutes in (41, 42):
        clock[0] = SENT + timedelta(minutes=minutes)
        orch_role_limit.tick(ctx)
    assert len([row for row in orch_inbox.requests(store) if row.get("wake") == "usage-limit"]) == 1


def test_clear_event_is_not_repeated_when_its_record_write_fails(tmp_path, monkeypatch):
    store, ctx, clock = _limited_role(tmp_path, monkeypatch, lambda *args, **kwargs: True)
    orch_role_limit.tick(ctx)
    append_event(store, SENT + timedelta(minutes=5), None, "architect-done", "exit code 0", started_at="s2",
                 exit_code=0, message="Proposal filed.")
    write = orch_role_limit._write

    def failing_clear_write(store, role, record):
        if record.get("cleared_at"):
            raise OSError("disk full")
        write(store, role, record)

    monkeypatch.setattr(orch_role_limit, "_write", failing_clear_write)
    clock[0] = SENT + timedelta(minutes=6)
    try:
        orch_role_limit.tick(ctx)
    except OSError:
        pass
    monkeypatch.setattr(orch_role_limit, "_write", write)
    clock[0] = SENT + timedelta(minutes=7)
    orch_role_limit.tick(ctx)
    clears = [line for line in (store.root / "orch-events.jsonl").read_text().splitlines() if "usage-limit-clear" in line]
    assert len(clears) == 1 and orch_role_limit.active(store) == []
