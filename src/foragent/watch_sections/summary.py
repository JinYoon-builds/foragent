"""The first line: lane counts and when the issue list was last observed."""

from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.status import summary_values, unknown as status_unknown
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot

ORDER = 10


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    from foragent import watch

    language = config.language
    status = snapshot.status
    t = lambda key, **values: watch.text(key, language, **values)
    summary = t("summary", **summary_values(status["summary"]))
    if status.get("as_of"):
        observed = watch.datetime.fromisoformat(status["as_of"]).astimezone().strftime("%H:%M")
        summary += " · " + t("status_as_of", time=observed)
    if status_unknown(status):
        return [watch._line(t("status_unknown") + "  " + summary, "yellow")]
    return [watch._line(summary, "bold")]
