"""Per-repository launchd jobs with injectable paths and command execution."""

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha1
import os
from pathlib import Path
import plistlib
import re
import fcntl
import shutil
import subprocess
import sys
import time

from foragent.git import GitRepo
from foragent.state import StateStore


class LaunchdError(RuntimeError):
    """A launchd job could not be installed or removed."""


@dataclass(frozen=True)
class LaunchdStatus:
    label: str
    plist_present: bool
    loaded: bool
    last_exit_status: int | None = None


def label_for(repo: str) -> str:
    return f"{_legacy_label_for(repo)}.{sha1(repo.encode('utf-8')).hexdigest()[:8]}"


def _legacy_label_for(repo: str) -> str:
    return re.sub(r"[^a-z0-9.-]", "-", "dev.foragent." + repo.replace("/", ".").lower())


def fa_executable() -> Path:
    current = Path(sys.argv[0]).expanduser()
    if current.name in {"fa", "foragent"}:
        if current.is_file():
            return current.resolve()
        if found := shutil.which(str(current)):
            return Path(found).resolve()
    if found := shutil.which("fa"):
        return Path(found).resolve()
    raise LaunchdError("cannot locate the fa executable; install Foragent or run its fa entry point")


def _run_launchctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)


def _bootout(call: Callable[[list[str]], subprocess.CompletedProcess[str]], target: str) -> None:
    result = call(["bootout", target])
    if result.returncode:
        message = (result.stderr or result.stdout or "").strip()
        if re.search(r"not loaded|could not find service|no such process|service not found", message, re.I):
            return
        if result.returncode == 3 and not message:
            return
        raise LaunchdError(f"launchctl bootout: {message or result.returncode}")


# Gate processes must not write __pycache__ into the tool directory that a
# reinstall empties. Python writes bytecode while importing, so only the
# environment a process starts with can stop it.
GATE_ENVIRONMENT = {"PYTHONDONTWRITEBYTECODE": "1"}
REFRESH_WAIT_SECONDS = 1800
REFRESH_POLL_SECONDS = 5


def _environment(path: Path) -> dict | None:
    """The plist's EnvironmentVariables, or None when the file cannot be read as a plist."""
    try:
        data = plistlib.loads(Path(path).read_bytes())
        environment = data.get("EnvironmentVariables") or {}
    except (OSError, ValueError, TypeError, AttributeError, plistlib.InvalidFileException):
        return None
    # A malformed value sets nothing in launchd; migrating replaces it.
    return environment if isinstance(environment, dict) else {}


def has_gate_environment(path: Path) -> bool:
    """True only when the plist is readable and carries the gate environment.

    An unreadable file proves nothing about the loaded job, so it is not current.
    """
    environment = _environment(path)
    return environment is not None and all(
        environment.get(name) == value for name, value in GATE_ENVIRONMENT.items())


def needs_environment_refresh(path: Path) -> bool:
    """True when a readable plist predates the gate environment and can be migrated."""
    return _environment(path) is not None and not has_gate_environment(path)


def _migrate_environment(path: Path) -> None:
    """Add the gate environment to an older plist, keeping every other key."""
    if not needs_environment_refresh(path):
        return
    data = plistlib.loads(path.read_bytes())
    data["EnvironmentVariables"] = {**_environment(path), **GATE_ENVIRONMENT}
    path.write_bytes(plistlib.dumps(data))


def installed_plist(repo: str, home: Path | None = None) -> Path:
    return (Path(home) if home is not None else Path.home()) / "Library/LaunchAgents" / f"{label_for(repo)}.plist"


def stale_jobs(repo_root: Path, home: Path | None = None) -> list["InstalledJob"]:
    """Every installed job that ticks ``repo_root`` without the gate environment.

    Found by the plist's --repo-root, as fa update finds gates, so legacy labels
    and plists written under an earlier repository name are included.
    """
    root = Path(repo_root).resolve()
    return [job for job in installed_jobs(home) if job.repo_root == root
            and not has_gate_environment(job.plist_path)]


def _unreadable_plist(repo: str) -> Path | None:
    path = installed_plist(repo)
    return path if path.exists() and _environment(path) is None else None


def refresh_if_stale(repo: str, repo_root: Path, log_dir: Path) -> bool:
    """From a tick: start a detached reload when a job for this repo predates the gate environment.

    The reload waits for the tick lock, so it never stops a running tick.
    """
    if sys.platform != "darwin":
        return False
    if (unreadable := _unreadable_plist(repo)) is not None:
        # Its repository and environment are unknown, so it cannot be migrated;
        # kick already refuses to kickstart it.
        print(f"foragent: cannot read {unreadable}; run fa gate install to rewrite it", file=sys.stderr)
    if not _jobs_to_refresh(Path(repo_root).resolve()):
        return False
    from foragent.launcher import gate_environment

    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "launchd-refresh.log").open("ab") as stream:
        subprocess.Popen(
            [sys.executable, "-m", "foragent.launchd", "refresh", str(Path(repo_root).resolve())],
            stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, start_new_session=True,
            env=gate_environment(),
        )
    return True


def refresh_from_tick(repo: str, repo_root: Path, log_dir: Path) -> None:
    """The tick's own work never depends on this reload."""
    try:
        refresh_if_stale(repo, repo_root, log_dir)
    except Exception as exc:
        print(f"foragent: launchd refresh failed: {' '.join(str(exc).split())}", file=sys.stderr)


def refresh_jobs(repo_root: Path, *, sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> str:
    """Reload this repo's stale jobs through fa update's restart: between ticks, under the tick lock."""
    from foragent import update_check

    root = Path(repo_root).resolve()
    agents = (Path.home() / "Library/LaunchAgents").resolve()
    language = update_check._language(root)
    deadline = monotonic() + REFRESH_WAIT_SECONDS
    lock_path = agents / f".foragent-refresh-{sha1(str(root).encode()).hexdigest()[:8]}.lock"
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "already"  # Another refresher is waiting for the same gate.
        # A plist changes only inside restart (bootout, migrate, bootstrap), never
        # on disk alone: a file with the variable always means a reloaded job.
        while jobs := _jobs_to_refresh(root):
            if any(state == "unknown" for _, state in jobs):
                return "unknown"  # Cannot tell whether launchd holds it; the next tick retries.
            result = update_check._restart(root, [job for job, _ in jobs], language)
            if result != "busy" or monotonic() >= deadline:
                return result
            sleep(REFRESH_POLL_SECONDS)
    return "current"


def _jobs_to_refresh(root: Path) -> list[tuple["InstalledJob", str]]:
    """Stale jobs of the gate that launchd may hold; a confirmed absent job starts no tick."""
    states = [(job, job.state()) for job in stale_jobs(root)]
    return [(job, state) for job, state in states if state != "absent"]


@dataclass(frozen=True)
class InstalledJob:
    """A gate job exactly as its plist file names it, including legacy labels."""
    plist_path: Path
    label: str
    repo_root: Path
    domain: str

    @property
    def target(self) -> str:
        return f"{self.domain}/{self.label}"

    def _call(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return _run_launchctl(args)
        except OSError as exc:
            raise LaunchdError(f"launchctl: {exc}") from exc

    def loaded(self) -> bool:
        try:
            return _run_launchctl(["print", self.target]).returncode == 0
        except FileNotFoundError:
            return False

    def state(self) -> str:
        """loaded, absent (launchd says no such service) or unknown (any other failure)."""
        try:
            result = _run_launchctl(["print", self.target])
        except OSError:
            return "unknown"
        if result.returncode == 0:
            return "loaded"
        output = f"{result.stderr or ''}{result.stdout or ''}".lower()
        return "absent" if result.returncode == 113 or "could not find service" in output else "unknown"

    def restart(self) -> None:
        """Reload the plist as installed: bootout, then bootstrap the same file.

        An older plist gains the gate environment while the job is out, so a
        kickstart never sees the new file with the old definition loaded.
        """
        _bootout(self._call, self.target)
        _migrate_environment(self.plist_path)
        result = self._call(["bootstrap", self.domain, str(self.plist_path)])
        if result.returncode:
            raise LaunchdError(f"launchctl bootstrap: {result.stderr or result.stdout or result.returncode}")


def installed_jobs(home: Path | None = None, uid: int | None = None) -> list[InstalledJob]:
    """Every dev.foragent.*.plist gate job on this machine, hashed or legacy label."""
    directory = ((Path(home).expanduser() if home is not None else Path.home()) / "Library/LaunchAgents").resolve()
    domain = f"gui/{os.getuid() if uid is None else uid}"
    jobs = []
    for path in sorted(directory.glob("dev.foragent.*.plist")):
        try:
            data = plistlib.loads(path.read_bytes())
            arguments = list(data.get("ProgramArguments") or [])
            root = Path(arguments[arguments.index("--repo-root") + 1]).expanduser().resolve()
            label = data["Label"]
        except (OSError, ValueError, IndexError, KeyError, TypeError, AttributeError,
                plistlib.InvalidFileException):
            continue
        if isinstance(label, str) and label.startswith("dev.foragent."):
            jobs.append(InstalledJob(path, label, root, domain))
    return jobs


class Launchd:
    def __init__(
        self, repo_root: Path, *, repo: str | None = None, git: GitRepo | None = None,
        base_dir: Path | None = None, state_root: Path | None = None,
        run: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
        home: Path | None = None, uid: int | None = None, executable: Path | None = None,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.repo = repo if repo is not None else (git or GitRepo(self.repo_root)).remote_slug()
        self.label = label_for(self.repo)
        self.home = Path(home).expanduser().resolve() if home is not None else Path.home()
        directory = Path(base_dir) if base_dir is not None else self.home / "Library/LaunchAgents"
        self.plist_path = directory.expanduser().resolve() / f"{self.label}.plist"
        self.legacy_label = _legacy_label_for(self.repo)
        self.legacy_plist_path = self.plist_path.with_name(f"{self.legacy_label}.plist")
        self.state_root = (
            Path(state_root).expanduser().resolve() if state_root is not None
            else StateStore.for_repo(self.repo).root
        )
        self.domain = f"gui/{os.getuid() if uid is None else uid}"
        self.target = f"{self.domain}/{self.label}"
        self.executable = executable
        self._run = run or _run_launchctl

    def plist(self) -> bytes:
        executable = (
            Path(self.executable).expanduser().resolve()
            if self.executable is not None else fa_executable()
        )
        return plistlib.dumps({
            "Label": self.label,
            "ProgramArguments": [str(executable), "gate", "tick", "--repo-root", str(self.repo_root)],
            "StartInterval": 300,
            "RunAtLoad": True,
            # Background confines the tick and every run it starts (tests, agents) to
            # throttled efficiency cores; measured 6x slower CPU work on Apple Silicon.
            "ProcessType": "Standard",
            "EnvironmentVariables": {
                "PATH": f"{self.home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                "HOME": str(self.home), "LANG": "en_US.UTF-8",
                **GATE_ENVIRONMENT,
            },
            "StandardOutPath": str(self.state_root / "launchd.out.log"),
            "StandardErrorPath": str(self.state_root / "launchd.err.log"),
        })

    def _call(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return self._run(args)
        except OSError as exc:
            raise LaunchdError(f"launchctl: {exc}") from exc

    def _bootout(self, target: str | None = None) -> None:
        _bootout(self._call, target or self.target)

    def _remove_legacy(self) -> None:
        if self.legacy_plist_path.exists():
            self._bootout(f"{self.domain}/{self.legacy_label}")
            self.legacy_plist_path.unlink()

    def install(self) -> str:
        content = self.plist()
        self.plist_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self._remove_legacy()
        # Write while the job is out: a kickstart must not find the new file
        # with the old definition still loaded.
        self._bootout()
        if not self.plist_path.exists() or self.plist_path.read_bytes() != content:
            self.plist_path.write_bytes(content)
        result = self._call(["bootstrap", self.domain, str(self.plist_path)])
        if result.returncode:
            raise LaunchdError(f"launchctl bootstrap: {result.stderr or result.stdout or result.returncode}")
        return self.label

    def uninstall(self) -> str:
        self._remove_legacy()
        self._bootout()
        self.plist_path.unlink(missing_ok=True)
        return self.label

    def status(self) -> LaunchdStatus:
        try:
            result = self._run(["print", self.target])
        except FileNotFoundError:
            return LaunchdStatus(self.label, self.plist_path.is_file(), False)
        loaded = result.returncode == 0
        match = re.search(r"\blast exit (?:code|status)\s*=\s*(-?\d+)", result.stdout or "") if loaded else None
        return LaunchdStatus(
            self.label, self.plist_path.is_file(), loaded, int(match[1]) if match else None,
        )


def install(repo_root: Path, **kwargs) -> str:
    return Launchd(repo_root, **kwargs).install()


def uninstall(repo_root: Path, **kwargs) -> str:
    return Launchd(repo_root, **kwargs).uninstall()


def status(repo_root: Path, **kwargs) -> LaunchdStatus:
    return Launchd(repo_root, **kwargs).status()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "refresh":
        print(f"launchd refresh {sys.argv[2]}: {refresh_jobs(Path(sys.argv[2]))}", flush=True)
    else:
        sys.exit("usage: python -m foragent.launchd refresh <repo-root>")
