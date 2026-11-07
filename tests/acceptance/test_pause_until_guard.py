"""Guard (pause-past-until): behaviour that already passes on main and must stay.

A future --until and no --until pause train building as today; a malformed time
is still refused with exit 2. Not a Done when file (it passes on main).
"""

import pytest

from acceptance.train_pause_cli import REASON, make_cli, pause_in_force


@pytest.mark.parametrize("extra, until", [
    (["--until", "2026-11-08T13:00:00+00:00"], "2026-11-08T13:00:00+00:00"),
    ([], None),
])
def test_future_or_no_until_pauses_as_today(tmp_path, monkeypatch, capsys, extra, until):
    root, store, fa = make_cli(tmp_path, monkeypatch)
    code, out = fa(capsys, "orch", "pause-trains", "--why", REASON, *extra)

    assert code == 0, out
    assert "Train building paused" in out, out
    paused = pause_in_force(root, store)
    assert paused and paused["why"] == REASON and paused["until"] == until


def test_malformed_until_is_still_refused_with_exit_2(tmp_path, monkeypatch, capsys):
    root, store, fa = make_cli(tmp_path, monkeypatch)
    code, out = fa(capsys, "orch", "pause-trains", "--why", REASON, "--until", "tomorrow")
    assert code == 2, out
    assert pause_in_force(root, store) is None
