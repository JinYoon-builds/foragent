"""The current-work table beside the handled-events log."""

from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.i18n import text
from foragent.status import _age
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot
from foragent.watch_layout import _clones, _lane_issues, _split, _widths

ORDER = 40
# The orchestrator verbs that take --say; they write their summary as "#N: ...".
SAY_KINDS = ("retry", "rerun-review", "resolve", "escalate")


def person_line(event: dict[str, Any], language: str) -> str:
    """The line the handled-events log shows for one journal event.

    The journal keeps the raw ``summary`` for logs and audits. The view shows
    the event's ``say`` line when the orchestrator gave one. Otherwise an event
    from a --say verb shows a short localized line for its kind; the verbs have
    always written ``#N: ...`` (also before --say existed), so older journal
    lines read the same way. Any other event keeps its raw summary.
    """
    say = event.get("say")
    if isinstance(say, str) and say.strip():
        return say
    kind, issue = event.get("kind"), event.get("issue")
    if kind in SAY_KINDS and issue is not None and event["summary"].startswith(f"#{issue}: "):
        return text(f"orch_say_{kind}", language, issue=issue)
    return event["summary"]


def _runner_progress(run: dict[str, Any] | None, captured_at: Any) -> float | None:
    """Seconds since the runner's own last progress, or None before it made any.

    start_run sets last_progress_at to the start time; that value is the default,
    not evidence that the runner moved.
    """
    if not run or run.get("last_progress_at") in (None, run.get("started_at")):
        return None
    return _age(run["last_progress_at"], captured_at)


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    from foragent import watch

    language = config.language
    status = snapshot.status
    t = lambda key, **values: watch.text(key, language, **values)
    side_by_side, _, _, table_width, events_width = _widths(context["width"])
    busy, _ = _split(status)
    clones = _clones(status)
    age = max(0, (context["now"] - snapshot.captured_at).total_seconds())
    runs = {run["number"]: run for run in status["active_runs"]}
    inner = table_width - 2
    lane_width = min(13, inner // 6)
    phase_width = min(11, inner // 6)
    activity_width = min(24, inner // 3)
    widths = [lane_width, inner - lane_width - phase_width - activity_width - 3, phase_width, activity_width]
    table = watch._columns([t("watch_lane"), t("watch_now"), t("watch_phase"), ""], widths, "bold")
    for _, row in busy:
        titles = {item["number"]: item.get("title") for item in row.get("running", [])}
        for number in _lane_issues(row, clones) or [row["issue"]]:
            run = runs.get(number)
            if number == row["issue"]:
                shown = row
            else:
                # Another worker of the lane: its own state, phase and title.
                shown = {**row, "state": snapshot.worker_states.get(number, "working"), "issue": number, "title": titles.get(number) or "",
                         "phase": run.get("phase") if run else None,
                         "last_activity_seconds": _age(run.get("last_activity_at") or run.get("started_at"),
                                                       snapshot.captured_at) if run else None}
            phase = "" if shown["state"] == "blocked" else watch.text(shown["phase"] or shown["state"], language)
            task = f"#{number} {shown['title'] or ''}".rstrip() if number is not None else "—"
            if run and run["attempts"] > 1:
                task += " · " + t("watch_retry", count=run["attempts"])
            activity = watch._activity(
                shown, language, age, config.stall_minutes, run.get("pid") if run else None,
                snapshot.log_ages.get(number) if run else None,
                _runner_progress(run, snapshot.captured_at),
                _age(run.get("phase_started_at"), snapshot.captured_at) if run else None)
            # Each worker's issue shows with the plain lane name; the road numbers the workers.
            table += watch._columns([row["lane_display"], task, phase, activity], widths,
                                    "yellow" if activity.startswith(("○", "?", "⚠")) or shown["state"] == "blocked" else "")
    if not busy:
        table.append(watch._line(t("watch_all_idle")))
    events: list[Line] = []
    for event in snapshot.events:
        when = watch.datetime.fromisoformat(event["at"]).astimezone().strftime("%H:%M") if event.get("at") else "—"
        line = person_line(event, language)
        number = f" #{event['issue']}" if event.get("issue") is not None and line == event["summary"] else ""
        events.extend(watch._wrap(f"{when}{number} {line} ✓", events_width - 2))
    if not events:
        events.append(watch._line(t("watch_no_events")))
    work = watch._panel(table, table_width, t("watch_now"))
    handled = watch._panel(events, events_width, t("watch_events"))
    return watch._beside(work, handled, table_width) if side_by_side else work + handled
