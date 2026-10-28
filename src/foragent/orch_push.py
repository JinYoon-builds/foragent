"""Push once when work becomes blocked, and once when the orchestrator is off while work runs (#129).

The gate calls ``after_tick`` after its spawn decision, only on a tick that got
past the GitHub refresh and the following wait check. Writers:

- ``<state>/notify-blocked.json`` is written only here. It holds the blocked set
  this push saw last (issue -> reason) and whether the current outage was pushed.
  An issue that leaves the set and comes back is pushed again.
- Each sent push appends ``push-blocked`` (one per issue) or ``push-orch-off``
  events to the local orch-events journal. A failed or suppressed push records
  nothing, so the next completing tick sends it.
"""

from datetime import datetime
import json
from pathlib import Path

from foragent import notify
from foragent.commands.orch import ALIVE_SECONDS
from foragent.i18n import text
from foragent.labels import BLOCKED, NEEDS_HUMAN
from foragent.orch_events import append_event
from foragent.state import StateStore
from foragent.tracker import local_issue_models

PUSH_BLOCKED = "push-blocked"
PUSH_ORCH_OFF = "push-orch-off"


def _state_path(store: StateStore) -> Path:
    return store.root / "notify-blocked.json"


def _read_state(store: StateStore) -> tuple[dict[int, str] | None, bool]:
    """The last saved blocked set (None when never saved) and the outage-pushed mark."""
    try:
        data = json.loads(_state_path(store).read_text(encoding="utf-8"))
        blocked = {int(n): str(reason) for n, reason in data["blocked"].items()}
        return blocked, bool(data.get("orch_off_pushed", False))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None, False


def _save_state(store: StateStore, saved: dict[str, str], orch_off_pushed: bool) -> None:
    with store._state_lock():
        store._write_json(_state_path(store), {"blocked": saved, "orch_off_pushed": orch_off_pushed})


def _journal(store: StateStore) -> list[dict]:
    try:
        lines = (store.root / "orch-events.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def tick_line(issues: list[int], outage: bool, language: str) -> str | None:
    """The gate report's line for what this tick pushed, or None when nothing went out."""
    items = [f"#{n}" for n in issues] + ([text("push_blocked_tick_orch_off", language)] if outage else [])
    return text("push_blocked_tick", language, items=", ".join(items)) if items else None


def blocked_now(store: StateStore, events: list[dict]) -> dict[int, tuple[str, str]]:
    """Open issues whose cached labels include blocked or needs-human: number -> (label, reason).

    The reason is the local flag reason; for needs-human alone it is the newest
    escalate event summary. Either is the empty string when there is no record.
    """
    escalations: dict[int, str] = {}
    for event in events:
        if event.get("kind") == "escalate" and isinstance(event.get("issue"), int):
            escalations[event["issue"]] = str(event.get("summary", ""))
    current = {}
    for issue in local_issue_models(store):
        waiting = issue.labels & {BLOCKED, NEEDS_HUMAN}
        if issue.state != "open" or issue.is_pull_request or not waiting:
            continue
        flag = store.load_flag(issue.number)
        if BLOCKED in waiting:
            current[issue.number] = (BLOCKED, flag.reason if flag is not None else "")
        else:
            current[issue.number] = (NEEDS_HUMAN, escalations.get(issue.number, ""))
    return current


def _orch_off(ctx, now: datetime) -> tuple[bool, bool]:
    """(off while work runs, fresh heartbeat). A missing heartbeat is off."""
    from foragent import orch_spawn  # Late: orch_spawn calls this module.

    last = ctx.store.heartbeats()["orchestrator"]
    fresh = last is not None and (now - last).total_seconds() <= ALIVE_SECONDS
    if fresh or not ctx.store.active_runs():
        return False, fresh
    return not orch_spawn._session_alive(ctx.store, ctx.repo_root.resolve()), fresh


def after_tick(ctx, spawned: bool) -> tuple[list[int], bool]:
    """Send at most one combined push for this tick; return the issue numbers and whether the outage was sent.

    ``spawned`` means the gate opened an orchestrator this tick: its own push is
    the only one, and nothing due here is recorded.
    """
    store, config, now = ctx.store, ctx.config, ctx.now()
    language = config.language
    events = _journal(store)
    previous, orch_off_pushed = _read_state(store)
    pushed_reason: dict[int, str] = {}
    for event in events:
        if event.get("kind") == PUSH_BLOCKED and isinstance(event.get("issue"), int):
            pushed_reason[event["issue"]] = str(event.get("reason", ""))
    current = blocked_now(store, events)
    due = sorted(n for n, (_, reason) in current.items()
                 if n not in pushed_reason or pushed_reason[n] != reason
                 or (previous is not None and n not in previous))
    off, fresh = _orch_off(ctx, now)
    if fresh:
        orch_off_pushed = False  # The orchestrator came back; the next outage pushes again.
    outage_due = off and not orch_off_pushed

    sent = False
    if (due or outage_due) and not spawned:
        lines = []
        for n in due:
            label, reason = current[n]
            lines.append(text("push_blocked_issue", language, number=n, label=label,
                              reason=f": {reason}" if reason else ""))
        if outage_due:
            lines.append(text("push_blocked_orch_off", language, repo=config.repo or ctx.repo_root.name))
        link = (f"https://github.com/{config.repo}/issues/{due[0]}"
                if due and not outage_due and config.repo else None)
        try:
            sent = notify.send(config, text("push_blocked_title", language), "\n".join(lines), link) is not False
        except Exception:  # noqa: BLE001 - a failed push is retried on the next completing tick
            sent = False
    # An issue due but not sent stays out of the saved set, so the next tick sees it as new.
    unsent = set() if sent else set(due)
    saved = {str(n): reason for n, (_, reason) in sorted(current.items()) if n not in unsent}
    if sent and outage_due:
        orch_off_pushed = True
    # After a sent push each record is written on its own, so one failed write does not
    # drop the others; the first error is raised once all were tried.
    writes = []
    if sent:
        for n in due:
            label, reason = current[n]
            writes.append(lambda n=n, label=label, reason=reason: append_event(
                store, now, n, PUSH_BLOCKED, f"#{n}: pushed {label}" + (f"; {reason}" if reason else ""),
                reason=reason, label=label))
        if outage_due:
            writes.append(lambda: append_event(store, now, None, PUSH_ORCH_OFF,
                                               "pushed: orchestrator off while work runs"))
    writes.append(lambda: _save_state(store, saved, orch_off_pushed))
    error = None
    for write in writes:
        try:
            write()
        except Exception as exc:  # noqa: BLE001 - raised below, after the other writes
            error = error or exc
    if error is not None:
        raise error
    return (due, outage_due) if sent else ([], False)
