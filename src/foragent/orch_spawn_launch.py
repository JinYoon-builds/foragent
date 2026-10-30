"""Launch steps shared by the gate's spawn, `fa orch ensure` and `fa orch refresh` (#413).

Split from ``orch_spawn`` (its line limit). ``orch_spawn`` re-exports these
names, so ``foragent.orch_spawn.spawn_orchestrator`` stays the seam refresh calls.
"""

from datetime import datetime
import os
from pathlib import Path
import subprocess
import uuid

from foragent import notify, orch_spawn
from foragent.config import Config
from foragent.i18n import text
from foragent.state import StateStore


def transcript_path(root: Path, session_id: str) -> str:
    """Where Claude writes this session's transcript; not checked to exist (#413)."""
    base = Path(os.environ["CLAUDE_CONFIG_DIR"]) if os.environ.get("CLAUDE_CONFIG_DIR") else Path.home() / ".claude"
    project = "".join(char if char.isalnum() else "-" for char in str(root))
    return str(base / "projects" / project / f"{session_id}.jsonl")


def _environment(session_id: str) -> dict[str, str]:
    """The launch command's own environment: the role and this launch's new session id.

    The new id replaces any ``FORAGENT_SESSION_ID`` the launcher inherited (a
    gate started from an orchestrator session), so the new session never
    writes heartbeats under someone else's id.
    """
    return {**os.environ, **orch_spawn._role_env(session_id)}


def plan_launch(root: Path, store: StateStore, now: datetime,
                prompt: str | None = None) -> tuple[str, list[str], dict, dict] | None:
    """Pick a new session id and the launch command: ``(screen, argv, env, descriptor)``, or None without Claude.

    Every screen (Orca, tmux, ``claude --bg``) passes ``--session-id`` with the
    new id, so every launch has a descriptor to record.
    """
    session_id = str(uuid.uuid4())
    chosen = orch_spawn._command(root, store, f"orch: {root.name}", session_id, prompt)
    if chosen is None:
        return None
    screen, argv = chosen
    descriptor = {"session_id": session_id, "transcript_path": transcript_path(root, session_id),
                  "started_at": now.isoformat()}
    return screen, argv, _environment(session_id), descriptor


def launch(root: Path, store: StateStore, now: datetime, planned) -> bool:
    """Reserve the spacing, run the launch command and record its outcome; whether it ran."""
    screen, argv, env, _descriptor = planned
    # A failed or timed-out command may still open later, so it reserves the spacing too.
    store._write_json(orch_spawn._record_path(store), {"spawned_at": now.isoformat(), "screen": screen})
    try:
        result = subprocess.run(argv, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, check=True, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError):
        orch_spawn.record_launch(store, now, screen, None)
        return False
    orch_spawn.record_launch(store, now, screen, result)
    return True


def adopt(store: StateStore, planned, now: datetime) -> None:
    """Inside the openers' lock, right after a launch: the new session becomes the heartbeat's active one."""
    from foragent import orch_sessions

    orch_sessions.activate(store, planned[3], now)


def _opened(store: StateStore, config: Config, root: Path, now: datetime, planned) -> None:
    """After a launch, outside the lock: the push."""
    screen = planned[0]
    language = config.language
    try:
        notify.send(config, text("orch_spawn_title", language),
                    text("orch_spawn_opened", language, repo=config.repo or root.name, screen=screen))
    except Exception:  # noqa: BLE001 - the session opened even when its push fails
        pass


def spawn_orchestrator(repo_root: Path, store: StateStore, config: Config, *, prompt: str,
                       now: datetime) -> dict | None:
    """Open a fresh orchestrator session for ``fa orch refresh --ready`` (#413).

    The gate's launch without its spacing, liveness and approved-work checks
    (a refresh replaces a live session on purpose). Claude gets a new
    ``--session-id`` and ``prompt`` after ``/fa-orchestrator``. Returns
    ``{session_id, transcript_path, started_at}``, or None when spawning is off,
    Claude or a screen is missing, or the launch failed. The caller holds the
    openers' lock (``watch_open._opening``) from its checks until it has made
    the descriptor active, so two refreshes or a refresh and a gate spawn never
    interleave; this function takes no lock and writes no heartbeat.
    """
    if os.environ.get("FORAGENT_ORCH_SPAWN") == "off":
        return None
    root = repo_root.resolve()
    planned = plan_launch(root, store, now, prompt)
    if planned is None or not launch(root, store, now, planned):
        return None
    _opened(store, config, root, now, planned)
    return planned[3]
