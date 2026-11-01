"""Issue-owned copies of advice a worker will receive (retry hints, accepted talk messages).

The local hint file only waits for delivery and is removed once a prompt carries it, so
the issue comment is the durable work fact. A write GitHub cannot take now (rate limit,
GitHub wait, or any other error) is kept in the outbox, which each tick retries, so an
accepted message never loses its public copy.
"""

from __future__ import annotations

from foragent.tracker import RecordingTracker, TrackerError

TALK_MARKER = "<!-- foragent:talk -->"


def after_retry_hint(current: str | None, hint: str | None) -> str | None:
    """The pending advice after a retry; an omitted or unchanged hint adds nothing."""
    if not hint or not hint.strip() or hint == current:
        return current
    return f"{current.rstrip()}\n\n{hint}" if current and current.strip() else hint


def publish_retry(tracker, context, n: int, hint: str | None, record: str | None) -> str | None:
    """A retry's state line also owns its advice; review retries retain their public copy."""
    body = record
    if body is None and hint:
        body = "<!-- foragent:orch -->\nRetry advice:\n\n" + hint
    return publish(tracker, context, n, body) if body is not None else None


def publish(tracker, context, n: int, body: str) -> str | None:
    """Comment ``body`` on issue ``n`` or queue it; return the error that forced a queue, if any.

    ``context`` needs ``store``, ``config`` and ``now``.
    """
    try:
        RecordingTracker(tracker, context, queue_writes=True, accepted_advice=True).comment(n, body)
        return None
    except (TrackerError, ValueError, OSError) as exc:
        context.store.queue_github_write(n, "comment", lanes=context.config.lanes, body=body, accepted_advice=True)
        return " ".join(str(exc).split())[:200] or type(exc).__name__


def talk_body(message: str) -> str:
    return f"{TALK_MARKER}\n{message}"
