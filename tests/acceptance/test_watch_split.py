"""Acceptance test for watch-split. Locked by Foragent; do not edit."""

from pathlib import Path

import pytest

from test_skills_autosync import isolated_home
from test_state_split import offline_acceptance, split_modules


# Frozen from src/foragent/*.py before the split.
BASE_MODULES = (
    "__init__.py", "actions.py", "adopt.py", "bench.py", "board.py", "board_fake.py",
    "cli.py", "config.py", "design.py", "executors.py", "git.py", "i18n.py", "init.py",
    "inspect_actions.py", "inspect_data.py", "inspect_runtime.py", "issue.py", "kick.py",
    "labels.py", "lanes.py", "launchd.py", "launcher.py", "license_texts.py", "notify.py",
    "orch.py", "orch_events.py", "orch_reinstall.py", "order.py", "paths.py", "policy.py",
    "processes.py", "reasons.py", "runner.py", "scheduler.py", "skills.py", "state.py",
    "status.py", "tracker.py", "trycard.py", "watch.py", "watch_open.py",
)
WATCH_NAMES = (
    "Any", "Callable", "Config", "ConfigError", "Counter", "Issue", "Line", "PICK", "Path",
    "Snapshot", "Span", "StateStore", "ThreadPoolExecutor", "Tracker", "UTC", "_activity",
    "_beside", "_cells", "_clean", "_columns", "_fit", "_frame", "_has_work", "_line",
    "_panel", "_terminal", "_wrap", "collect", "dataclass", "datetime", "deque", "islice",
    "issue_lane", "json", "lane_display", "nullcontext", "os", "recent_events", "registered",
    "render_detail", "render_frame", "run", "sys", "text", "time", "timedelta",
    "unicodedata", "zip_longest",
)


def test_watch_split_modules_meet_line_limits_and_names():
    package = Path(__file__).resolve().parents[2] / "src/foragent"
    new = split_modules(package, "watch.py", BASE_MODULES)
    assert all(path.name.startswith("watch_") for path in new), sorted(path.name for path in new)
    from foragent import watch

    missing = [name for name in WATCH_NAMES if not hasattr(watch, name)]
    assert not missing, f"foragent.watch lost original bindings: {missing}"


def test_watch_split_original_names_remain_importable():
    from foragent import watch

    missing = [name for name in WATCH_NAMES if not hasattr(watch, name)]
    assert not missing, f"foragent.watch lost original bindings: {missing}"


def test_watch_split_collect_and_render_patches_reach_run_once(tmp_path, monkeypatch, capsys):
    from conftest import BASE_TIME
    from foragent import watch
    from foragent.config import Config
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker

    store, config, tracker = StateStore(tmp_path / "state"), Config(), FakeTracker()
    status, snapshot, calls = {"probe": "status"}, object(), []
    store.root.mkdir()
    pid_file = store.root / "watch.pid"
    pid_file.write_text("123\n", encoding="utf-8")

    def collect(*args):
        calls.append(("collect", args))
        return snapshot

    def render_frame(*args):
        calls.append(("render_frame", args))
        return "facade-rendered frame"

    # Both bindings are patched by tests/test_watch_open.py too.
    monkeypatch.setattr(watch, "collect", collect)
    monkeypatch.setattr(watch, "render_frame", render_frame)
    watch.run(lambda: status, tracker, store, config, tmp_path, once=True, now=lambda: BASE_TIME)
    assert calls == [
        ("collect", (status, tracker, store, config, BASE_TIME)),
        ("render_frame", (snapshot, config, tmp_path)),
    ]
    assert capsys.readouterr().out == "facade-rendered frame\n"
    assert pid_file.read_text(encoding="utf-8") == "123\n"


def test_watch_split_frame_patch_reaches_render_frame(tmp_path, monkeypatch):
    from conftest import BASE_TIME
    from foragent import watch
    from foragent.config import Config

    snapshot = watch.Snapshot({}, {}, None, 0, 0, None, [], BASE_TIME)
    config, calls = Config(), []

    def frame(*args, **kwargs):
        calls.append((args, kwargs))
        return [[("patched ", "bold"), ("frame", "")]]

    monkeypatch.setattr(watch, "_frame", frame)
    assert watch.render_frame(snapshot, config, tmp_path, tick=3, width=80) == "patched frame"
    assert calls == [((snapshot, config, tmp_path),
                      {"tick": 3, "width": 80, "selected": None, "now": BASE_TIME})]


def test_watch_split_collection_error_patch_propagates_without_printing(
    tmp_path, monkeypatch, capsys,
):
    from foragent import watch
    from foragent.config import Config
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker

    calls = []

    def unavailable(*args):
        calls.append(args)
        raise RuntimeError("offline snapshot unavailable")

    monkeypatch.setattr(watch, "collect", unavailable)
    with pytest.raises(RuntimeError, match="offline snapshot unavailable"):
        watch.run(lambda: {}, FakeTracker(), StateStore(tmp_path / "state"), Config(),
                  tmp_path, once=True)
    assert len(calls) == 1
    assert capsys.readouterr().out == ""
    assert not (tmp_path / "state/watch.pid").exists()
