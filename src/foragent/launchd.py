"""Per-repository launchd jobs with injectable paths and command execution."""

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha1
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys

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
            "ProcessType": "Background",
            "EnvironmentVariables": {
                "PATH": f"{self.home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                "HOME": str(self.home), "LANG": "en_US.UTF-8",
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
        result = self._call(["bootout", target or self.target])
        if result.returncode:
            message = (result.stderr or result.stdout or "").strip()
            if re.search(r"not loaded|could not find service|no such process|service not found", message, re.I):
                return
            if result.returncode == 3 and not message:
                return
            raise LaunchdError(f"launchctl bootout: {message or result.returncode}")

    def _remove_legacy(self) -> None:
        if self.legacy_plist_path.exists():
            self._bootout(f"{self.domain}/{self.legacy_label}")
            self.legacy_plist_path.unlink()

    def install(self) -> str:
        content = self.plist()
        self.plist_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_root.mkdir(parents=True, exist_ok=True)
        if not self.plist_path.exists() or self.plist_path.read_bytes() != content:
            self.plist_path.write_bytes(content)
        self._remove_legacy()
        self._bootout()
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
