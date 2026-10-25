"""Open a lane view without making terminal availability a caller's failure."""

from contextlib import contextmanager, suppress
from datetime import datetime, timedelta
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


def _recorded_terminal(store: StateStore, root: Path) -> tuple[str, str] | None:
    try:
        record = json.loads((store.root / "watch-terminal.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    executable, handle = record.get("executable"), record.get("handle")
    if not isinstance(executable, str) or not isinstance(handle, str) or not executable or not handle:
        return None
    try:
        _call([executable, "terminal", "show", "--terminal", handle], root)
    except (OSError, subprocess.SubprocessError):
        return None
    return executable, handle


REOPEN_SPACING = timedelta(minutes=5)


def reopen(repo_root: Path, store: StateStore, language: str, now: datetime) -> None:
    """Gate tick: open a missing view while work runs, one attempt per spacing."""
    if os.environ.get("FORAGENT_VIEW") == "off" or not store.active_runs() or _running(store):
        return
    path = store.root / "watch-open.json"
    try:
        last = datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["attempted_at"])
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        last = None
    if last is not None and now - last < REOPEN_SPACING:
        return
    # Separate gate processes share this record; failures are retried after the spacing.
    store._write_json(path, {"attempted_at": now.isoformat()})
    open_view(repo_root, store, language)


def open_view(repo_root: Path, store: StateStore, language: str = "en") -> None:
    """Request a split or window and return; the new view registers itself."""
    if os.environ.get("FORAGENT_VIEW") == "off":
        return
    try:
        if _running(store):
            return
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
            terminal = _recorded_terminal(store, root)
        if terminal:
            executable, handle = terminal
            _call([executable, "terminal", "split", "--terminal", handle,
                   "--direction", "vertical", "--command", command], root)
        else:
            # The innermost multiplexer owns the current pane, so check it first.
            tmux = shutil.which("tmux") if os.environ.get("TMUX") else None
            if tmux:
                _call([tmux, "split-window", "-h", "-c", str(root), command], root)
                return
            wezterm = shutil.which("wezterm") if os.environ.get("WEZTERM_PANE") else None
            if wezterm:
                _call([wezterm, "cli", "split-pane", "--right", "--cwd", str(root), "--", *program], root)
                return
            kitty = shutil.which("kitty") if os.environ.get("KITTY_WINDOW_ID") else None
            if kitty:
                listen = os.environ.get("KITTY_LISTEN_ON")
                _call([kitty, "@", *(["--to", listen] if listen else []), "launch",
                       "--location=vsplit", "--cwd", str(root), *program], root)
                return
            osascript = shutil.which("osascript")
            if not osascript:
                manual = shlex.join(["fa", "gate", "watch", "--repo-root", str(root)])
                print("foragent: " + text("watch_open_manual", language, command=manual), file=sys.stderr)
                return
            quoted = json.dumps(command, ensure_ascii=False)
            if os.environ.get("TERM_PROGRAM") == "iTerm.app":
                script = ('tell application "iTerm2" to tell current session of current window'
                          ' to split vertically with default profile command ' + quoted)
            else:
                script = 'tell application "Terminal" to do script ' + quoted
            _call([osascript, "-e", script], root)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # Subprocess output is suppressed; every failure stays on one short line.
        if isinstance(exc, subprocess.CalledProcessError):
            error = f"{Path(exc.cmd[0]).name} exited with status {exc.returncode}"
        elif isinstance(exc, subprocess.TimeoutExpired):
            error = "terminal opener timed out"
        else:
            error = " ".join(str(exc).split())[:200]
        print("foragent: " + text("watch_open_failed", language, error=error), file=sys.stderr)
