"""Acceptance test for installed-gates-visible. Locked by Foragent; do not edit."""

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from test_heartbeats import heartbeats_write
from test_orch_review_handoff import bind_cli, make_case
from test_skills_autosync import cli_exit, isolated_home
from test_state_split import offline_acceptance


@pytest.fixture(autouse=True)
def installed_environment(monkeypatch, isolated_home, offline_acceptance):
    """Only local Git fixture processes are permitted; launchd is supplied separately."""
    from foragent import capacity, launchd
    from foragent.executors import ClaudeExecutor, CodexExecutor

    monkeypatch.setattr(capacity, "machine_root", lambda: isolated_home / ".local/share/foragent")

    def forbidden(*args, **kwargs):
        pytest.fail("installed-gates-visible must not run OS jobs, send signals or start models")

    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(signal, "raise_signal", forbidden)
    monkeypatch.setattr(ClaudeExecutor, "run", forbidden)
    monkeypatch.setattr(CodexExecutor, "run", forbidden)
    monkeypatch.setattr(launchd, "_run_launchctl", forbidden)
    popen = subprocess.Popen

    def local_git(argv, *args, **kwargs):
        assert isinstance(argv, (list, tuple)) and Path(argv[0]).name == "git", argv
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", local_git)


@pytest.fixture
def installed_gates(git_history, tmp_path, monkeypatch, isolated_home, installed_environment):
    from foragent import cli, launchd
    from foragent.config import load_config
    from foragent.state import StateStore

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], language="en")
    case.store = case.ctx.store = StateStore.for_repo(case.ctx.repo, now=case.ctx.now)
    # bind_cli isolates GitHub and the clock. Restore its single-store/job shortcuts:
    # discovering other repos must exercise the real StateStore and launchd paths.
    lookup, status = StateStore.__dict__["for_repo"], launchd.status
    main = bind_cli(case, monkeypatch)
    monkeypatch.setattr(StateStore, "for_repo", lookup)
    monkeypatch.setattr(launchd, "status", status)
    monkeypatch.setattr(cli, "load_config", load_config)
    loaded, probes, expected, jobs = {}, [], {}, {}

    def launchctl(argv):
        assert len(argv) == 2 and argv[0] == "print", argv
        probes.append(argv[1])
        return subprocess.CompletedProcess(
            argv, 0 if loaded.get(argv[1], False) else 113,
            stdout="state = waiting\nlast exit code = 0\n", stderr="",
        )

    monkeypatch.setattr(launchd, "_run_launchctl", launchctl)
    now = case.clock[0]
    fixtures = [
        # repo, tick age, running count, job loaded, plist present
        ("owner/repo", timedelta(seconds=30), 1, True, True),
        ("owner/forgotten", timedelta(minutes=20), 0, True, True),
        ("owner/busy", timedelta(seconds=45), 2, True, True),
        ("owner/unloaded", timedelta(hours=2), 0, False, True),
        ("owner/never-ticked", None, 0, True, True),
        ("owner/state-only", timedelta(hours=1), 0, False, False),
    ]
    for repo, age, running, is_loaded, has_plist in fixtures:
        directory = case.root if repo == case.ctx.repo else tmp_path / repo.split("/")[1]
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".foragent.yml").write_text(yaml.safe_dump({
            "repo": repo, "skill_libraries": [], "language": "en",
        }), encoding="utf-8")
        store = StateStore.for_repo(repo, now=case.ctx.now)
        assert store.root.parent == isolated_home / ".local/share/foragent"
        last_tick = now - age if age is not None else None
        if last_tick is not None:
            heartbeats_write(store, "gate", last_tick)
        for number in range(1, running + 1):
            store.start_run(number, branch=f"foragent/issue-{number}", worktree=directory / f"run-{number}")
        job = launchd.Launchd(directory, repo=repo, executable=isolated_home / ".local/bin/fa")
        if has_plist:
            job.plist_path.parent.mkdir(parents=True, exist_ok=True)
            # Use the exact format written by Launchd.install(), without installing anything.
            job.plist_path.write_bytes(job.plist())
        loaded[job.target] = is_loaded
        jobs[repo] = job
        expected[repo] = {
            "last_tick": last_tick.isoformat() if last_tick is not None else None,
            "running_runs": running, "loaded": is_loaded,
        }

    assert not jobs["owner/never-ticked"].state_root.exists(), "exercise discovery from a plist alone"
    assert not jobs["owner/state-only"].plist_path.exists(), "exercise discovery from state alone"
    # Other launch agents and ordinary machine-data directories are not Foragent gates.
    unrelated = isolated_home / "Library/LaunchAgents/com.example.other.plist"
    unrelated.write_bytes(plistlib.dumps({"Label": "com.example.other", "ProgramArguments": ["true"]}))
    (isolated_home / ".local/share/foragent/cache").mkdir()
    return SimpleNamespace(case=case, main=main, expected=expected, jobs=jobs,
                           loaded=loaded, probes=probes)


def test_installed_gates_visible_json_discovers_plists_and_state_even_without_work(
    installed_gates, capsys,
):
    gates = installed_gates
    assert gates.main(["gate", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    rows = payload.get("installed_gates")
    assert isinstance(rows, list), (
        "gate status --json must expose installed_gates, including forgotten idle repositories; "
        f"got {rows!r}"
    )
    assert all(isinstance(row, dict) and isinstance(row.get("repo"), str) for row in rows), rows
    actual = {row["repo"]: row for row in rows}
    assert len(actual) == len(rows), "a gate found through both plist and state must appear once"
    assert actual.keys() == gates.expected.keys(), (
        f"inventory must combine Foragent plists and state folders: {sorted(actual)!r}"
    )
    for repo, expected in gates.expected.items():
        row = actual[repo]
        assert {key: row.get(key) for key in expected} == expected, (repo, row, expected)
        assert type(row["running_runs"]) is int and type(row["loaded"]) is bool, row
        if row["last_tick"] is not None:
            datetime.fromisoformat(row["last_tick"])
    assert {job.target for repo, job in gates.jobs.items() if gates.expected[repo]["loaded"]} <= set(gates.probes)


def test_installed_gates_visible_fa_status_lists_tick_runs_and_scheduler_state(installed_gates, capsys):
    gates = installed_gates
    result = cli_exit(gates.main, ["status", "--repo-root", str(gates.case.root)])
    output = capsys.readouterr()
    assert result == 0, f"fa status must provide the human machine gate inventory: {output.err}"
    for repo, expected in gates.expected.items():
        rows = [line for line in output.out.splitlines() if repo in line]
        assert rows, f"fa status omitted installed gate {repo}, even though it may have no work"
        assert any(f"running={expected['running_runs']}" in row for row in rows), (repo, rows)
        assert any(("loaded" if expected["loaded"] else "not loaded") in row for row in rows), (repo, rows)
        if expected["last_tick"] is not None:
            assert any(expected["last_tick"] in row for row in rows), (repo, rows)
        else:
            assert any("never" in row.lower() or "unknown" in row.lower() for row in rows), rows


def test_installed_gates_visible_only_loaded_old_tick_gets_human_stale_warning(installed_gates, capsys):
    gates = installed_gates
    job = gates.jobs["owner/forgotten"]
    warnings = []
    for age, loaded in ((timedelta(minutes=20), True), (timedelta(seconds=30), True),
                        (timedelta(minutes=20), False)):
        from foragent.state import StateStore

        heartbeats_write(StateStore(job.state_root), "gate", gates.case.clock[0] - age)
        gates.loaded[job.target] = loaded
        assert gates.main(["gate", "status"]) == 0
        rows = [line for line in capsys.readouterr().out.splitlines() if "owner/forgotten" in line]
        assert rows, "human status must show the forgotten idle gate before classifying its heartbeat"
        warnings.append(any("⚠" in row for row in rows))
    assert warnings == [True, False, False], (
        f"old loaded gates need ⚠; fresh or unloaded gates do not: {warnings!r}"
    )


def test_installed_gates_visible_lane_frame_names_other_gates_in_one_section(installed_gates, capsys):
    from foragent import watch

    gates = installed_gates
    assert gates.main(["gate", "status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    snapshot = watch.collect(status, gates.case.tracker, gates.case.store,
                             gates.case.config, gates.case.clock[0])
    frame = watch.render_frame(snapshot, gates.case.config, gates.case.root,
                               width=240, now=gates.case.clock[0])
    others = set(gates.expected) - {gates.case.ctx.repo}
    lines = frame.splitlines()
    matches = [index for index, line in enumerate(lines) if all(repo in line for repo in others)]
    assert len(matches) == 1, (
        f"rendered lane frame must have one compact line naming all other installed gates {sorted(others)!r}:\n{frame}"
    )
    index = matches[0]
    assert gates.case.ctx.repo not in lines[index], "the other-gates line must exclude the current repo"
    assert index > 0 and not lines[index - 1].strip(), "installed gates must occupy their own view section"
    assert index + 1 == len(lines) or not lines[index + 1].strip(), "keep the next section separate"
