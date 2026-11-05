"""Explicit architect topics, kept independently of the latest role process record.

This module alone writes ``architect-topics/*.json``, under ``architect-topics.lock``.
Turns keep executor identity and the request ids produced during that turn. Decisions
stay authoritative in the inbox; an accepted decision terminates the topic, while
consumed rebuttals are derived from turns. The inbox owns the PM question (``ask_once``).
Light per-merge-group reviews use their existing one-shot path, outside this launcher.
"""

from contextlib import contextmanager
import json
from uuid import uuid4

from foragent import orch_inbox
from foragent.state_locks import _file_lock


MAX_TURNS = 3


def _path(store, topic):
    return store.root / "architect-topics" / f"{topic}.json"


def _lock(store):
    return _file_lock(store.root / "architect-topics.lock")


def _read(store, topic):
    return json.loads(_path(store, topic).read_text(encoding="utf-8"))


def _requests(store):
    return {row["id"]: row for row in orch_inbox.requests(store) if row["from"] == "architect"}


def _closed(topic, requests):
    return topic.get("closed_by") or any(requests.get(request, {}).get("accepted") is True
               for turn in topic["turns"] for request in turn.get("proposals", []))


def _consumed(topic, request):
    return any(turn.get("rebuttal") == request for turn in topic["turns"])


@contextmanager
def launch(store, topic, started_at, why, executor, rebuttal=None):
    """Reserve a turn before Popen, and undo only an unlaunched reservation on failure.

    The caller holds the architect call lock; the topic lock is released before Popen
    so even a child that finishes before Popen returns can record its result.
    """
    if not topic:
        yield {}
        return
    with _lock(store):
        requests = _requests(store)
        data = ({"id": uuid4().hex, "why": why, "executor": executor, "turns": []}
                if topic is True else _read(store, topic))
        turns = data["turns"]
        if turns:
            if (_closed(data, requests) or _consumed(data, rebuttal)
                    or rebuttal not in turns[-1].get("proposals", []) or len(turns) >= MAX_TURNS):
                raise orch_inbox.InboxError("This architect rebuttal has already been handled or its topic closed")
            if data["executor"] != executor or not turns[-1].get("session_id"):
                raise orch_inbox.InboxError("The architect's executor session is unavailable; cannot resume this topic")
        turn = {"started_at": started_at, "before": list(requests), "rebuttal": rebuttal,
                "resume_session_id": turns[-1]["session_id"] if turns else None}
        turns.append(turn)
        store._write_json(_path(store, data["id"]), data)
        context = {"topic": data["id"], "topic_turn": len(turns) - 1}
    try:
        yield context
    except BaseException:
        with _lock(store):
            data = _read(store, context["topic"])
            if len(data["turns"]) == context["topic_turn"] + 1 and "exit_code" not in data["turns"][-1]:
                data["turns"].pop()
                store._write_json(_path(store, data["id"]), data)
        raise


def options(store, record):
    """Executor options for this turn; a fresh topic has no resume option."""
    if not record or not record.get("topic"):
        return {}
    turn = _read(store, record["topic"])["turns"][record["topic_turn"]]
    session = turn["resume_session_id"]
    return {"resume_session_id": session} if session else {}


def finish(store, record, result, code):
    """Persist the real executor id and associate only this turn's new proposals."""
    if not record or not record.get("topic"):
        return
    with _lock(store):
        data = _read(store, record["topic"])
        turn = data["turns"][record["topic_turn"]]
        turn.update(exit_code=code, session_id=result.session_id if result is not None else None,
                    proposals=[request for request in _requests(store) if request not in turn["before"]])
        store._write_json(_path(store, data["id"]), data)


def respond(root, store, config, request_id):
    """A decision command resumes once, or leaves both sides in one PM question."""
    from foragent import orch_architect

    with _lock(store):
        requests = _requests(store)
        request = requests.get(request_id)
        if request is None or type(request.get("accepted")) is not bool:
            return None
        topic = None
        for path in (store.root / "architect-topics").glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            if any(request_id in turn.get("proposals", []) for turn in data["turns"]):
                topic = data
                break
        if topic is None:
            return None
        if request["accepted"]:
            topic["closed_by"] = request_id
            store._write_json(_path(store, topic["id"]), topic)
            return None
        if (_closed(topic, requests) or _consumed(topic, request_id)
                or request_id not in topic["turns"][-1].get("proposals", [])):
            return None
        key = f"architect-topic:{topic['id']}"
        if any(row.get("key") == key for row in orch_inbox.questions(store, include_closed=True)):
            return None
        why = (f"{topic['why']}\n\nLast architect proposal ({request_id}):\n{request['text']}\n\n"
               f"Orchestrator rebuttal:\n{request['answer']}")
        if len(topic["turns"]) >= MAX_TURNS:
            ko = config.language == "ko"
            question = ("아키텍트와 오케스트레이터가 3회 논의 후에도 합의하지 못했습니다.\n\n" if ko else
                        "The architect and orchestrator still disagree after three architect turns.\n\n") + why
            options = (["아키텍트의 마지막 제안 채택", "오케스트레이터의 반론 채택", "보류하고 근거 추가 (추천)"]
                       if ko else ["Adopt the architect's last proposal", "Keep the orchestrator's rebuttal",
                                   "Defer and gather more evidence (recommended)"])
            orch_inbox.ask_once(store, key, question, options, 3)
            return "Architect topic is waiting for the PM's decision."
    try:
        return orch_architect.call(root, store, config, why, store._now(),
                                   topic=topic["id"], rebuttal=request_id)
    except (orch_architect.ArchitectRefused, orch_inbox.InboxError) as exc:
        return f"Architect continuation pending: {exc}. Retry the same role-decision command."
