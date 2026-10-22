"""Open a lane view without making terminal availability a caller's failure."""

from contextlib import contextmanager, suppress
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
        command = shlex.join([
            *([str(fa)] if fa.is_file() else [sys.executable, "-m", "foragent.cli"]),
            "gate", "watch", "--repo-root", str(root),
        ])
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
                   "--direction", "horizontal", "--command", command], root)
        else:
            osascript = shutil.which("osascript")
            if not osascript:
                raise OSError("no terminal opener available")
            script = 'tell application "Terminal" to do script ' + json.dumps(command, ensure_ascii=False)
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
