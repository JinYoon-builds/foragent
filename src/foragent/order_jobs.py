"""Durable background preread/approval work a PM session submits and FA alone runs (#1487).

A session only submits: ``submit`` stores a content-bound snapshot of the plan and every
file the order publishes, then appends one ``submitted`` row with the explicit
authorization. Nothing here starts an agent. The worker that FA runs (a later issue)
reads the snapshot with ``snapshot``, never the session's plan file, and appends its
progress with ``record``. ``jobs`` and ``pending`` derive each job's state from the log,
so the work survives the PM shell, the plan file and a later PM takeover.

The contract (directory, row shapes, states) is written in docs/order.md.
"""

from base64 import b64decode, b64encode
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from foragent.i18n import text
from foragent.order import PlanError, validate_plan

FOLDER = "order-jobs"
LOG = "jobs.jsonl"
# Progress events the worker appends after ``submitted``; these end a job.
FINISHED = {"approved", "clean", "question", "failed", "cancelled"}


def _folder(store) -> Path:
    path = store.root / FOLDER
    path.mkdir(parents=True, exist_ok=True)
    return path


def _encoded(files: dict[str, str | bytes]) -> dict[str, dict[str, str]]:
    return {path: ({"text": content} if isinstance(content, str)
                   else {"base64": b64encode(content).decode("ascii")})
            for path, content in sorted(files.items())}


def _store_snapshot(store, plan: dict, files: dict[str, str | bytes]) -> str:
    """Write the snapshot once under its own content hash and return that hash."""
    data = json.dumps({"plan": plan, "files": _encoded(files)}, ensure_ascii=False,
                      sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    path = _folder(store) / f"{digest}.json"
    if path.is_file() and not path.is_symlink() and path.read_bytes() == data:
        return digest
    temporary = path.with_name(f".{digest}.{uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return digest


def _append(store, row: dict) -> dict:
    """One O_APPEND write per row; a leading newline isolates an interrupted earlier tail."""
    data = b"\n" + json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
    descriptor = os.open(_folder(store) / LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        written = 0
        while written < len(data):
            written += os.write(descriptor, data[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return row


def rows(store) -> list[dict]:
    """Every complete log row in order; a torn last write is skipped."""
    try:
        raw = (store.root / FOLDER / LOG).read_bytes()
    except FileNotFoundError:
        return []
    result = []
    for line in raw.split(b"\n")[:-1]:
        try:
            row = json.loads(line) if line.strip() else None
        except (ValueError, UnicodeError):
            continue
        if isinstance(row, dict) and isinstance(row.get("job"), str):
            result.append(row)
    return result


def jobs(store) -> list[dict]:
    """Each submitted job with its latest progress event as ``state``, oldest first."""
    found: dict[str, dict] = {}
    for row in rows(store):
        if row.get("event") == "submitted":
            found.setdefault(row["job"], {**row, "state": "submitted", "events": []})
        elif row["job"] in found:
            found[row["job"]]["state"] = row.get("event")
            found[row["job"]]["events"].append(row)
    return list(found.values())


def pending(store) -> list[dict]:
    """Jobs FA has not finished, in the shape ``fa order context`` shows."""
    return [{key: job[key] for key in ("job", "plan_id", "title", "snapshot", "approve_if_clean",
                                       "session", "submitted_at", "state")}
            for job in jobs(store) if job["state"] not in FINISHED]


def snapshot(store, job: str) -> tuple[dict, dict[str, str | bytes]]:
    """The plan and published files exactly as submitted, checked against their hash."""
    row = next((item for item in jobs(store) if item["job"] == job), None)
    if row is None:
        raise KeyError(job)
    data = (store.root / FOLDER / f"{row['snapshot']}.json").read_bytes()
    if hashlib.sha256(data).hexdigest() != row["snapshot"]:
        raise ValueError(f"order job {job}: snapshot does not match its hash")
    content = json.loads(data)
    files = {path: value["text"] if "text" in value else b64decode(value["base64"])
             for path, value in content["files"].items()}
    return content["plan"], files


def record(store, job: str, event: str, **facts) -> dict:
    """Append one progress event for the worker; the session never calls this."""
    if event == "submitted":
        raise ValueError("only submit() records a submission")
    return _append(store, {"job": job, "event": event, "at": datetime.now(UTC).isoformat(), **facts})


def _require_holder(store, session: str | None, language: str) -> None:
    """Refuse unless ``session`` holds this person's PM seat now.

    Only the holder hands work to FA: a session without an id, one that never took the
    seat, one that lost it, or one whose standing cannot be read is refused. "Not
    displaced" is not enough, because a session that never entered is not displaced.
    """
    from foragent import status_pm

    state = status_pm.authority(store, session)
    if state == "holder":
        return
    if not session:
        raise PlanError(text("pm_seat_no_session", language))
    if state == "unknown":
        try:
            status_pm.read(store)
        except status_pm.SeatUnreadable as exc:
            raise PlanError(text("pm_seat_unreadable", language, reason=str(exc))) from exc
    seat = status_pm.read(store)
    if state == "displaced" and seat is not None:
        raise PlanError(text("pm_seat_moved", language, holder=status_pm.short(seat["holder"]["session_id"])))
    raise PlanError(text("order_job_not_pm", language))


def submit(store, plan: dict, root: Path, config, *, approve_if_clean: bool,
           session: str | None) -> dict:
    """Queue preread (and approval only if clean, when authorized) and return at once.

    The seat check and the ``submitted`` row share the seat record's lock, so the seat
    cannot move to another session between the check and the write.
    """
    _require_holder(store, session, config.language)  # Fail before any snapshot is written.
    try:
        files = validate_plan(plan, root, config)
    except ValueError as exc:
        raise PlanError(str(exc)) from exc
    digest = _store_snapshot(store, plan, files)
    with store._state_lock():
        _require_holder(store, session, config.language)
        for job in pending(store):
            # The same content with the same authorization is already waiting: one job, not two.
            if job["snapshot"] == digest and job["approve_if_clean"] == approve_if_clean:
                return {**job, "existing": True}
        row = _append(store, {"job": uuid4().hex, "event": "submitted", "snapshot": digest,
                              "plan_id": plan["id"], "title": plan["title"],
                              "approve_if_clean": approve_if_clean, "session": session,
                              "submitted_at": datetime.now(UTC).isoformat()})
    return {**row, "state": "submitted", "existing": False}


def run(args, root: Path) -> int:
    """``fa order preread PLAN --background [--approve-if-clean]``: submit and return."""
    from foragent import status_pm
    from foragent.commands import facade, repo_store

    cli = facade()
    config, _repo, store = repo_store(root)
    job = submit(store, cli.order.read_plan(args.plan), root, config,
                 approve_if_clean=args.approve_if_clean, session=status_pm.session_from_env())
    if args.json:
        print(cli.json.dumps({"job": job}, ensure_ascii=False))
    else:
        key = "order_job_existing" if job["existing"] else "order_job_submitted"
        print(text(key + ("_approve" if job["approve_if_clean"] else ""), config.language,
                   title=job["title"], job=job["job"][:12]))
    return 0
