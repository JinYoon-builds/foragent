"""Boundary tests for the daily release check and fa update retries (#417)."""

from datetime import UTC, datetime
import fcntl
import json
import os
import plistlib
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from foragent import capacity, update_check


@pytest.fixture
def machine(tmp_path, monkeypatch):
    root = tmp_path / "machine"
    root.mkdir()
    monkeypatch.setattr(capacity, "machine_root", lambda: root)
    monkeypatch.delenv("FORAGENT_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(update_check, "current_version", lambda: "0.1.9")
    return root


def test_concurrent_checks_share_one_lookup(machine, monkeypatch):
    lookups = []
    monkeypatch.setattr(update_check, "lookup", lambda: lookups.append(1) or "v0.1.10")
    results = []
    with (machine / "update-check.lock").open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        worker = threading.Thread(target=lambda: results.append(update_check.check(None)))
        worker.start()
        time.sleep(0.2)
        assert worker.is_alive(), "A second check must wait for the machine check lock"
        # The process holding the lock finishes its lookup and records it.
        (machine / "update-check.json").write_text(json.dumps({
            "checked_at": datetime.now(UTC).isoformat(), "latest": "v0.1.10"}))
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        worker.join(5)
    assert lookups == [], "The waiting check must reuse the fresh record, not call GitHub again"
    assert results == [{"current": "0.1.9", "latest": "0.1.10", "command": "fa update"}]


def _fake_update(monkeypatch, gates, busy):
    installs, restarts = [], []
    monkeypatch.setattr(update_check, "lookup", lambda: "v0.1.10")
    monkeypatch.setattr(update_check, "installed_gates", lambda: {gate: [] for gate in gates})
    monkeypatch.setattr(update_check.orch_reinstall, "_uv", lambda args, cwd: installs.append(args) or "")

    def restart(root: Path, jobs, language: str) -> str:
        if root in busy:
            return "busy"
        restarts.append(root)
        return "restarted"

    monkeypatch.setattr(update_check, "_restart", restart)
    return installs, restarts


def test_installed_tag_counts_as_current_when_package_metadata_does_not_change(machine, monkeypatch, tmp_path):
    gate = (tmp_path / "gate").resolve()
    installs, restarts = _fake_update(monkeypatch, [gate], set())
    monkeypatch.setattr(update_check, "current_version", lambda: "0.1.0")
    assert update_check.update() == 0
    assert len(installs) == 1 and restarts == [gate]
    assert update_check.update() == 0
    assert len(installs) == 1 and restarts == [gate], "Same release again: report nothing newer"
    assert update_check.check(None) is None, "The installed tag is no longer offered"


def test_busy_gate_skipped_after_install_is_finished_by_the_next_update(machine, monkeypatch, tmp_path):
    busy, free = (tmp_path / "busy").resolve(), (tmp_path / "free").resolve()
    skipped = {busy}
    installs, restarts = _fake_update(monkeypatch, [busy, free], skipped)
    assert update_check.update() == 0
    assert len(installs) == 1 and restarts == [free]
    # A new process now runs the installed release; the busy tick has ended.
    monkeypatch.setattr(update_check, "current_version", lambda: "0.1.10")
    skipped.clear()
    restarts.clear()
    assert update_check.update() == 0
    assert len(installs) == 2, "A skipped restart repeats the whole update"
    assert sorted(restarts) == sorted([busy, free])
    restarts.clear()
    assert update_check.update() == 0
    assert len(installs) == 2 and restarts == [], "Once every gate restarted, nothing is newer"


def test_all_pending_gates_still_busy_exits_75(machine, monkeypatch, tmp_path):
    gate = (tmp_path / "gate").resolve()
    _fake_update(monkeypatch, [gate], {gate})
    (machine / "update-check.json").write_text(json.dumps({
        "installed": "v0.1.10", "pending_restart": [str(gate)]}))
    assert update_check.update() == update_check.BUSY_EXIT
    assert json.loads((machine / "update-check.json").read_text())["pending_restart"] == [str(gate)]


def _gate(tmp_path, monkeypatch, *, repo, origin, label):
    """One installed gate whose plist uses ``label``; launchctl calls are recorded."""
    from foragent import launchd

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "project"
    root.mkdir()
    (root / ".foragent.yml").write_text(f"repo: {repo}\n" if repo else "")
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    plist = agents / f"{label}.plist"
    plist.write_bytes(plistlib.dumps({
        "Label": label, "ProgramArguments": ["/bin/fa", "gate", "tick", "--repo-root", str(root)]}))
    stores = tmp_path / "stores"
    monkeypatch.setattr(update_check.StateStore, "for_repo", classmethod(
        lambda cls, name, **kw: cls(stores / name.replace("/", "__"))))
    monkeypatch.setattr(update_check, "GitRepo", lambda path: SimpleNamespace(remote_slug=lambda: origin))
    calls = []

    def launchctl(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="state = waiting\n", stderr="")

    monkeypatch.setattr(launchd, "_run_launchctl", launchctl)
    return root.resolve(), plist.resolve(), stores, calls


def test_restart_locks_the_store_named_by_config_repo_not_origin(machine, tmp_path, monkeypatch):
    root, plist, stores, calls = _gate(tmp_path, monkeypatch, repo="owner/renamed", origin="owner/origin",
                                       label="dev.foragent.owner.origin.12345678")
    gates = update_check.installed_gates()
    with update_check.StateStore(stores / "owner__renamed").tick_lock() as held:
        assert held
        assert update_check._restart(root, gates[root], "en") == "busy"
    assert [argv[0] for argv in calls] == ["print"], "Never restart while the gate's own tick runs"
    # Origin's directory is not the gate's lock: holding it must not stop the restart.
    with update_check.StateStore(stores / "owner__origin").tick_lock() as held:
        assert held
        assert update_check._restart(root, gates[root], "en") == "restarted"
    assert [argv[0] for argv in calls[1:]] == ["print", "bootout", "bootstrap"]


def test_loaded_legacy_label_plist_is_restarted(machine, tmp_path, monkeypatch):
    root, plist, stores, calls = _gate(tmp_path, monkeypatch, repo=None, origin="owner/repo",
                                       label="dev.foragent.owner.repo")
    gates = update_check.installed_gates()
    assert list(gates) == [root]
    assert update_check._restart(root, gates[root], "en") == "restarted"
    target = f"gui/{os.getuid()}/dev.foragent.owner.repo"
    assert calls == [["print", target], ["bootout", target], ["bootstrap", f"gui/{os.getuid()}", str(plist)]]


@pytest.mark.parametrize("command", [["brief"], ["gate", "status"]])
def test_check_lock_failure_is_silent_in_brief_and_status(machine, monkeypatch, tmp_path, capsys, command):
    """A machine root that cannot hold the lock file skips the check, without a lookup."""
    from foragent import cli

    blocked = tmp_path / "not-a-directory"
    blocked.write_text("")
    monkeypatch.setattr(capacity, "machine_root", lambda: blocked / "foragent")
    lookups = []
    monkeypatch.setattr(update_check, "lookup", lambda: lookups.append(1) or "v0.1.10")
    assert update_check.check(None) is None
    calls = []
    real = update_check.check
    monkeypatch.setattr(update_check, "check", lambda config: calls.append(real(config)) or calls[-1])
    root = tmp_path / "project"
    root.mkdir()
    (root / ".foragent.yml").write_text("repo: owner/repo\n")
    monkeypatch.setattr(cli.StateStore, "for_repo", classmethod(lambda cls, repo, **kw: cls(tmp_path / "state")))
    monkeypatch.setattr(cli.launchd, "status", lambda *a, **kw: cli.launchd.LaunchdStatus("x", False, False))
    monkeypatch.setattr(cli, "GhTracker", lambda repo: SimpleNamespace())
    capsys.readouterr()
    assert cli.main([*command, "--json", "--repo-root", str(root)]) == 0
    output = capsys.readouterr()
    assert output.err == "" and "update" not in json.loads(output.out)
    assert calls == [None] and lookups == []
