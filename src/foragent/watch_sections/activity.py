"""The log of events the orchestrator handled, below the lane rows."""

from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.i18n import text
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot

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


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    """Events without a say line; the newest say is the header, so says are not repeated here."""
    from foragent import watch

    language, width = config.language, context["width"]
    events: list[Line] = []
    for event in snapshot.events:
        if isinstance(event.get("say"), str) and event["say"].strip():
            continue
        when = watch.datetime.fromisoformat(event["at"]).astimezone().strftime("%H:%M") if event.get("at") else "—"
        line = person_line(event, language)
        number = f" #{event['issue']}" if event.get("issue") is not None and line == event["summary"] else ""
        events.extend(watch._wrap(f"{when}{number} {line} ✓", width - 2))
    return watch._panel(events, width, watch.text("watch_events", language)) if events else []
