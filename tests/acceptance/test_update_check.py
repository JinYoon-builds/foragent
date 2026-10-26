"""Acceptance test for update-check. Locked by Foragent; do not edit."""

from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
import fcntl
import importlib.metadata
import json
from pathlib import Path
import plistlib
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from test_local_truth import record_gh
from test_orch_review_handoff import bind_cli, make_case
from test_skills_autosync import cli_exit, isolated_home, write_global_config
from test_view_open import CommandLog


# Public names selected for this contract: /fa startup facts use `fa brief`;
# ordinary CLI coverage also includes `fa gate status`. JSON has
# update={current, latest, command}, or null/absent when there is no offer.
# Machine data/update-check.json records checked_at as a UTC ISO timestamp.
# A day means 24 elapsed hours; the release transport is the existing gh seam.
# FORAGENT_UPDATE_CHECK=off opts out; the offered installation is `fa update`.
# Jin's 2026-10-25 07:05 decision supersedes the original install assertions:
# install the upstream release tag under machine data/install.lock, then restart
# plist-discovered gates under their individual tick locks. Historical test
# function names below remain locked, including the old reinstall/busy wording.
CURRENT, LATEST = "0.1.9", "0.1.10"  # Lexical comparison would miss this update.
RELEASE_API = "repos/JinYoon-builds/foragent/releases/latest"
INSTALL_SOURCE = "git+https://github.com/JinYoon-builds/foragent@"
MAX_CHECK_SECONDS = 5


@pytest.fixture
def update_case(git_history, tmp_path, monkeypatch, isolated_home):
    from foragent import capacity, cli, executors, launchd
    from foragent.config import load_config
    from foragent.state import StateStore

    log = tmp_path / "commands.jsonl"
    log.write_text("")
    commands = CommandLog(log, isolated_home / ".local/share/uv/tools")
    monkeypatch.setenv("UV_TOOL_DIR", str(commands.tool_dir))
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(isolated_home / ".local/bin"))
    monkeypatch.setenv("UV_CACHE_DIR", str(isolated_home / ".cache/uv"))
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
    case.uv_reply = case.launchctl_reply = None

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
        assert isinstance(argv, (list, tuple)) and argv, f"Unexpected shell command: {argv}"
        executable = Path(argv[0]).name
        if executable == "gh":
            case.requests.append(dict(kwargs))
            return recorded_run(["gh", *argv[1:]], *args, **kwargs)
        if executable in {"uv", "launchctl"}:
            row = {"executable": executable, "argv": list(argv[1:]),
                   "cwd": str(Path(kwargs.get("cwd") or Path.cwd()).resolve())}
            with log.open("a") as stream:
                stream.write(json.dumps(row) + "\n")
            handler = case.uv_reply if executable == "uv" else case.launchctl_reply
            assert handler is not None, f"Routine observations must not run {executable}: {argv}"
            result = handler(list(argv[1:]))
            if kwargs.get("check") and result.returncode:
                raise subprocess.CalledProcessError(result.returncode, argv,
                                                    output=result.stdout, stderr=result.stderr)
            return result
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

    # All uv/gh/launchctl calls return above without spawning any executable.
    # Only local Git fixture processes may reach Popen.
    popen = subprocess.Popen

    def local_popen(argv, *args, **kwargs):
        assert isinstance(argv, (list, tuple)) and argv, f"Unexpected shell command: {argv}"
        executable = Path(argv[0]).name
        assert executable == "git", f"Unrecorded process: {argv}"
        assert not any("://" in str(arg) or str(arg).startswith("git@") for arg in argv)
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", local_popen)
    monkeypatch.setattr(launchd, "_run_launchctl", lambda argv: subprocess.run(
        ["launchctl", *argv], capture_output=True, text=True, check=False))
    return case


def invoke(case, capsys, *args, root=None, with_repo_root=True):
    capsys.readouterr()
    argv = list(args)
    if with_repo_root:
        argv += ["--repo-root", str(root or case.root)]
    code = cli_exit(case.main, argv)
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


@pytest.mark.parametrize("switch", ["env", "global", "repo"])
def test_update_check_env_opt_out_makes_no_request_and_does_not_consume_the_day(
    update_case, monkeypatch, capsys, switch,
):
    case = update_case
    config = case.root / ".foragent.yml"
    original = config.read_text()
    if switch == "env":
        monkeypatch.setenv("FORAGENT_UPDATE_CHECK", "off")
    elif switch == "global":
        write_global_config({"skill_libraries": [], "update_check": False})
    else:
        config.write_text(original + "update_check: false\n")
    assert facts(case, capsys, "brief").get("update") is None
    assert facts(case, capsys, "gate", "status").get("update") is None
    assert_lookup(case, 0)
    assert not (case.machine / "update-check.json").exists()
    if switch == "env":
        monkeypatch.delenv("FORAGENT_UPDATE_CHECK")
    elif switch == "global":
        write_global_config({"skill_libraries": []})
    else:
        config.write_text(original)
    assert_offer(facts(case, capsys, "brief"))
    assert_lookup(case, 1)  # Positive control: opting back in works on the same day.
    assert case.commands.calls() == []


def assert_install_lock(case, *, held):
    """Probe a second real descriptor; an in-memory/per-repo lock cannot pass."""
    path = case.machine / "install.lock"
    assert path.is_file(), "Use one install.lock in the machine data root, independent of the caller repo"
    with path.open("a") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            acquired = False
        else:
            acquired = True
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    assert acquired == (not held), "The machine install lock must cover uv and be released on return"


@pytest.fixture
def installer(update_case, monkeypatch, tmp_path):
    from foragent import launchd, orch_reinstall
    from foragent.config import load_config
    from foragent.state import StateStore

    case = update_case
    case.jobs, case.busy, case.events = {}, set(), []
    case.install_exit, case.install_succeeded = 0, False
    case.outside = tmp_path / "unconfigured directory"
    case.outside.mkdir()
    case.receipt = case.commands.tool_dir / "foragent/uv-receipt.toml"
    case.receipt.parent.mkdir(parents=True)
    local = case.root / "unrelated-local.txt"
    local.write_text("keep me\n")
    case.local = local
    executable = Path.home() / ".local/bin/fa"
    monkeypatch.setattr(launchd, "fa_executable", lambda: executable)
    monkeypatch.setattr(launchd, "GitRepo", lambda root: SimpleNamespace(
        remote_slug=lambda: load_config(Path(root)).repo))
    for repo, root in (("owner/repo", case.root),
                       ("owner/forgotten", tmp_path / "forgotten"),
                       ("another/free", tmp_path / "free project")):
        root.mkdir(exist_ok=True)
        (root / ".foragent.yml").write_text(
            f"repo: {repo}\nlanguage: en\nskill_libraries: []\nnotify: none\n")
        job = launchd.Launchd(root, repo=repo, executable=executable)
        job.plist_path.parent.mkdir(parents=True, exist_ok=True)
        job.plist_path.write_bytes(job.plist())
        case.jobs[repo] = job
    agents = Path.home() / "Library/LaunchAgents"
    (agents / "com.example.other.plist").write_bytes(plistlib.dumps({
        "Label": "com.example.other", "ProgramArguments": ["true"]}))
    # These machine records have no installed gate and must not be restarted.
    (case.machine / "owner__state-only").mkdir(parents=True)

    def no_local_reinstall(*args, **kwargs):
        pytest.fail("fa update must install the upstream release tag, not fetch/reinstall a local origin")

    monkeypatch.setattr(orch_reinstall, "_reinstall", no_local_reinstall)
    monkeypatch.setattr(case.git, "fetch", no_local_reinstall)

    def uv(argv):
        if argv == ["tool", "dir"]:
            return subprocess.CompletedProcess(["uv", *argv], 0,
                                               stdout=str(case.commands.tool_dir), stderr="")
        assert argv == ["tool", "install", INSTALL_SOURCE + case.release["tag_name"]], (
            "Install exactly the latest upstream tag regardless of receipt, cwd, or selected repository")
        assert case.gh, "The install tag must come from the recorded upstream release lookup"
        assert_lookup(case, len(case.gh))
        assert_install_lock(case, held=True)
        for repo, job in case.jobs.items():
            with StateStore(job.state_root).tick_lock() as acquired:
                assert acquired == (repo not in case.busy), (
                    "Install under the machine lock before acquiring any gate's tick lock")
        case.events.append(("install", case.release["tag_name"]))
        if case.install_exit == "missing-uv":
            raise FileNotFoundError("offline fake: uv is unavailable")
        case.install_succeeded = case.install_exit == 0
        return subprocess.CompletedProcess(["uv", *argv], case.install_exit,
                                           stdout="installed" if case.install_succeeded else "",
                                           stderr="" if case.install_succeeded else "offline install failed")

    def launchctl(argv):
        jobs = {job.target: (repo, job) for repo, job in case.jobs.items()}
        if argv[0] == "bootstrap":
            matches = [(repo, job) for repo, job in case.jobs.items()
                       if str(job.plist_path) == argv[-1]]
            assert len(matches) == 1, f"Only installed Foragent plists may restart: {argv}"
            repo, job = matches[0]
        else:
            assert argv[-1] in jobs, f"Unknown gate or unrelated launch agent: {argv}"
            repo, job = jobs[argv[-1]]
        if argv[0] == "print":
            return subprocess.CompletedProcess(["launchctl", *argv], 0,
                                               stdout="state = waiting\n", stderr="")
        assert argv[0] in {"bootout", "bootstrap", "kickstart"}, argv
        assert case.install_succeeded, "Never restart any gate before a successful install"
        assert repo not in case.busy, f"Skip busy gate {repo}; never restart it mid-tick"
        with StateStore(job.state_root).tick_lock() as acquired:
            assert not acquired, f"Restart {repo} only while holding its own tick lock"
        case.events.append((argv[0], repo))
        return subprocess.CompletedProcess(["launchctl", *argv], 0, stdout="", stderr="")

    case.uv_reply, case.launchctl_reply = uv, launchctl
    return case


def assert_installed(case, *, restarted):
    from foragent.state import StateStore

    calls = [row["argv"] for row in case.commands.calls("uv") if row["argv"][:2] == ["tool", "install"]]
    assert calls == [["tool", "install", INSTALL_SOURCE + case.release["tag_name"]]], (
        "fa update must run exactly one uv tool install for the latest upstream release tag")
    assert case.events[0] == ("install", case.release["tag_name"])
    actual = [repo for event, repo in case.events if event in {"bootstrap", "kickstart"}]
    assert sorted(actual) == sorted(restarted), "Restart every free gate once, deduplicating --repo-root"
    assert_install_lock(case, held=False)
    assert case.local.read_text() == "keep me\n"
    for repo, job in case.jobs.items():
        with StateStore(job.state_root).tick_lock() as acquired:
            assert acquired == (repo not in case.busy), f"Do not leave a tick lock held for {repo}"


@pytest.mark.parametrize("receipt,tag", [
    ("pypi", f"v{LATEST}"), ("editable", "v0.2.3"),
    ("other-git", "v1.2.0"), ("missing", "v2.0.1"),
])
@pytest.mark.parametrize("entry", ["project-cwd", "explicit-root", "outside", "no-gates"])
def test_update_check_explicit_fa_update_reuses_verified_reinstall_then_restarts_between_ticks(
    installer, monkeypatch, capsys, receipt, tag, entry,
):
    case = installer
    case.release["tag_name"] = tag
    requirements = {
        "pypi": 'name = "foragent", specifier = "==0.0.1"',
        "editable": f'name = "foragent", editable = "{case.root}"',
        "other-git": 'name = "foragent", git = "https://github.com/elsewhere/foragent", rev = "old"',
    }
    if receipt != "missing":
        case.receipt.write_text("[tool]\nrequirements = [{" + requirements[receipt] + "}]\n")
    if entry == "no-gates":
        for job in case.jobs.values():
            job.plist_path.unlink()
        case.jobs.clear()
    if entry != "project-cwd":
        monkeypatch.chdir(case.outside)
    code, output = invoke(case, capsys, "update", with_repo_root=entry == "explicit-root")
    assert code == 0, f"fa update must install and restart successfully (exit {code}): {output.err.strip()}"
    assert_installed(case, restarted=case.jobs)


@pytest.mark.parametrize("busy", [("owner/forgotten",), ("owner/repo",),
                                 ("owner/repo", "owner/forgotten", "another/free")],
                         ids=["another-gate-busy", "selected-gate-busy", "all-gates-busy"])
def test_update_check_busy_tick_prevents_install_and_restart_then_allows_retry(installer, capsys, busy):
    from foragent.state import StateStore

    case = installer
    case.busy = set(busy)
    with ExitStack() as locks:
        for repo in busy:
            assert locks.enter_context(StateStore(case.jobs[repo].state_root).tick_lock())
        code, output = invoke(case, capsys, "update")
        free = set(case.jobs) - case.busy
        assert code == (0 if free else 75), "Exit 75 only when every installed gate is busy"
        assert_installed(case, restarted=free)  # Even when all gates are busy, install once.
        note = (output.out + output.err).lower()
        for repo in busy:
            assert (repo.lower() in note or str(case.jobs[repo].repo_root).lower() in note) and (
                "busy" in note or "tick" in note), f"Name skipped busy gate {repo} in the output"
    case.busy.clear()
    case.events.clear()
    case.commands.clear()
    case.install_succeeded = False
    code, output = invoke(case, capsys, "update")
    assert code == 0, f"fa update must work once the tick finishes: {output.err.strip()}"
    assert_installed(case, restarted=case.jobs)


@pytest.mark.parametrize("install_exit", [1, 42, "missing-uv"])
def test_update_check_failed_install_or_verification_never_restarts_gate(installer, capsys, install_exit):
    case = installer
    case.install_exit = install_exit
    code, output = invoke(case, capsys, "update")
    assert case.events == [("install", case.release["tag_name"])], (
        "fa update must attempt the upstream install and never restart a gate on failure")
    assert code == 1, "An installation failure must exit 1, regardless of uv's exit status"
    assert (output.out + output.err).strip(), "Report the failed installation"
    assert_installed(case, restarted=[])
