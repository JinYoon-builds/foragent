"""Acceptance test for state-split. Locked by Foragent; do not edit."""

import ast
from pathlib import Path

import pytest

from test_local_truth import record_gh
from test_skills_autosync import isolated_home


# Frozen from src/foragent/*.py before any of these splits.
BASE_MODULES = (
    "__init__.py", "actions.py", "adopt.py", "bench.py", "board.py", "board_fake.py",
    "cli.py", "config.py", "design.py", "executors.py", "git.py", "i18n.py", "init.py",
    "inspect_actions.py", "inspect_data.py", "inspect_runtime.py", "issue.py", "kick.py",
    "labels.py", "lanes.py", "launchd.py", "launcher.py", "license_texts.py", "notify.py",
    "orch.py", "orch_events.py", "orch_reinstall.py", "order.py", "paths.py", "policy.py",
    "processes.py", "reasons.py", "runner.py", "scheduler.py", "skills.py", "state.py",
    "status.py", "tracker.py", "trycard.py", "watch.py", "watch_open.py",
)

# Frozen from today's AST, including imported and private bindings.
STATE_NAMES = (
    "Any", "Approval", "Callable", "Flag", "InspectState", "Iterator", "Path", "Run",
    "StateStore", "UTC", "asdict", "contextmanager", "dataclass", "datetime", "fcntl",
    "field", "json", "os", "replace", "tempfile",
)
STATE_METHODS = (
    "for_repo", "order_progress", "update_order_progress", "save_approval", "load_approval",
    "clear_approval", "save_flag", "load_flag", "load_inspect", "update_inspect", "start_run",
    "update_run", "get_run", "active_runs", "pause_lane", "lane_paused", "finish_run",
    "read_history", "worktree_path", "log_path", "merge_lock", "notify_lock", "tick_lock",
)


@pytest.fixture(autouse=True)
def offline_acceptance(tmp_path, monkeypatch, isolated_home):
    """Keep temporary worktrees, HOME, terminals and all external I/O isolated."""
    import socket
    import tempfile

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")

    def no_network(*args, **kwargs):
        raise AssertionError("acceptance tests must not access the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    calls = record_gh(monkeypatch)
    yield calls
    assert calls == []


def split_modules(package, facade, baseline):
    """Find new sibling modules statically imported by the compatibility facade."""
    modules = set()
    for node in ast.walk(ast.parse((package / facade).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "foragent" or (node.level and not module):
                modules.update(f"foragent.{alias.name}" for alias in node.names)
            elif node.level:
                modules.add(f"foragent.{module}")
            else:
                modules.add(module)
    return {
        path for path in package.glob("*.py")
        if path.name not in baseline and f"foragent.{path.stem}" in modules
    }


def test_state_split_modules_meet_line_limits():
    from foragent import state

    missing = [name for name in STATE_NAMES if not hasattr(state, name)]
    assert not missing, f"foragent.state lost original bindings: {missing}"


def test_state_split_original_names_and_public_methods_remain_importable():
    from foragent import state
    from foragent.state import StateStore

    missing = [name for name in STATE_NAMES if not hasattr(state, name)]
    missing_methods = [name for name in STATE_METHODS if not hasattr(StateStore, name)]
    assert not missing, f"foragent.state lost original bindings: {missing}"
    assert not missing_methods, f"StateStore lost public methods: {missing_methods}"


def test_state_split_get_run_patch_reaches_gate_status(
    git_history, tmp_path, monkeypatch, capsys,
):
    from dataclasses import replace
    import json

    from foragent.state import StateStore
    from test_orch_review_handoff import bind_cli, make_case

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    main = bind_cli(case, monkeypatch)
    original = StateStore.get_run
    calls = []

    def get_run(store, number):
        calls.append((store, number))
        run = original(store, number)
        return replace(run, phase="state-facade-probe") if run is not None else None

    monkeypatch.setattr(StateStore, "get_run", get_run)
    assert main(["gate", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["active_runs"][0]["phase"] == "state-facade-probe"
    assert payload["lanes"]["main"]["phase"] == "state-facade-probe"
    assert (case.store, 1) in calls
    assert original(case.store, 1).phase is None


def test_state_split_missing_record_patch_is_honoured_by_active_runs(tmp_path, monkeypatch):
    from conftest import BASE_TIME
    from foragent.state import StateStore

    store = StateStore(tmp_path / "state", now=lambda: BASE_TIME)
    store.start_run(7, branch="foragent/issue-7", worktree=store.worktree_path(7))
    calls = []
    original = StateStore.get_run

    def missing_record(instance, number):
        calls.append((instance, number))
        return None

    monkeypatch.setattr(StateStore, "get_run", missing_record)
    assert store.active_runs() == []
    assert calls == [(store, 7)]
    assert original(store, 7).status == "running"
