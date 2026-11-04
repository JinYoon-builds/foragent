"""Refresh the installed Foragent tool after merges and fetched base moves."""

from pathlib import Path
import json
import re
import subprocess
from tempfile import TemporaryDirectory
from time import sleep
import tomllib
from typing import TYPE_CHECKING

from foragent import capacity, tool_install
from foragent.git import GitError
from foragent.orch_events import append_event

if TYPE_CHECKING:
    from foragent.runner import RunContext


def _uv(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    """Shared uv boundary, including retries for uv's directory-removal race."""
    for attempt in range(1, 4):
        result = subprocess.run(
            ["uv", *args], cwd=cwd, env=env, capture_output=True, text=True,
            errors="replace", timeout=300,
        )
        if not result.returncode:
            return result.stdout.strip()
        output = f"{result.stderr}\n{result.stdout}"
        if attempt == 3 or not any(error in output for error in (
            "Directory not empty", "failed to remove directory",
        )):
            raise RuntimeError(result.stderr.strip() or result.stdout.strip()
                               or f"uv {' '.join(args)} exited {result.returncode}")
        sleep(2.0 * attempt)
    raise AssertionError("unreachable")


def _reinstall(ctx: "RunContext", base: str) -> str | None:
    ctx.git.fetch("origin", base)
    sha = ctx.git.ref_sha(f"refs/remotes/origin/{base}")
    return _install_sha(ctx, sha)


def _install_sha(ctx: "RunContext", sha: str) -> str | None:
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
            tool_install.install(worktree, cwd=worktree, revision=sha,
                                 verify=lambda directory: tool_install.verify_package(source, directory))
        finally:
            ctx.git.remove_worktree(worktree)
    return f"reinstalled Foragent {sha}; installed package matches merged source"


def after_merge(ctx: "RunContext", pr: int, base: str) -> None:
    """Keep the successful merge even if installing or verifying fails."""
    config = getattr(ctx, "config", None)
    if getattr(config, "merge_owner", False) and base != config.base_branch:
        return
    try:
        message = _reinstall(ctx, base)
    except Exception as exc:
        reason = " ".join(str(exc).split()) or type(exc).__name__
        print(f"reinstall failed: {reason}")
        append_event(ctx.store, ctx.now(), None, "reinstall-failed", f"PR #{pr}: {reason}")
    else:
        if message is not None:
            print(message)


def _installed_revision() -> str | None:
    installed = tool_install.installed_tool()
    if installed is not None:
        return installed[0]
    # Older/fake builds may lack _build.py; only a published version names a build.
    try:
        version = (capacity.machine_root() / "tool-versions/current").resolve(strict=True)
    except OSError:
        return None
    match = re.fullmatch(r"\d{8}T\d{12}Z-([0-9a-f]{40})-.+", version.name)
    return match[1] if match else None


def follow_base(ctx) -> None:
    """Follow the fetched base under the tick lock, regardless of who merged it."""
    sha = None
    try:
        base = f"refs/remotes/origin/{ctx.config.base_branch}"
        project = ctx.git.file_at(base, "pyproject.toml")
        if project is None or tomllib.loads(project).get("project", {}).get("name") != "foragent":
            return
        installed = _installed_revision()
        if installed is None:
            return
        sha = ctx.git.ref_sha(base)
        if sha == installed:
            return
        path = ctx.store.root / "tool-follow.json"
        try:
            failed = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            failed = {}
        if failed.get("failed_sha") == sha:
            return
        try:
            changed = ctx.git._call(["diff", "--name-only", f"{installed}..{sha}", "--", "src/", "pyproject.toml"])
        except GitError:
            changed = True  # A pruned/shallow installed commit cannot prove the move harmless.
        if not changed:
            return
        message = _install_sha(ctx, sha)
        if message is not None:
            print(message)
    except Exception as exc:
        if sha is not None:
            ctx.store._write_json(ctx.store.root / "tool-follow.json", {"failed_sha": sha})
        reason = " ".join(str(exc).split()) or type(exc).__name__
        print(f"reinstall failed: {reason}")
        append_event(ctx.store, ctx.now(), None, "reinstall-failed", f"base {sha}: {reason}")
