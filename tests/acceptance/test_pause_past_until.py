"""Acceptance (pause-past-until, statement 1).

UX r1130 (2026-11-08): `fa orch pause-trains --why X --until <past time>` printed
the paused confirmation and exited 0, yet nothing was paused. After this issue an
--until at or before the current time is refused: one plain line says the end
time has already passed and names the time it read, the exit code is non-zero,
and train building stays as it was.
"""

import re

import pytest

from acceptance.train_pause_cli import REASON, make_cli, pause_in_force

PAST = "2020-01-01T00:00"


def assert_refused(code, out, shown):
    assert code != 0, f"a past --until must exit non-zero; it exited 0 with:\n{out}"
    assert "Train building paused" not in out, f"a refused pause must not print the paused confirmation:\n{out}"
    assert re.search(r"\b(passed|past)\b", out, re.I), f"the refusal must say the end time has passed:\n{out}"
    assert shown in out, f"the refusal must name the time it read ({shown}):\n{out}"
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1, f"the refusal is one plain line, got:\n{out}"


@pytest.mark.parametrize("until, shown", [
    (PAST, "2020-01-01"),
    ("2026-11-08T11:59:00+00:00", "11:59"),
    ("2026-11-08T12:00:00+00:00", "12:00"),  # at the current time: already over
])
def test_past_until_is_refused_and_pauses_nothing(tmp_path, monkeypatch, capsys, until, shown):
    root, store, fa = make_cli(tmp_path, monkeypatch)
    code, out = fa(capsys, "orch", "pause-trains", "--why", REASON, "--until", until)

    assert_refused(code, out, shown)
    assert pause_in_force(root, store) is None, "train building stays unpaused"
    code, out = fa(capsys, "orch", "resume-trains")
    assert code == 0 and "not paused" in out, f"resume-trains finds nothing paused:\n{out}"


def test_past_until_leaves_a_running_pause_unchanged(tmp_path, monkeypatch, capsys):
    root, store, fa = make_cli(tmp_path, monkeypatch)
    code, out = fa(capsys, "orch", "pause-trains", "--why", "earlier pause")
    assert code == 0, out
    before = pause_in_force(root, store)
    assert before and before["why"] == "earlier pause"

    code, out = fa(capsys, "orch", "pause-trains", "--why", REASON, "--until", PAST)

    assert_refused(code, out, "2020-01-01")
    assert pause_in_force(root, store) == before, "train building stays as it was: the earlier pause holds"
    code, out = fa(capsys, "orch", "resume-trains")
    assert code == 0 and "not paused" not in out, f"the earlier pause is still there to resume:\n{out}"


def test_past_until_is_refused_in_korean_too(tmp_path, monkeypatch, capsys):
    root, store, fa = make_cli(tmp_path, monkeypatch, language="ko")
    code, out = fa(capsys, "orch", "pause-trains", "--why", REASON, "--until", PAST)

    assert code != 0, f"a past --until must exit non-zero in Korean too:\n{out}"
    assert "2020-01-01" in out, f"the refusal names the time it read:\n{out}"
    assert "열차 만들기를 멈췄어요" not in out, f"no paused confirmation:\n{out}"
    assert len([line for line in out.splitlines() if line.strip()]) == 1, out
    assert pause_in_force(root, store) is None, "train building stays unpaused"
