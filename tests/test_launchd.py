from pathlib import Path
from hashlib import sha1
import plistlib
import subprocess

import pytest

from foragent import launchd
from foragent.git import GitRepo
from foragent.state import StateStore


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
    monkeypatch.setattr(launchd.shutil, "which", lambda name: pytest.fail("must prefer running executable"))
    assert launchd.fa_executable() == executable


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

