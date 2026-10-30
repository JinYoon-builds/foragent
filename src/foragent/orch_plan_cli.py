"""`fa orch pause|resume|plan` and `fa orch queue LANE`: the command side of the lane plan.

``foragent.commands.orch`` registers these parsers and calls ``run``; the plan rules live in
``foragent.orch_plan``. Errors go to stderr with exit code 1, so a refusal is readable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import NoReturn

from foragent import orch_assign, orch_plan, orch_queue
from foragent.i18n import text
from foragent.labels import lane_order

ACTIONS = {"pause", "resume", "plan", *orch_assign.ACTIONS}


def lane_queue(args, store, config) -> None:
    """`fa orch queue LANE [N...] [--clear] [--json] [--by]`: one lane's queue, kept in the plan."""
    if args.lane is None:
        raise SystemExit(text("orch_queue_lane_required", config.language))
    if args.clear and args.numbers:
        raise SystemExit("fa orch queue: give issue numbers or --clear, not both")
    try:
        order = orch_queue.set_order(store, args.lane, [], args.by) if args.clear else (
            orch_queue.set_order(store, args.lane, args.numbers, args.by) if args.numbers
            else orch_queue.get(store, args.lane))
    except orch_plan.PlanRefused as exc:
        refuse("queue", exc, config.language)
    if args.json:
        print(json.dumps({"lane": args.lane, "order": order}))
    elif order:
        print(f"lane:{args.lane} queue: " + " ".join(f"#{n}" for n in order))
    else:
        print(f"lane:{args.lane} queue is empty; issues start by number.")


def refuse(action: str, exc: Exception, language: str) -> NoReturn:
    """A refused plan edit: name the reason (``human`` for a person's item) and change nothing."""
    _fail(text("orch_plan_refused", language, action=action, reason=exc))


def add_parsers(orch_actions, queue) -> None:
    pause = orch_actions.add_parser("pause", help="Hold a lane in the plan until a time, with a reason")
    pause.add_argument("lane")
    pause.add_argument("--until", required=True, help="ISO time; without a timezone it is local time")
    pause.add_argument("--why", required=True)
    resume = orch_actions.add_parser("resume", help="Remove a lane's plan pause")
    resume.add_argument("lane")
    plan = orch_actions.add_parser("plan", help="Show the plan, or set lane widths, file overlap limits and expiry")
    plan.add_argument("--width", action="append", default=[], metavar="LANE=N",
                      help="Cap a lane's running runs (N >= 1), or LANE=off to clear; may repeat")
    plan.add_argument("--overlap", action="append", default=[], metavar="PATH=N",
                      help="Let N running runs share one repository file (N >= 1), replacing "
                           "overlap_per_file for it, or PATH=off to clear; may repeat; needs --why")
    plan.add_argument("--why", help="With --overlap: the recorded evidence behind the limit")
    plan.add_argument("--expires", help="ISO time when the whole plan lapses; with --overlap, when those "
                                        "file limits lapse")
    plan.add_argument("--json", action="store_true")
    for command in (pause, resume, plan, queue):
        command.add_argument("--by", choices=orch_plan.AUTHORS, default="human",
                             help="Who writes this (default: human); the orchestrator passes --by orchestrator")
        if command is not queue:
            command.add_argument("--repo-root", type=Path, default=Path.cwd())
    orch_assign.add_parsers(orch_actions)


def _fail(message: str) -> NoReturn:
    """Print the reason where a person sees it, then exit non-zero."""
    print(message, file=sys.stderr)
    raise SystemExit(1)


def _plan_time(action: str, value: str, language: str):
    try:
        return orch_plan.parse_time(value)
    except ValueError:
        _fail(text("orch_plan_bad_time", language, action=action, value=value))


def _check_lanes(action: str, lanes, config) -> None:
    known = sorted({"main", *config.lanes})
    unknown = [lane for lane in lanes if lane not in known]
    if unknown:
        _fail(text("orch_plan_unknown_lanes", config.language, action=action,
                                  lanes=", ".join(unknown), known=", ".join(known)))


def _widths(values: list[str], language: str) -> dict:
    widths = {}
    for value in values:
        lane, sep, raw = value.partition("=")
        try:
            width = None if raw == "off" else int(raw)
        except ValueError:
            width = 0
        if not sep or not lane or (width is not None and width < 1):
            _fail(text("orch_plan_bad_width", language, value=value))
        widths[lane] = width
    return widths


def _overlaps(values: list[str], language: str) -> dict:
    limits = {}
    for value in values:
        path, sep, raw = value.rpartition("=")
        try:
            limit = None if raw == "off" else int(raw)
        except ValueError:
            limit = 0
        if not sep or not path or (limit is not None and limit < 1):
            _fail(text("orch_plan_bad_overlap", language, value=value))
        limits[path] = limit
    return limits


def run(args, store, config) -> None:
    """Plan pauses, widths, expiry and their display; queues go through ``fa orch queue``."""
    plan_module, language = orch_plan, config.language
    if args.action in orch_assign.ACTIONS:
        orch_assign.run(args, store, config)
        return
    try:
        if args.action == "pause":
            _check_lanes("pause", [args.lane], config)
            until = _plan_time("pause", args.until, language)
            item = plan_module.pause(store, args.lane, until, args.why, args.by)["paused"][args.lane]
            print(text("orch_plan_paused", language, lane=args.lane, **item))
            return
        if args.action == "resume":
            removed = plan_module.resume(store, args.lane, args.by)
            print(text("orch_plan_resumed" if removed else "orch_plan_not_paused", language, lane=args.lane))
            return
        widths = _widths(args.width, language)
        overlaps = _overlaps(args.overlap, language)
        _check_lanes("plan", list(widths), config)
        expires = _plan_time("plan", args.expires, language) if args.expires else None
        if args.why is not None and not overlaps:
            _fail(text("orch_plan_why_without_overlap", language))
        if overlaps and widths:
            _fail(text("orch_plan_overlap_and_width", language))
        if overlaps:
            # With --overlap, --expires ends those file limits only, never the rest of the plan.
            from foragent.scheduler_lanes import _filesystem_case_sensitive

            current = plan_module.set_files(store, overlaps, args.why or "", expires, args.by,
                                            case_sensitive=_filesystem_case_sensitive(args.repo_root),
                                            root=args.repo_root)
        elif widths or expires:
            current = plan_module.set_plan(store, widths, expires, args.by)
        else:
            current = plan_module.load(store)
    except plan_module.PlanRefused as exc:
        refuse(args.action, exc, language)
    if args.json:
        print(json.dumps(plan_module.view(current), ensure_ascii=False))
        return
    for line in _plan_lines(current, language):
        print(line)


def _plan_lines(plan: dict, language: str) -> list[str]:
    authors = plan["authors"]
    lines = []
    for lane in sorted({*plan["queues"], *plan["paused"], *plan["width"]}, key=lane_order):
        parts = []
        if lane in plan["queues"]:
            parts.append(text("orch_plan_queue", language, by=authors["queues"][lane],
                              order=" ".join(f"#{n}" for n in plan["queues"][lane])))
        if lane in plan["paused"]:
            parts.append(text("orch_plan_pause", language, **plan["paused"][lane]))
        if lane in plan["width"]:
            parts.append(text("orch_plan_width", language, width=plan["width"][lane], by=authors["width"][lane]))
        lines.append(text("orch_plan_line", language, lane=lane, parts="; ".join(parts)))
    for path, item in sorted(plan["files"].items()):
        lines.append(text("orch_plan_file", language, path=path, limit=item["limit"], by=item["by"],
                          why=item["why"], until=item["expires_at"] or text("orch_plan_file_no_expiry", language)))
    controls = orch_assign.plan_lines(plan)
    if not lines and not plan["expires_at"]:
        return controls or [text("orch_plan_none", language)]
    if plan["expires_at"]:
        lines.append(text("orch_plan_expires", language, at=plan["expires_at"], by=authors["expires_at"]))
    else:
        lines.append(text("orch_plan_no_expiry", language))
    return controls + lines
