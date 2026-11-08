"""Issue tokens per scorecard window: how much of the recorded issue spend went to merged work.

Evidence is each executor's own structured output, which ``run_logged`` forwards into every
session log an issue owns (``issue_logs``): the issue log ``<state>/logs/issue-N.log`` that
every attempt and single reviewer appends to, each panel reviewer's
``logs/issue-N-review-<round>-<k>.log``, the lane test draft's ``logs/draft-N.log`` and each
review scorer's ``runs/issue-N/score-<round>-<k>.log``. Cut copies of log tails
(``*-executor-<offset>.log``) repeat those sessions and are never read.

- Claude: a session opens with its ``system``/``init`` event and closes with its ``result``
  (a review's single JSON document has no ``type`` and closes a session of its own). The
  result's ``usage`` holds the whole session, so the per-message ``assistant`` usage blocks are
  never added again. Input, cache creation, cache read and output tokens are disjoint
  categories and are summed once each. An opened session that never reached a result
  (interrupted, timed out) has no usage.
- Codex: a session opens with ``thread.started``; each ``turn.completed`` ``usage`` counts input
  (cached input is part of it, so it is not added again) and output tokens. A session whose
  last started turn never completed, or that completed no turn, has incomplete usage: the
  tokens it did report still count, and the session is still disclosed.

Dollar fields such as ``total_cost_usd`` are a different unit and never count as tokens.
Role sessions (architect, dream, roles) write other logs and are not issue work.

An issue sits in the window of its latest finished execution, and all its retained attempts
count there once. Its merge state is read from the run records at report time: merged when an
execution merged; unknown while its last execution left a PR waiting for a merge
(``pr-open``, ``merge-queued``); otherwise not merged. The share is merged tokens over all
recorded issue tokens and is None (unknown) when no issue token was recorded. Issues whose
logs hold no session, or a session without complete usage, are listed, never counted as zero spend.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import re
from typing import Any

from foragent.orch_facts_resources import _time
from foragent.state import StateStore

DAY = timedelta(hours=24)
# A PR is open and waiting for a merge that may already have happened elsewhere.
MERGE_PENDING = {"pr-open", "merge-queued"}
CLAUDE_CATEGORIES = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
CODEX_CATEGORIES = ("input_tokens", "output_tokens")


def _count(usage: Any, categories: tuple[str, ...]) -> int | None:
    """The token total of one usage block, or None when it reports no usable count."""
    if not isinstance(usage, dict):
        return None
    values = [usage.get(name) for name in categories]
    if not any(type(value) is int for value in values):
        return None
    return sum(value for value in values if type(value) is int and value > 0)


def issue_logs(store: StateStore, number: int) -> list[Path]:
    """Every session log issue ``number`` owns; see the module text for the writers."""
    logs, runs = store.root / "logs", store.root / "runs" / f"issue-{number}"
    reviewers = re.compile(rf"issue-{number}-review-\d+-\d+\.log")
    scorers = re.compile(r"score-\d+-\d+\.log")
    found = [store.log_path(number), logs / f"draft-{number}.log"]
    found += sorted(path for path in _listing(logs) if reviewers.fullmatch(path.name))
    found += sorted(path for path in _listing(runs) if scorers.fullmatch(path.name))
    return [path for path in found if path.is_file()]


def _listing(directory: Path) -> list[Path]:
    try:
        return list(directory.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []


def sessions(log: Path) -> list[tuple[str, int | None, bool]]:
    """(executor, tokens or None, usage complete) for every executor session in one log.

    A session counts from its opening event, so one that never reported usage still appears.
    """
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except (FileNotFoundError, NotADirectoryError):
        return []
    found: list[tuple[str, int | None, bool]] = []
    claude = False  # A Claude session opened and has not reached its result yet.
    codex: dict[str, Any] | None = None  # The open Codex session.

    def close_claude():
        nonlocal claude
        if claude:
            found.append(("claude", None, False))
        claude = False

    def close_codex():
        nonlocal codex
        if codex is not None:
            complete = codex["turns"] > 0 and not codex["open_turn"] and not codex["lost"]
            found.append(("codex", codex["tokens"], complete))
        codex = None

    for line in lines:
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            close_claude()
            claude = True
        elif kind == "result" or (kind is None and isinstance(event.get("usage"), dict)):
            tokens = _count(event.get("usage"), CLAUDE_CATEGORIES)
            found.append(("claude", tokens, tokens is not None))
            claude = False
        elif kind == "thread.started":
            close_codex()
            codex = {"tokens": None, "turns": 0, "open_turn": False, "lost": False}
        elif codex is not None and kind == "turn.started":
            codex["open_turn"] = True
        elif codex is not None and kind in ("turn.completed", "turn.failed"):
            tokens = _count(event.get("usage"), CODEX_CATEGORIES) if kind == "turn.completed" else None
            codex["open_turn"] = False
            if tokens is None:
                codex["lost"] = True  # A turn ended without usage: the session stays incomplete.
                continue
            codex["tokens"] = (codex["tokens"] or 0) + tokens
            codex["turns"] += 1
    close_claude()
    close_codex()
    return found


def _placed(store: StateStore, now: datetime, days: int) -> dict[int, tuple[int, str]]:
    """Issue -> (bucket of its latest finished execution, merge state) for this card's days."""
    finished_at = store.finish_times()
    owner = store.owner_merge_facts()
    latest: dict[int, tuple[datetime, str]] = {}
    merged: set[int] = set()
    for run in store.read_history():
        if run.status != "finished":
            continue
        if store.run_merged(run, owner_facts=owner):
            merged.add(run.number)
        moment = (_time(finished_at.get((run.number, run.started_at))) or _time(run.last_progress_at)
                  or _time(run.started_at))
        if moment is not None and (run.number not in latest or moment >= latest[run.number][0]):
            latest[run.number] = (moment, run.outcome or "")
    placed = {}
    for number, (moment, outcome) in latest.items():
        if moment > now or (bucket := (now - moment) // DAY) >= days:
            continue
        state = "merged" if number in merged else "unknown" if outcome in MERGE_PENDING else "unmerged"
        placed[number] = (bucket, state)
    return placed


def _summary(issues: list[tuple[int, str, list[tuple[str, int | None, bool]]]],
             since: datetime, until: datetime) -> dict[str, Any]:
    merged = recorded = unknown_tokens = 0
    missing, unknown = [], []
    coverage: dict[str, dict[str, int]] = {}
    for number, state, found in issues:
        if not found or not all(complete for _, _, complete in found):
            missing.append(number)
        if state == "unknown":
            unknown.append(number)
        for executor, tokens, complete in found:
            row = coverage.setdefault(executor, {"sessions": 0, "with_usage": 0})
            row["sessions"] += 1
            row["with_usage"] += complete
            if tokens is None:
                continue
            recorded += tokens
            merged += tokens if state == "merged" else 0
            unknown_tokens += tokens if state == "unknown" else 0
    return {"since": since.isoformat(), "until": until.isoformat(), "issues": len(issues),
            "merged_tokens": merged, "recorded_tokens": recorded,
            "merged_share": merged / recorded if recorded else None,
            "unknown_merge": sorted(unknown), "unknown_merge_tokens": unknown_tokens,
            "missing_usage": sorted(missing),
            "executor_coverage": [{"executor": name, **row} for name, row in sorted(coverage.items())]}


def windows(store: StateStore, now: datetime, days: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Today's issue tokens and the previous days' issue tokens (a total over those days)."""
    today, before = [], []
    for number, (bucket, state) in sorted(_placed(store, now, days).items()):
        found = [session for log in issue_logs(store, number) for session in sessions(log)]
        (today if bucket == 0 else before).append((number, state, found))
    return (_summary(today, now - DAY, now), _summary(before, now - days * DAY, now - DAY))
