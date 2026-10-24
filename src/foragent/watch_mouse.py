"""Mouse targets in the lane view.

render_frame fills a caller-owned layout with hit boxes; coordinates are
zero-based screen cells. Runtime dependencies resolve through foragent.watch
to preserve its patch points.
"""

from typing import Any

from foragent import orch_inbox
from foragent.config import Config
from foragent.state import StateStore


def hit_test(layout: dict[str, Any], x: int, y: int) -> Any:
    """The target under one screen cell, or None. Later boxes are drawn on top."""
    for left, top, right, bottom, target in reversed(layout.get("hits", [])):
        if left <= x < right and top <= y < bottom:
            return target
    return None


def handle_mouse(
    button: str, x: int, y: int, layout: dict[str, Any], store: StateStore, config: Config,
    *, state: dict[str, Any] | None = None,
) -> None:
    """Act on a click. The big squirrel switches to the orchestrator's recorded terminal."""
    from foragent import watch

    if button != "left" or watch.hit_test(layout, x, y) != ("orchestrator", None):
        return
    watch.switch_to_orchestrator(store)


def switch_to_orchestrator(store: StateStore) -> bool:
    """Switch to the Orca terminal named in the orchestrator's latest note.

    The destination is the recorded handle, never the pane running the view.
    Without a recorded Orca terminal, or without Orca, nothing happens.
    """
    from foragent import watch

    note = orch_inbox.read_note(store) or {}
    where = note.get("where") if isinstance(note.get("where"), dict) else {}
    handle = where.get("handle") if where.get("kind") == "orca" else None
    executable = watch.shutil.which("orca") if handle else None
    if not executable:
        return False
    try:
        watch.subprocess.run(
            [executable, "terminal", "switch", "--terminal", str(handle)],
            stdin=watch.subprocess.DEVNULL, stdout=watch.subprocess.DEVNULL,
            stderr=watch.subprocess.DEVNULL, check=True, timeout=5,
        )
    except (OSError, watch.subprocess.SubprocessError):
        return False  # A closed tab must not break the view.
    return True
