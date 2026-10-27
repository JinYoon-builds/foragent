"""Durable records of runner steps a stop's kill may cut (D95).

The stop terminates the runner first and acts only on a dead tree, so a kill
can land inside a step that replaces the worktree (a rebuild with a patch to
re-apply, a replay restore, a reviewer's edits being undone) or inside a merge.
Before such a step the runner writes what its own work is, as Git objects (a
commit and a staged tree), and removes the record when the step is done. A
runner killed inside the step leaves the record; the stop restores the work
from it before saving. Runners die without unwinding (no SIGTERM handler), so
a record that is present means the step did not finish.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from typing import Any, Iterator

from foragent.git import GitError


def _path(store, number: int, kind: str) -> Path:
    return store.root / "runs" / f"issue-{number}.{kind}.json"


def _current(store, number: int, kind: str) -> dict[str, Any] | None:
    run = store.get_run(number)
    try:
        record = json.loads(_path(store, number, kind).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    return record if run is not None and record.get("started_at") == run.started_at else None


@contextmanager
def _record(store, number: int, kind: str, fields: dict[str, Any]) -> Iterator[None]:
    """Keep the record for the step's duration; an outer record of the same run wins."""
    run = store.get_run(number)
    if run is None or _current(store, number, kind) is not None:
        yield
        return
    path = _path(store, number, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    store._atomic_write(path, json.dumps({"started_at": run.started_at, **fields}))
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def worktree_state(git, branch: str) -> dict[str, Any] | None:
    """The run's own work in a worktree: HEAD, the staged tree and the branch tip when HEAD is on it."""
    try:
        head, tree = git.head_sha(), git.staged_tree_sha()
    except GitError:
        return None  # An unmerged index: no tree to restore (a rebuild discards it anyway).
    try:
        on_branch = git._call(["symbolic-ref", "-q", "HEAD"]).strip() == f"refs/heads/{branch}"
    except GitError:
        on_branch = False
    return {"head": head, "tree": tree, "branch": branch, "branch_tip": head if on_branch else None}


@contextmanager
def checkpoint(store, number: int, state: dict[str, Any] | None) -> Iterator[None]:
    """Record `state` (from worktree_state, or an explicit commit and tree) around a destructive step."""
    if state is None:
        yield
        return
    with _record(store, number, "checkpoint", state):
        yield


def read_checkpoint(store, number: int) -> dict[str, Any] | None:
    return _current(store, number, "checkpoint")


def drop_checkpoint(store, number: int) -> None:
    _path(store, number, "checkpoint").unlink(missing_ok=True)


def restore(ctx, worktree: Path, state: dict[str, Any]) -> None:
    """Put a cut step's recorded work back in a fresh worktree, so the stop saves that work."""
    if state.get("branch_tip"):
        ctx.git._call(["update-ref", f"refs/heads/{state['branch']}", state["branch_tip"]])
    ctx.git.remove_worktree(worktree)
    ctx.git.add_detached_worktree(worktree, state["head"])
    ctx.git.at(worktree)._call(["read-tree", "--reset", "-u", state["tree"]])


@contextmanager
def merging(store, number: int) -> Iterator[None]:
    """Mark a merge call in flight: the stop waits for it instead of cutting it."""
    with _record(store, number, "merging", {}):
        yield


def merge_in_flight(store, number: int) -> bool:
    return _current(store, number, "merging") is not None
