"""#2905 regressions beyond its Done-when file: the lane detail names what the runner uses."""

from pathlib import Path

import pytest

from foragent.config import Config
from foragent.run_implementation import lane_config
from foragent.watch_sections import detail


@pytest.mark.parametrize("lane, expected", [
    ({"paths": ["a/**"]}, ("codex", "gpt-global")),
    ({"paths": ["a/**"], "executor": "claude", "model": "opus-lane"}, ("claude", "opus-lane")),
    # An explicit empty lane model means the executor default, as the runner reads it.
    ({"paths": ["a/**"], "model": ""}, ("codex", "executor default")),
])
def test_lane_detail_names_the_executor_and_model_the_runner_uses(tmp_path: Path, lane, expected):
    config = Config(executor="codex", executor_model="gpt-global", lanes={"work": lane})
    effective = lane_config(config, "work")
    assert (effective.executor, effective.executor_model or "executor default") == expected
    text = detail.render_detail(config, "work", tmp_path)
    assert f"executor: {expected[0]}" in text and f"model: {expected[1]}" in text


# ---------------------------------------------------------------- holds masked by an earlier reason

import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))

from owner_records_support import (  # noqa: E402
    HELD, admission_startable, admission_world, finish_sentences, next_tick, screen,
)


@pytest.fixture
def utf8(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")


def _mask(case, kind):
    """Put an earlier admission reason in front of #3's own hold, then tick so now.json follows."""
    from datetime import timedelta

    from foragent import capacity

    if kind == "cap":  # Two slots: the running #1 and #2 take them, so the replay stops at #3 with `cap`.
        capacity.write_state(case.store, 2, case.clock[0])
        next_tick(case)
    else:  # A tick-wide hold (a recorded GitHub pause): every waiting issue gets `github`.
        case.store.pause_github(case.clock[0] + timedelta(days=3650), "rate limit")


@pytest.mark.parametrize("held", ["dependency", "no_machine_check"])
@pytest.mark.parametrize("kind", ["cap", "github"])
def test_an_earlier_admission_reason_does_not_hide_the_issues_own_hold_from_the_estimate(
        git_history, tmp_path, monkeypatch, capsys, utf8, held, kind):
    """#2905 review: the replay records one reason per issue, so `cap` (or a tick hold) in front of
    #3's dependency or missing machine check must not put #3 back into the finish estimate."""
    alone, alone_main = admission_world(git_history, tmp_path, monkeypatch, name="alone")
    _mask(alone, kind)
    expected = finish_sentences(screen(alone, alone_main, capsys))
    case, main = admission_world(git_history, tmp_path, monkeypatch, held=held, name="held")
    _mask(case, kind)
    _, work = admission_startable(case, main, capsys)
    assert str(HELD) in (work.get("held") or {}) and work["held"][str(HELD)] != held, (
        f"fixture guard: an earlier reason masks #{HELD}'s own hold: {work}")
    shown = screen(case, main, capsys)
    assert expected and finish_sentences(shown) == expected, (expected, finish_sentences(shown), shown)
