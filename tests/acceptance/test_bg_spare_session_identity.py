"""Acceptance test for bg-spare-session-identity (bug 11-09).

The gate launches ``claude --bg --session-id <new id>`` with a
FORAGENT_SESSION_ID for that id, but Claude serves it from a pre-started
process whose environment still holds a FORAGENT_SESSION_ID retired the day
before and whose own Claude session id (``CLAUDE_CODE_SESSION_ID``, the
transcript's name) differs from the recorded id. The session actually opened
must be the active one: its watch renews the heartbeat, refresh-due measures
its real transcript, and once a refresh replaces it, its watch is ignored.
"""

from test_heartbeats import heartbeats_offline  # noqa: F401 - autouse isolation
from test_orch_refresh import note_text

from bg_spare_world import SPARE, enter_spare, same_file, spare_world


def test_watch_in_the_session_a_spare_served_makes_alive_report_running(
    git_history, tmp_path, monkeypatch, capsys,
):
    world, ids = spare_world(git_history, tmp_path, monkeypatch, capsys)
    assert world.alive()["alive"] is False, "setup: nothing renewed the heartbeat since the launch"

    enter_spare(world, ids)
    assert world.watch_first_poll() == 0
    alive = world.alive()
    assert alive["alive"] is True, (
        "the session the launch actually opened (stale FORAGENT_SESSION_ID, its own Claude "
        f"session id) must renew the heartbeat on its watch's first poll; got {alive}")
    assert alive["last_seen"] == world.now.isoformat(), alive

    # It keeps renewing on later polls, not only once.
    world.advance(minutes=5)
    assert world.alive()["alive"] is False
    world.watch_first_poll()
    assert world.alive() == {"alive": True, "last_seen": world.now.isoformat(), "age_seconds": 0}


def test_refresh_due_in_the_spare_session_names_its_real_transcript(
    git_history, tmp_path, monkeypatch, capsys,
):
    world, ids = spare_world(git_history, tmp_path, monkeypatch, capsys)
    real = world.write_transcript(SPARE, compactions=2)
    # The launch's recorded id has no transcript: Claude never wrote one under it.
    assert not world.transcript_file(ids["recorded"]).exists()

    enter_spare(world, ids)
    world.watch_first_poll()
    report = world.refresh_due()
    assert same_file(report.get("transcript_path"), real), (
        f"refresh-due must name the transcript of Claude session {SPARE}, the one the session "
        f"reports; got {report}")
    assert report.get("session_id") == SPARE, report
    assert report.get("compactions") == 2, (
        f"refresh-due must read the existing transcript, not report it missing; got {report}")


def test_refresh_replacing_the_spare_session_stops_its_watch_renewing(
    git_history, tmp_path, monkeypatch, capsys,
):
    world, ids = spare_world(git_history, tmp_path, monkeypatch, capsys)
    enter_spare(world, ids)
    world.watch_first_poll()
    assert world.alive()["alive"] is True, "the spare-served session must first be the active one"

    # The active orchestrator hands off and refreshes; Claude serves this launch normally.
    world.advance(minutes=1)
    fresh = world.refresh_launch(note_text())
    assert fresh not in ids.values() and fresh != SPARE
    assert world.heartbeat().get("session_id") == fresh

    world.advance(minutes=2)  # Every earlier heartbeat is stale now.
    before = world.heartbeat()
    enter_spare(world, ids)
    world.watch_first_poll()
    assert world.alive()["alive"] is False, (
        "a watch from the session the refresh replaced must not renew the heartbeat")
    after = world.heartbeat()
    assert after.get("at") == before.get("at"), (before, after)
    assert after.get("session_id") == fresh, (
        f"the replaced session's watch must not make itself active again; heartbeat {after}")

    # The fresh session itself (launched normally, ids agree) does renew.
    world.enter_session(foragent_id=fresh, claude_id=fresh)
    world.watch_first_poll()
    assert world.alive()["alive"] is True
    assert world.heartbeat().get("session_id") == fresh
