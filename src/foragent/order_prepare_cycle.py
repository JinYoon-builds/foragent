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
            "acceptance_dir": resolved.acceptance_dir, "base_branch": resolved.base_branch,
            # Every lane setting the writer's resolution reads (agent, skills, paths, model, effort...),
            # whole rather than picked, and the global settings that resolve executors and skills.
            "lane": config.lanes.get(lane) if lane != "main" else None,
            "executor_list": resolved.executor_list, "fallback_model": resolved.executor_fallback_model,
            "skill_libraries": resolved.skill_libraries, "skill_roles": resolved.skill_roles,
            "language": resolved.language}


def contracts(plan: dict, item: dict) -> dict:
    """The other issues as this issue's writer and reviewer depend on them: every issue's and edit's
    requirements, and the locked tests of legacy issues. A statement issue's tests are this
    command's own output, bound by that issue's cycle; they are neither context nor shown."""
    others = [other for other in plan["issues"] if other.get("key") != item["key"]]
    others += [other for other in plan.get("edits", []) if f"#{other.get('number')}" != item["key"]]
    return {str(other.get("key", f"#{other.get('number')}")): {
        **{name: other.get(name) for name in _CYCLE_FIELDS + ("number",)},
        "acceptance_files": None if "statements" in other else other.get("acceptance_files", {}),
        "done_when": None if "statements" in other else other.get("done_when")} for other in others}


def identity(plan: dict, item: dict, config: Config, root: Path, files: dict | None = None) -> str:
    """What the tests are written and reviewed for: the issue's requirements, every other issue's
    requirements and legacy contracts, the shared spec and references, the risk guidance both agents
    read, and the executor setup. A change starts a new cycle with the history retained. Publication
    passes the ``files`` it will write, so the identity it checks is of exactly those bytes."""
    from foragent.order_prepare_prompt import _risk_notes
    from foragent.order_prepare_setup import context_digest, published_digest

    shared = {"item": {name: item.get(name) for name in _CYCLE_FIELDS}, "title": plan.get("title"),
              "risks": _risk_notes(root, config, item),
              "setup": _setup(config, item["lane"]), "context": context_digest(plan, item, config, root),
              "published": published_digest(plan, root, config, files),
              "summary": plan.get("summary"), "spec": plan.get("spec_markdown"),
              "new_lanes": plan.get("new_lanes"),
              "references": {name: hashlib.sha256(data).hexdigest()
                             for name, data in sorted(_references(plan, item).items())},
              "contracts": contracts(plan, item)}
    return hashlib.sha256(json.dumps(shared, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def requirement(plan: dict, item: dict) -> str:
    """What planning asked of this issue: its own fields, the order's summary and spec, its references.

    Holds and an open question belong to it. Another issue's edit, a setup change or a new main
    starts a new cycle (new evidence) but keeps them; planning's clarified statement starts afresh.
    """
    asked = {"key": item.get("key"), "item": {name: item.get(name) for name in _CYCLE_FIELDS},
             "title": plan.get("title"), "summary": plan.get("summary"), "spec": plan.get("spec_markdown"),
             "references": {name: hashlib.sha256(data).hexdigest()
                            for name, data in sorted(_references(plan, item).items())}}
    return hashlib.sha256(json.dumps(asked, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def behaviour(item: dict) -> str:
    """The behaviour planning states for this issue: only a change here answers the lane's question."""
    stated = {name: item.get(name) for name in ("goal", "statements", "out_of_scope", "checklist")}
    return hashlib.sha256(json.dumps(stated, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _scoped(entry: dict, attempt: dict) -> bool:
    """Whether an attempt answers the entry's current requirement (records before it: the same cycle)."""
    if entry.get("requirement") is not None and attempt.get("requirement") is not None:
        return attempt["requirement"] == entry["requirement"]
    return attempt.get("cycle") == entry.get("cycle")


def holds(entry: dict | None) -> list[str]:
    """The reasons of the quality holds of this issue's current requirement, oldest first.

    Counted from each review attempt's recorded outcome, never its raw verdict: a malformed reply,
    an execution failure or a changed scratch tree is no quality decision and no hold.
    """
    if not entry:
        return []
    return [attempt["reason"] for attempt in entry.get("attempts", [])
            if attempt.get("role") == "reviewer" and attempt.get("status") == "hold" and _scoped(entry, attempt)]


def same_requirement(entry: dict | None, cycle: str, asked: str) -> bool:
    """Whether ``entry`` was prepared for ``asked`` (records before requirements: for ``cycle``)."""
    if entry is None:
        return False
    if entry.get("requirement") is not None:
        return entry["requirement"] == asked
    return entry.get("cycle") == cycle


def revision_notes(entry: dict | None) -> str:
    """What the next writer attempt is told about this cycle's holds."""
    reasons = holds(entry)
    if not reasons:
        return ""
    return ("\n## Earlier review holds\nAn independent reviewer held the earlier tests for these "
            "reasons; revise the tests so they no longer apply:\n"
            + "".join(f"- {reason}\n" for reason in reasons))


def prepare_issue(plan: dict, item: dict, root: Path, config: Config, git: GitRepo, path: Path,
                   record: dict, base: str, plan_path: Path | None, store=None) -> None:
    """Write, review and promote one statement issue, reusing what this cycle already settled.

    An open question starts no agent until planning clarifies this issue's behaviour (its goal,
    statements, scope or checklist); a change to the order's summary, spec or setup leaves it open.
    A reviewed result is reused, and written candidates waiting for review are reviewed without a new
    writer, only while they were made for the current cycle on the current ``base``; otherwise the lane
    writes again on a new scratch copy, keeping the holds of the unchanged requirement.
    """
    from foragent import order_prepare_review as reviewing
    from foragent.order_prepare import _write, _write_tests

    cycle, asked = identity(plan, item, config, root), requirement(plan, item)
    entry = record["issues"].get(item["key"])
    current = entry is not None and entry.get("cycle") == cycle
    fresh = current and entry.get("base_revision") == base
    # A question waits for planning to clarify this issue's behaviour, whatever else changed.
    question = entry is not None and entry["status"] == "question" and (
        same_requirement(entry, cycle, asked) or entry.get("behaviour") == behaviour(item))
    if question and not same_requirement(entry, cycle, asked):
        entry.update(reason=text("prepare_question_open", config.language))
        _write(path, record)
    from foragent.order_prepare_setup import others_digest

    beside = entry is not None and entry.get("others") == others_digest(plan, record, item)
    settled = question or (fresh and entry["status"] == "reviewed" and beside)
    if not settled:
        # Another issue's tests changed beside reviewed ones: review the same tests again, write nothing.
        pending = fresh and bool(entry.get("written")) and (entry["status"] in {"written", "reviewed"} or (
            entry["status"] in {"waiting", "interrupted", "reviewing_tests"} and entry.get("phase") == "reviewing_tests"))
        if not pending:
            _write_tests(plan, item, root, config, git, path, record, base, cycle, asked, store)
        if pending or record["issues"][item["key"]]["status"] == "written":
            reviewing.review(plan, item, root, config, git, path, record, store)
    entry = record["issues"][item["key"]]
    if entry["status"] == "reviewed" and plan_path is not None and not settled:
        # The agents may have run for long: main is read again right before the result is promoted.
        from foragent.order_scratch import base_revision

        base = base_revision(git, config.base_branch)
        if entry.get("base_revision") != base:
            entry.update(status="interrupted", phase="reviewing_tests",
                         reason=text("prepare_main_moved", config.language))
            _write(path, record)
            return
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
    if "statements" not in target or identity(plan, current, config, root) != entry.get("cycle"):
        return False
    files, commands = dict(entry["written"]), list(entry["done_when"])
    if target.get("acceptance_files") == files and target.get("done_when") == commands:
        return True
    target["acceptance_files"], target["done_when"] = files, commands
    validate_plan(plan, root, config)
    return replace_unchanged(plan_path, before, (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _release(name: Path, expected: bytes) -> bytes | None:
    """Remove ``name`` and return its bytes if they are not ``expected``; None when nothing else was there.

    The bytes are read through a descriptor opened before the name is removed, so the read comes after
    the removal: a writer that still had the file open and wrote before that read is seen, never lost.
    """
    descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.unlink(name)
        chunks = []
        while chunk := os.read(descriptor, 1 << 16):
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    return None if data == expected else data


def _park(path: Path, token: str, data: bytes) -> Path:
    """Keep planning bytes that could not go back to ``path`` in a visible file beside it."""
    kept = path.with_name(f"{path.name}.conflict-{token}")
    with open(kept, "xb") as handle:
        handle.write(data)
    return kept


def replace_unchanged(path: Path, before: bytes, after: bytes) -> bool:
    """Replace ``path`` with ``after`` only while it still holds ``before``; no other writer's bytes are lost.

    Planning edits the plan with its own tools and shares no lock, so this is a compare-and-swap built
    from atomic steps, and no file that may hold planning's bytes is deleted after a separate read:
    - the plan is moved aside, so a writer that saves now creates a new file, which wins;
    - moved bytes that are not ``before`` go back under the plan's name (a newer plan already there
      keeps the name; the moved file is renamed to ``<name>.conflict-*``, still the same file);
    - ours are linked in only while the name is free (``os.link`` never overwrites);
    - the moved plan is released with ``_release``; a write that reached it during promotion is put
      back over ours, or kept as ``<name>.conflict-*`` when the plan changed yet again.
    Every conflict returns False.
    """
    token = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    staged, held = path.with_name(f".{path.name}.prepare-{token}.new"), path.with_name(f".{path.name}.prepare-{token}.old")
    with open(staged, "xb") as handle:
        handle.write(after)
    try:
        try:
            os.rename(path, held)
        except FileNotFoundError:
            return False
        if held.read_bytes() != before:
            try:
                os.link(held, path)
            except FileExistsError:
                os.rename(held, path.with_name(f"{path.name}.conflict-{token}"))
                return False
            os.unlink(held)  # only a second name: the same file stays at ``path``
            return False
        try:
            os.link(staged, path)
        except FileExistsError:
            if (late := _release(held, before)) is not None:
                _park(path, token, late)
            return False
        if (late := _release(held, before)) is None:
            return True
        # A writer that had the old plan open wrote into it during promotion: its bytes win over ours.
        if not replace_unchanged(path, after, late):
            _park(path, token, late)
        return False
    finally:
        # Our own file: once linked, ``path`` is a second name for it, so this removes no one's bytes.
        staged.unlink(missing_ok=True)
