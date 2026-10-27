from pathlib import Path
from hashlib import sha1
import plistlib
import subprocess

import pytest

from foragent import launchd
from foragent.git import GitRepo
from foragent.state import StateStore


@pytest.fixture(autouse=True)
def fake_fa_shim(tmp_path, monkeypatch):
    """Executable discovery and home paths never reach the real installation."""
    shim = tmp_path / ".local/bin/fa"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(launchd.shutil, "which", lambda name: str(shim) if name == "fa" else None)
    return shim


@pytest.mark.parametrize("repo,expected", [
    ("Owner/Repo", "dev.foragent.owner.repo"),
    ("Some_Owner/Some_Repo.v1", "dev.foragent.some-owner.some-repo.v1"),
    ("UPPER/Spaces & !", "dev.foragent.upper.spaces----"),
])
def test_label_sanitizing(repo, expected):
    assert launchd.label_for(repo) == f"{expected}.{sha1(repo.encode()).hexdigest()[:8]}"


def test_sanitized_label_collisions_have_distinct_hashes():
    assert launchd.label_for("owner/a_b") != launchd.label_for("owner/a-b")
    assert launchd.label_for("a.b/c") != launchd.label_for("a/b.c")


@pytest.fixture
def job(tmp_path):
    calls = []

    def run(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    git = GitRepo(tmp_path, lambda args: "git@github.com:Some_Owner/Repo.git")
    job = launchd.Launchd(
        tmp_path / "project", git=git, base_dir=tmp_path / "agents",
        state_root=tmp_path / "state", run=run, uid=501, home=tmp_path / "home",
        executable=tmp_path / "project/.venv/bin/fa",
    )
    return job, calls


def test_plist_round_trip_has_exact_job_and_environment(job):
    job, calls = job
    assert plistlib.loads(job.plist()) == {
        "Label": launchd.label_for("Some_Owner/Repo"),
        "ProgramArguments": [str(job.repo_root / ".venv/bin/fa"), "gate", "tick", "--repo-root", str(job.repo_root)],
        "StartInterval": 300,
        "RunAtLoad": True,
        "ProcessType": "Standard",
        "EnvironmentVariables": {
            "PATH": f"{job.home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(job.home), "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
        },
        "StandardOutPath": str(job.state_root / "launchd.out.log"),
        "StandardErrorPath": str(job.state_root / "launchd.err.log"),
    }
    assert calls == [] and not job.state_root.exists() and not job.plist_path.exists()


def test_install_creates_directories_and_uses_exact_argv(job):
    job, calls = job
    assert job.install() == job.label
    assert job.plist_path.read_bytes() == job.plist()
    assert job.state_root.is_dir()
    assert calls == [
        ["bootout", job.target],
        ["bootstrap", "gui/501", str(job.plist_path)],
    ]


def test_install_preserves_identical_plist_and_replaces_changed_content(job):
    job, calls = job
    job.install()
    job.plist_path.touch()
    before = job.plist_path.stat().st_mtime_ns
    job.install()
    assert job.plist_path.stat().st_mtime_ns == before
    job.plist_path.write_bytes(plistlib.dumps({"Label": "old"}))
    job.install()
    assert plistlib.loads(job.plist_path.read_bytes())["Label"] == job.label
    assert len(calls) == 6


@pytest.mark.parametrize("message", [
    "Boot-out failed: 3: No such process", "Service not loaded", "Could not find service", "",
])
def test_bootout_not_loaded_is_ignored(job, message):
    job, calls = job

    def run(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 3 if args[0] == "bootout" else 0, stdout="", stderr=message)

    job._run = run
    job.install()
    job.uninstall()
    job.uninstall()
    assert not job.plist_path.exists()


def test_uninstall_leaves_state_and_github_untouched(job):
    job, calls = job
    job.install()
    (job.state_root / "history.txt").write_text("Keep this\n")
    before = {path.name: path.read_bytes() for path in job.state_root.iterdir()}
    calls.clear()
    assert job.uninstall() == job.label
    assert calls == [["bootout", job.target]]
    assert not job.plist_path.exists()
    assert before == {path.name: path.read_bytes() for path in job.state_root.iterdir()}


@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_install_and_uninstall_remove_legacy_job_and_plist(job, operation):
    job, calls = job
    job.legacy_plist_path.parent.mkdir(parents=True)
    job.legacy_plist_path.write_bytes(plistlib.dumps({"Label": job.legacy_label}))
    getattr(job, operation)()
    assert not job.legacy_plist_path.exists()
    assert calls == [
        ["bootout", f"{job.domain}/{job.legacy_label}"],
        ["bootout", job.target],
        *([["bootstrap", job.domain, str(job.plist_path)]] if operation == "install" else []),
    ]


@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_legacy_cleanup_error_preserves_legacy_plist(job, operation):
    job, calls = job
    job.legacy_plist_path.parent.mkdir(parents=True)
    job.legacy_plist_path.write_bytes(b"legacy")
    job._run = lambda args: subprocess.CompletedProcess(args, 1, stdout="", stderr="Operation not permitted")
    with pytest.raises(launchd.LaunchdError, match="Operation not permitted"):
        getattr(job, operation)()
    assert job.legacy_plist_path.read_bytes() == b"legacy"


def test_colliding_repositories_install_and_uninstall_independently(job):
    original, calls = job
    jobs = [launchd.Launchd(
        original.repo_root, repo=repo, base_dir=original.plist_path.parent,
        state_root=original.state_root, run=original._run, uid=501, home=original.home,
        executable=original.executable,
    ) for repo in ("owner/a_b", "owner/a-b")]
    for selected in jobs:
        selected.install()
    assert jobs[0].plist_path != jobs[1].plist_path
    assert all(selected.plist_path.exists() for selected in jobs)
    calls.clear()
    jobs[0].uninstall()
    assert jobs[1].plist_path.exists()
    assert calls == [["bootout", jobs[0].target]]


def test_explicit_repo_does_not_require_origin(tmp_path):
    def no_origin(args):
        pytest.fail(f"must use the configured repository: {args}")

    job = launchd.Launchd(tmp_path, repo="owner/repo", git=GitRepo(tmp_path, no_origin), home=tmp_path)
    assert job.label == launchd.label_for("owner/repo")


@pytest.mark.parametrize("plist_present", [False, True])
@pytest.mark.parametrize("code,output,expected", [
    (0, "state = waiting\n last exit code = 0\n", 0),
    (0, "last exit status = -15", -15),
    (0, "state = running", None),
    (113, "", None),
])
def test_status_reports_presence_loaded_and_last_exit_without_writing(job, plist_present, code, output, expected):
    job, calls = job
    if plist_present:
        job.plist_path.parent.mkdir()
        job.plist_path.write_bytes(job.plist())

    def run(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, code, stdout=output, stderr="")

    job._run = run
    report = job.status()
    assert report == launchd.LaunchdStatus(job.label, plist_present, code == 0, expected)
    assert calls == [["print", job.target]]
    assert not job.state_root.exists()


@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_real_bootout_error_propagates_and_preserves_plist(job, operation):
    job, calls = job
    job.install()
    job._run = lambda args: subprocess.CompletedProcess(args, 1, stdout="", stderr="Operation not permitted")
    with pytest.raises(launchd.LaunchdError, match="Operation not permitted"):
        getattr(job, operation)()
    assert job.plist_path.exists()


def test_bootstrap_failure_is_clear(job):
    job, calls = job
    job._run = lambda args: subprocess.CompletedProcess(
        args, 5 if args[0] == "bootstrap" else 0, stdout="", stderr="Invalid plist",
    )
    with pytest.raises(launchd.LaunchdError, match="launchctl bootstrap: Invalid plist"):
        job.install()


def test_missing_launchctl_reports_not_loaded_but_install_fails(job):
    job, calls = job

    def missing(args):
        raise FileNotFoundError("launchctl not found")

    job._run = missing
    assert job.status() == launchd.LaunchdStatus(job.label, False, False)
    with pytest.raises(launchd.LaunchdError, match="launchctl not found"):
        job.install()


def test_default_plist_state_paths_and_uid_use_isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(launchd.os, "getuid", lambda: 502)
    git = GitRepo(tmp_path, lambda args: "https://github.com/Owner/Repo")
    job = launchd.Launchd(tmp_path, git=git)
    label = launchd.label_for("Owner/Repo")
    assert job.plist_path == tmp_path / f"Library/LaunchAgents/{label}.plist"
    assert job.state_root == StateStore.for_repo("Owner/Repo").root
    assert job.target == f"gui/502/{label}"
    assert not job.plist_path.exists() and not job.state_root.exists()


@pytest.mark.parametrize("command", ["fa", "foragent"])
def test_running_project_venv_executable_is_preferred(tmp_path, monkeypatch, command):
    executable = tmp_path / ".venv/bin" / command
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(launchd.sys, "argv", [str(executable)])
    # The running entry point is the fallback when PATH has no stable shim.
    monkeypatch.setattr(launchd.shutil, "which", lambda name: None)
    assert launchd.fa_executable() == executable


@pytest.mark.parametrize("command", ["fa", "foragent"])
def test_fa_executable_prefers_path_shim_over_running_uv_path(tmp_path, monkeypatch, fake_fa_shim, command):
    pinned = tmp_path / ".local/share/uv/tools/foragent/bin" / command
    pinned.parent.mkdir(parents=True)
    pinned.touch()
    fake_fa_shim.parent.mkdir(parents=True)
    fake_fa_shim.symlink_to(pinned)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(launchd.sys, "argv", [str(pinned)])
    lookups = []
    monkeypatch.setattr(launchd.shutil, "which", lambda name: lookups.append(name) or ".local/bin/fa")
    found = launchd.fa_executable()
    assert found == fake_fa_shim and found != fake_fa_shim.resolve()
    assert lookups == ["fa"]


def test_bare_entrypoint_uses_which_and_python_module_falls_back_to_fa(tmp_path, monkeypatch):
    executable = tmp_path / ".venv/bin/fa"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(launchd.sys, "argv", ["fa"])
    monkeypatch.setattr(launchd.shutil, "which", lambda name: str(executable))
    assert launchd.fa_executable() == executable
    monkeypatch.setattr(launchd.sys, "argv", ["/installed/foragent/cli.py"])
    assert launchd.fa_executable() == executable


def test_no_fa_executable_has_clear_error(monkeypatch):
    monkeypatch.setattr(launchd.sys, "argv", ["pytest"])
    monkeypatch.setattr(launchd.shutil, "which", lambda name: None)
    with pytest.raises(launchd.LaunchdError, match="cannot locate the fa executable"):
        launchd.fa_executable()


def test_default_launchctl_runner_invokes_subprocess_without_shell(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="output", stderr="")

    monkeypatch.setattr(launchd.subprocess, "run", run)
    assert launchd._run_launchctl(["print", "gui/501/job"]).stdout == "output"
    assert calls == [(
        ["launchctl", "print", "gui/501/job"],
        {"capture_output": True, "text": True, "check": False},
    )]


@pytest.mark.parametrize("operation", ["install", "uninstall", "status"])
def test_module_entry_points_forward_dependencies(job, operation):
    job, calls = job
    result = getattr(launchd, operation)(
        job.repo_root, git=GitRepo(job.repo_root, lambda args: "git@github.com:Some_Owner/Repo.git"),
        base_dir=job.plist_path.parent, state_root=job.state_root, run=job._run,
        uid=501, home=job.home, executable=job.executable,
    )
    assert result == (launchd.LaunchdStatus(job.label, False, True) if operation == "status" else job.label)
    assert calls[0][1] == job.target



def _old_plist(path, root, label=None):
    """A plist as an older Foragent wrote it: no gate environment."""
    old = {"Label": label or path.stem, "ProgramArguments": ["fa", "gate", "tick", "--repo-root", str(root)],
           "EnvironmentVariables": {"HOME": str(root), "LANG": "en_US.UTF-8"}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(old))
    return old


def test_refresh_decision_reads_an_existing_plist(tmp_path, job, fake_fa_shim):
    job, _ = job
    path = tmp_path / "LaunchAgents/dev.foragent.old.plist"
    assert not launchd.needs_environment_refresh(path) and not launchd.has_gate_environment(path)
    _old_plist(path, tmp_path)
    assert launchd.needs_environment_refresh(path) and not launchd.has_gate_environment(path)
    job.executable = fake_fa_shim
    path.write_bytes(job.plist())
    assert not launchd.needs_environment_refresh(path) and launchd.has_gate_environment(path)
    # Unreadable proves nothing about the loaded job: never current, and not migratable.
    path.write_text("not a plist")
    assert not launchd.has_gate_environment(path) and not launchd.needs_environment_refresh(path)
    path.write_bytes(plistlib.dumps({"EnvironmentVariables": "PYTHONDONTWRITEBYTECODE=1"}))
    assert not launchd.has_gate_environment(path) and launchd.needs_environment_refresh(path)
    launchd._migrate_environment(path)
    assert launchd.has_gate_environment(path)


def test_restart_adds_gate_environment_while_the_job_is_out(tmp_path, monkeypatch, fake_fa_shim):
    path = tmp_path / "dev.foragent.old.plist"
    old = _old_plist(path, tmp_path)
    calls = []

    def run(args):
        environment = plistlib.loads(path.read_bytes())["EnvironmentVariables"]
        calls.append((args[0], environment.get("PYTHONDONTWRITEBYTECODE")))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(launchd, "_run_launchctl", run)
    launchd.InstalledJob(path, "dev.foragent.old", tmp_path, "gui/501").restart()
    assert calls == [("bootout", None), ("bootstrap", "1")]
    data = plistlib.loads(path.read_bytes())
    assert data["EnvironmentVariables"] == {**old["EnvironmentVariables"], "PYTHONDONTWRITEBYTECODE": "1"}
    assert data["ProgramArguments"] == [str(fake_fa_shim), *old["ProgramArguments"][1:]]
    assert {k: v for k, v in data.items() if k not in {"EnvironmentVariables", "ProgramArguments"}} == {
        k: v for k, v in old.items() if k not in {"EnvironmentVariables", "ProgramArguments"}}
    current = path.read_bytes()
    launchd.InstalledJob(path, "dev.foragent.old", tmp_path, "gui/501").restart()
    assert path.read_bytes() == current


def test_install_writes_the_plist_only_after_bootout(job):
    job, _ = job
    seen = []
    job._run = lambda args: seen.append((args[0], job.plist_path.exists())) or subprocess.CompletedProcess(
        args, 0, stdout="", stderr="")
    job.install()
    assert seen[0] == ("bootout", False) and seen[-1] == ("bootstrap", True)


def _jobs_for_one_gate(home, root):
    """Hashed, legacy and renamed-repository plists that all tick ``root``, plus a current and another gate."""
    agents = home / "Library/LaunchAgents"
    hashed = agents / f"{launchd.label_for('o/r')}.plist"
    legacy = agents / f"{launchd._legacy_label_for('o/r')}.plist"
    renamed = agents / f"{launchd.label_for('o/old-name')}.plist"
    for path in (hashed, legacy, renamed):
        _old_plist(path, root)
    current = agents / f"{launchd.label_for('o/current')}.plist"
    current.write_bytes(launchd.Launchd(root, repo="o/current", home=home, state_root=home / "s",
                                        executable=home / ".local/bin/fa").plist())
    other = agents / f"{launchd.label_for('o/other')}.plist"
    _old_plist(other, home / "other-root")
    return hashed, legacy, renamed, other


def test_stale_jobs_finds_every_plist_that_ticks_the_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    hashed, legacy, renamed, other = _jobs_for_one_gate(tmp_path, root)
    found = {job.plist_path for job in launchd.stale_jobs(root, home=tmp_path)}
    assert found == {path.resolve() for path in (hashed, legacy, renamed)}


@pytest.mark.parametrize("pinned", [True, False], ids=["pinned-uv", "stable-shim"])
def test_refresh_detects_the_executable_even_with_current_environment(tmp_path, monkeypatch, fake_fa_shim, pinned):
    executable = tmp_path / ".local/share/uv/tools/foragent/bin/fa" if pinned else fake_fa_shim
    job = launchd.Launchd(tmp_path, repo="o/r", home=tmp_path, state_root=tmp_path / "state",
                          executable=executable)
    job.plist_path.parent.mkdir(parents=True)
    job.plist_path.write_bytes(job.plist())
    before = job.plist_path.read_bytes()
    monkeypatch.setattr(launchd.InstalledJob, "state", lambda job: "loaded")
    monkeypatch.setattr(launchd.sys, "platform", "darwin")
    spawned = []
    monkeypatch.setattr(launchd.subprocess, "Popen", lambda argv, **kw: spawned.append((argv, kw)))

    assert launchd.has_gate_environment(job.plist_path)
    assert launchd.needs_environment_refresh(job.plist_path) is pinned
    assert [found.plist_path for found, _ in launchd._jobs_to_refresh(tmp_path)] == (
        [job.plist_path] if pinned else [])
    assert launchd.refresh_if_stale("o/r", tmp_path, tmp_path / "state") is pinned
    assert len(spawned) == int(pinned)
    assert job.plist_path.read_bytes() == before


def test_pinned_uv_path_is_rewritten_and_reloaded_only_between_ticks(tmp_path, monkeypatch, fake_fa_shim):
    from foragent import update_check

    root = tmp_path / "repo"
    root.mkdir()
    (root / ".foragent.yml").write_text("repo: o/r\n")
    pinned = tmp_path / ".local/share/uv/tools/foragent/bin/fa"
    job = launchd.Launchd(root, repo="o/r", home=tmp_path, state_root=tmp_path / "state", executable=pinned)
    job.plist_path.parent.mkdir(parents=True)
    job.plist_path.write_bytes(job.plist())
    before = job.plist_path.read_bytes()
    old = plistlib.loads(before)
    store = StateStore(tmp_path / "state")
    monkeypatch.setattr(update_check.StateStore, "for_repo", classmethod(lambda cls, repo: store))
    monkeypatch.setattr(launchd.InstalledJob, "state", lambda job: "loaded")
    calls = []

    def launchctl(args):
        if args[0] in {"bootout", "bootstrap"}:
            with store.tick_lock() as acquired:
                assert not acquired, "The refresher must hold the gate's tick lock"
        calls.append((args[0], plistlib.loads(job.plist_path.read_bytes())))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(launchd, "_run_launchctl", launchctl)
    with store.tick_lock() as acquired:
        assert acquired
        times = iter([0, launchd.REFRESH_WAIT_SECONDS])
        assert launchd.refresh_jobs(root, monotonic=lambda: next(times)) == "busy"
        assert job.plist_path.read_bytes() == before
        assert [command for command, _ in calls] == ["print"]
    calls.clear()
    assert launchd.refresh_jobs(root) == "restarted"
    updated = {**old, "ProgramArguments": [str(fake_fa_shim), *old["ProgramArguments"][1:]]}
    assert calls == [("print", old), ("bootout", old), ("bootstrap", updated)]
    assert plistlib.loads(job.plist_path.read_bytes()) == updated
    assert launchd.refresh_jobs(root) == "current"


@pytest.mark.parametrize("gate_environment", [False, True])
def test_no_shim_leaves_the_job_unchanged_without_refresh(tmp_path, monkeypatch, capsys, gate_environment):
    path = launchd.installed_plist("o/r")
    data = _old_plist(path, tmp_path)
    data["ProgramArguments"][0] = str(tmp_path / ".local/share/uv/tools/foragent/bin/fa")
    if gate_environment:
        data["EnvironmentVariables"].update(launchd.GATE_ENVIRONMENT)
    path.write_bytes(plistlib.dumps(data))
    before = path.read_bytes()
    monkeypatch.setattr(launchd.shutil, "which", lambda name: None)
    monkeypatch.setattr(launchd.sys, "argv", [data["ProgramArguments"][0]])
    monkeypatch.setattr(launchd.sys, "platform", "darwin")
    monkeypatch.setattr(launchd.InstalledJob, "state", lambda job: "loaded")
    spawned = []
    monkeypatch.setattr(launchd.subprocess, "Popen", lambda *a, **kw: spawned.append((a, kw)))

    assert launchd.needs_environment_refresh(path) is (not gate_environment)
    assert [job.plist_path for job, _ in launchd._jobs_to_refresh(tmp_path)] == (
        [] if gate_environment else [path])
    assert launchd.refresh_if_stale("o/r", tmp_path, tmp_path / "state") is (not gate_environment)
    assert len(spawned) == int(not gate_environment)
    assert path.read_bytes() == before
    launchd._migrate_environment(path)
    assert plistlib.loads(path.read_bytes()) == {
        **data, "EnvironmentVariables": {**data["EnvironmentVariables"], **launchd.GATE_ENVIRONMENT},
    }
    assert not launchd.needs_environment_refresh(path)
    assert "cannot locate fa on PATH" in capsys.readouterr().err


def test_tick_starts_a_detached_refresh_for_any_stale_job_of_the_repo(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(launchd.sys, "platform", "darwin")
    monkeypatch.setattr(launchd.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "0")
    root = tmp_path / "repo"
    root.mkdir()
    spawned, states = [], {"value": "loaded"}
    monkeypatch.setattr(launchd.InstalledJob, "state", lambda job: states["value"])
    monkeypatch.setattr(launchd.subprocess, "Popen", lambda argv, **kwargs: spawned.append((argv, kwargs)))
    assert launchd.refresh_if_stale("o/r", root, tmp_path / "state") is False  # nothing installed
    legacy = tmp_path / "Library/LaunchAgents" / f"{launchd._legacy_label_for('o/r')}.plist"
    _old_plist(legacy, root)  # only a legacy-label job: still found
    states["value"] = "absent"  # launchd confirms no such job: it starts no tick
    assert launchd.refresh_if_stale("o/r", root, tmp_path / "state") is False and spawned == []
    for state in ("unknown", "loaded"):  # unknown is not absence
        states["value"] = state
        assert launchd.refresh_if_stale("o/r", root, tmp_path / "state") is True
    del spawned[1:]
    argv, kwargs = spawned[0]
    assert argv[1:] == ["-m", "foragent.launchd", "refresh", str(root.resolve())]
    assert kwargs["start_new_session"] is True and kwargs["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    launchd._migrate_environment(legacy)
    assert launchd.refresh_if_stale("o/r", root, tmp_path / "state") is False and len(spawned) == 1
    launchd.installed_plist("o/r").write_text("not a plist")
    assert launchd.refresh_if_stale("o/r", root, tmp_path / "state") is False
    err = capsys.readouterr().err
    assert "cannot read" in err and "fa gate install" in err
    monkeypatch.setattr(launchd.sys, "platform", "linux")
    _old_plist(legacy, root)
    assert launchd.refresh_if_stale("o/r", root, tmp_path / "state") is False


def test_job_state_separates_absent_from_unknown(tmp_path, monkeypatch):
    job = launchd.InstalledJob(tmp_path / "x.plist", "dev.foragent.x", tmp_path, "gui/501")
    for returncode, stderr, expected in [
        (0, "", "loaded"),
        (113, "Could not find service \"dev.foragent.x\" in domain for user gui: 501", "absent"),
        (1, "Could not find service", "absent"),
        (5, "Input/output error", "unknown"),
        (1, "Operation not permitted", "unknown"),
    ]:
        monkeypatch.setattr(launchd, "_run_launchctl", lambda args: subprocess.CompletedProcess(
            args, returncode, stdout="", stderr=stderr))
        assert job.state() == expected, (returncode, stderr)

    def missing(args):
        raise FileNotFoundError("launchctl")

    monkeypatch.setattr(launchd, "_run_launchctl", missing)
    assert job.state() == "unknown"


def test_refresh_waits_for_the_tick_then_restarts_every_stale_job_launchd_may_hold(tmp_path, monkeypatch):
    from foragent import update_check
    monkeypatch.setattr(launchd.Path, "home", lambda: tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    hashed, legacy, renamed, other = _jobs_for_one_gate(tmp_path, root)
    renamed_before = renamed.read_bytes()
    states = {hashed.resolve(): "loaded", legacy.resolve(): "loaded", renamed.resolve(): "absent"}
    monkeypatch.setattr(launchd.InstalledJob, "state", lambda job: states[job.plist_path])
    results, restarts, sleeps = ["busy", "busy", "restarted"], [], []

    def restart(gate, jobs, language):
        restarts.append((gate, sorted(job.plist_path for job in jobs)))
        result = results.pop(0)
        if result == "restarted":
            for job in jobs:
                launchd._migrate_environment(job.plist_path)
        return result

    monkeypatch.setattr(update_check, "_restart", restart)
    monkeypatch.setattr(update_check, "_language", lambda root: "en")
    assert launchd.refresh_jobs(root, sleep=sleeps.append, monotonic=lambda: 0) == "restarted"
    assert sleeps == [launchd.REFRESH_POLL_SECONDS] * 2
    assert restarts == [(root.resolve(), sorted([hashed.resolve(), legacy.resolve()]))] * 3
    # A confirmed absent plist is never rewritten without a reload; another gate is untouched.
    assert renamed.read_bytes() == renamed_before and not launchd.has_gate_environment(other)
    assert launchd.refresh_jobs(root, sleep=sleeps.append) == "current" and len(restarts) == 3


def test_refresh_never_rewrites_a_plist_whose_job_state_is_unknown(tmp_path, monkeypatch):
    from foragent import update_check
    monkeypatch.setattr(launchd.Path, "home", lambda: tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    hashed, legacy, renamed, _ = _jobs_for_one_gate(tmp_path, root)
    before = {path: path.read_bytes() for path in (hashed, legacy, renamed)}
    monkeypatch.setattr(launchd.InstalledJob, "state",
                        lambda job: "unknown" if job.plist_path == legacy.resolve() else "loaded")
    monkeypatch.setattr(update_check, "_restart", lambda *args: pytest.fail("no restart while unknown"))
    monkeypatch.setattr(update_check, "_language", lambda root: "en")
    assert launchd.refresh_jobs(root) == "unknown"
    assert {path: path.read_bytes() for path in before} == before
    # The plists stay stale, so the next tick starts another refresh.
    assert len(launchd.stale_jobs(root)) == 3


def test_failed_print_during_restart_leaves_the_plist_stale_for_the_next_tick(tmp_path, monkeypatch):
    """update's restart skips a job whose print fails; the file must then stay unmigrated."""
    from foragent import update_check
    monkeypatch.setattr(launchd.Path, "home", lambda: tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    hashed, *_ = _jobs_for_one_gate(tmp_path, root)
    monkeypatch.setattr(launchd.InstalledJob, "state", lambda job: "loaded")
    monkeypatch.setattr(launchd, "_run_launchctl", lambda args: subprocess.CompletedProcess(
        args, 5, stdout="", stderr="Input/output error"))
    monkeypatch.setattr(update_check, "_language", lambda root: "en")
    before = hashed.read_bytes()
    assert launchd.refresh_jobs(root) == "skipped"
    assert hashed.read_bytes() == before and launchd.needs_environment_refresh(hashed)
