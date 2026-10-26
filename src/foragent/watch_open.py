"""Open a lane view without making terminal availability a caller's failure."""

from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from foragent.i18n import text
from foragent.state import StateStore


def _running(store: StateStore) -> bool:
    try:
        pid = int((store.root / "watch.pid").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # A process owned by another user still exists.
    except OverflowError:
        return False
    return True


LOCK_NAME = "watch.lock"
# A build reload passes the held descriptor to the replacement view.
LOCK_FD_ENV = "FORAGENT_WATCH_LOCK_FD"


def _locked(store: StateStore) -> bool:
    """Probe the view lock on our own descriptor and release it at once."""
    try:
        fd = os.open(store.root / LOCK_NAME, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)  # Closing also releases a lock this probe took.
    return False


def _inherited_lock(path: Path) -> int | None:
    value = os.environ.pop(LOCK_FD_ENV, None)
    try:
        fd = int(value) if value else -1
        if fd < 0 or os.fstat(fd).st_ino != os.stat(path).st_ino:
            return None
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ValueError, OSError):
        return None
    return fd


@contextmanager
def exclusive(store: StateStore):
    """Hold the repository's single-view lock for the view's whole life.

    Yields the descriptor, or None when another live view holds the lock. The
    kernel releases the lock when the view exits or crashes. The descriptor is
    inheritable so a build reload keeps the lock across os.execv.
    """
    path = store.root / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = _inherited_lock(path)
    if fd is None:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            yield None
            return
    os.set_inheritable(fd, True)
    try:
        yield fd
    finally:
        with suppress(OSError):
            os.close(fd)


def _live(store: StateStore) -> bool:
    return _locked(store) or _running(store)


@contextmanager
def registered(store: StateStore):
    """Only the interactive view owns its PID, never the short-lived opener."""
    path = store.root / "watch.pid"
    content = str(os.getpid()) + "\n"
    store._atomic_write(path, content)
    try:
        yield
    finally:
        with suppress(OSError):
            if path.read_text(encoding="utf-8") == content:
                path.unlink()


def _call(argv: list[str], root: Path) -> None:
    subprocess.run(
        argv, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=True, timeout=5,
    )


class _ViewLive(Exception):
    """A view became live while an opener was working: stop without a command."""


def _guard(store: StateStore) -> None:
    # A view can take its lock at any moment, so check right before each command.
    if _live(store):
        raise _ViewLive


def _recorded_terminal(store: StateStore, root: Path, *, guarded: bool = False) -> tuple[str, str] | None:
    try:
        record = json.loads((store.root / "watch-terminal.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    executable, handle = record.get("executable"), record.get("handle")
    if not isinstance(executable, str) or not isinstance(handle, str) or not executable or not handle:
        return None
    if guarded:
        _guard(store)
    try:
        _call([executable, "terminal", "show", "--terminal", handle], root)
    except (OSError, subprocess.SubprocessError):
        return None
    return executable, handle


REOPEN_SPACING = timedelta(minutes=5)
# Set while reopen calls open_view, which then skips the record reopen just wrote.
# The dict notes when only the manual command was printed, which is not an attempt.
_reopening: ContextVar[dict | None] = ContextVar("foragent_watch_reopening", default=None)


def _attempt_path(store: StateStore) -> Path:
    return store.root / "watch-open.json"


def _spaced(store: StateStore, now: datetime) -> bool:
    """Every opener honours a recent attempt, whoever wrote it."""
    try:
        record = json.loads(_attempt_path(store).read_text(encoding="utf-8"))
        last = datetime.fromisoformat(record["attempted_at"])
        return now - last < REOPEN_SPACING
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _record_attempt(store: StateStore, now: datetime) -> None:
    store._write_json(_attempt_path(store), {"attempted_at": now.isoformat()})


@contextmanager
def _opening(store: StateStore):
    """Serialize openers so each sees the record any other opener just wrote.

    A blocking flock on a separate file: it is held only for one opener's
    checks and terminal commands (each bounded by the 5 s timeout), and the
    kernel releases it if the opener dies.
    """
    store.root.mkdir(parents=True, exist_ok=True)
    fd = os.open(store.root / "watch-open.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def reopen(repo_root: Path, store: StateStore, language: str, now: datetime) -> None:
    """Gate tick: open a missing view while work runs, one attempt per spacing."""
    if os.environ.get("FORAGENT_VIEW") == "off" or not store.active_runs():
        return
    with _opening(store):
        if _live(store) or _spaced(store, now):
            return
        path = _attempt_path(store)
        try:
            previous = path.read_bytes()
        except OSError:
            previous = None
        # Separate gate processes share this record; failures are retried after the spacing.
        _record_attempt(store, now)
        attempt = {"manual": False}
        token = _reopening.set(attempt)
        try:
            open_view(repo_root, store, language)
        finally:
            _reopening.reset(token)
        if attempt["manual"]:
            # No opener exists: printing the manual command must not reserve the spacing.
            with suppress(OSError):
                if previous is None:
                    path.unlink()
                else:
                    store._atomic_write(path, previous.decode("utf-8"))


def open_view(repo_root: Path, store: StateStore, language: str = "en") -> None:
    """Request a split or window and return; the new view registers itself."""
    if os.environ.get("FORAGENT_VIEW") == "off":
        return
    if _reopening.get() is not None:
        # reopen holds the opener lock, checked the view and spacing, and recorded.
        _launch(repo_root, store, language, None)
        return
    try:
        with _opening(store):
            if _live(store):
                return
            now = datetime.now(UTC)
            if _spaced(store, now):
                return
            # A timed-out command may still open the pane later, so it reserves the spacing.
            _launch(repo_root, store, language, now)
    except (OSError, ValueError) as exc:
        error = " ".join(str(exc).split())[:200]
        print("foragent: " + text("watch_open_failed", language, error=error), file=sys.stderr)


def _launch(repo_root: Path, store: StateStore, language: str, record_at: datetime | None) -> None:
    """Run the terminal command; with record_at, a failed final command is recorded."""

    def final(argv: list[str], root: Path) -> None:
        # Only the final split or window command counts as an attempt.
        _guard(store)
        try:
            _call(argv, root)
        except (OSError, subprocess.SubprocessError):
            if record_at is not None:
                with suppress(OSError):
                    _record_attempt(store, record_at)
            raise

    try:
        root = repo_root.resolve()
        # Use this installation even when a launchd PATH cannot find fa.
        fa = Path(sys.executable).with_name("fa")
        program = [
            *([str(fa)] if fa.is_file() else [sys.executable, "-m", "foragent.cli"]),
            "gate", "watch", "--repo-root", str(root),
        ]
        command = shlex.join(program)
        handle = os.environ.get("ORCA_TERMINAL_HANDLE")
        executable = shutil.which("orca") if handle else None
        terminal = None
        if executable:
            executable = str(Path(executable).absolute())
            store._write_json(store.root / "watch-terminal.json", {
                "executable": executable, "handle": handle,
            })
            terminal = executable, handle
        elif not handle:
            terminal = _recorded_terminal(store, root, guarded=True)
        if terminal:
            executable, handle = terminal
            final([executable, "terminal", "split", "--terminal", handle,
                   "--direction", "vertical", "--command", command], root)
        else:
            # The innermost multiplexer owns the current pane, so check it first.
            tmux = shutil.which("tmux") if os.environ.get("TMUX") else None
            if tmux:
                final([tmux, "split-window", "-h", "-c", str(root), command], root)
                return
            wezterm = shutil.which("wezterm") if os.environ.get("WEZTERM_PANE") else None
            if wezterm:
                final([wezterm, "cli", "split-pane", "--right", "--cwd", str(root), "--", *program], root)
                return
            kitty = shutil.which("kitty") if os.environ.get("KITTY_WINDOW_ID") else None
            if kitty:
                listen = os.environ.get("KITTY_LISTEN_ON")
                final([kitty, "@", *(["--to", listen] if listen else []), "launch",
                       "--location=vsplit", "--cwd", str(root), *program], root)
                return
            osascript = shutil.which("osascript")
            if not osascript:
                manual = shlex.join(["fa", "gate", "watch", "--repo-root", str(root)])
                print("foragent: " + text("watch_open_manual", language, command=manual), file=sys.stderr)
                if attempt := _reopening.get():
                    attempt["manual"] = True
                return
            quoted = json.dumps(command, ensure_ascii=False)
            if os.environ.get("TERM_PROGRAM") == "iTerm.app":
                script = ('tell application "iTerm2" to tell current session of current window'
                          ' to split vertically with default profile command ' + quoted)
            else:
                script = 'tell application "Terminal" to do script ' + quoted
            final([osascript, "-e", script], root)
    except _ViewLive:
        return
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # Subprocess output is suppressed; every failure stays on one short line.
        if isinstance(exc, subprocess.CalledProcessError):
            error = f"{Path(exc.cmd[0]).name} exited with status {exc.returncode}"
        elif isinstance(exc, subprocess.TimeoutExpired):
            error = "terminal opener timed out"
        else:
            error = " ".join(str(exc).split())[:200]
        print("foragent: " + text("watch_open_failed", language, error=error), file=sys.stderr)
