"""A one-time hint for Korean lane views in macOS Terminal.app.

Terminal.app's default font (SF Mono) has no Hangul, so each syllable is drawn in a
narrower fallback font and Korean reads with a gap after every syllable. The view's
cell widths are right and fa cannot change the terminal's font, so the first live
view in Terminal.app with Korean as the language names a font that draws Hangul
exactly two cells wide, where to get it and where to set it.

The hint counts as shown only after a view saw it stay on screen whole: a window too
narrow for every form shows nothing, and a resize before the person could read it
records nothing. While one view shows it, it holds a machine-wide lock (`hold`), so
two first sessions cannot both show it. Other
terminals and English never read or write the record, so they do not use up the hint.
"""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile

from foragent import capacity, i18n

RECORD = "terminal-hangul-hint.json"
LOCK = "terminal-hangul-hint.lock"
# Longest first: the full sentence, then a form that fits an 80-column window.
FORMS = ("terminal_hint_font", "terminal_hint_font_short")


def _path() -> Path:
    return capacity.machine_root() / RECORD


def applies(language: str, environ=None) -> bool:
    """Korean as the language, in Terminal.app (read from this process's environment)."""
    environ = os.environ if environ is None else environ
    return environ.get("TERM_PROGRAM") == "Apple_Terminal" and language.lower().split("-")[0] == "ko"


def _shown() -> bool:
    try:
        return bool(json.loads(_path().read_text(encoding="utf-8")).get("shown_at"))
    except (OSError, ValueError, AttributeError):
        return False  # No record, or one we cannot read: not shown yet.


def due(language: str, environ=None) -> tuple[str, ...] | None:
    """Every form of the hint, longest first, when this view may show it; else None.

    Only a first look: reads, never writes. `hold` decides who shows it."""
    if not applies(language, environ) or _shown():
        return None
    return tuple(i18n.text(key, language) for key in FORMS)


def fitting(forms: tuple[str, ...] | None, width: int) -> str | None:
    """The longest form that fits `width` cells whole, so no address is ever cut."""
    from foragent import watch

    return next((form for form in forms or () if watch._cells(form) <= width), None)


BUSY, SHOWN = "busy", "shown"


class Hold:
    """This view's turn to show the hint: the machine-wide lock, held while it shows.

    Holding is not consuming. `confirm` writes the record only after the view saw the
    hint stay on screen whole; a view that ends or loses its window first lets go of the
    lock and leaves the hint for the next start."""

    def __init__(self, stream=None) -> None:
        self._stream = stream  # None: the lock could not be opened; show it this session.
        self.confirmed = False

    def confirm(self, now: datetime | None = None) -> bool:
        if not self.confirmed:
            try:
                _write(now)
            except OSError:
                return False  # Not recorded: a later start shows it again.
            self.confirmed = True
        return True

    def release(self) -> None:
        if self._stream is not None:
            self._stream.close()  # Closing the file lets go of the lock.
            self._stream = None


def hold() -> Hold | str:
    """Take the turn to show the hint without waiting.

    BUSY while another view shows it (try again on a later frame), SHOWN once a view
    confirmed it. The check reads the record under the lock, so two views can never
    both show it. A lock that cannot be opened never stops the view."""
    root = capacity.machine_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        stream = open(root / LOCK, "a", encoding="utf-8")
    except OSError:
        return Hold()
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        return BUSY
    except OSError:
        stream.close()
        return Hold()
    if _shown():
        stream.close()
        return SHOWN
    return Hold(stream)


def _write(now: datetime | None) -> None:
    path = _path()
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    handle, name = tempfile.mkstemp(prefix=".terminal-hangul-hint-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"shown_at": stamp}) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
