"""The node ids a premerge test command selects on the base (#1105).

A base-passing test protects a merge only if the head ran the same selection.
A full-suite command may name paths, `-k`/`-m` filters or ini `testpaths`, so the
only exact scope is the command's own collection on a checkout of the base.
"""

from __future__ import annotations

import json
import secrets
import shlex
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from foragent.run_attempt import AttemptFailure
from foragent.run_baseline import (
    COLLECT_PLUGIN,
    _validate_baseline_checkout,
    _with_python_path,
)
from foragent.run_checks import CheckResult

if TYPE_CHECKING:
    from foragent.runner import RunContext


# Options that run only part of the selection whatever --maxfail says (the cache provider's
# last-failed/stepwise state). --maxfail=0, appended after the command, ends -x and --maxfail.
PARTIAL_FLAGS = {"--sw", "--stepwise", "--sw-skip", "--stepwise-skip", "--lf", "--last-failed",
                 "--ff", "--failed-first", "--nf", "--new-first"}
PARTIAL_VALUES = {"--lfnf", "--last-failed-no-failures"}


def full_selection(command: str) -> str:
    """``command`` running every test it selects: no inherited addopts or partial-run options.

    The caller appends --maxfail=0 after its own options; ini addopts stay, so the head run,
    the base run and the base collection all select exactly the same tests.
    """
    words = shlex.split(command)
    kept, skip = [], False
    for word in words:
        if skip:
            skip = False
            continue
        option = word.split("=", 1)[0]
        if word in PARTIAL_FLAGS:
            continue
        if option in PARTIAL_VALUES:
            skip = "=" not in word
            continue
        kept.append(word)
    rendered, leading = [], True
    for word in kept:
        name, _, value = word.partition("=")
        if leading and _ and name.isidentifier():
            rendered.append(f"{name}={shlex.quote(value)}")  # An assignment must stay NAME=value.
            continue
        leading = False
        rendered.append(shlex.quote(word))
    return " ".join(["PYTEST_ADDOPTS=", *rendered])


def selected_on_base(ctx: RunContext, command: str, base: str, *, log: Path,
                     check: Callable[[str, Path], CheckResult],
                     acceptance: tuple[str, list[str]] | None = None) -> frozenset[str] | None:
    """Node ids ``command`` collects on ``base``; None when collection gives no exact listing.

    Collection errors do not make the listing inexact: a module that fails to import has no
    items on the base, so none of its tests passed there. With ``acceptance`` (directory,
    excluded files) a base with no remaining acceptance file selects nothing worth protecting,
    so pytest is not started. ``command`` is a ``full_selection`` (or acceptance) command.
    """
    with tempfile.TemporaryDirectory(prefix="foragent-premerge-scope-") as directory:
        worktree = Path(directory) / "worktree"
        listing = Path(directory) / "collected.json"
        plugins = Path(directory) / "plugins"
        plugins.mkdir()
        plugin = f"_foragent_nodeids_{secrets.token_hex(8)}"
        (plugins / f"{plugin}.py").write_text(COLLECT_PLUGIN.format(listing=str(listing)), encoding="utf-8")
        ctx.git.add_detached_worktree(worktree, base)
        try:
            if acceptance is not None:
                root, excluded = worktree / acceptance[0], set(acceptance[1])
                if not root.is_dir() or not any(
                        path.is_file() and path.relative_to(worktree).as_posix() not in excluded
                        for path in root.rglob("*")):
                    return frozenset()
            # The head ran with --rootdir=.; collect the same way so ids match its JUnit ids.
            collect = _with_python_path(f"{command} --rootdir=. --collect-only -p {plugin}", str(plugins))
            result = check(collect, worktree)
            try:
                _validate_baseline_checkout(ctx.git.at(worktree), base)
                found = json.loads(listing.read_text(encoding="utf-8"))
                ids = list(found["ids"])
                prefix = Path(found["rootdir"]).resolve().relative_to(worktree.resolve()).as_posix()
            except (AttemptFailure, OSError, ValueError, KeyError, TypeError):
                ids, prefix = None, "."
            if result.timed_out or ids is None:
                with log.open("a", encoding="utf-8") as stream:
                    stream.write(f"Pre-merge selection on base={base} unknown (exit_code={result.exit_code}, "
                                 f"timed_out={result.timed_out})\n")
                return None
            return frozenset(node if prefix == "." else f"{prefix}/{node}" for node in ids)
        finally:
            ctx.git.remove_worktree(worktree)
