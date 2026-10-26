"""Refresh the installed Foragent tool after a successful self-merge."""

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from time import sleep
import tomllib
from typing import TYPE_CHECKING

from foragent.orch_events import append_event

if TYPE_CHECKING:
    from foragent.runner import RunContext


# Running processes can write __pycache__ into the tool directory while uv
# empties it; a moment later the same install succeeds.
RACE_ERRORS = ("Directory not empty", "failed to remove directory")
INSTALL_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0


def _uv(args: list[str], *, cwd: Path) -> str:
    for attempt in range(1, INSTALL_ATTEMPTS + 1):
        result = subprocess.run(
            ["uv", *args], cwd=cwd, capture_output=True, text=True,
            errors="replace", timeout=300,
        )
        if not result.returncode:
            return result.stdout.strip()
        output = f"{result.stderr}\n{result.stdout}"
        if attempt == INSTALL_ATTEMPTS or not any(error in output for error in RACE_ERRORS):
            raise RuntimeError(result.stderr.strip() or result.stdout.strip()
                               or f"uv {' '.join(args)} exited {result.returncode}")
        sleep(BACKOFF_SECONDS * attempt)
    raise AssertionError("unreachable")


def _package_files(root: Path) -> dict[Path, bytes]:
    if not root.is_dir():
        raise RuntimeError(f"package directory missing: {root}")
    # Python creates caches when the installed CLI runs; they are not source.
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.relative_to(root).parts
        and path.suffix not in {".pyc", ".pyo"}
    }


def _reinstall(ctx: "RunContext", base: str) -> str | None:
    ctx.git.fetch("origin", base)
    sha = ctx.git.ref_sha(f"refs/remotes/origin/{base}")
    with TemporaryDirectory(prefix="foragent-reinstall-") as directory:
        worktree = Path(directory) / "worktree"
        ctx.git.add_detached_worktree(worktree, sha)
        try:
            project_file = worktree / "pyproject.toml"
            if not project_file.is_file():
                return None
            with project_file.open("rb") as stream:
                project = tomllib.load(stream).get("project", {})
            if project.get("name") != "foragent":
                return None
            source = worktree / "src" / "foragent"
            if not source.is_dir():
                raise RuntimeError(f"package directory missing: {source}")
            _uv(["tool", "install", "--force", "--reinstall", str(worktree)], cwd=worktree)
            tool_dir = _uv(["tool", "dir"], cwd=worktree)
            if not tool_dir:
                raise RuntimeError("uv tool dir returned an empty path")
            packages = list((Path(tool_dir) / "foragent" / "lib").glob("python*/site-packages/foragent"))
            if len(packages) != 1:
                raise RuntimeError(f"expected one installed foragent package, found {len(packages)}")
            # Compare after building: setup.py also writes build provenance into
            # src/foragent, and that generated file must match the installation.
            expected, installed = _package_files(source), _package_files(packages[0])
            different = sorted(str(path) for path in expected.keys() | installed.keys()
                               if expected.get(path) != installed.get(path))
            if different:
                raise RuntimeError(f"installed package differs from merged source: {', '.join(different)}")
        finally:
            ctx.git.remove_worktree(worktree)
    return f"reinstalled Foragent {sha}; installed package matches merged source"


def after_merge(ctx: "RunContext", pr: int, base: str) -> None:
    """Keep the successful merge even if installing or verifying fails."""
    try:
        message = _reinstall(ctx, base)
    except Exception as exc:
        reason = " ".join(str(exc).split()) or type(exc).__name__
        print(f"reinstall failed: {reason}")
        append_event(ctx.store, ctx.now(), None, "reinstall-failed", f"PR #{pr}: {reason}")
    else:
        if message is not None:
            print(message)
