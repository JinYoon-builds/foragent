"""Acceptance test for update-check. Locked by Foragent; do not edit."""

from datetime import UTC, datetime, timedelta
import importlib.metadata
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from test_local_truth import record_gh
from test_orch_review_handoff import bind_cli, make_case
from test_skills_autosync import cli_exit, isolated_home
from test_view_open import fake_commands


# Public names selected for this contract: /fa startup facts use `fa brief`;
# ordinary CLI coverage also includes `fa gate status`. JSON has
# update={current, latest, command}, or null/absent when there is no offer.
# Machine data/update-check.json records checked_at as a UTC ISO timestamp.
# A day means 24 elapsed hours; the release transport is the existing gh seam.
# FORAGENT_UPDATE_CHECK=off opts out; the offered installation is `fa update`.
CURRENT, LATEST = "0.1.9", "0.1.10"  # Lexical comparison would miss this update.
RELEASE_API = "repos/JinYoon-builds/foragent/releases/latest"
MAX_CHECK_SECONDS = 5


@pytest.fixture
def update_case(git_history, tmp_path, monkeypatch, isolated_home):
    from foragent import capacity, cli, executors, launchd
    from foragent.config import load_config
    from foragent.state import StateStore

    commands = fake_commands(tmp_path, monkeypatch)
    monkeypatch.setenv("FORAGENT_VIEW", "off")
    monkeypatch.delenv("FORAGENT_UPDATE_CHECK", raising=False)
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], language="en")
    machine = isolated_home / ".local/share/foragent"
    monkeypatch.setattr(capacity, "machine_root", lambda: machine)
    case.store.root = machine / "owner__repo"
    main = bind_cli(case, monkeypatch)
    (case.root / ".foragent.yml").write_text(
        "repo: owner/repo\nlanguage: en\nskill_libraries: []\nnotify: none\n")
    monkeypatch.setattr(cli, "load_config", load_config)
    monkeypatch.setattr(StateStore, "for_repo", classmethod(
        lambda cls, repo, **kw: cls(machine / repo.replace("/", "__"), now=lambda: case.clock[0])))
    case.clock[0] = datetime(2026, 10, 26, 12, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            now = case.clock[0]
            return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)

    # Freeze the same datetime seam bind_cli uses, plus time.time for disk TTLs.
    # Patch loaded consumers too, so importing __version__/datetime by value
    # cannot accidentally use the installed distribution or the real clock.
    for name, module in tuple(sys.modules.items()):
        if name == "foragent" or name.startswith("foragent."):
            if getattr(module, "datetime", None) is datetime:
                monkeypatch.setattr(module, "datetime", Clock)
            if hasattr(module, "__version__"):
                monkeypatch.setattr(module, "__version__", CURRENT)
    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(time, "time", lambda: case.clock[0].timestamp())
    version = importlib.metadata.version
    monkeypatch.setattr(importlib.metadata, "version", lambda name: (
        CURRENT if name == "foragent" else version(name)))

    case.main, case.commands, case.machine = main, commands, machine
    case.release = {"tag_name": f"v{LATEST}", "target_commitish": "main",
                    "draft": False, "prerelease": False,
                    "html_url": f"https://github.com/JinYoon-builds/foragent/releases/tag/v{LATEST}"}
    case.failure = None
    case.requests = []

    def reply(argv):
        assert argv[:2] == ["api", RELEASE_API], f"Unexpected GitHub request: {argv}"
        if case.failure == "timeout":
            raise subprocess.TimeoutExpired(["gh", *argv], MAX_CHECK_SECONDS)
        if case.failure == "command-error":
            raise subprocess.CalledProcessError(1, ["gh", *argv], stderr="offline release lookup failed")
        return "not valid JSON" if case.failure == "bad-json" else json.dumps(case.release)

    case.gh = record_gh(monkeypatch, reply=reply)
    recorded_run = subprocess.run

    def run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and str(argv[0]) == "gh":
            case.requests.append(dict(kwargs))
        return recorded_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)

    def forbidden(*args, **kwargs):
        pytest.fail("Update checks must stay offline in tests and must never start a model turn")

    for method in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, method, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(executors.ClaudeExecutor, "run", forbidden)
    monkeypatch.setattr(executors.CodexExecutor, "run", forbidden)

    # Only local Git fixtures and the recorded installer may spawn processes.
    # In particular, an unmocked gh/launchctl/model executable cannot escape.
    popen = subprocess.Popen

    def local_popen(argv, *args, **kwargs):
        assert isinstance(argv, (list, tuple)) and argv, f"Unexpected shell command: {argv}"
        executable = Path(argv[0]).name
        assert executable in {"git", "uv"}, f"Unrecorded process: {argv}"
        if executable == "git":
            assert not any("://" in str(arg) or str(arg).startswith("git@") for arg in argv)
        else:
            assert Path(shutil.which(str(argv[0]))).resolve() == (tmp_path / "bin/uv").resolve()
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", local_popen)
    monkeypatch.setattr(launchd, "_run_launchctl", forbidden)
    return case


def invoke(case, capsys, *args, root=None):
    capsys.readouterr()
    code = cli_exit(case.main, [*args, "--repo-root", str(root or case.root)])
    output = capsys.readouterr()
    return code, output


def facts(case, capsys, *entry, root=None):
    code, output = invoke(case, capsys, *entry, "--json", root=root)
    assert code == 0, f"fa {' '.join(entry)} must succeed: {output.err}"
    assert output.err == "", "A routine update lookup must be silent on stderr"
    return json.loads(output.out)


def assert_lookup(case, count):
    assert len(case.gh) == count, f"Expected {count} release lookup(s), observed {len(case.gh)}"
    assert len(case.requests) == count
    assert all(type(row.get("timeout")) in (int, float)
               and 0 < row["timeout"] <= MAX_CHECK_SECONDS for row in case.requests), (
        "Every release lookup needs a positive subprocess timeout of at most 5 seconds")


def assert_offer(payload):
    offer = payload.get("update")
    assert isinstance(offer, dict), "Startup/CLI facts must carry an update field when a newer release exists"
    assert {key: offer.get(key) for key in ("current", "latest", "command")} == {
        "current": CURRENT, "latest": LATEST, "command": "fa update"}


def assert_checked_at(case, expected):
    path = case.machine / "update-check.json"
    assert path.is_file(), "The daily check timestamp must persist in the machine data directory"
    timestamp = json.loads(path.read_text())["checked_at"]
    assert datetime.fromisoformat(timestamp.replace("Z", "+00:00")) == expected


@pytest.mark.parametrize("entry", [("brief",), ("gate", "status")], ids=["fa-startup", "cli-status"])
def test_update_check_offers_one_human_line_and_json_facts_without_installing(update_case, capsys, entry):
    case = update_case
    code, output = invoke(case, capsys, *entry)
    assert code == 0 and output.err == ""
    assert_lookup(case, 1)
    lines = [line for line in output.out.splitlines() if "fa update" in line]
    assert len(lines) == 1 and LATEST in lines[0], "Offer the newer version and exact command on one line"
    assert_offer(facts(case, capsys, *entry))
    assert_lookup(case, 1)  # The cached offer must survive switching output modes.
    assert case.commands.calls() == [], "Offering an update must never install it or restart the gate"


def test_update_check_machine_timestamp_is_shared_across_commands_and_projects_for_24_hours(
    update_case, tmp_path, capsys,
):
    case = update_case
    started = case.clock[0]
    assert_offer(facts(case, capsys, "brief"))
    assert_lookup(case, 1)
    assert_checked_at(case, started)
    other = tmp_path / "another-project"
    other.mkdir()
    (other / ".foragent.yml").write_text("repo: another/project\nlanguage: en\nskill_libraries: []\n")
    case.clock[0] = started + timedelta(days=1, seconds=-1)
    assert_offer(facts(case, capsys, "gate", "status", root=other))
    assert_lookup(case, 1)
    assert_checked_at(case, started)
    case.clock[0] = started + timedelta(days=1)
    assert_offer(facts(case, capsys, "brief", root=other))
    assert_lookup(case, 2)
    assert_checked_at(case, case.clock[0])
    assert case.commands.calls() == []


@pytest.mark.parametrize("failure", ["timeout", "command-error", "bad-json"])
def test_update_check_failure_is_silent_cached_and_retried_next_day(update_case, capsys, failure):
    case = update_case
    started = case.clock[0]
    case.failure = failure
    assert facts(case, capsys, "brief").get("update") is None
    assert_lookup(case, 1)
    assert_checked_at(case, started)
    code, output = invoke(case, capsys, "brief")
    assert code == 0 and output.err == ""
    assert "fa update" not in output.out and "offline" not in output.out.lower()
    assert "timeout" not in output.out.lower() and "failed" not in output.out.lower()
    assert_lookup(case, 1)
    case.failure = None
    case.clock[0] = started + timedelta(days=1, seconds=-1)
    assert facts(case, capsys, "brief").get("update") is None
    assert_lookup(case, 1)
    case.clock[0] = started + timedelta(days=1)
    assert_offer(facts(case, capsys, "brief"))
    assert_lookup(case, 2)
    assert_checked_at(case, case.clock[0])
    assert case.commands.calls() == []


@pytest.mark.parametrize("latest", [CURRENT, "0.1.8"], ids=["equal", "older"])
def test_update_check_equal_or_older_release_has_no_offer(update_case, capsys, latest):
    case = update_case
    case.release["tag_name"] = f"v{latest}"
    assert facts(case, capsys, "brief").get("update") is None
    assert_lookup(case, 1)  # An absent check cannot satisfy the negative case.
    code, output = invoke(case, capsys, "brief")
    assert code == 0 and output.err == "" and "fa update" not in output.out
    case.clock[0] += timedelta(days=1)
    case.release["tag_name"] = f"v{LATEST}"
    assert_offer(facts(case, capsys, "brief"))
    assert_lookup(case, 2)
    assert case.commands.calls() == []


def test_update_check_env_opt_out_makes_no_request_and_does_not_consume_the_day(
    update_case, monkeypatch, capsys,
):
    case = update_case
    monkeypatch.setenv("FORAGENT_UPDATE_CHECK", "off")
    assert facts(case, capsys, "brief").get("update") is None
    assert facts(case, capsys, "gate", "status").get("update") is None
    assert_lookup(case, 0)
    assert not (case.machine / "update-check.json").exists()
    monkeypatch.delenv("FORAGENT_UPDATE_CHECK")
    assert_offer(facts(case, capsys, "brief"))
    assert_lookup(case, 1)  # Positive control: opting back in works on the same day.
    assert case.commands.calls() == []


@pytest.fixture
def installer(update_case, git_history, monkeypatch, tmp_path):
    from conftest import git_command
    from foragent import launchd, orch_reinstall
    from foragent.state import StateStore

    case = update_case
    source_init = f"__version__ = '{LATEST}'\n"
    payload = "RELEASE_PAYLOAD = 'the new release, not the caller checkout'\n"
    git_history.update({"pyproject.toml": f'[project]\nname = "foragent"\nversion = "{LATEST}"\n',
                        "src/foragent/__init__.py": source_init,
                        "src/foragent/release_payload.py": payload})
    git_command(git_history.origin, "update-ref", f"refs/tags/v{LATEST}", git_history.tip)
    before = git_command(case.root, "rev-parse", "HEAD")
    local = case.root / "unrelated-local.txt"
    local.write_text("keep me\n")
    events = []
    original = orch_reinstall._reinstall

    def assert_tick_held():
        with StateStore(case.store.root).tick_lock() as acquired:
            assert not acquired, "Install and gate restart must hold the actual gate tick lock"

    def reinstall(*args, **kwargs):
        assert_tick_held()
        events.append("reinstall")
        result = original(*args, **kwargs)
        assert result is not None, "fa update must install the Foragent release"
        events.append("verified")
        return result

    monkeypatch.setattr(orch_reinstall, "_reinstall", reinstall)
    monkeypatch.setattr(case.git, "remote_slug", lambda: "owner/repo")
    monkeypatch.setattr(launchd, "GitRepo", lambda root: case.git)
    monkeypatch.setattr(launchd, "fa_executable", lambda: tmp_path / "bin/fa")
    monkeypatch.setattr(launchd, "status", lambda *a, **kw: launchd.LaunchdStatus("offline", True, True))

    def launchctl(argv):
        assert_tick_held()
        assert events and events[-1] in {"verified", "bootout", "bootstrap", "kickstart"}, (
            "Restart only after the shared reinstaller verifies the installed package")
        assert argv[0] in {"bootout", "bootstrap", "kickstart"}, argv
        events.append(argv[0])
        return subprocess.CompletedProcess(["launchctl", *argv], 0, stdout="", stderr="")

    monkeypatch.setattr(launchd, "_run_launchctl", launchctl)
    return SimpleNamespace(case=case, events=events, before=before, local=local,
                           source_init=source_init, payload=payload)


def assert_installed(installer):
    from conftest import git_command
    from test_orch_reinstall import install_calls

    item, case = installer, installer.case
    assert item.events.count("reinstall") == 1, "fa update must reuse orch_reinstall._reinstall exactly once"
    calls = install_calls(case.commands)
    assert len(calls) == 1 and calls[0]["argv"][:4] == ["tool", "install", "--force", "--reinstall"]
    assert calls[0]["source_init"] == item.source_init
    assert Path(calls[0]["source_root"]).resolve() != case.root.resolve()
    package = case.commands.tool_dir / "foragent/lib/python3.12/site-packages/foragent"
    assert (package / "__init__.py").read_text() == item.source_init
    assert (package / "release_payload.py").read_text() == item.payload
    assert any(row["argv"] == ["tool", "dir"] for row in case.commands.calls("uv"))
    assert "bootstrap" in item.events or "kickstart" in item.events, "Updating must restart the gate"
    assert item.events.index("verified") < next(
        index for index, event in enumerate(item.events) if event in {"bootout", "bootstrap", "kickstart"})
    assert git_command(case.root, "rev-parse", "HEAD") == item.before
    assert item.local.read_text() == "keep me\n"
    with case.store.tick_lock() as acquired:
        assert acquired, "fa update must release the tick lock when finished"


def test_update_check_explicit_fa_update_reuses_verified_reinstall_then_restarts_between_ticks(installer, capsys):
    code, output = invoke(installer.case, capsys, "update")
    assert code == 0, f"fa update must install and restart successfully (exit {code}): {output.err.strip()}"
    assert_installed(installer)


def test_update_check_busy_tick_prevents_install_and_restart_then_allows_retry(installer, monkeypatch, capsys):
    case = installer.case

    def no_wait(seconds):
        pytest.fail("A busy tick must return for retry rather than wait inside fa update")

    # StateStore.tick_lock is nonblocking. Choose a nonzero, retryable busy exit.
    with monkeypatch.context() as busy, case.store.tick_lock() as acquired:
        busy.setattr(time, "sleep", no_wait)
        assert acquired
        code, output = invoke(case, capsys, "update")
        assert code != 0, "fa update must defer while a gate tick owns the lock"
        assert installer.events == [] and case.commands.calls() == []
        assert "tick" in (output.out + output.err).lower(), "Explain the busy tick so the person can retry"
    code, output = invoke(case, capsys, "update")
    assert code == 0, f"fa update must work once the tick finishes: {output.err.strip()}"
    assert_installed(installer)


@pytest.mark.parametrize("mode", ["error", "mismatch"])
def test_update_check_failed_install_or_verification_never_restarts_gate(installer, monkeypatch, capsys, mode):
    from test_orch_reinstall import install_calls

    monkeypatch.setenv("FA_D57_INSTALL_MODE", mode)
    code, output = invoke(installer.case, capsys, "update")
    assert installer.events == ["reinstall"], "fa update must attempt the shared installer and stop on failure"
    assert len(install_calls(installer.case.commands)) == 1
    assert code != 0 and (output.out + output.err).strip(), "Report installation/verification failure"
    with installer.case.store.tick_lock() as acquired:
        assert acquired, "Installation failure must release the tick lock"
