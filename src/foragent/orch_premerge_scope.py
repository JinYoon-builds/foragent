"""The node ids a premerge test command selects on the base (#1105).

A base-passing test protects a merge only if the head ran the same selection.
A full-suite command may name paths, `-k`/`-m` filters or ini `testpaths`, so the
only exact scope is the command's own collection on a checkout of the base.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shlex
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from foragent.run_attempt import AttemptFailure
from foragent.run_baseline import COLLECT_PLUGIN, _validate_baseline_checkout
from foragent.run_checks import CheckResult

if TYPE_CHECKING:
    from foragent.runner import RunContext


# Stepwise runs stop at the first failure whatever --maxfail says. The command text is never
# rewritten (globs, $VAR, ~ and quoting must reach the shell as written), so such a command is
# reported as unusable for the comparison instead of being edited.
STEPWISE = {"--sw", "--stepwise", "--sw-skip", "--stepwise-skip"}


def stepwise_options(command: str) -> list[str]:
    """Stepwise options in ``command`` (read only; the command itself is not rebuilt)."""
    try:
        words = shlex.split(command)
    except ValueError:
        return []
    return sorted({word for word in words if word in STEPWISE})


def full_selection(command: str) -> str:
    """``command`` verbatim, without inherited addopts. The caller appends --maxfail=0 last,
    so -x and --maxfail cannot stop the head run, the base run or the base collection early."""
    return f"PYTEST_ADDOPTS= {command}"


def head_only_paths(command: str, base_files: set[str], head_files: list[str]) -> list[str]:
    """Files the command names (paths, globs, ``opt=path``) that exist only at the head.

    A selector the PR added (a new test file or directory, a new ``-c`` config) is valid on the
    merge result but makes a base collection fail with a usage error. Read only: the words are
    inspected, never used to rebuild the command.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        return []
    found = set()
    head_dirs = {"/".join(parts[:i]) for path in head_files
                 for parts in [path.split("/")] for i in range(1, len(parts))}
    for word in words:
        for candidate in (word, word.partition("=")[2]):
            candidate = os.path.expanduser(os.path.expandvars(candidate.split("::", 1)[0]))
            candidate = candidate.removeprefix("./").rstrip("/")
            if not candidate or candidate.startswith("-"):
                continue
            if any(char in candidate for char in "*?["):
                found.update(path for path in head_files
                             if fnmatch.fnmatch(path, candidate) and path not in base_files)
            elif candidate in head_dirs and not any(path.startswith(candidate + "/") for path in base_files):
                found.add(candidate + "/")  # A directory placeholder: nothing of the head's inside.
            elif candidate in head_files and candidate not in base_files:
                found.add(candidate)
    return sorted(found)


def prepare_base_checkout(ctx: RunContext, worktree: Path, command: str, base: str,
                          head: str | None) -> set[str]:
    """Make head-only paths ``command`` names resolve in a base checkout; return the base's files.

    Each such path gets an empty placeholder (an empty file, or a directory for a path the head
    has files under), never the head's content: a head conftest, config or test module must not
    run in, or reshape, the base measurement. A placeholder selects nothing, so the base answers
    for its own tests only, and an empty ``-c`` config only widens what the base selects.
    """
    base_files = set(ctx.git.tree_files(base))
    if head is None:
        return base_files
    head_files = ctx.git.tree_files(head)
    for path in head_only_paths(command, base_files, head_files):
        if path.endswith("/"):
            (worktree / path).mkdir(parents=True, exist_ok=True)
        else:
            (worktree / path).parent.mkdir(parents=True, exist_ok=True)
            (worktree / path).touch()
    return base_files


def base_ids(ids, base_files: set[str]) -> frozenset[str]:
    return frozenset(node for node in ids if node.split("::", 1)[0] in base_files)


def selected_on_base(ctx: RunContext, command: str, base: str, *, log: Path,
                     check: Callable[[str, Path], CheckResult],
                     acceptance: tuple[str, list[str]] | None = None,
                     head: str | None = None) -> frozenset[str] | None:
    """Node ids ``command`` collects on ``base``; None when collection gives no exact listing.

    Collection errors do not make the listing inexact: a module that fails to import has no
    items on the base, so none of its tests passed there. With ``acceptance`` (directory,
    excluded files) a base with no remaining acceptance file selects nothing worth protecting,
    so pytest is not started. ``command`` is a ``full_selection`` (or acceptance) command.

    With ``head``, paths the command names that only the head has get empty placeholders in the
    base checkout (``prepare_base_checkout``), so the head's selectors resolve without any head
    code running; ids from files the base does not track are left out.
    """
    with tempfile.TemporaryDirectory(prefix="foragent-premerge-scope-") as directory:
        worktree = Path(directory) / "worktree"
        listing = Path(directory) / "collected.json"
        # The listing hooks live in a conftest one level above the checkout, loaded through
        # --confcutdir: no PYTHONPATH or -p, which the command's own `env -u`, `env -i` or
        # `python -I` would drop, and nothing added to the checkout itself.
        (Path(directory) / "conftest.py").write_text(COLLECT_PLUGIN.format(listing=str(listing)),
                                                     encoding="utf-8")
        ctx.git.add_detached_worktree(worktree, base)
        try:
            if acceptance is not None:
                root, excluded = worktree / acceptance[0], set(acceptance[1])
                if not root.is_dir() or not any(
                        path.is_file() and path.relative_to(worktree).as_posix() not in excluded
                        for path in root.rglob("*")):
                    return frozenset()
            base_files = prepare_base_checkout(ctx, worktree, command, base, head)
            # The head ran with --rootdir=.; collect the same way so ids match its JUnit ids.
            collect = f"{command} --rootdir=. --confcutdir={shlex.quote(directory)} --collect-only"
            result = check(collect, worktree)
            try:
                _validate_baseline_checkout(ctx.git.at(worktree), base)
                found = json.loads(listing.read_text(encoding="utf-8"))
                ids = list(found["ids"])
                prefix = Path(found["rootdir"]).resolve().relative_to(worktree.resolve()).as_posix()
            except (AttemptFailure, OSError, ValueError, KeyError, TypeError):
                ids, prefix = None, "."
            # 0 collected, 1/2 collected with errors, 5 nothing selected; 3 (internal error) and
            # 4 (usage error, e.g. a missing path) write a listing that describes no selection.
            if result.timed_out or ids is None or result.exit_code not in (0, 1, 2, 5):
                with log.open("a", encoding="utf-8") as stream:
                    stream.write(f"Pre-merge selection on base={base} unknown (exit_code={result.exit_code}, "
                                 f"timed_out={result.timed_out})\n")
                return None
            ids = (node if prefix == "." else f"{prefix}/{node}" for node in ids)
            return base_ids(ids, base_files)
        finally:
            ctx.git.remove_worktree(worktree)
