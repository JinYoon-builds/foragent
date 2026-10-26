"""Open an orchestrator session where people can see it when approved work has none (D76).

The gate calls ``ensure_orchestrator`` every tick. It opens one Claude session
running ``/fa-orchestrator`` only when approved work exists, no orchestrator
renewed its heartbeat lately, and no spawn happened in the last two minutes.
The session opens in the background: a new Orca tab without focus, a detached
tmux window, or ``claude --bg`` without a screen. The lane view stays beside the
planning session, so nothing here splits a pane or switches focus.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
from types import SimpleNamespace

from foragent import notify, watch_open
from foragent.commands.orch import ALIVE_SECONDS  # `fa orch alive` uses the same limit.
from foragent.config import Config
from foragent.i18n import text
from foragent.state import StateStore

SPAWN_SPACING = timedelta(minutes=2)
SKILL = "/fa-orchestrator"


def _record_path(store: StateStore) -> Path:
    return store.root / "orch-spawn.json"


def _alive(store: StateStore, now: datetime) -> bool:
    last = store.heartbeats()["orchestrator"]
    return last is not None and (now - last).total_seconds() <= ALIVE_SECONDS


def _spaced(store: StateStore, now: datetime) -> bool:
    try:
        last = datetime.fromisoformat(json.loads(_record_path(store).read_text(encoding="utf-8"))["spawned_at"])
        return now - last < SPAWN_SPACING
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _approved_work(store: StateStore) -> bool:
    """Running work or a stored approval snapshot (queued or waiting to start)."""
    return bool(store.active_runs()) or any((store.root / "approvals").glob("*.json"))


def _orca(store: StateStore, root: Path) -> str | None:
    handle = os.environ.get("ORCA_TERMINAL_HANDLE")
    executable = shutil.which("orca") if handle else None
    if executable:
        return str(Path(executable).absolute())
    if not handle and (terminal := watch_open._recorded_terminal(store, root)):
        return terminal[0]
    return None


def _command(root: Path, store: StateStore, title: str) -> tuple[str, list[str]] | None:
    claude = shutil.which("claude")
    if not claude:
        return None
    session = shlex.join([claude, SKILL])
    if orca := _orca(store, root):
        return "orca", [orca, "terminal", "create", "--worktree", f"path:{root}",
                        "--title", title, "--command", session]
    tmux = shutil.which("tmux") if os.environ.get("TMUX") else None
    if tmux:
        return "tmux", [tmux, "new-window", "-d", "-n", title, "-c", str(root), session]
    return "background", [claude, "--bg", "-n", title, SKILL]


def ensure_orchestrator(repo_root: Path, store: StateStore, config: Config, *, now: datetime) -> str | None:
    """Open one orchestrator if needed; return the screen used, or None."""
    if os.environ.get("FORAGENT_ORCH_SPAWN") == "off":
        return None
    if _alive(store, now) or not _approved_work(store):
        return None
    with watch_open._opening(store):  # Serialize with other openers in this state root.
        if _spaced(store, now):
            return None
        root = repo_root.resolve()
        title = f"orch: {root.name}"
        chosen = _command(root, store, title)
        if chosen is None:
            return None
        screen, argv = chosen
        # A failed or timed-out command may still open later, so it reserves the spacing too.
        store._write_json(_record_path(store), {"spawned_at": now.isoformat(), "screen": screen})
        try:
            subprocess.run(argv, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
    language = config.language
    notify.send(config, text("orch_spawn_title", language),
                text("orch_spawn_opened", language, repo=config.repo or root.name, screen=screen))
    return screen


def on_tick(ctx, report):
    """Open an orchestrator when needed and note it in ``report``; never raise. Returns ``report``."""
    language = ctx.config.language
    try:
        screen = ensure_orchestrator(ctx.repo_root, ctx.store, ctx.config, now=ctx.now())
    except Exception as exc:  # noqa: BLE001 - the optional spawn must not stop the tick
        report.actions.append(text("orch_spawn_failed", language, error=" ".join(str(exc).split())[:200]))
        return report
    if screen:
        report.actions.append(text("orch_spawn_tick", language, screen=screen))
    return report


@contextmanager
def deciding(ctx):
    """Wrap a whole gate tick so the spawn decision runs on every exit.

    The decision reads only local facts (heartbeats, active runs, approvals), so
    it does not depend on any tick step: a busy tick, a GitHub wait, a failed
    fetch, refresh, recovery or budget step all still open a missing
    orchestrator. Yields ``keep(report) -> report`` for the tick's result; on an
    error the spawn runs and the original error propagates unchanged.
    """
    kept = []

    def keep(report):
        kept.append(report)
        return report

    try:
        yield keep
    except Exception:
        on_tick(ctx, SimpleNamespace(actions=[]))  # The tick's report is lost with the error.
        raise
    on_tick(ctx, kept[-1] if kept else SimpleNamespace(actions=[]))
