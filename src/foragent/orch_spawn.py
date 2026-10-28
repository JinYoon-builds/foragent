"""Open an orchestrator session where people can see it when approved work has none (D76).

The gate calls ``ensure_orchestrator`` every tick. It opens one Claude session
running ``/fa-orchestrator`` only when approved work exists, no orchestrator
renewed its heartbeat lately, no recorded or gate-spawned orchestrator session
is still running, and no spawn happened in the last two minutes.
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

from foragent import notify, orch_binding, orch_push, orch_stale_files, processes, watch_open
from foragent.commands.orch import ALIVE_SECONDS  # `fa orch alive` uses the same limit.
from foragent.config import Config
from foragent.i18n import text
from foragent.orch_events import append_event
from foragent.orch_tick_outcome import step
from foragent.state import StateStore

SPAWN_SPACING = timedelta(minutes=2)
SKILL = "/fa-orchestrator"
AGENT = ".claude/agents/fa-orchestrator.md"
# A live orchestrator without a fresh heartbeat is noted at most this often.
SILENT_NOTE_SPACING = timedelta(hours=1)


def _record_path(store: StateStore) -> Path:
    return store.root / "orch-spawn.json"


def _alive(store: StateStore, now: datetime) -> bool:
    last = store.heartbeats()["orchestrator"]
    return last is not None and (now - last).total_seconds() <= ALIVE_SECONDS


def _session_alive(store: StateStore, root: Path) -> bool:
    """The heartbeat's recorded session, or a gate-spawned session, is still running.

    The recorded session must match its PID and start time, so a reused PID is
    not a live orchestrator. A plain Claude at the repository root (a planning
    or lane session) carries no ``/fa-orchestrator`` argument and does not count.
    """
    session = store.heartbeat_session("orchestrator")
    if session is not None and processes.recorded_identity_evidence(
            session["pid"], session["start_time"], fresh=True).alive:
        return True
    return processes.orchestrator_process_alive(root, SKILL)


def _note_silent(store: StateStore, now: datetime) -> None:
    """Record one orch event per hour for a live orchestrator without a heartbeat."""
    path = store.root / "orch-silent-note.json"
    with store._state_lock():
        try:
            last = datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["at"])
            if now - last < SILENT_NOTE_SPACING:
                return
        except (OSError, ValueError, KeyError, TypeError):
            pass
        store._write_json(path, {"at": now.isoformat()})
    append_event(store, now, None, "orch-silent",
                 "orchestrator alive without heartbeat; no second orchestrator opened")


def _spaced(store: StateStore, now: datetime) -> bool:
    try:
        last = datetime.fromisoformat(json.loads(_record_path(store).read_text(encoding="utf-8"))["spawned_at"])
        return now - last < SPAWN_SPACING
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _record_session(store: StateStore, now: datetime, screen: str, pid) -> None:
    """Add the launcher's PID and its start time to the spawn record, when known."""
    if not isinstance(pid, int) or isinstance(pid, bool):
        return
    try:
        row = processes.read_process_identity(pid)
    except Exception:  # noqa: BLE001 - the identity is optional evidence
        row = None
    if row is None:
        return
    store._write_json(_record_path(store), {"spawned_at": now.isoformat(), "screen": screen,
                                            "pid": pid, "start_time": row.start_time})


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
    # The role agent loads its skill and project memory when the project defines it.
    agent = ["--agent", "fa-orchestrator"] if (root / AGENT).is_file() else []
    session = shlex.join([claude, *agent, SKILL])
    if orca := _orca(store, root):
        return "orca", [orca, "terminal", "create", "--worktree", f"path:{root}",
                        "--title", title, "--command", session]
    tmux = shutil.which("tmux") if os.environ.get("TMUX") else None
    if tmux:
        return "tmux", [tmux, "new-window", "-d", "-n", title, "-c", str(root), session]
    return "background", [claude, "--bg", "-n", title, *agent, SKILL]


def ensure_orchestrator(repo_root: Path, store: StateStore, config: Config, *, now: datetime,
                        spacing: bool = True, why: list | None = None) -> str | None:
    """Open one orchestrator if needed; return the screen used, or None.

    ``spacing=False`` is the explicit planning-session call (``fa orch ensure``,
    see ``orch_ensure.ensure_explicit``, which appends why it opened nothing to ``why``);
    the gate's default path below is unchanged.
    """
    if not spacing:
        from foragent.orch_ensure import ensure_explicit  # Late: orch_ensure imports this module.
        return ensure_explicit(repo_root, store, config, now, why)
    if os.environ.get("FORAGENT_ORCH_SPAWN") == "off":
        return None
    if _alive(store, now) or not _approved_work(store):
        return None
    if _session_alive(store, repo_root.resolve()):
        _note_silent(store, now)
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
            result = subprocess.run(argv, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, check=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        # The session is open from here on: later bookkeeping failures must not hide it,
        # or the tick would treat this as no spawn and push beside it (#129).
        try:
            _record_session(store, now, screen, getattr(result, "pid", None))
        except Exception:  # noqa: BLE001 - the PID record is optional evidence
            pass
    language = config.language
    try:
        notify.send(config, text("orch_spawn_title", language),
                    text("orch_spawn_opened", language, repo=config.repo or root.name, screen=screen))
    except Exception:  # noqa: BLE001 - the session opened even when its push fails
        pass
    return screen


def on_tick(ctx, report):
    """Open an orchestrator when needed and note it in ``report``; never raise. Returns ``report``."""
    _spawn(ctx, report)
    return report


def _spawn(ctx, report) -> str | None:
    """``on_tick``'s decision; returns the screen used, or None."""
    language = "en"
    try:
        language = ctx.config.language
        screen = ensure_orchestrator(ctx.repo_root, ctx.store, ctx.config, now=ctx.now())
    except Exception as exc:  # noqa: BLE001 - the optional spawn must not stop the tick
        report.actions.append(text("orch_spawn_failed", language, error=" ".join(str(exc).split())[:200]))
        return None
    if screen:
        report.actions.append(text("orch_spawn_tick", language, screen=screen))
    return screen


@contextmanager
def deciding(ctx, acquired: bool = True):
    """Wrap a whole gate tick (inside its lock) so the stale-files scan and spawn decision run on every exit.

    The decision reads only local facts (heartbeats, active runs, approvals), so
    it does not depend on any tick step: a GitHub wait or a failed heartbeat,
    fetch, refresh, recovery or budget step all still open a missing
    orchestrator. Yields ``keep(report) -> report`` for the tick's result; on an
    error the spawn runs and the original error propagates unchanged. A tick
    that did not get the lock touches nothing; the tick holding it decides.
    """
    kept = []
    ctx._push_ready = False  # #129: set by orch_push.refresh_waited once this tick passes its wait check.

    def keep(report):
        kept.append(report)
        return report

    if not acquired:
        yield keep
        return
    try:
        yield keep
    except Exception:
        try:  # Local facts only; a failed scan must not replace the tick's own error.
            with step("stale_files"):
                orch_stale_files.report(ctx)
        except Exception:
            pass
        with step("orchestrator_spawn"):
            on_tick(ctx, SimpleNamespace(actions=[]))  # The tick's report is lost with the error.
        raise
    with step("binding_record"):  # D96: the tick is the binding event's only writer.
        orch_binding.record_tick(ctx)
    # Stale Files are facts for the orchestrator, so they are read before its spawn decision.
    with step("stale_files"):
        orch_stale_files.report(ctx)
    report = kept[-1] if kept else SimpleNamespace(actions=[])
    with step("orchestrator_spawn"):
        screen = _spawn(ctx, report)
    # #129: only a tick past its GitHub refresh and wait check pushes, after the spawn decision.
    if getattr(ctx, "_push_ready", False):
        with step("blocked_push"):
            language = ctx.config.language
            try:
                if line := orch_push.tick_line(*orch_push.after_tick(ctx, spawned=bool(screen)), language):
                    report.actions.append(line)
            except Exception as exc:  # noqa: BLE001 - the push must not fail a finished tick
                report.actions.append(text("push_blocked_failed", language,
                                           error=" ".join(str(exc).split())[:200]))
