"""`fa orch scorecard`: today's speed and quality against the days before, from local records only.

Today is the 24 hours back from now; each earlier day is the next 24-hour bucket before
it. A bucket holds a time when ``older end < time <= newer end`` (UTC). The command only
reads the state directory and writes nothing.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
from statistics import median
from typing import Any

from foragent.config import Config
from foragent.i18n import text
from foragent.orch_facts_resources import _jsonl, _seconds, _time
from foragent.state import StateStore

DAY = timedelta(hours=24)
# Finished outcomes that are not failures; pr-open waits for a person unless it later merged.
NOT_FAILURES = {"merged", "pr-open", "spec-question", "limit-wait", "stalled-restart", "killed"}
INTEGERS = ("runs", "review_returns", "reverts_after_merge", "spec_questions")
# Metric -> +1 when a higher value is worse, -1 when a lower value is worse; runs is never worse.
DIRECTION = {"merge_rate": -1, "median_minutes_to_merge": 1, "github_calls_per_hour": 1,
             "review_returns": 1, "reverts_after_merge": 1, "spec_questions": 1, "load": 1}


def _bucket(moment: datetime | None, now: datetime, days: int) -> int | None:
    """0 for today, k for the k-th day before; None outside every bucket or in the future."""
    if moment is None or moment > now:
        return None
    index = (now - moment) // DAY  # Newer end inclusive: exactly 24 h back is the day before.
    return index if index < days else None


def _median(values: list[float]) -> float | None:
    return median(values) if values else None


def _half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def _runs(store: StateStore, now: datetime, days: int) -> list[tuple[int, dict[str, Any]]]:
    """(bucket, facts) for each finished execution, placed by its finish time."""
    finished_at, merged_at = store.finish_times(), store.merge_times()
    rows = []
    for run in store.read_history():
        key = (run.number, run.started_at)
        started = _time(run.started_at)
        finished = _time(finished_at.get(key)) or _time(run.last_progress_at) or started
        bucket = _bucket(finished, now, days)
        if bucket is None or run.status != "finished":
            continue
        merge = _time(merged_at.get(key))
        # A merge time older than this execution's start belongs to an earlier execution.
        merged = run.outcome == "merged" or (
            run.outcome == "pr-open" and merge is not None and started is not None and merge >= started)
        minutes = (finished - started).total_seconds() / 60 if merged and started and finished else None
        rows.append((bucket, {"number": run.number, "outcome": run.outcome or "", "detail": run.detail,
                              "merged": merged, "minutes": minutes,
                              "failed": not merged and run.outcome not in NOT_FAILURES}))
    return rows


def _events(store: StateStore, now: datetime, days: int) -> list[tuple[int, dict[str, Any]]]:
    """(bucket, event) for journal events by their ``at``; a phase interval journaled again counts once."""
    unique: dict[Any, dict[str, Any]] = {}
    for index, event in enumerate(_jsonl(store.root / "orch-events.jsonl")):
        opened = event.get("phase_started_at")
        if event.get("kind") == "phase" and opened:
            unique[("phase", event.get("issue"), opened)] = event  # The last replay wins.
        else:
            unique[index] = event
    return [(bucket, event) for event in unique.values()
            if (bucket := _bucket(_time(event.get("at")), now, days)) is not None]


def _day(runs: list[dict[str, Any]], events: list[dict[str, Any]], calls: int) -> dict[str, Any]:
    """One window's per-day metrics, before phases and load."""
    merged = [run for run in runs if run["merged"]]
    return {
        "runs": len(runs),
        "merge_rate": len(merged) / len(runs) if runs else None,
        "median_minutes_to_merge": _median([run["minutes"] for run in merged if run["minutes"] is not None]),
        "github_calls_per_hour": calls / 24,
        "review_returns": sum(1 for event in events if event.get("kind") == "review"
                              and event.get("verdict") != "PASS"),
        "reverts_after_merge": sum(1 for event in events if event.get("kind") == "revert"),
        "spec_questions": sum(1 for run in runs if run["outcome"] == "spec-question"),
    }


def _phases(events: list[dict[str, Any]]) -> tuple[dict[str, float], float | None]:
    """Median minutes per phase and median known load over the given phase events."""
    minutes: dict[str, list[float]] = {}
    loads = []
    for event in events:
        seconds = _seconds(event.get("seconds"))
        if event.get("kind") != "phase" or seconds is None or not isinstance(event.get("phase"), str):
            continue
        minutes.setdefault(event["phase"], []).append(seconds / 60)
        if type(event.get("load")) in (int, float):
            loads.append(float(event["load"]))
    return {phase: median(values) for phase, values in minutes.items()}, _median(loads)


def _failures(runs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[int]]:
    """Today's failures grouped by their normalised reason, and the issues failing without one."""
    groups: dict[str, list[int]] = {}
    unexplained: set[int] = set()
    for run in runs:
        if not run["failed"]:
            continue
        reason = " ".join(run["detail"].split())
        if not reason or reason == f"Run finished with outcome {run['outcome']}.":
            unexplained.add(run["number"])
        else:
            groups.setdefault(reason, []).append(run["number"])
    repeated = [{"reason": reason, "count": len(numbers), "issues": sorted(set(numbers))}
                for reason, numbers in groups.items() if len(numbers) >= 2]
    repeated.sort(key=lambda item: (-item["count"], item["reason"]))
    return repeated, sorted(unexplained)


def _worse(today: dict[str, Any], previous: dict[str, Any]) -> list[str]:
    pairs = [(name, today[name], previous[name], direction) for name, direction in DIRECTION.items()]
    pairs += [(f"phase_minutes.{phase}", minutes, previous["phase_minutes"][phase], 1)
              for phase, minutes in today["phase_minutes"].items() if phase in previous["phase_minutes"]]
    return [name for name, now, before, direction in pairs
            if now is not None and before is not None and (now - before) * direction > 0]


def collect(store: StateStore, days: int = 7) -> dict[str, Any]:
    now = store._now()
    runs, events = _runs(store, now, days), _events(store, now, days)
    calls = [0] * days
    for row in store.github_calls():
        # An hour counts in the window that holds its start.
        if (bucket := _bucket(_time(row["hour"]), now, days)) is not None:
            calls[bucket] += row["calls"]
    daily = [_day([run for index, run in runs if index == bucket],
                  [event for index, event in events if index == bucket], calls[bucket])
             for bucket in range(days)]
    today = daily[0]
    today["phase_minutes"], today["load"] = _phases([event for index, event in events if index == 0])
    previous: dict[str, Any] = {}
    for name in today.keys() - {"phase_minutes", "load"}:
        value = _median([day[name] for day in daily[1:] if day[name] is not None])
        previous[name] = _half_up(value) if name in INTEGERS and value is not None else value
    previous["phase_minutes"], previous["load"] = _phases([event for index, event in events if index > 0])
    order = [*_day([], [], 0), "phase_minutes", "load"]
    today, previous = {name: today[name] for name in order}, {name: previous[name] for name in order}
    repeated, unexplained = _failures([run for index, run in runs if index == 0])
    return {"now": now.isoformat(), "days": days, "window": {"today": today, "previous": previous},
            "worse": _worse(today, previous), "repeated_failures": repeated,
            "unexplained_failures": unexplained}


def _number(value: Any, suffix: str = "") -> str:
    if value is None:
        return "-"
    return f"{value}{suffix}" if type(value) is int else f"{value:.1f}{suffix}"


def _shown(name: str, value: Any) -> str:
    """A metric for people: the merge rate as a percentage, the rest as numbers."""
    return _number(None if value is None else value * 100, "%") if name == "merge_rate" else _number(value)


def _window_line(key: str, metrics: dict[str, Any], language: str, days: int) -> str:
    phases = ", ".join(f"{phase} {_number(minutes)}" for phase, minutes in sorted(metrics["phase_minutes"].items()))
    return text(key, language, days=days - 1, runs=_number(metrics["runs"]),
                merge_rate=_shown("merge_rate", metrics["merge_rate"]),
                merge_minutes=_number(metrics["median_minutes_to_merge"]),
                calls=_number(metrics["github_calls_per_hour"]),
                returns=_number(metrics["review_returns"]), reverts=_number(metrics["reverts_after_merge"]),
                questions=_number(metrics["spec_questions"]), load=_number(metrics["load"]),
                phases=phases or "-")


def human(card: dict[str, Any], language: str) -> str:
    today, previous = card["window"]["today"], card["window"]["previous"]
    lines = [_window_line("scorecard_today", today, language, card["days"]),
             _window_line("scorecard_previous", previous, language, card["days"])]
    for name in card["worse"]:
        if name.startswith("phase_minutes."):
            phase = name.split(".", 1)[1]
            before, now = previous["phase_minutes"][phase], today["phase_minutes"][phase]
        else:
            before, now = previous[name], today[name]
        lines.append(text("scorecard_worse", language, metric=name, before=_shown(name, before),
                          now=_shown(name, now)))
    if not card["worse"]:
        lines.append(text("scorecard_nothing_worse", language))
    if card["repeated_failures"]:
        items = "; ".join(f"{item['reason']} ×{item['count']} ("
                          + ", ".join(f"#{n}" for n in item["issues"]) + ")"
                          for item in card["repeated_failures"])
        lines.append(text("scorecard_repeated", language, items=items))
    if card["unexplained_failures"]:
        lines.append(text("scorecard_unexplained", language,
                          issues=", ".join(f"#{n}" for n in card["unexplained_failures"])))
    return "\n".join(lines)


def _days(value: str) -> int:
    try:
        days = int(value)
    except ValueError:
        days = 0
    if days < 2:
        raise argparse.ArgumentTypeError(f"--days must be an integer of at least 2, not {value!r}")
    return days


def add_parser(orch_actions) -> None:
    """`fa orch scorecard [--json] [--days N]` under `fa orch`."""
    scorecard = orch_actions.add_parser("scorecard", help="Compare the last 24 hours with the days before")
    scorecard.add_argument("--repo-root", type=Path, default=Path.cwd())
    scorecard.add_argument("--json", action="store_true")
    scorecard.add_argument("--days", type=_days, default=7,
                           help="Today plus N-1 earlier days to compare against (default: 7, at least 2)")


def command(args, store: StateStore, config: Config) -> None:
    card = collect(store, args.days)
    print(json.dumps(card, ensure_ascii=False) if args.json else human(card, config.language))
