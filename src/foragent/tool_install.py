"""Install tools in private versions, then atomically publish their entry points."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile
from uuid import uuid4

import psutil

from foragent import capacity

logger = logging.getLogger(__name__)
ENTRY_POINTS = ("fa", "foragent")
OLDER_VERSIONS = 3


@contextmanager
def machine_lock(name: str) -> Iterator[None]:
    """Wait for an exclusive lock file in the machine data root."""
    path = capacity.machine_root() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def run_uv(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    from foragent import orch_reinstall

    return orch_reinstall._uv(args, cwd=cwd, env=env)


def _package_files(root: Path) -> dict[Path, bytes]:
    if not root.is_dir():
        raise RuntimeError(f"package directory missing: {root}")
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.relative_to(root).parts
        and path.suffix not in {".pyc", ".pyo"}
    }


def verify_package(source: Path, tool_dir: Path) -> None:
    """Compare the entire built source package, including generated provenance."""
    packages = list((tool_dir / "foragent" / "lib").glob("python*/site-packages/foragent"))
    if len(packages) != 1:
        raise RuntimeError(f"expected one installed foragent package, found {len(packages)}")
    # setup.py writes provenance into the source during the build; compare after it.
    expected, installed = _package_files(source), _package_files(packages[0])
    different = sorted(str(path) for path in expected.keys() | installed.keys()
                       if expected.get(path) != installed.get(path))
    if different:
        raise RuntimeError(f"installed package differs from merged source: {', '.join(different)}")


def _version_for(path: Path, root: Path) -> Path | None:
    # Resolve directory aliases without following python out of its venv.
    for candidate in (path.parent.resolve() / path.name, path.resolve()):
        if candidate.is_relative_to(root):
            parts = candidate.relative_to(root).parts
            if parts:
                return root / parts[0]
    return None


def _shim_versions(bin_dir: Path, root: Path) -> set[Path]:
    return {version for name in ENTRY_POINTS
            if (version := _version_for(bin_dir / name, root)) is not None}


def _active_versions(root: Path) -> set[Path]:
    active = set()
    unreadable = object()
    paths = [sys.executable]
    for process in psutil.process_iter(["exe", "cmdline"], ad_value=unreadable):
        exe, cmdline = process.info["exe"], process.info["cmdline"]
        if exe is unreadable or cmdline is unreadable:
            raise RuntimeError("process paths unavailable; retaining tool versions")
        paths.extend([exe, *(cmdline or [])])
    for value in paths:
        if value and Path(value).is_absolute():
            if (version := _version_for(Path(value), root)) is not None:
                active.add(version)
    return active


def _collect_versions(root: Path, bin_dir: Path, previous: set[Path]) -> None:
    """Best effort only: an unreadable process table must never put an install at risk."""
    try:
        current = _shim_versions(bin_dir, root)
        protected = current | previous | _active_versions(root)
        versions = sorted((path for path in root.iterdir()
                           if path.is_dir() and not path.is_symlink()), reverse=True)
        # Previous and running versions count toward the older-version budget,
        # but remain protected even when they exceed it.
        remaining = max(0, OLDER_VERSIONS - len((protected & set(versions)) - current))
        for version in versions:
            if version in protected:
                continue
            if remaining:
                remaining -= 1
                continue
            try:
                shutil.rmtree(version)
            except Exception:
                logger.warning("could not remove tool version %s", version, exc_info=True)
    except Exception:
        logger.warning("could not collect tool versions", exc_info=True)


def _publish(version: Path, bin_dir: Path, root: Path) -> None:
    """Migrate the stable shims while still on the old tool, then switch once."""
    current = root / "current"
    if not current.is_symlink():
        # uv's original shims both resolve into the same venv bin directory.
        # Keeping that directory live makes first-time shim migration harmless.
        directories = {(bin_dir / name).resolve().parent for name in ENTRY_POINTS
                       if (bin_dir / name).exists()}
        if directories:
            if len(directories) == 1 and bin_dir.resolve() not in directories:
                initial = next(iter(directories)).parent
            else:
                # Regular shims, or shims in different venvs, need a snapshot:
                # pointing current back at the live bin would create a cycle.
                initial = version.with_name(f"{version.name}-legacy")
                (initial / "bin").mkdir(parents=True)
                for name in ENTRY_POINTS:
                    shim = bin_dir / name
                    if shim.exists():
                        shutil.copy2(shim, initial / "bin" / name)
            current.symlink_to(initial, target_is_directory=True)
        # On a fresh install the shims stay dangling until current is published.
    temporary = []
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
        for name in ENTRY_POINTS:
            shim = bin_dir / name
            target = current / "bin" / name
            if shim.is_symlink() and shim.readlink() == target:
                continue
            link = bin_dir / f".{name}.{uuid4().hex}.tmp"
            temporary.append(link)
            link.symlink_to(target)
            os.replace(link, shim)
        link = root / f".current.{uuid4().hex}.tmp"
        temporary.append(link)
        link.symlink_to(version, target_is_directory=True)
        os.replace(link, current)
    finally:
        for link in temporary:
            link.unlink(missing_ok=True)


def _refresh_gates(version: Path) -> None:
    """Let the new code migrate pinned jobs, even when old ticks cannot."""
    if sys.platform != "darwin":
        return
    from foragent import launchd

    try:
        roots = {job.repo_root for job in launchd.installed_jobs()}
        for root in sorted(roots):
            try:
                launchd.spawn_refresh(root, capacity.machine_root() / "launchd-refresh",
                                      python=version / "tools/foragent/bin/python")
            except Exception:
                logger.warning("could not enqueue launchd refresh for %s", root, exc_info=True)
    except Exception:
        logger.warning("could not enumerate installed gates", exc_info=True)


def install(source: str | Path, *, cwd: Path, revision: str,
            verify: Callable[[Path], None] | None = None) -> Path | None:
    """Install and verify before switching; retain every published version on switch failure."""
    with machine_lock("install.lock"):
        if sys.platform == "win32":
            run_uv(["tool", "install", "--force", "--reinstall", str(source)], cwd=cwd)
            if verify is not None:
                directory = run_uv(["tool", "dir"], cwd=cwd)
                if not directory:
                    raise RuntimeError("uv tool dir returned an empty path")
                verify(Path(directory))
            return None

        directory = run_uv(["tool", "dir", "--bin"], cwd=cwd)
        if not directory:
            raise RuntimeError("uv tool dir --bin returned an empty path")
        bin_dir = Path(directory).expanduser().absolute()
        root = (capacity.machine_root() / "tool-versions").resolve()
        root.mkdir(parents=True, exist_ok=True)
        previous = _shim_versions(bin_dir, root)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        version = Path(tempfile.mkdtemp(prefix=f"{stamp}-{revision}-", dir=root))
        env = {**os.environ, "UV_TOOL_DIR": str(version / "tools"),
               "UV_TOOL_BIN_DIR": str(version / "bin")}
        try:
            run_uv(["tool", "install", "--force", str(source)], cwd=cwd, env=env)
            if verify is not None:
                verify(version / "tools")
            for name in ENTRY_POINTS:
                if not (version / "bin" / name).is_file():
                    raise RuntimeError(f"installed entry point missing: {name}")
        except BaseException:
            try:
                shutil.rmtree(version)
            except Exception:
                logger.warning("could not remove unpublished tool version %s", version, exc_info=True)
            raise

        _publish(version, bin_dir, root)
        _refresh_gates(version)
        _collect_versions(root, bin_dir, previous)
        return version
