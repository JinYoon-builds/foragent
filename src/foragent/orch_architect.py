"""Standing-role sessions the orchestrator starts with `fa orch role <name> --why`.

`fa orch architect --why` is the architect instance of the same command. A tick also
calls it when an order finishes (``orch_role_schedule``, D99); no other path starts a role
session. ``call`` is the only writer of ``<state>/<name>.json`` (pid, process start time, started_at, why)
and of the ``<name>-call`` event. The session itself, started as the hidden
``fa gate run-architect --role <name>``, is the only writer of its ``<name>-done``
event. Roles never message a person: proposals reach the orchestrator through
``fa orch request``. Roles are read from ``foragent.roles.load_roles`` (D95).

Every start takes one machine slot through ``capacity.hold_workload``, then passes that
slot's one workload record to the session's pid, so the worker counts once for as long as
it lives. The session removes that record when it ends, and a record left by a crash stops
counting with its pid; the next start removes it. A start that cannot record its session
stops that session, because nothing else could count or find it.
"""

from contextlib import contextmanager
import contextvars
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Iterator

from foragent import capacity, capacity_sessions, executors, processes, review_notes
from foragent.config import Config
from foragent.i18n import text
from foragent.launcher import gate_environment
from foragent.orch_events import append_event
from foragent.orch_role_prompt import ARCHITECT, REFACTOR, _entry, _has_agent, build_prompt  # noqa: F401
from foragent.roles import load_roles
from foragent.state import StateStore


class ArchitectRefused(Exception):
    """The role session did not start; the message is for a person."""


def record_path(store: StateStore, role: str = ARCHITECT) -> Path:
    return store.root / f"{role}.json"


def log_path(store: StateStore, started_at: str, role: str = ARCHITECT) -> Path:
    stamp = datetime.fromisoformat(started_at).astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return store.root / "logs" / f"{role}-{stamp}.log"


def read_record(store: StateStore, role: str = ARCHITECT) -> dict | None:
    try:
        record = json.loads(record_path(store, role).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def _probe(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def session_alive(record: dict | None) -> bool:
    """The recorded pid with its start time when readable, otherwise the signal-0 probe alone."""
    pid = record.get("pid") if record else None
    if type(pid) is not int or pid <= 0:
        return False
    start = record.get("start_time")
    if isinstance(start, (int, float)):
        try:
            identity = processes.read_process_identity(pid)
        except PermissionError:
            return _probe(pid)
        return identity is not None and identity.start_time == start
    return _probe(pid)


def _start_time(pid: int) -> float | None:
    try:
        identity = processes.read_process_identity(pid)
    except PermissionError:
        return None
    return identity.start_time if identity is not None else None


@contextmanager
def _call_lock(store: StateStore, role: str = ARCHITECT) -> Iterator[None]:
    """Two calls at once cannot both see no live session and both launch."""
    store.root.mkdir(parents=True, exist_ok=True)
    with (store.root / f"{role}.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _remove_workload(record: dict | None) -> None:
    """Remove the machine-slot record that a role record names, if it names one."""
    workload = record.get("workload") if record else None
    if isinstance(workload, str):
        capacity_sessions.remove(Path(workload))


def _pass_slot(store: StateStore, role: str, pid: int, target: Path) -> None:
    """Turn this command's held slot into the session's record at ``target``.

    The one record changes owner in place, so no reader counts the worker twice. It then
    moves to the session's own name, which this command's hold does not remove. The machine
    lock keeps every admission out until the move ends, so none can see the record missing.
    """
    with capacity.machine_lock(store):
        held = next(item.path for item in capacity_sessions.read(store.root)
                    if item.pid == os.getpid() and item.kind == "role" and item.role == role)
        capacity_sessions.add(store, held, pid=pid, kind="role", role=role)
        os.replace(held, target)


def _say(key: str, role: str, language: str, **values) -> str:
    """The architect keeps its own wording; every other role shares the role_* strings."""
    if role == ARCHITECT:
        return text(f"architect_{key}", language, **values)
    return text(f"role_{key}", language, role=role, **values)


def call(root: Path, store: StateStore, config: Config, why: str, now: datetime,
         role: str = ARCHITECT, short: str | None = None) -> str:
    """Start one session of ``role`` and return the line for the caller; refuse with ArchitectRefused."""
    language = config.language
    if _entry(config, role) is None:
        raise ArchitectRefused(text("role_unknown", language, role=role,
                                    roles=", ".join(load_roles(config)) or "-"))
    why = why.strip()
    if not why:
        raise ArchitectRefused(_say("why_required", role, language))
    # The caller writes the one-line reason; it is stored as given, never derived from why.
    short = (short or "").strip() or None
    if short is not None and "\n" in short:
        raise ArchitectRefused(text("role_short_one_line", language))
    until = store.executor_pause_until(config.executor, now)
    if until is not None:
        raise ArchitectRefused(_say("paused", role, language, executor=config.executor,
                                    until=until.isoformat()))
    with _call_lock(store, role):
        record = read_record(store, role)
        if session_alive(record):
            raise ArchitectRefused(_say("running", role, language, pid=record["pid"],
                                        started_at=record.get("started_at", "?")))
        _remove_workload(record)  # The session is gone, so a crash's leftover is no one's slot.
        started_at = now.astimezone(UTC).isoformat()
        log = log_path(store, started_at, role)
        log.parent.mkdir(parents=True, exist_ok=True)

        def start() -> subprocess.Popen:
            with capacity.hold_workload(store, config, wait_s=0, kind="role", role=role, now=lambda: now):
                with log.open("ab") as stream:
                    process = subprocess.Popen(
                        [sys.executable, "-m", "foragent.cli", "gate", "run-architect",
                         "--repo-root", str(root), "--started-at", started_at, "--why", why, "--role", role],
                        cwd=root, stdin=subprocess.DEVNULL, start_new_session=True,
                        stdout=stream, stderr=stream, env=gate_environment(),
                    )
                workload = capacity_sessions.new_path(store, process.pid)
                try:
                    _pass_slot(store, role, process.pid, workload)
                    store._write_json(record_path(store, role), {
                        "pid": process.pid, "start_time": _start_time(process.pid),
                        "started_at": started_at, "why": why, "workload": str(workload),
                        **({"short": short} if short else {}),
                    })
                except BaseException:
                    # No record can count or find this session, so it must not run. It has only just started.
                    process.kill()
                    process.wait()
                    capacity_sessions.remove(workload)
                    raise
            return process

        try:
            # A fresh context: the session is a new worker, so an outer hold of this thread cannot cover it.
            process = contextvars.Context().run(start)
        except TimeoutError as exc:
            raise ArchitectRefused(str(exc)) from exc
    append_event(store, now, None, f"{role}-call", why, why=why, pid=process.pid, log=str(log),
                 **({"short": short} if short else {}))
    return _say("started", role, language, pid=process.pid, log=log)


def session_options(config: Config, store: StateStore, root: Path | None = None,
                    role: str = ARCHITECT, entry: dict | None = None) -> dict:
    """The size-l tier's model and effort; Claude loads the role's agent when the project has it.

    Codex gets the state directory, and the role's memory directory when present, as writable roots.
    """
    tier = config.tiers["l"]
    entry = entry if entry is not None else _entry(config, role)
    if config.executor == "claude":
        options = {"model": tier.get("claude_model") or config.executor_model}
        if _has_agent(root, entry):
            options["agent"] = entry["agent"]
        return options
    memory = root / entry["memory"] if root is not None else None
    extra = (memory,) if memory is not None and memory.is_dir() else ()
    return {"model": config.executor_model, "effort": tier.get("codex_effort"),
            "add_dirs": (store.root, *extra)}


def events_path(store: StateStore, started_at: str, role: str = ARCHITECT) -> Path:
    """The session's JSON events beside its log, as Claude keeps its ``.stdout``."""
    return log_path(store, started_at, role).with_suffix(".stdout")


MESSAGE_LIMIT = 4000


def _keep_message(log: Path, message: str, role: str = ARCHITECT) -> str:
    """Append the session's final answer to its log; return the tail for the event."""
    message = message.strip()
    with log.open("a", encoding="utf-8") as stream:
        stream.write(f"\n--- {role} final message ---\n{message or '(empty)'}\n")
    return message[-MESSAGE_LIMIT:]


def run_session(root: Path, store: StateStore, config: Config, why: str, started_at: str,
                now, role: str = ARCHITECT) -> int:
    """Run the role's turn in the repository root; append one ``<role>-done`` event on exit."""
    code, message = 1, ""
    log = log_path(store, started_at, role)
    try:
        entry = _entry(config, role)
        if entry is None:
            raise ArchitectRefused(text("role_unknown", config.language, role=role,
                                        roles=", ".join(load_roles(config)) or "-"))
        options = session_options(config, store, root, role, entry)
        if config.executor != "claude":
            options["events_path"] = events_path(store, started_at, role)
        notes = review_notes.architect_section(store, role) if role == ARCHITECT else ""
        result = executors.executor_for(config.executor).run(
            worktree=root, prompt=build_prompt(why, root, config.executor, role, entry, review_notes=notes),
            timeout_s=config.max_runtime_minutes * 60, log_path=log, **options,
        )
        code = 1 if result.timed_out or result.exit_code is None else result.exit_code
        message = _keep_message(log, result.full_message or result.last_message or "", role)
    finally:
        first = message.splitlines()[0][:200] if message else ""
        try:
            # A success covers the input from before the session started; arrivals during it stay due.
            append_event(store, now(), None, f"{role}-done",
                         f"exit code {code}" + (f": {first}" if first else ""),
                         started_at=started_at, exit_code=code, message=message,
                         **({"covered_until": started_at} if code == 0 else {}))
        finally:
            record = read_record(store, role)
            if record is not None and record.get("started_at") == started_at:
                _remove_workload(record)
    return code
