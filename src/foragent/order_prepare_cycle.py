"""Cycles, holds and promotion of `fa order prepare` (#1048).

A cycle is what the tests are written for: the issue's statements and the shared context.
Holds are counted from the review attempts of the current cycle; the second one becomes a
question to planning, and planning's clarified statements start a new cycle with the history
retained. Only reviewed bytes of the current cycle reach the plan file.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import uuid

from foragent.config import Config, resolved_reviewer
from foragent.git import GitRepo
from foragent.i18n import text
from foragent.order_prepare_prompt import _references

# The second quality hold within one cycle goes to planning as a question.
HOLD_LIMIT = 2
_CYCLE_FIELDS = ("title", "goal", "statements", "out_of_scope", "lane", "size", "files", "blocked_by",
                 "checklist", "interactive", "edit")


def _setup(config: Config, lane: str) -> dict:
    """The resolved writer and reviewer setup of the lane; another setup needs new evidence."""
    from foragent.run_implementation import lane_config

    resolved = lane_config(config, lane)
    return {"executor": resolved.executor, "model": resolved.executor_model, "tiers": resolved.tiers,
            "reviewer": resolved_reviewer(resolved), "reviewer_model": resolved.reviewer_model,
            "acceptance_dir": resolved.acceptance_dir, "base_branch": resolved.base_branch}


def identity(plan: dict, item: dict, config: Config) -> str:
    """What the tests are written and reviewed for: the issue's requirements, the shared context
    and the executor setup. A change starts a new cycle with the history retained."""
    shared = {"item": {name: item.get(name) for name in _CYCLE_FIELDS}, "title": plan.get("title"),
              "setup": _setup(config, item["lane"]),
              "summary": plan.get("summary"), "spec": plan.get("spec_markdown"),
              "new_lanes": plan.get("new_lanes"),
              "references": {name: hashlib.sha256(data).hexdigest()
                             for name, data in sorted(_references(plan, item).items())},
              "contracts": {other["key"]: other.get("acceptance_files", {}) for other in plan["issues"]
                            if other.get("key") != item["key"] and "statements" not in other}}
    return hashlib.sha256(json.dumps(shared, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def holds(entry: dict | None) -> list[str]:
    """The reasons of this cycle's quality holds, oldest first."""
    if not entry:
        return []
    return [attempt.get("reason") or "" for attempt in entry.get("attempts", [])
            if attempt.get("role") == "reviewer" and attempt.get("verdict") == "hold"
            and attempt.get("cycle") == entry.get("cycle")]


def revision_notes(entry: dict | None) -> str:
    """What the next writer attempt is told about this cycle's holds."""
    reasons = holds(entry)
    if not reasons:
        return ""
    return ("\n## Earlier review holds\nAn independent reviewer held the earlier tests for these "
            "reasons; revise the tests so they no longer apply:\n"
            + "".join(f"- {reason}\n" for reason in reasons))


def prepare_issue(plan: dict, item: dict, root: Path, config: Config, git: GitRepo, path: Path,
                   record: dict, base: str, plan_path: Path | None) -> None:
    """Write, review and promote one statement issue, reusing what this cycle already settled.

    An open question of the same cycle starts no agent: only planning's answer (a new cycle) does.
    A reviewed result is reused, and written candidates waiting for review are reviewed without a new
    writer, only while they were made on the current ``base``; after main moves the lane writes again
    on a new scratch copy, keeping the cycle's holds.
    """
    from foragent import order_prepare_review as reviewing
    from foragent.order_prepare import _write, _write_tests

    cycle = identity(plan, item, config)
    entry = record["issues"].get(item["key"])
    current = entry is not None and entry.get("cycle") == cycle
    fresh = current and entry.get("base_revision") == base
    settled = current and (entry["status"] == "question" or (fresh and entry["status"] == "reviewed"))
    if not settled:
        pending = fresh and bool(entry.get("written")) and (entry["status"] == "written" or (
            entry["status"] in {"waiting", "interrupted", "reviewing_tests"} and entry.get("phase") == "reviewing_tests"))
        if not pending:
            _write_tests(plan, item, root, config, git, path, record, base, cycle)
        if pending or record["issues"][item["key"]]["status"] == "written":
            reviewing.review(plan, item, root, config, git, path, record)
    entry = record["issues"][item["key"]]
    if entry["status"] == "reviewed" and plan_path is not None and not promote(plan_path, item, entry, root, config, base):
        # Planning changed the issue during preparation: these tests answer the old requirements.
        entry.update(status="interrupted", phase="reviewing_tests", reason=text("prepare_plan_changed", config.language))
        _write(path, record)


def promote(plan_path: Path, item: dict, entry: dict, root: Path, config: Config, base: str) -> bool:
    """Put the exact reviewed bytes and commands into this issue of the plan file; nothing else changes.

    Only a result reviewed on ``base`` for the plan file's current requirements is promoted: the whole
    cycle identity (statements, goal, scope, spec, contracts, references, setup) is read again from disk.
    """
    from foragent.order import read_plan, validate_plan

    before = plan_path.read_bytes()
    plan = read_plan(plan_path)
    if plan_path.read_bytes() != before:
        return False
    edit = str(item["key"]).startswith("#")
    targets = ([target for target in plan.get("edits", []) if f"#{target.get('number')}" == item["key"]] if edit
               else [target for target in plan.get("issues", []) if target.get("key") == item["key"]])
    if len(targets) != 1 or entry.get("base_revision") != base:
        return False
    target = targets[0]
    # An edit's title, lane and size come from the tracker, as when it was prepared.
    current = {**target, **{name: item.get(name) for name in ("key", "title", "lane", "size", "interactive", "edit")
                            if edit}}
    if "statements" not in target or identity(plan, current, config) != entry.get("cycle"):
        return False
    files, commands = dict(entry["written"]), list(entry["done_when"])
    if target.get("acceptance_files") == files and target.get("done_when") == commands:
        return True
    target["acceptance_files"], target["done_when"] = files, commands
    validate_plan(plan, root, config)
    return replace_unchanged(plan_path, before, (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _link_back(kept: Path, path: Path) -> None:
    """Put ``kept`` back at ``path`` unless a newer file already took the name; never overwrite."""
    try:
        os.link(kept, path)
    except FileExistsError:
        pass
    kept.unlink()


def replace_unchanged(path: Path, before: bytes, after: bytes) -> bool:
    """Replace ``path`` with ``after`` only while it still holds ``before``; no other writer's bytes are lost.

    Planning edits the plan with its own tools and shares no lock, so this is a compare-and-swap built
    from atomic steps. The plan is moved aside (so a later writer creates a new file, which wins), its
    moved bytes are compared, and ours are linked in only if the name is still free (``os.link`` never
    overwrites). A write into the moved file by a writer that still had it open is detected afterwards
    and put back (should a newer plan already have replaced ours, that write stays beside it as
    ``.<name>.prepare-*.old``). A conflict leaves planning's newest bytes at ``path`` and returns False.
    """
    stem = f".{path.name}.prepare-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    staged, held = path.with_name(stem + ".new"), path.with_name(stem + ".old")
    with open(staged, "xb") as handle:
        handle.write(after)
    try:
        try:
            os.rename(path, held)
        except FileNotFoundError:
            return False
        if held.read_bytes() != before:
            _link_back(held, path)
            return False
        try:
            os.link(staged, path)
        except FileExistsError:
            held.unlink()  # planning wrote a new plan meanwhile; the moved one is older
            return False
        if held.read_bytes() != before:
            # A writer that had the old plan open wrote into it: its bytes win over ours.
            mine = path.with_name(stem + ".mine")
            os.rename(path, mine)
            if mine.read_bytes() == after:
                mine.unlink()
                _link_back(held, path)
            else:
                _link_back(mine, path)  # a newer plan replaced ours; the old-fd write stays beside it
            return False
        held.unlink()
        return True
    finally:
        staged.unlink(missing_ok=True)
