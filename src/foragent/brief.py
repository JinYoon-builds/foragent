"""Start-of-session briefing for the planning session (`fa brief`).

Reads local state only: the issue snapshot the gate records, runner records,
the question box, recorded executor usage and the project configuration. It
reports facts; choosing what to do next is the /fa agent's job.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from foragent import orch_inbox
from foragent.config import Config
from foragent.i18n import text
from foragent.labels import NEEDS_HUMAN, lane_display, lane_order
from foragent.state import StateStore
from foragent.status import collect as collect_status

CONFIG_FILE = ".foragent.yml"


def configured(root: Path) -> bool:
    """Whether Foragent has been set up in ``root``."""
    return (root / CONFIG_FILE).is_file()


def unconfigured() -> dict[str, Any]:
    return {"setup": False}


def collect(store: StateStore, config: Config | None = None, now: datetime | None = None) -> dict[str, Any]:
    """The briefing facts; without a config only the question count is known."""
    facts: dict[str, Any] = {"questions_open": len(orch_inbox.questions(store))}
    if config is None or now is None:
        return facts
    status = collect_status(store, config, now)
    titles = {row["number"]: row["title"] for row in store.local_issues()}
    return {
        "setup": True,
        **facts,
        "working": _working(status, titles, now),
        "queued": _queued(status),
        "to_see": _to_see(status),
        "team": _team(store, config),
        "profile": config.profile,
        "usage": store.usage(),
        "as_of": status["as_of"],
        "stale": status["stale"],
        "next": next_step(config.language),
    }


# The /fa choices at the end of the briefing (D59 addendum 2); ids name the flow, labels are what people see.
CHOICES = ("order", "design", "inspect")


def next_step(language: str = "en") -> dict[str, Any]:
    return {"question": text("brief_next_question", language),
            "choices": [{"id": choice, "label": text(f"brief_choice_{choice}", language)} for choice in CHOICES]}


def _minutes(started: str | None, now: datetime) -> int | None:
    if not started:
        return None
    return int(max(0, (now - datetime.fromisoformat(started)).total_seconds()) // 60)


def _working(status: dict[str, Any], titles: dict[int, str], now: datetime) -> list[dict[str, Any]]:
    items = [{"lane": run["lane"], "issue": run["number"], "title": titles.get(run["number"]),
              "phase": run["phase"], "minutes": _minutes(run["phase_started_at"], now)}
             for run in status["active_runs"]]
    seen = {item["issue"] for item in items}
    # Every running-labelled issue without a local run record is still work in progress.
    for slug, row in status["lanes"].items():
        for item in row["running"]:
            if item["number"] not in seen:
                seen.add(item["number"])
                items.append({"lane": slug, "issue": item["number"], "title": item["title"],
                              "phase": None, "minutes": None})
    return items


def _queued(status: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for slug, row in status["lanes"].items():
        for item in row["queued"]:
            waiting = row["waiting_for"] if row["issue"] == item["number"] else []
            items.append({"lane": slug, "issue": item["number"], "title": item["title"],
                          "waiting_for": waiting})
    return items


def _to_see(status: dict[str, Any]) -> list[dict[str, Any]]:
    """What only a person can settle: open decisions and PRs waiting for review."""
    items = [{"issue": item["number"], "title": item["title"], "kind": "needs-human",
              "reason": item.get("reason")} for item in status["issues"][NEEDS_HUMAN]]
    items += [{"issue": item["number"], "title": item["title"], "kind": "pr-open", "reason": None}
              for item in status["issues"]["pr-open"]]
    return sorted(items, key=lambda item: item["issue"])


def _team(store: StateStore, config: Config) -> list[dict[str, Any]]:
    used: dict[str, list[str]] = {}
    for run in [*store.read_history(), *store.active_runs()]:
        names = used.setdefault(run.lane, [])
        names.extend(name for name in run.skills_used if name not in names)
    slugs = list(config.lanes)
    # The implicit main lane takes work when no lane matches, so it is part of the team.
    if "main" not in slugs and (not slugs or "main" in used):
        slugs.append("main")
    team = []
    for slug in sorted(slugs, key=lane_order):
        settings = config.lanes.get(slug) or {}
        fixed = list(settings.get("skills") or [])
        lane_used = used.get(slug, [])
        team.append({
            "lane": slug, "title": lane_display(config, slug, config.language),
            "executor": settings.get("executor") or config.executor,
            "fixed_skills": fixed, "used_skills": lane_used,
            "warnings": [] if fixed or lane_used else ["no-skills"],
        })
    return team


def render(brief: dict[str, Any], language: str = "en") -> str:
    if brief.get("setup") is False:
        return text("brief_setup_missing", language)
    lines = []
    if brief["questions_open"]:
        lines.append(text("brief_questions", language, count=brief["questions_open"]))
    if "working" not in brief:
        return "\n".join(lines) or text("brief_empty", language)
    lines.append(text("brief_working_heading", language, count=len(brief["working"])))
    lines += [text("brief_working_item", language, lane=item["lane"], issue=item["issue"],
                   title=item["title"] or "", phase=item["phase"] or "-",
                   minutes="?" if item["minutes"] is None else item["minutes"])
              for item in brief["working"]]
    lines.append(text("brief_queued_heading", language, count=len(brief["queued"])))
    for item in brief["queued"]:
        line = text("brief_queued_item", language, lane=item["lane"], issue=item["issue"], title=item["title"])
        if item["waiting_for"]:
            line += text("brief_queued_waiting", language,
                         numbers=", #".join(str(number) for number in item["waiting_for"]))
        lines.append(line)
    lines.append(text("brief_to_see_heading", language, count=len(brief["to_see"])))
    lines += [text("brief_to_see_item", language, issue=item["issue"], title=item["title"],
                   kind=text("brief_kind_needs_human" if item["kind"] == NEEDS_HUMAN else "brief_kind_pr",
                             language))
              for item in brief["to_see"]]
    none = text("brief_team_none", language)
    lines.append(text("brief_team_heading", language))
    for member in brief["team"]:
        lines.append(text("brief_team_item", language, lane=member["title"], executor=member["executor"],
                          fixed=", ".join(member["fixed_skills"]) or none,
                          used=", ".join(member["used_skills"]) or none))
        if "no-skills" in member["warnings"]:
            lines.append(text("brief_warning_no_skills", language))
    profile = brief["profile"]
    lines.append(text("brief_profile", language, profile=", ".join(
        f"{key}={value}" for key, value in profile.items())) if profile
                 else text("brief_profile_missing", language))
    if not brief["usage"]:
        lines.append(text("brief_usage_missing", language))
    for executor, record in brief["usage"].items():
        windows = ", ".join(text("brief_usage_window", language, name=window["name"],
                                 percent=f"{window['used_percent']:g}")
                            for window in record.get("windows") or [])
        lines.append(text("brief_usage_item", language, executor=executor, windows=windows or none))
    step = next_step(language)
    lines.append(" ".join([step["question"], *(f"[{choice['label']}]" for choice in step["choices"])]))
    return "\n".join(lines)
