"""The public restart and gate projection use fake launchctl only."""

from datetime import UTC, datetime
import plistlib
import subprocess
from types import SimpleNamespace

import pytest

from foragent import gates, launchd
from foragent.state import StateStore


@pytest.mark.parametrize("failures", [1, 2])
@pytest.mark.parametrize("failure_kind", ["exit", "oserror"])
@pytest.mark.parametrize("language", ["en", "ko"])
def test_restart_recovers_failed_bootstrap_and_status_keeps_failure_fact(tmp_path, monkeypatch, failures, failure_kind, language):
    home, root, state = tmp_path / "home", tmp_path / "project", tmp_path / "machine/owner__repo"
    root.mkdir()
    (root / ".foragent.yml").write_text(f"repo: owner/repo\nlanguage: {language}\n")
    path = home / "Library/LaunchAgents/dev.foragent.owner.repo.plist"
    path.parent.mkdir(parents=True)
    path.write_bytes(plistlib.dumps({"Label": "dev.foragent.owner.repo", "ProgramArguments": [
        "/offline/fa", "gate", "tick", "--repo-root", str(root)], "StartInterval": 300,
        "EnvironmentVariables": {"PYTHONDONTWRITEBYTECODE": "1"}}))
    job = launchd.InstalledJob(path, "dev.foragent.owner.repo", root, "gui/501")
    loaded, attempts, calls, definitions = [True], [0], [], []

    def call(argv):
        calls.append(argv)
        if argv[0] == "bootout":
            loaded[0] = False
        elif argv[0] == "bootstrap":
            attempts[0] += 1
            definitions.append(plistlib.loads(path.read_bytes()))
            if attempts[0] <= failures:
                if failure_kind == "oserror":
                    raise OSError("injected launchd unavailable")
                return subprocess.CompletedProcess(argv, 5, "", "injected bootstrap failure")
            loaded[0] = True
        elif argv[0] == "print":
            return subprocess.CompletedProcess(argv, 0 if loaded[0] else 113, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(launchd, "_run_launchctl", call)
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, *a, **kw: cls(state)))
    monkeypatch.setattr(launchd.time, "sleep", lambda _: None)
    try:
        job.restart()
    except launchd.LaunchdError:
        pass  # A recovered failure can still be reported as a failed reload.
    assert loaded[0], "Failed bootstrap left the gate unloaded"
    assert attempts[0] == failures + 1
    rows = gates.collect(datetime.now(UTC), home=home, data_root=state.parent)
    row, = rows
    assert row["loaded"] is True
    fact = row.get("reload")
    assert isinstance(fact, dict) and fact["recovered"] is True
    assert fact["attempts"] == failures + 1 and fact["error"] and fact["at"]
    human = "\n".join(gates.human_lines(rows, datetime.now(UTC), language))
    assert "fa gate install --repo-root" in human and str(root) in human
    if language == "ko":
        assert any("가" <= ch <= "힣" for ch in human)
    # Even rollback from a legacy unwrapped plist must retain/apply D97.
    assert definitions[-1]["ProgramArguments"][:3] == ["/usr/bin/osascript", "-l", "JavaScript"]
    # A successful later reload replaces the latest fact rather than lying about the old failure.
    job.restart()
    row, = gates.collect(datetime.now(UTC), home=home, data_root=state.parent)
    assert row["reload"]["error"] is None and row["reload"]["recovered"] is False


def test_detached_refresh_retries_a_recorded_absent_job_without_needing_another_tick(tmp_path, monkeypatch):
    from foragent import update_check

    home, root = tmp_path / "home", tmp_path / "project"
    root.mkdir()
    (root / ".foragent.yml").write_text("repo: owner/repo\nlanguage: en\n")
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    path = agents / "dev.foragent.owner.repo.plist"
    path.write_bytes(plistlib.dumps({"Label": "dev.foragent.owner.repo", "ProgramArguments": [
        "/offline/fa", "gate", "tick", "--repo-root", str(root)],
        "EnvironmentVariables": {"PYTHONDONTWRITEBYTECODE": "1"}}))
    store = StateStore(tmp_path / "state")
    store._write_json(store.root / "launchd-reload.json", {
        "at": datetime.now(UTC).isoformat(), "error": "bootstrap recovery exhausted", "attempts": 3,
        "recovered": False, "loaded": False, "pending": True, "label": "dev.foragent.owner.repo",
        "plist": str(path)})
    loaded, starts = [False], []

    def restart():
        starts.append("restart")
        loaded[0] = True

    job = SimpleNamespace(repo_root=root, plist_path=path, label="dev.foragent.owner.repo",
                          state=lambda: "loaded" if loaded[0] else "absent", loaded=lambda: loaded[0], restart=restart)
    monkeypatch.setattr(launchd, "installed_jobs", lambda *a, **kw: [job])
    monkeypatch.setattr(launchd, "needs_environment_refresh", lambda _: False)
    monkeypatch.setattr(update_check, "gate_store", lambda _: ("owner/repo", store))
    assert launchd.refresh_jobs(root, sleep=lambda _: None) == "restarted"
    assert loaded[0] and starts == ["restart"]
