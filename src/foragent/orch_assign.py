"""`fa orch assign` and `fa orch goal` (D80): per-issue executor/model choices and the goal.

Both live in the plan file (``<state>/plan.json``) and follow its rules (``orch_plan``): only
these ``fa orch`` commands write them, each item names who wrote it, when and why, every
change appends one event to ``orch-events.jsonl``, and the orchestrator never changes or
clears an item a person wrote. A refused edit changes nothing.

An assignment is a choice above ordinary executor selection, not above the safety lines:
the tick still applies blockers, pauses, widths, budgets and hard limits, and the run keeps
a cross review. It lapses at its ``until``; then ordinary selection applies again.

The goal is Jin's: the orchestrator reads it in every planning session (``fa orch plan``)
and plans within it, but cannot replace or clear a goal a person wrote.
"""

from __future__ import annotations

from datetime import datetime
import sys
from pathlib import Path
from typing import NoReturn

from foragent import orch_plan
from foragent.orch_plan import PlanRefused

ACTIONS = {"assign", "goal"}


def runnable(name: str) -> bool:
    """Whether Foragent has an adapter that runs ``name``; being configured is not enough."""
    from foragent.executors import executor_for

    try:
        executor_for(name)
    except ValueError:
        return False
    return True


def known_executors(config) -> set[str]:
    """The executors an assignment may name: configured (the built-in pair or an ``executors``
    entry) and runnable by an adapter."""
    from foragent.config import _EXECUTORS

    return {name for name in set(_EXECUTORS) | {entry["name"] for entry in config.executors} if runnable(name)}


def assignment(store, number: int, now: datetime | None = None) -> dict | None:
    """The live assignment of issue ``number``; None when there is none or it has lapsed."""
    return orch_plan.load(store, now)["assignments"].get(str(number))


def goal(store, now: datetime | None = None) -> dict | None:
    return orch_plan.load(store, now)["goal"]


def assign(store, number: int, executor: str, model: str, until: datetime | None, why: str,
           by: str, known: set[str]) -> dict:
    """Set issue ``number``'s executor and model; all or nothing."""
    if executor not in known:
        raise PlanRefused(f"executor {executor!r} is not available; use one of: {', '.join(sorted(known))}")
    if not model.strip():
        raise PlanRefused("--model must name a model")

    def change(plan, now):
        current = plan["assignments"].get(str(number))
        orch_plan._check_author(current and current["by"], by, f"issue #{number}'s assignment")
        if until is not None and until <= now:
            raise PlanRefused(f"--until {until.isoformat()} is in the past")
        item = {"executor": executor, "model": model, "until": until and until.isoformat(),
                "why": why, "by": by, "at": now.isoformat()}
        plan["assignments"][str(number)] = item
        lapse = f" until {item['until']}" if item["until"] else ""
        summary = f"#{number} assigned to {executor} model {model}{lapse} by {by}: {why}"
        return [("plan-assign", None, summary, {"issue_number": number, "executor": executor,
                                                "model": model, "until": item["until"], "why": why})]

    return orch_plan._edit(store, by, change)["assignments"][str(number)]


def clear_assignment(store, number: int, why: str, by: str) -> bool:
    """Remove issue ``number``'s assignment; False when it had none."""
    removed = []

    def change(plan, _now):
        current = plan["assignments"].get(str(number))
        if current is None:
            return []
        orch_plan._check_author(current["by"], by, f"issue #{number}'s assignment")
        del plan["assignments"][str(number)]
        removed.append(number)
        return [("plan-assign-clear", None, f"#{number} assignment cleared by {by}: {why}",
                 {"issue_number": number, "why": why})]

    orch_plan._edit(store, by, change)
    return bool(removed)


def set_goal(store, text: str, deadline: datetime, why: str, by: str) -> dict:
    """Set the goal and its deadline; all or nothing."""
    if not text.strip():
        raise PlanRefused("the goal needs a text")

    def change(plan, now):
        orch_plan._check_author(plan["goal"] and plan["goal"]["by"], by, "the goal")
        if deadline <= now:
            raise PlanRefused(f"--deadline {deadline.isoformat()} is in the past")
        plan["goal"] = {"text": text, "deadline": deadline.isoformat(), "why": why, "by": by,
                        "at": now.isoformat()}
        summary = f"goal {text!r} by {deadline.isoformat()} set by {by}: {why}"
        return [("plan-goal", None, summary, {"text": text, "deadline": deadline.isoformat(), "why": why})]

    return orch_plan._edit(store, by, change)["goal"]


def clear_goal(store, why: str, by: str) -> bool:
    """Remove the goal; False when there was none."""
    removed = []

    def change(plan, _now):
        if plan["goal"] is None:
            return []
        orch_plan._check_author(plan["goal"]["by"], by, "the goal")
        plan["goal"] = None
        removed.append(True)
        return [("plan-goal-clear", None, f"goal cleared by {by}: {why}", {"why": why})]

    orch_plan._edit(store, by, change)
    return bool(removed)


def add_parsers(orch_actions) -> None:
    assign_parser = orch_actions.add_parser(
        "assign", help="Choose the executor and model for one issue, or --clear the choice")
    assign_parser.add_argument("number", type=int)
    assign_parser.add_argument("--executor")
    assign_parser.add_argument("--model")
    assign_parser.add_argument("--until", help="ISO time when the choice lapses; without a timezone it is local time")
    assign_parser.add_argument("--clear", action="store_true")
    goal_parser = orch_actions.add_parser("goal", help="Set the goal with a deadline, or --clear it")
    goal_parser.add_argument("text", nargs="?")
    goal_parser.add_argument("--deadline", help="ISO time; without a timezone it is local time")
    goal_parser.add_argument("--clear", action="store_true")
    for command in (assign_parser, goal_parser):
        command.add_argument("--why", required=True, help="Why this changes; kept in the history")
        command.add_argument("--by", choices=orch_plan.AUTHORS, default="human",
                             help="Who writes this (default: human); the orchestrator passes --by orchestrator")
        command.add_argument("--repo-root", type=Path, default=Path.cwd())


def _fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def _time(action: str, flag: str, value: str) -> datetime:
    try:
        return orch_plan.parse_time(value)
    except ValueError:
        _fail(f"fa orch {action}: {flag} '{value}' is not an ISO time. Nothing changed.")


def run(args, store, config) -> None:
    """One assignment or goal edit; a refusal names its reason on stderr and exits 1."""
    try:
        if args.action == "assign":
            _assign(args, store, config)
        else:
            _goal(args, store)
    except PlanRefused as exc:
        _fail(f"fa orch {args.action}: {exc}. Nothing changed.")


def _assign(args, store, config) -> None:
    number = args.number
    if args.clear:
        if args.executor or args.model or args.until:
            _fail("fa orch assign: give --executor/--model or --clear, not both. Nothing changed.")
        removed = clear_assignment(store, number, args.why, args.by)
        print(f"#{number}: assignment cleared; ordinary executor selection applies."
              if removed else f"#{number} has no assignment.")
        return
    if not args.executor or not args.model:
        _fail("fa orch assign: give both --executor and --model, or --clear. Nothing changed.")
    until = _time("assign", "--until", args.until) if args.until else None
    item = assign(store, number, args.executor, args.model, until, args.why, args.by, known_executors(config))
    lapse = f" until {item['until']}" if item["until"] else ""
    print(f"#{number}: {item['executor']} model {item['model']}{lapse} ({item['by']}): {item['why']}")


def _goal(args, store) -> None:
    if args.clear:
        if args.text or args.deadline:
            _fail("fa orch goal: give a goal with --deadline or --clear, not both. Nothing changed.")
        removed = clear_goal(store, args.why, args.by)
        print("Goal cleared." if removed else "There is no goal.")
        return
    if not args.text or not args.deadline:
        _fail("fa orch goal: give the goal text and --deadline, or --clear. Nothing changed.")
    item = set_goal(store, args.text, _time("goal", "--deadline", args.deadline), args.why, args.by)
    print(f"Goal: {item['text']} by {item['deadline']} ({item['by']}): {item['why']}")


def plan_lines(plan: dict) -> list[str]:
    """The assignment and goal lines of ``fa orch plan``."""
    lines = []
    if plan["goal"]:
        item = plan["goal"]
        lines.append(f"Goal: {item['text']} by {item['deadline']} ({item['by']}, {item['at']}): {item['why']}")
    for number, item in sorted(plan["assignments"].items(), key=lambda pair: int(pair[0])):
        lapse = f" until {item['until']}" if item.get("until") else ""
        lines.append(f"#{number}: {item['executor']} model {item['model']}{lapse} "
                     f"({item['by']}, {item['at']}): {item['why']}")
    return lines
