"""Publication reads the reviewed evidence of `fa order prepare` (#1049).

Preparation owns the record (``order_prepare``); publication only reads it. A statement issue is
published only when the record holds a passing review of exactly the plan's current tests and
commands, for the issue's current cycle identity (requirements, shared contracts, references,
risk guidance, writer and reviewer setup), made on the base publication is about to write onto.
No model starts here and nothing is written: stale or missing evidence sends the person back to
``fa order prepare``. The scheduler still captures Approval snapshots and the publisher still owns
the published bytes; this is only the approval lock's input check.
"""

from __future__ import annotations

from pathlib import Path

from foragent.config import Config
from foragent.git import GitError, GitRepo
from foragent.i18n import text


def statement_items(plan: dict, by_number: dict, config: Config) -> list[dict]:
    """The statement issues and edits, read as preparation read them (edits through the tracker)."""
    from foragent.order import edit_view

    items = [item for item in plan["issues"] if "statements" in item]
    items += [edit_view(item, by_number[item["number"]], config)
              for item in plan.get("edits", []) if "statements" in item]
    return items


def _language(config: Config) -> str:
    return (getattr(config, "language", None) or "en").lower().split("-")[0]


def require_reviewed_tests(plan: dict, root: Path, config: Config, repo: str,
                           items: list[dict]) -> dict[str, str]:
    """The tested base revision of each statement issue; refuses unless every one has current evidence.

    The base itself is checked by ``require_tested_base`` against the base actually published onto.
    """
    from foragent.order import PlanError
    from foragent.order_prepare import _read, _record_path
    from foragent.order_prepare_cycle import identity
    from foragent.state import StateStore

    if not items:
        return {}
    language = _language(config)
    record = _read(_record_path(StateStore.for_repo(repo), plan), plan)["issues"]
    missing, stale, differ, bases = [], [], [], {}
    for item in items:
        key, entry = item["key"], record.get(item["key"])
        if not isinstance(entry, dict) or entry.get("status") != "reviewed":
            status = entry.get("status") if isinstance(entry, dict) else None
            missing.append(f"{key} ({status})" if status else key)
        elif entry.get("cycle") != identity(plan, item, config, root):
            stale.append(key)
        elif (entry.get("written") != item.get("acceptance_files")
              or entry.get("done_when") != item.get("done_when")):
            differ.append(key)
        else:
            bases[key] = entry.get("base_revision")
    problems = []
    if missing:
        problems.append(text("reviewed_missing", language, keys=", ".join(missing)))
    if stale:
        problems.append(text("reviewed_stale", language, keys=", ".join(stale)))
    if differ:
        problems.append(text("reviewed_differ", language, keys=", ".join(differ)))
    if problems:
        raise PlanError(" ".join(problems))
    return bases


def require_tested_base(git: GitRepo, config: Config, bases: dict[str, str], current: str) -> None:
    """Refuse when the base moved since review; a base with the very same content keeps its evidence."""
    from foragent.order import PlanError

    moved = []
    for key, base in bases.items():
        if base == current:
            continue
        try:
            same = isinstance(base, str) and not git.committed_diff(base, current)
        except GitError:  # an unknown or unreachable revision is no evidence for this base
            same = False
        if not same:
            moved.append(key)
    if moved:
        raise PlanError(text("reviewed_base_moved", _language(config), keys=", ".join(moved),
                             base=current[:12]))
