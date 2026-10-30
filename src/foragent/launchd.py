"""Per-repository launchd jobs with injectable paths and command execution."""

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha1
import os
from pathlib import Path
import plistlib
import re
import fcntl
import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime

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


def _fa_shim() -> Path | None:
    """Keep the PATH entry stable when a versioned install switches its target."""
    if found := shutil.which("fa"):
        return Path(found).absolute()
    return None


def fa_executable() -> Path:
    if shim := _fa_shim():
        return shim
    current = Path(sys.argv[0]).expanduser()
    if current.name in {"fa", "foragent"}:
        if current.is_file():
            return current.absolute()
        if found := shutil.which(str(current)):
            return Path(found).absolute()
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


# D97: launchd starts the gate through osascript's JXA $.system, so macOS holds
# the Apple platform binary responsible for the gate's children and denies a
# protected-folder request without a permission prompt. $.system, not
# `do shell script`, which keeps polling the WindowServer. The gate command
# follows the script as its run(argv), so --repo-root stays readable.
OSASCRIPT = "/usr/bin/osascript"
_JXA = (r"""function run(argv) { ObjC.import("stdlib"); """
        r"""var quote = function (a) { return "'" + a.split("'").join("'\\''") + "'" }; """
        r"""$.exit($.system("exec " + argv.map(quote).join(" ")) >> 8) }""")
_WRAPPER = [OSASCRIPT, "-l", "JavaScript", "-e"]


def wrap_arguments(command: list[str]) -> list[str]:
    """The launchd ProgramArguments that run ``command`` under the D97 wrapper."""
    return [*_WRAPPER, _JXA, *gate_command(command)]


def gate_command(arguments: list) -> list:
    """The gate command inside ProgramArguments, wrapped (D97) or legacy."""
    arguments = list(arguments)
    return arguments[len(_WRAPPER) + 1:] if arguments[:len(_WRAPPER)] == _WRAPPER else arguments


# Keep gate imports from writing bytecode into installed package trees.
# Python needs this environment when the process starts, before any imports.
GATE_ENVIRONMENT = {"PYTHONDONTWRITEBYTECODE": "1"}
REFRESH_WAIT_SECONDS = 1800
REFRESH_POLL_SECONDS = 5


def _plist(path: Path) -> dict | None:
    try:
        data = plistlib.loads(Path(path).read_bytes())
    except (OSError, ValueError, TypeError, AttributeError, plistlib.InvalidFileException):
        return None
    return data if isinstance(data, dict) else None


def _environment(path: Path) -> dict | None:
    """The plist's EnvironmentVariables, or None when the file cannot be read as a plist."""
    if (data := _plist(path)) is None:
        return None
    environment = data.get("EnvironmentVariables") or {}
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
    """True when a readable plist needs the gate environment or stable executable."""
    return _refreshed_plist(path) is not None


def _refreshed_plist(path: Path) -> dict | None:
    """Build independent environment and executable migrations without writing."""
    if (data := _plist(path)) is None:
        return None
    shim = _fa_shim()
    if shim is None:
        print(f"foragent: cannot locate fa on PATH; keeping executable in {path}", file=sys.stderr)
    environment = data.get("EnvironmentVariables")
    environment = environment if isinstance(environment, dict) else {}
    arguments = data.get("ProgramArguments")
    command = gate_command(arguments) if isinstance(arguments, list) else []
    current_executable = shim is None or not command or command[0] == str(shim)
    if current_executable and all(environment.get(name) == value for name, value in GATE_ENVIRONMENT.items()):
        return None
    data["EnvironmentVariables"] = {**environment, **GATE_ENVIRONMENT}
    if shim is not None and command:
        data["ProgramArguments"] = _rewrap(arguments, [str(shim), *command[1:]])
    return data


def _rewrap(arguments: list, command: list) -> list:
    """``command`` in the shape ``arguments`` had: wrapped stays wrapped, legacy stays legacy."""
    return wrap_arguments(command) if arguments[:len(_WRAPPER)] == _WRAPPER else command


def _definitions(path: Path) -> tuple[dict, dict]:
    """The candidate and rollback definitions for a reload, validated before any bootout.

    Candidate: the refreshed environment and stable executable. Rollback: the
    installed definition, its removed executable replaced by the stable shim.
    Both run under the D97 wrapper; a legacy unwrapped definition is never loaded.
    """
    if (old := _plist(path)) is None:
        raise LaunchdError(f"cannot read {path}; run fa gate install to rewrite it")
    candidate = _refreshed_plist(path) or _plist(path)
    rollback = dict(old)
    command = gate_command(old.get("ProgramArguments") or [])
    if command and not Path(str(command[0])).is_file() and (shim := _fa_shim()) is not None:
        command = [str(shim), *command[1:]]
    rollback["ProgramArguments"] = command
    for definition in (candidate, rollback):
        command = definition.get("ProgramArguments")
        if (not isinstance(definition.get("Label"), str) or not isinstance(command, list) or not command
                or "--repo-root" not in gate_command(command)
                or not all(isinstance(value, str) for value in command)):
            raise LaunchdError(f"{path} does not define a gate tick; run fa gate install to rewrite it")
        definition["ProgramArguments"] = wrap_arguments(command)
        plistlib.loads(plistlib.dumps(definition))
    return candidate, rollback


RELOAD_FACT = "launchd-reload.json"


def _reload_store(root: Path) -> StateStore:
    from foragent import update_check

    return update_check.gate_store(root)[1]


def reload_fact(state_root: Path | None) -> dict | None:
    """The latest reload fact of a gate's state folder; None until a reload is attempted."""
    if state_root is None:
        return None
    try:
        fact = json.loads((Path(state_root) / RELOAD_FACT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return fact if isinstance(fact, dict) else None


def reload_pending(job: "InstalledJob") -> bool:
    """True when this job's last reload left it unloaded, so a refresher retries it."""
    try:
        fact = reload_fact(_reload_store(job.repo_root).root)
    except Exception:
        return False
    return bool(fact and fact.get("pending") and fact.get("plist") == str(job.plist_path))


def _write_plist(path: Path, content: bytes) -> None:
    """Replace the plist atomically: launchd finds the old file or the new one, never a torn one."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _service_state(run: Callable[[list[str]], subprocess.CompletedProcess[str]], target: str) -> str:
    """loaded, absent (launchd says no such service) or unknown (any other failure)."""
    try:
        result = run(["print", target])
    except OSError:
        return "unknown"
    if result.returncode == 0:
        return "loaded"
    output = f"{result.stderr or ''}{result.stdout or ''}".lower()
    return "absent" if result.returncode == 113 or "could not find service" in output else "unknown"


def _reload_record(label: str, plist: Path, *, error: str | None, attempts: int, state: str,
                   rolled_back: bool = False) -> dict:
    """One reload fact. A job not confirmed loaded is pending, and always names why."""
    loaded = state == "loaded"
    if not loaded and error is None:
        error = f"launchctl print: job {state} after bootstrap"
    return {"at": datetime.now(UTC).isoformat(), "error": error, "attempts": attempts,
            "recovered": error is not None and loaded, "loaded": loaded, "pending": not loaded,
            "rolled_back": rolled_back, "label": label, "plist": str(plist)}


def _one_line(error: BaseException | str) -> str:
    return " ".join(str(error).split()) or type(error).__name__


def _record_reload(root: Path, store: StateStore | None, fact: dict) -> None:
    """Replace the latest reload fact; a lost record never undoes the reload."""
    try:
        store = store or _reload_store(root)
        store._write_json(store.root / RELOAD_FACT, fact)
    except Exception as exc:
        print(f"foragent: cannot record launchd reload: {' '.join(str(exc).split())}", file=sys.stderr)


def _migrate_environment(path: Path) -> None:
    """Refresh the gate environment and executable, keeping every other key."""
    if (data := _refreshed_plist(path)) is not None:
        path.write_bytes(plistlib.dumps(data))


def installed_plist(repo: str, home: Path | None = None) -> Path:
    return (Path(home) if home is not None else Path.home()) / "Library/LaunchAgents" / f"{label_for(repo)}.plist"


def stale_jobs(repo_root: Path, home: Path | None = None) -> list["InstalledJob"]:
    """Every installed job ticking ``repo_root`` with stale environment or executable.

    Found by the plist's --repo-root, as fa update finds gates, so legacy labels
    and plists written under an earlier repository name are included.
    """
    root = Path(repo_root).resolve()
    return [job for job in installed_jobs(home) if job.repo_root == root
            and needs_environment_refresh(job.plist_path)]


def _unreadable_plist(repo: str) -> Path | None:
    path = installed_plist(repo)
    return path if path.exists() and _environment(path) is None else None


def refresh_if_stale(repo: str, repo_root: Path, log_dir: Path) -> bool:
    """From a tick: start a detached reload for a stale environment or executable.

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
    spawn_refresh(repo_root, log_dir, python=sys.executable)
    return True


def spawn_refresh(repo_root: Path, log_dir: Path, *, python: str | Path) -> None:
    """Enqueue the between-ticks refresher using the requested installation."""
    from foragent.launcher import gate_environment

    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "launchd-refresh.log").open("ab") as stream:
        subprocess.Popen(
            [str(python), "-m", "foragent.launchd", "refresh", str(Path(repo_root).resolve())],
            stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, start_new_session=True,
            env=gate_environment(),
        )


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
        # on disk alone: a current file always means a reloaded job.
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
    stale = stale_jobs(root)
    states = [(job, job.state()) for job in stale]
    jobs = [(job, state) for job, state in states if state != "absent"]
    # A reload that ended unloaded is recorded as pending: retry it although no tick runs.
    known = {job.plist_path for job, _ in jobs}
    for job in installed_jobs():
        if (Path(job.repo_root).resolve() == root and job.plist_path not in known
                and reload_pending(job) and (state := job.state()) != "loaded"):
            jobs.append((job, state))  # unknown stops the refresher; the next tick retries.
    return jobs


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
        return _service_state(_run_launchctl, self.target)

    def restart(self) -> None:
        """Reload the plist: one bootout, then at most three bootstraps.

        The candidate (gate environment, stable shim, D97 wrapper) is built and
        validated before the bootout and written while the job is out, so a
        kickstart never sees the new file with the old definition loaded. A failed
        bootstrap gets one retry, then one rollback to the installed definition
        (still D97-wrapped). The outcome is recorded as the gate's latest reload fact.
        """
        candidate, rollback = _definitions(self.plist_path)
        _bootout(self._call, self.target)
        # From here the job may be out: every failure is recorded, never raised past the record.
        error, attempts, applied = None, 0, None
        for definition in (candidate, candidate, rollback):
            if attempts:
                time.sleep(1)  # Let launchd finish the bootout before trying again.
            try:
                _write_plist(self.plist_path, plistlib.dumps(definition))
            except Exception as exc:  # The next definition may still be written and loaded.
                error = error or _one_line(f"cannot write {self.plist_path}: {exc}")
                continue
            attempts += 1
            try:
                result = self._call(["bootstrap", self.domain, str(self.plist_path)])
                failure = (f"launchctl bootstrap: {result.stderr or result.stdout or result.returncode}"
                           if result.returncode else None)
            except Exception as exc:
                failure = str(exc)
            if failure is None:
                applied = definition
                break
            error = error or _one_line(failure)
        fact = _reload_record(self.label, self.plist_path, error=error, attempts=attempts,
                              state=self.state(), rolled_back=applied is rollback and applied is not candidate)
        _record_reload(self.repo_root, None, fact)
        if not fact["loaded"]:
            raise LaunchdError(f"{fact['error']}; job not loaded")
        if applied is not candidate:
            raise LaunchdError(f"{fact['error']}; the new definition was not loaded")


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
            Path(self.executable).expanduser().absolute()
            if self.executable is not None else fa_executable()
        )
        return plistlib.dumps({
            "Label": self.label,
            "ProgramArguments": wrap_arguments(
                [str(executable), "gate", "tick", "--repo-root", str(self.repo_root)]),
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
        # The job is out: the outcome replaces the gate's latest reload fact, failed or not.
        error, attempts = None, 0
        try:
            if not self.plist_path.exists() or self.plist_path.read_bytes() != content:
                _write_plist(self.plist_path, content)
            attempts = 1
            result = self._call(["bootstrap", self.domain, str(self.plist_path)])
            if result.returncode:
                error = f"launchctl bootstrap: {result.stderr or result.stdout or result.returncode}"
        except LaunchdError as exc:
            error = str(exc)
        except OSError as exc:
            error = f"cannot write {self.plist_path}: {exc}"
        state = _service_state(self._run, self.target)
        fact = _reload_record(self.label, self.plist_path, error=error and _one_line(error),
                              attempts=attempts, state=state)
        _record_reload(self.repo_root, StateStore(self.state_root), fact)
        if error is not None:
            raise LaunchdError(error)
        if not fact["loaded"]:
            raise LaunchdError(f"{fact['error']}; job not loaded")
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
