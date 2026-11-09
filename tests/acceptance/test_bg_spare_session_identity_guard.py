"""Regression guard for bg-spare-session-identity (bug 11-09); passes on main too.

A watch from a session that a later launch or refresh really replaced must
stay unable to renew the heartbeat or take the active record back, also once
sessions served by pre-started processes are recognised.
"""

from test_heartbeats import heartbeats_offline  # noqa: F401 - autouse isolation
from test_orch_refresh import note_text

from bg_spare_world import World, spare_world


def assert_ignored(world, active):
    before = world.heartbeat()
    world.watch_first_poll()
    after = world.heartbeat()
    assert world.alive()["alive"] is False, "a replaced session's watch must not renew the heartbeat"
    assert after.get("at") == before.get("at"), (before, after)
    assert after.get("session_id") == active, (
        f"a replaced session's watch must not make itself active; heartbeat {after}")


def test_sessions_the_gate_replaced_stay_ignored_beside_the_spare(
    git_history, tmp_path, monkeypatch, capsys,
):
    world, ids = spare_world(git_history, tmp_path, monkeypatch, capsys)
    # Sessions opened normally (Claude's id is the launch's id) that later launches retired.
    for retired in (ids["first"], ids["second"]):
        world.enter_session(foragent_id=retired, claude_id=retired)
        assert_ignored(world, ids["recorded"])
        # Without Claude's id, the inherited id alone still names a retired session.
        world.enter_session(foragent_id=retired, claude_id=None)
        assert_ignored(world, ids["recorded"])


def test_session_a_refresh_replaced_stays_ignored(git_history, tmp_path, monkeypatch, capsys):
    world = World(git_history, tmp_path, monkeypatch, capsys)
    old = world.gate_launch()
    world.enter_session(foragent_id=old, claude_id=old)
    world.watch_first_poll()
    assert world.alive()["alive"] is True, "setup: the normally opened session renews its heartbeat"

    world.advance(minutes=1)
    fresh = world.refresh_launch(note_text())
    world.advance(minutes=2)
    world.enter_session(foragent_id=old, claude_id=old)
    assert_ignored(world, fresh)
    # The old session keeps polling: still ignored, the record still names the fresh session.
    world.advance(minutes=1)
    assert_ignored(world, fresh)

    world.enter_session(foragent_id=fresh, claude_id=fresh)
    world.watch_first_poll()
    assert world.alive()["alive"] is True
    assert world.heartbeat().get("session_id") == fresh

