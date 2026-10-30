"""The selected lane's configuration and first memory lines."""

from pathlib import Path
from typing import Any

from foragent import lane_memory
from foragent.config import Config
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot

ORDER = 50
KEEP = True  # A person asked for these details; they stay when the screen is short.


def render_detail(config: Config, slug: str, repo_root: Path, done_count: int = 0) -> str:
    """Show lane configuration and the first three lines of the curated and pending memory, each with its path."""
    from foragent import watch

    language = config.language
    lane = config.lanes.get(slug, {})
    executor = lane.get("executor") or config.executor
    model = lane.get("model") or config.executor_model or watch.text("watch_default_model", language)
    none = watch.text("watch_none", language)
    lines = [
        f"{watch.text('watch_lane', language)}: {watch.lane_display(config, slug, language)} ({slug})",
        f"{watch.text('watch_paths', language)}: {', '.join(lane.get('paths', [])) or none}",
        f"{watch.text('watch_executor', language)}: {executor}",
        f"{watch.text('watch_model', language)}: {model}",
        f"{watch.text('watch_skills', language)}: {', '.join(lane.get('skills', [])) or none}",
        watch.text("watch_done_count", language, count=done_count),
        watch.text("watch_memory", language) + ":",
    ]
    folder = watch.Path(repo_root) / lane_memory.directory(lane_memory.memory_dir(config, slug))
    shown = False
    for name, title in ((lane_memory.CURATED, None), (lane_memory.PENDING, "watch_pending_memory")):
        if name == lane_memory.PENDING:  # Derived: lessons whose id the dream has not consumed.
            derived = [line for line in lane_memory.read_pending(folder).splitlines() if line.strip()]
            if derived:  # No file path: MEMORY.new.md still holds consumed lessons.
                lines.append(watch.text(title, language) + ":")
                lines.extend(derived[:3])
                if len(derived) > 3:
                    lines.append(watch.text("watch_pending_more", language, count=len(derived) - 3))
                shown = True
            continue
        else:
            try:
                with (folder / name).open(encoding="utf-8", errors="replace") as stream:
                    lessons = [line.rstrip("\n") for line in watch.islice(stream, 3)]
            except FileNotFoundError:
                continue
        if not any(line.strip() for line in lessons):
            continue
        if title is not None:
            lines.append(watch.text(title, language) + ":")
        lines.extend([*lessons, str((folder / name).absolute())])
        shown = True
    if not shown:
        lines.append(watch.text("watch_no_memory", language))
    return "\n".join(watch._clean(line) for line in lines)


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    from foragent import watch

    selected = context.get("selected")
    if selected is None:
        return []
    detail = watch.render_detail(config, selected, repo_root, done_count=len(snapshot.done.get(selected, [])))
    return watch._panel([watch._line(line) for line in detail.splitlines()], context["width"],
                        watch.text("watch_detail", config.language))
