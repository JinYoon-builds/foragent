"""Acceptance test for reinstall-race. Locked by Foragent; do not edit."""

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import time
from types import SimpleNamespace

import pytest

from foragent import capacity, kick, launchd, launcher, orch_reinstall, run_baseline, update_check
from foragent.config import Config
from foragent.state import StateStore


RACE_ERRORS = ("Directory not empty (os error 66)", "failed to remove directory")
OTHER_ERROR = "failed to download package: certificate verification failed"
NOW = datetime(2026, 10, 26, tzinfo=UTC)
SHA = "a" * 40


@pytest.fixture
def race_sandbox(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    def forbidden(*args, **kwargs):
        pytest.fail(f"Unexpected external command or process signal: {args!r}")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(update_check, "lookup", lambda: "v0.1.10")
    monkeypatch.setattr(update_check, "current_version", lambda: "0.1.9")
    monkeypatch.setattr(update_check, "_language", lambda root: "en")
    monkeypatch.setattr(update_check, "installed_gates", lambda: {})
    return tmp_path


def exercise_install(root, entry, error, failures, monkeypatch, capsys):
    """Keep the real install/reporting paths, replacing only OS and Git boundaries."""
    root.mkdir(parents=True)
    tools = root / "fake-tools"
    package = tools / "foragent/lib/python3.12/site-packages/foragent"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("BUILD = 'previous'\n")
    checkout = root / "checkout"
    checkout.mkdir()
    (checkout / "local.txt").write_text("keep local work\n")
    store = StateStore(root / "state", now=lambda: NOW)
    store.start_run(7, branch="merged", worktree=checkout)
    store.finish_run(7, "merged")
    state_before = {path.relative_to(store.root): path.read_bytes()
                    for path in store.root.rglob("*") if path.is_file()}
    machine = root / "machine"
    machine.mkdir()
    cache = machine / "update-check.json"
    cache.write_text(json.dumps({"installed": "v0.1.9", "pending_restart": []}))
    cache_before = cache.read_bytes()
    monkeypatch.setattr(capacity, "machine_root", lambda: machine)
    worktrees, removed, fetches, calls, trace = [], [], [], [], []

    def add_worktree(path, sha):
        assert sha == SHA
        source = path / "src/foragent"
        source.mkdir(parents=True)
        (path / "pyproject.toml").write_text('[project]\nname = "foragent"\n')
        (source / "__init__.py").write_text("BUILD = 'merged'\n")
        worktrees.append(path)

    def remove_worktree(path):
        removed.append(path)
        shutil.rmtree(path)

    git = SimpleNamespace(
        fetch=lambda *args: fetches.append(args),
        ref_sha=lambda ref: SHA,
        add_detached_worktree=add_worktree,
        remove_worktree=remove_worktree,
    )

    def fake_uv(argv, **kwargs):
        assert argv[0] == "uv", f"Only the fake uv boundary is allowed: {argv!r}"
        if argv[1:] == ["tool", "dir"]:
            return subprocess.CompletedProcess(argv, 0, stdout=str(tools), stderr="")
        assert argv[1:3] == ["tool", "install"], argv
        calls.append(list(argv))
        trace.append(("install", len(calls)))
        assert len(calls) <= 3, "Never exceed three uv install attempts"
        if entry == "after-merge":
            assert argv[3:5] == ["--force", "--reinstall"]
            source = Path(argv[-1])
            assert source in worktrees and Path(kwargs["cwd"]) == source
        else:
            assert argv[3:] == [update_check.INSTALL_SOURCE + "v0.1.10"]
            assert Path(kwargs["cwd"]) == Path.home()
        failed = failures is None or len(calls) <= failures
        if not failed and entry == "after-merge":
            shutil.copytree(source / "src/foragent", package, dirs_exist_ok=True)
        result = subprocess.CompletedProcess(argv, 1 if failed else 0,
                                             stdout="" if failed else "installed",
                                             stderr=error if failed else "")
        if kwargs.get("check"):
            result.check_returncode()
        return result

    def backoff(delay):
        trace.append(("backoff", delay))

    # Observe subprocess.run rather than replacing _uv: a shared retry in _uv
    # must remain visible to both callers. No uv executable is ever invoked.
    monkeypatch.setattr(orch_reinstall.subprocess, "run", fake_uv)
    monkeypatch.setattr(time, "sleep", backoff)
    for module in (orch_reinstall, update_check):
        monkeypatch.setattr(module, "sleep", backoff, raising=False)
    if entry == "after-merge":
        code = orch_reinstall.after_merge(SimpleNamespace(git=git, store=store, now=lambda: NOW), 42, "main")
        assert fetches == [("origin", "main")]
        assert removed == worktrees and len(worktrees) == 1
        assert not worktrees[0].exists()
    else:
        code = update_check.update(checkout)
        assert not worktrees
    captured = capsys.readouterr()
    events_path = store.root / "orch-events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()] if events_path.exists() else []
    state_after = {path.relative_to(store.root): path.read_bytes()
                   for path in store.root.rglob("*")
                   if path.is_file() and path != events_path}
    assert state_after == state_before, "Installing must preserve the already merged run and history"
    assert (checkout / "local.txt").read_text() == "keep local work\n"
    return SimpleNamespace(entry=entry, code=code, calls=calls, trace=trace, events=events,
                           out=captured.out, err=captured.err, cache=cache,
                           cache_before=cache_before, package=package)


def assert_failed(case, reason):
    if case.entry == "after-merge":
        assert case.code is None
        assert case.out == f"reinstall failed: {reason}\n" and case.err == ""
        assert case.events == [{"at": NOW.isoformat(), "issue": None,
                                "kind": "reinstall-failed", "summary": f"PR #42: {reason}"}]
    else:
        assert case.code == 1
        assert case.out == ""
        assert case.err == f"Couldn't install Foragent v0.1.10: {reason}. No gate was restarted.\n"
        assert case.events == []
    assert case.cache.read_bytes() == case.cache_before
    assert (case.package / "__init__.py").read_text() == "BUILD = 'previous'\n"


def assert_attempts(case, count):
    assert len(case.calls) == count, (
        f"{case.entry}: expected {count} uv install attempts, got {len(case.calls)}; "
        f"stdout={case.out!r}, stderr={case.err!r}")
    assert all(argv == case.calls[0] for argv in case.calls)
    assert [kind for kind, _ in case.trace] == [
        item for index in range(count) for item in (("install", "backoff") if index < count - 1 else ("install",))
    ], "Back off between failed attempts, never after the final attempt"
    assert all(delay > 0 for kind, delay in case.trace if kind == "backoff")


def test_reinstall_race_transient_directory_errors_recover_in_both_install_paths(
    race_sandbox, monkeypatch, capsys,
):
    cases = [exercise_install(race_sandbox / f"recover-{entry}-{index}", entry, error, 1,
                              monkeypatch, capsys)
             for entry in ("after-merge", "update") for index, error in enumerate(RACE_ERRORS)]
    for case in cases:
        assert_attempts(case, 2)
        assert case.events == [], "A recovered install must not leave a reinstall-failed event"
        if case.entry == "after-merge":
            assert case.code is None and case.err == ""
            assert f"reinstalled Foragent {SHA}" in case.out and "matches merged source" in case.out
            assert (case.package / "__init__.py").read_text() == "BUILD = 'merged'\n"
        else:
            assert case.code == 0 and case.err == ""
            assert "Installed Foragent v0.1.10." in case.out
            assert json.loads(case.cache.read_text())["installed"] == "v0.1.10"


def test_reinstall_race_other_errors_stop_once_and_exhausted_races_keep_failure_reporting(
    race_sandbox, monkeypatch, capsys,
):
    # The nonretryable control already works today. Pair it with exhaustion to
    # lock the retry boundary and expose the missing three-attempt budget.
    controls = [exercise_install(race_sandbox / f"other-{entry}", entry, OTHER_ERROR, None,
                                 monkeypatch, capsys) for entry in ("after-merge", "update")]
    exhausted = [(exercise_install(race_sandbox / f"exhausted-{entry}-{index}", entry, error, None,
                                   monkeypatch, capsys), error)
                 for entry in ("after-merge", "update") for index, error in enumerate(RACE_ERRORS)]
    for case in controls:
        assert_attempts(case, 1)
        assert_failed(case, OTHER_ERROR)
    for case, error in exhausted:
        assert_failed(case, error)
        assert_attempts(case, 3)


def test_reinstall_race_gate_child_environments_disable_bytecode_without_parent_opt_in(
    race_sandbox, monkeypatch,
):
    root = race_sandbox / "repo with spaces"
    root.mkdir()
    store = StateStore(race_sandbox / "gate-state")
    store.start_run(7, branch="running", worktree=root)
    calls = []

    def popen(argv, **kwargs):
        environment = kwargs.get("env")
        calls.append((list(argv), dict(os.environ if environment is None else environment)))
        return SimpleNamespace(pid=42000)

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(launcher, "observe_tree", lambda **kwargs: [])
    monkeypatch.setattr(kick, "load_config", lambda path: Config(repo="offline/reinstall-race"))
    monkeypatch.setattr(kick, "_launchd_kickstart", lambda repo: False)
    job = launchd.Launchd(root, repo="offline/reinstall-race", home=Path.home(),
                          state_root=store.root, executable=root / "bin/fa")
    environments = {}
    for inherited in (None, "0"):
        if inherited is None:
            monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
        else:
            monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", inherited)
        data = plistlib.loads(job.plist())
        environments[f"launchd (parent={inherited!r})"] = data["EnvironmentVariables"]
        calls.clear()
        assert launcher.spawn_runner(root, store)(7) == 42000
        launcher.spawn_notify(root, store)
        kick.kick_tick(root)
        baseline_dir = store.root.parent / "baselines"
        baseline_dir.mkdir(exist_ok=True)
        build = run_baseline.BaselineBuild(SHA, "pytest", [], "offline-key", "offline-scope")
        lock = (baseline_dir / "offline-key.lock").open("a")
        try:
            run_baseline.start_baseline_process(
                SimpleNamespace(repo_root=root, store=store, config=Config()), build, lock)
        finally:
            lock.close()
        assert len(calls) == 4, "Inspect runner, notification, kicked tick and baseline environments"
        for (argv, environment), command in zip(calls, ("run-issue", "notify", "tick", "foragent.run_baseline")):
            assert command in argv
            environments[f"{command} (parent={inherited!r})"] = environment
    assert all(environment.get("PYTHONDONTWRITEBYTECODE") == "1" for environment in environments.values()), (
        "Gate processes must suppress bytecode even when the caller does not: "
        + repr({name: env.get("PYTHONDONTWRITEBYTECODE") for name, env in environments.items()}))
    assert not job.plist_path.exists(), "Generating a plist must not install or start its launchd job"
