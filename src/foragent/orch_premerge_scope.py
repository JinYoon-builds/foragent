"""The node ids a premerge test command selects on the base (#1105).

A base-passing test protects a merge only if the head ran the same selection.
A full-suite command may name paths, `-k`/`-m` filters or ini `testpaths`, so the
only exact scope is the command's own collection on a checkout of the base.
"""

from __future__ import annotations

import json
import secrets
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


def selected_on_base(ctx: RunContext, command: str, base: str, *, log: Path,
                     check: Callable[[str, Path], CheckResult]) -> frozenset[str] | None:
    """Node ids ``command`` collects on ``base``; None when collection gives no exact listing."""
    with tempfile.TemporaryDirectory(prefix="foragent-premerge-scope-") as directory:
        worktree = Path(directory) / "worktree"
        listing = Path(directory) / "collected.json"
        plugins = Path(directory) / "plugins"
        plugins.mkdir()
        plugin = f"_foragent_nodeids_{secrets.token_hex(8)}"
        (plugins / f"{plugin}.py").write_text(COLLECT_PLUGIN.format(listing=str(listing)), encoding="utf-8")
        ctx.git.add_detached_worktree(worktree, base)
        try:
            # The head ran with --rootdir=.; collect the same way so ids match its JUnit ids.
            collect = _with_python_path(f"{command} --rootdir=. --collect-only -p {plugin}", str(plugins))
            result = check(collect, worktree)
            try:
                _validate_baseline_checkout(ctx.git.at(worktree), base)
                found = json.loads(listing.read_text(encoding="utf-8"))
                ids, errors = list(found["ids"]), int(found["errors"])
                prefix = Path(found["rootdir"]).resolve().relative_to(worktree.resolve()).as_posix()
            except (AttemptFailure, OSError, ValueError, KeyError, TypeError):
                ids, errors, prefix = None, None, "."
            if result.timed_out or result.exit_code not in (0, 5) or ids is None or errors:
                with log.open("a", encoding="utf-8") as stream:
                    stream.write(f"Pre-merge selection on base={base} unknown (exit_code={result.exit_code}, "
                                 f"timed_out={result.timed_out}); base-passing tests are not compared\n")
                return None
            return frozenset(node if prefix == "." else f"{prefix}/{node}" for node in ids)
        finally:
            ctx.git.remove_worktree(worktree)
