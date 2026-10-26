"""The first line: lane counts and when the issue list was last observed."""

from pathlib import Path
import string
from typing import Any

from foragent import i18n
from foragent.config import Config
from foragent.status import summary_values, unknown as status_unknown
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot

ORDER = 10
# The gate runs every few minutes; this long without its heartbeat is a warning (D67).
GATE_STALE_SECONDS = 15 * 60


def _counts(summary: dict[str, Any], language: str) -> str:
    """The status summary with counts that are exactly zero left out.

    The words and order are those of ``fa gate status``. When every count is zero
    or any is unknown, the whole line stays as it is so nothing reads as missing.
    """
    from foragent import watch

    values = summary_values(summary)
    whole = watch.text("summary", language, **values)
    if any(value == "?" for value in values.values()) or not any(values.values()):
        return whole
    template = i18n.MESSAGES.get(language.lower().split("-")[0], i18n.MESSAGES["en"])["summary"]
    parts = []
    for part in template.split(" · "):
        fields = [name for _, name, _, _ in string.Formatter().parse(part) if name]
        if not fields or any(values.get(name) != 0 for name in fields):
            parts.append(part.format(**values))
    return " · ".join(parts) or whole


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    from foragent import watch

    language = config.language
    status = snapshot.status
    t = lambda key, **values: watch.text(key, language, **values)
    summary = _counts(status["summary"], language)
    if status.get("as_of"):
        observed = watch.datetime.fromisoformat(status["as_of"]).astimezone().strftime("%H:%M")
        summary += " · " + t("status_as_of", time=observed)
    if status_unknown(status):
        return [watch._line(t("status_unknown") + "  " + summary, "yellow")]
    gate = snapshot.heartbeats.get("gate")
    if gate is not None:
        gate += max(0.0, (context["now"] - snapshot.captured_at).total_seconds())
        if gate > GATE_STALE_SECONDS:
            return [watch._line(t("watch_gate_stale", minutes=int(gate // 60)) + "  " + summary, "yellow")]
    return [watch._line(summary, "bold")]
