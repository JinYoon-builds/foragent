"""Claude hooks preserve unswept transcript bytes before starting a detached worker.

This module owns the immutable sweep jobs; workers alone append completion markers.
Candidate judgments belong to a fresh Claude session, never this hook.
"""

from contextlib import contextmanager
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from uuid import uuid4

from foragent.i18n import text

EVENTS = ("PreCompact", "SessionEnd", "UserPromptSubmit")
EVERY = 20


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def save(path, value):
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install(root):
    from foragent.config import load_config
    from foragent.order import PlanError

    path = root / ".claude/settings.local.json"
    with locked(path.with_name("decision-hooks.lock")):
        try:
            settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            hooks = settings.setdefault("hooks", {})
            if not isinstance(settings, dict) or not isinstance(hooks, dict):
                raise ValueError
            command = shlex.join(["fa", "decisions", "_sweep", "--repo-root", str(root.resolve())])
            for event in EVENTS:
                groups = hooks.setdefault(event, [])
                if not isinstance(groups, list):
                    raise ValueError
                if not any(hook.get("command") == command for group in groups
                           for hook in group.get("hooks", [])):
                    groups.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
        except (ValueError, AttributeError, TypeError) as exc:
            raise PlanError(text("decision_sweep_settings", load_config(root).language, path=path)) from exc
        save(path, settings)


def person(row):
    """Transcript schema distinguishes person content from user-role tool results."""
    if row.get("type") != "user" or row.get("isMeta"):
        return False
    content = row.get("message", {}).get("content")
    return isinstance(content, str) or isinstance(content, list) and any(
        block.get("type") in {"text", "image"} for block in content if isinstance(block, dict))


def hook(root):
    # Check before touching stdin, the project or state: launched sessions cannot recurse.
    if "FORAGENT_ROLE" in os.environ:
        return 0
    try:
        payload = json.load(sys.stdin)
        if payload.get("hook_event_name") not in EVENTS or not (root / ".foragent.yml").is_file():
            return 0
        from foragent.commands import repo_store
        _config, _repo, store = repo_store(root.resolve())
        source = Path(payload["transcript_path"]).resolve()
        identity = sha256(f"{source}\0{payload['session_id']}".encode()).hexdigest()
        directory = store.root / "decision-sweeps" / identity
        with locked(directory / "queue.lock"):
            jobs = sorted(directory.glob("[0-9]*.json"))
            offset = json.loads(jobs[-1].read_text())["end"] if jobs else 0
            data = source.read_bytes()
            # Only complete JSONL records are durable. The next hook sees a partial tail.
            end = data.rfind(b"\n") + 1
            segment = data[offset:end]
            rows = [json.loads(line) for line in segment.splitlines() if line.strip()]
            count = sum(person(row) for row in rows)
            if count and (payload["hook_event_name"] != "UserPromptSubmit" or count >= EVERY):
                save(directory / f"{end:020d}.json", {
                    "root": str(root.resolve()), "source": str(source), "start": offset, "end": end,
                    "transcript": segment.decode("utf-8"),
                })
            # A new planning session also wakes jobs held at the previous session's end.
            pending = [job for job in directory.parent.glob("*/[0-9]*.json")
                       if not job.with_suffix(".done").exists()]
            if pending:
                env = {**os.environ, "FORAGENT_ROLE": "launched"}
                env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(Path(__file__).resolve().parents[1]),
                                                                 env.get("PYTHONPATH")]))
                with (directory / "worker.log").open("a", encoding="utf-8") as log:
                    subprocess.Popen([sys.executable, "-m", "foragent.decision_sweep_worker", str(directory.parent)],
                                     cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=True)
    except (OSError, ValueError, KeyError, TypeError):
        # Hooks never interrupt planning; queued jobs remain for the next event.
        return 0
    return 0


def confirm(store, candidate, content, boundary, language):
    """PM's confirmation becomes a normal waiting note; candidates stay outside flush."""
    from foragent import order_notes
    from foragent.order import PlanError

    row = next((row for row in order_notes.entries(store, order_notes.CANDIDATES)
                if row.get("id") == candidate), None)
    if (row is None or not content.strip() or len(content.splitlines()) != 1
            or not boundary.strip() or boundary == "decision-sweep"):
        raise PlanError(text("decision_sweep_invalid", language))
    body = text("decision_sweep_confirmed", language, decision=content,
                quote=row["text"], source=row.get("source", ""))
    with locked(store.root / "decision-confirm.lock"):
        previous = next((entry for entry in reversed(order_notes.entries(store))
                         if entry.get("confirmed_candidate") == candidate), None)
        if previous is not None and previous["text"] == body and previous["boundary"] == boundary:
            return {**previous, "status": "waiting"}
        return order_notes.append(
            store, kind="decision", boundary=boundary, language=language,
            source=row.get("source", ""), confirmed_candidate=candidate,
            content=body, replaces=previous["id"] if previous else None,
        )
