"""The node ids a premerge test command selects on the base (#1105).

A base-passing test protects a merge only if the head ran the same selection.
A full-suite command may name paths, `-k`/`-m` filters or ini `testpaths`, so the
only exact scope is the command's own collection on a checkout of the base.
"""

from __future__ import annotations

import json
import re
import secrets
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
# One shell word as written (quotes and escapes kept), and a leading NAME=value assignment.
_WORD = re.compile(r"""(?:'[^']*'|"(?:\\.|[^"\\])*"|\\.|[^\s'"\\])+""")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


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


def _with_plugin_path(command: str, directory: str) -> str:
    """Put ``directory`` on PYTHONPATH without touching the rest of ``command``.

    Only the leading assignment and ``env`` words are read, as written. An inline PYTHONPATH
    assignment would replace an outer one, so the directory is appended to that word itself.
    """
    inline = []
    for match in _WORD.finditer(command):
        word = match.group()
        if not (_ASSIGNMENT.match(word) or word == "env" or word.endswith("/env")):
            break
        if word.startswith("PYTHONPATH="):
            inline.append(match.end())
    quoted = shlex.quote(directory)
    if not inline:
        return f"PYTHONPATH={quoted}${{PYTHONPATH:+:\"$PYTHONPATH\"}} {command}"
    for end in reversed(inline):
        command = f"{command[:end]}:{quoted}{command[end:]}"
    return command


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
            collect = _with_plugin_path(f"{command} --rootdir=. --collect-only -p {plugin}", str(plugins))
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
            return frozenset(node if prefix == "." else f"{prefix}/{node}" for node in ids)
        finally:
            ctx.git.remove_worktree(worktree)
