"""Daily check for a newer Foragent release, and the explicit `fa update`.

The routine check (`fa brief`, `fa gate status`) asks GitHub for the latest
release at most once per 24 hours per machine, with a short timeout, and is
silent on failure. It only offers the command; it never installs. `fa update`
makes a fresh lookup, installs the release tag under the machine install lock,
then restarts every installed gate between ticks under that gate's tick lock.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import fcntl
import importlib.metadata
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
from typing import Any

from foragent import capacity, launchd, orch_reinstall
from foragent.config import Config, ConfigError, load_config
from foragent.git import GitRepo
from foragent.i18n import text
from foragent.state import StateStore

RELEASE_API = "repos/JinYoon-builds/foragent/releases/latest"
INSTALL_SOURCE = "git+https://github.com/JinYoon-builds/foragent@"
COMMAND = "fa update"
LOOKUP_SECONDS = 5
CHECK_EVERY = timedelta(hours=24)
BUSY_EXIT = 75
_OFF = {"off", "0", "false", "no"}
_VERSION = re.compile(r"v?(\d+(?:\.\d+)*)")


def opted_out(config: Config | None) -> bool:
    if os.environ.get("FORAGENT_UPDATE_CHECK", "").strip().lower() in _OFF:
        return True
    return config is not None and config.update_check is False


def _parse(version: str | None) -> tuple[int, ...] | None:
    match = _VERSION.fullmatch(version.strip()) if isinstance(version, str) else None
    return tuple(int(part) for part in match[1].split(".")) if match else None


def current_version() -> str:
    try:
        return importlib.metadata.version("foragent")
    except importlib.metadata.PackageNotFoundError:
        from foragent import __version__
        return __version__


def _cache_path() -> Path:
    return capacity.machine_root() / "update-check.json"


def _read_cache() -> dict[str, Any]:
    try:
        value = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_cache(record: dict[str, Any]) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(record), encoding="utf-8")
    temporary.replace(path)


def lookup() -> str | None:
    """The latest release tag, or None when GitHub cannot say or the tag is not a version."""
    try:
        result = subprocess.run(["gh", "api", RELEASE_API], capture_output=True, text=True,
                                timeout=LOOKUP_SECONDS, check=True)
        tag = json.loads(result.stdout).get("tag_name")
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None
    return tag if _parse(tag) is not None else None


def _offer(latest: str | None, installed: str | None = None) -> dict[str, str] | None:
    current = current_version()
    have = [version for version in (_parse(current), _parse(installed)) if version is not None]
    wanted = _parse(latest)
    if wanted is None or not have or wanted <= max(have):
        return None
    return {"current": current, "latest": latest.lstrip("v"), "command": COMMAND}


def _newest(*versions: str | None) -> str:
    known = [version for version in versions if _parse(version) is not None]
    return max(known, key=_parse).lstrip("v") if known else str(versions[0])


def check(config: Config | None) -> dict[str, str] | None:
    """The routine check: at most one lookup per 24 hours on this machine."""
    if opted_out(config):
        return None
    # One machine-wide lock around read, lookup and write: concurrent brief/status
    # processes wait for the first lookup (at most LOOKUP_SECONDS) and reuse it.
    # Without the lock the once-a-day limit cannot hold, so a lock that cannot be
    # created or taken skips the check silently, like any other failed check.
    try:
        with _machine_lock("update-check.lock"):
            now = datetime.now(UTC)
            cache = _read_cache()
            try:
                checked = datetime.fromisoformat(str(cache["checked_at"]).replace("Z", "+00:00"))
                fresh = timedelta(0) <= now - checked < CHECK_EVERY
            except (KeyError, ValueError, TypeError):
                fresh = False
            if not fresh:
                # A failure is recorded too, so an offline machine retries the next day.
                cache = {**cache, "checked_at": now.isoformat(), "latest": lookup()}
                try:
                    _write_cache(cache)
                except OSError:
                    pass
    except OSError:
        return None
    return _offer(cache.get("latest"), cache.get("installed"))


def offer_line(offer: dict[str, str], language: str) -> str:
    return text("update_offer", language, latest=offer["latest"], current=offer["current"],
                command=offer["command"])


@contextmanager
def _machine_lock(name: str) -> Iterator[None]:
    """Wait for an exclusive lock file in the machine data root."""
    path = capacity.machine_root() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def installed_gates() -> dict[Path, list[launchd.InstalledJob]]:
    """Each repository root with its dev.foragent.*.plist jobs, hashed or legacy label."""
    gates: dict[Path, list[launchd.InstalledJob]] = {}
    for job in launchd.installed_jobs():
        gates.setdefault(job.repo_root, []).append(job)
    return gates


def gate_store(root: Path) -> tuple[str, StateStore]:
    """The repository and state store `fa gate tick --repo-root root` uses, so its tick lock."""
    config = load_config(root)
    repo = config.repo or GitRepo(root).remote_slug()
    return repo, StateStore.for_repo(repo)


def _language(root: Path | None) -> str:
    try:
        return load_config((root or Path.cwd()).resolve()).language
    except (ConfigError, OSError):
        return "en"


def _restart(root: Path, jobs: list[launchd.InstalledJob], language: str) -> str:
    """Restart one gate between ticks; return restarted, busy, failed or skipped."""
    try:
        loaded = [job for job in jobs if job.loaded()]
        if not loaded:
            print(text("update_gate_not_loaded", language, root=root))
            return "skipped"
        repo, store = gate_store(root)
        with store.tick_lock() as acquired:
            if not acquired:
                print(text("update_gate_busy", language, repo=repo, root=root))
                return "busy"
            for job in loaded:
                job.restart()
    except Exception as exc:  # One broken gate must not stop the others from restarting.
        print(text("update_gate_failed", language, root=root, reason=" ".join(str(exc).split())))
        return "failed"
    print(text("update_gate_restarted", language, repo=repo))
    return "restarted"


def _restart_gates(gates: dict[Path, list[launchd.InstalledJob]], language: str,
                   cache: dict[str, Any]) -> int:
    """Restart ``gates``; remember busy or failed ones so the next fa update retries them."""
    results = {root: _restart(root, jobs, language) for root, jobs in gates.items()}
    pending = [str(root) for root, result in results.items() if result in {"busy", "failed"}]
    try:
        with _machine_lock("update-check.lock"):
            _write_cache({**_read_cache(), **cache, "pending_restart": pending})
    except OSError as exc:
        # The restarts already happened; a lost record only means a retry repeats them.
        print(text("update_record_failed", language, reason=" ".join(str(exc).split())), file=sys.stderr)
    busy = bool(results) and all(result == "busy" for result in results.values())
    return BUSY_EXIT if busy else 0


def update(repo_root: Path | None = None) -> int:
    """`fa update`: install the latest release tag, then restart installed gates."""
    language = _language(repo_root)
    tag = lookup()
    if tag is None:
        print(text("update_lookup_failed", language))
        return 0
    cache = {"checked_at": datetime.now(UTC).isoformat(), "latest": tag}
    with _machine_lock("update-check.lock"):
        saved = _read_cache()
    pending = [root for root in installed_gates() if str(root) in (saved.get("pending_restart") or [])]
    # The tag names the version: a recorded successful install of it counts as current,
    # because package metadata need not change between releases.
    if _offer(tag, saved.get("installed")) is None and not pending:
        with _machine_lock("update-check.lock"):
            _write_cache({**_read_cache(), **cache})
        print(text("update_none", language, current=_newest(current_version(), saved.get("installed"))))
        return 0
    # Otherwise install: a newer release, or an earlier update that left gates
    # unrestarted, which this run finishes by repeating the whole update.
    with _machine_lock("install.lock"):
        try:
            orch_reinstall._uv(["tool", "install", INSTALL_SOURCE + tag], cwd=Path.home())
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            reason = " ".join(str(exc).split()) or type(exc).__name__
            print(text("update_install_failed", language, tag=tag, reason=reason), file=sys.stderr)
            return 1
    print(text("update_installed", language, tag=tag))
    cache["installed"] = tag
    gates = installed_gates()
    if not gates:
        print(text("update_no_gates", language))
    return _restart_gates(gates, language, cache)
