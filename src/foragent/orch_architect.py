"""The architect: one worker session the orchestrator starts with `fa orch architect --why`.

Only that command starts it; no tick, merge or schedule does. The command is the
only writer of ``<state>/architect.json`` (pid, process start time, started_at,
why) and of the ``architect-call`` event. The session itself, started as the
hidden ``fa gate run-architect``, is the only writer of its ``architect-done``
event. Proposals reach the orchestrator through ``fa orch request``.
"""

from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Iterator

from foragent import executors, processes
from foragent.config import Config
from foragent.i18n import text
from foragent.launcher import gate_environment
from foragent.orch_events import append_event
from foragent.state import StateStore

REFACTOR = "skills/fa-orchestrator/references/refactor.md"

PROMPT = """You are the architect for this repository. The orchestrator started you with this evidence:

{why}

Your role is only to propose. You never publish, edit or approve orders, and you do not change code,
labels or the board. The orchestrator judges each proposal and decides whether it becomes an order.

## Measure first

- Run `fa orch facts` for the hot files (overlap records and waits per file), the co-changed files and
  line counts. Use `fa orch facts --help` for its options.
- Run the project's tests with `pytest --durations=25` (for example `fa test --durations=25`) to find
  the slow tests.
- Follow the refactoring procedure in {refactor}.

## Nothing to change is a valid outcome

"Nothing to change" is a valid and often the best outcome. When the numbers show no clear gain, say so
in one line and stop without a request.

## Each proposal

- One proposal per file or concern.
- Every proposal cites fa orch facts numbers or a real incident (date and what happened).
- Every proposal states its expected measurable effect: the number you expect to move, from what to
  what, and how to measure it again.
- A split preserves behaviour through a compatibility facade: every module-level name stays importable
  and existing patches still reach the code. Each split order carries a line-limit acceptance test and
  reserves the file with `exclusive:` while it runs.
- Check every proposal against the state-ownership rules:
  1. One writer per piece of state. Others leave a request for the owner (for example a stop request)
     and never write it themselves; a fallback writer acts only after the owner is confirmed dead.
  2. Derive instead of store when the value can be computed from recorded facts (for example sleep
     credit from sleep and wake points).
  3. Append instead of overwrite (runs.jsonl style).
  Flag duplicated state (for example GitHub labels versus the local record) and name the reader that
  tolerates the lag.

## Deliver

Deliver each proposal through `fa orch request --from architect --text "<proposal>"`, one request per
proposal. Mark each proposal with its decision owner on its first line:

- `Decision: orchestrator` is an orchestrator decision. The orchestrator judges it and decides whether
  to turn it into an order.
- `Decision: person` is a person decision: lane boundary changes, anything irreversible or hard to undo,
  scope, or taste. The orchestrator forwards it to the person as a multiple-choice question with
  options and its recommendation.

Then write the proposal, its evidence and its expected measurable effect. End with a one-line summary
of what you proposed, or "Nothing to change.".
"""


class ArchitectRefused(Exception):
    """The architect did not start; the message is for a person."""


def record_path(store: StateStore) -> Path:
    return store.root / "architect.json"


def log_path(store: StateStore, started_at: str) -> Path:
    stamp = datetime.fromisoformat(started_at).astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return store.root / "logs" / f"architect-{stamp}.log"


def build_prompt(why: str) -> str:
    return PROMPT.format(why=why, refactor=REFACTOR)


def read_record(store: StateStore) -> dict | None:
    try:
        record = json.loads(record_path(store).read_text(encoding="utf-8"))
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
def _call_lock(store: StateStore) -> Iterator[None]:
    """Two calls at once cannot both see no live session and both launch."""
    store.root.mkdir(parents=True, exist_ok=True)
    with (store.root / "architect.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def call(root: Path, store: StateStore, config: Config, why: str, now: datetime) -> str:
    """Start one architect session and return the line for the caller; refuse with ArchitectRefused."""
    language = config.language
    why = why.strip()
    if not why:
        raise ArchitectRefused(text("architect_why_required", language))
    until = store.executor_pause_until(config.executor, now)
    if until is not None:
        raise ArchitectRefused(text("architect_paused", language, executor=config.executor,
                                    until=until.isoformat()))
    with _call_lock(store):
        record = read_record(store)
        if session_alive(record):
            raise ArchitectRefused(text("architect_running", language, pid=record["pid"],
                                        started_at=record.get("started_at", "?")))
        started_at = now.astimezone(UTC).isoformat()
        log = log_path(store, started_at)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as stream:
            process = subprocess.Popen(
                [sys.executable, "-m", "foragent.cli", "gate", "run-architect",
                 "--repo-root", str(root), "--started-at", started_at, "--why", why],
                cwd=root, stdin=subprocess.DEVNULL, start_new_session=True,
                stdout=stream, stderr=stream, env=gate_environment(),
            )
        store._write_json(record_path(store), {
            "pid": process.pid, "start_time": _start_time(process.pid),
            "started_at": started_at, "why": why,
        })
    append_event(store, now, None, "architect-call", why, why=why, pid=process.pid, log=str(log))
    return text("architect_started", language, pid=process.pid, log=log)


def session_options(config: Config, store: StateStore) -> dict:
    """The size-l tier's model and effort; Codex also gets the state directory as a writable root."""
    tier = config.tiers["l"]
    if config.executor == "claude":
        return {"model": tier.get("claude_model") or config.executor_model}
    return {"model": config.executor_model, "effort": tier.get("codex_effort"),
            "add_dirs": (store.root,)}


def events_path(store: StateStore, started_at: str) -> Path:
    """The session's JSON events beside its log, as Claude keeps its ``.stdout``."""
    return log_path(store, started_at).with_suffix(".stdout")


MESSAGE_LIMIT = 4000


def _keep_message(log: Path, message: str) -> str:
    """Append the session's final answer to its log; return the tail for the event."""
    message = message.strip()
    with log.open("a", encoding="utf-8") as stream:
        stream.write(f"\n--- architect final message ---\n{message or '(empty)'}\n")
    return message[-MESSAGE_LIMIT:]


def run_session(root: Path, store: StateStore, config: Config, why: str, started_at: str,
                now) -> int:
    """Run the architect turn in the repository root; append one architect-done event on exit."""
    code, message = 1, ""
    log = log_path(store, started_at)
    options = session_options(config, store)
    if config.executor != "claude":
        options["events_path"] = events_path(store, started_at)
    try:
        result = executors.executor_for(config.executor).run(
            worktree=root, prompt=build_prompt(why), timeout_s=config.max_runtime_minutes * 60,
            log_path=log, **options,
        )
        code = 1 if result.timed_out or result.exit_code is None else result.exit_code
        message = _keep_message(log, result.full_message or result.last_message or "")
    finally:
        first = message.splitlines()[0][:200] if message else ""
        append_event(store, now(), None, "architect-done",
                     f"exit code {code}" + (f": {first}" if first else ""),
                     started_at=started_at, exit_code=code, message=message)
    return code
