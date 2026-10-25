"""Boundary tests for the daily release check and fa update retries (#417)."""

from datetime import UTC, datetime
import fcntl
import json
import threading
import time
from pathlib import Path

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
    monkeypatch.setattr(update_check, "installed_gates", lambda: list(gates))
    monkeypatch.setattr(update_check.orch_reinstall, "_uv", lambda args, cwd: installs.append(args) or "")

    def restart(root: Path, language: str) -> str:
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
