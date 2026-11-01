"""Pluggable notification delivery; failures are logged and isolated."""

import fcntl
import json
import logging
import os
import subprocess
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from foragent.config import Config
from foragent.state import StateStore


logger = logging.getLogger(__name__)

# Producer kinds (#1030). The producer names what it sends; nothing here reads the words.
DECISION_KINDS = ("question", "merge", "order")  # Open D71 questions, merge approvals, order cards.
STATUS_KINDS = ("blocked", "orchestrator-stopped", "silence", "summary", "status")
KINDS = DECISION_KINDS + STATUS_KINDS


@dataclass
class Sending:
    """What a producer is sending and from which checkout; after ``send``, why it failed."""
    kind: str | None
    root: Path | None = None
    error: str | None = None


_SENDING: ContextVar[Sending | None] = ContextVar("foragent_notify_sending", default=None)


@contextmanager
def sending(kind: str | None, root: Path | None = None):
    """Name the kind of every ``send`` in this block; the producer knows it, the words do not.

    ``root`` is the producer's checkout, whose origin names the repository when the
    configuration has none. ``send`` keeps its ``(config, title, line, link)`` shape, so
    replaced senders keep working.
    """
    current = Sending(kind, root)
    token = _SENDING.set(current)
    try:
        yield current
    finally:
        _SENDING.reset(token)


def checkout_repo(start: Path | None = None) -> str | None:
    """``owner/name`` of the origin of the checkout holding ``start`` (default: the working folder).

    Git itself resolves the remote, so includes, conditional includes, ``insteadOf``
    rewrites, linked worktrees and subfolders follow Git's own rules. None outside a
    checkout, without an origin, or when the origin is not on GitHub.
    """
    from foragent.git import GitError, GitRepo

    folder = (start or Path.cwd()).resolve()
    while not folder.is_dir() and folder != folder.parent:
        folder = folder.parent  # A producer's root may name a folder not created yet.
    try:
        return GitRepo(folder).remote_slug()
    except GitError:
        return None


def pushes(config: Config, kind: str | None) -> bool:
    """Whether the person's filter lets this kind through; an untyped alert is not a decision."""
    return getattr(config, "notify_filter", "all") != "decisions" or kind in DECISION_KINDS


NO_REPOSITORY = "no repository could keep it: set repo in .foragent.yml or an origin remote"


def _notice(repo: str | None, status: str, title: str, line: str, kind: str | None) -> str | None:
    """Leave the fact in the planning context's journal; None once written, else why not.

    A caller may acknowledge an alert it did not push only when this returns None.
    """
    if not repo:
        return NO_REPOSITORY
    try:
        from foragent import orch_inbox

        orch_inbox.record_notice(StateStore.for_repo(repo), status, f"{title}: {line}",
                                 channel=kind or "untyped")
        return None
    except Exception as exc:  # noqa: BLE001 - reported to the caller, which then does not acknowledge
        logger.warning("Notification notice could not be recorded", exc_info=True)
        return "the notice could not be recorded: " + " ".join(f"{type(exc).__name__}: {exc}".split())


def macos_notifier(line: str, title: str) -> None:
    def quoted(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    subprocess.run(
        ["osascript", "-e", f"display notification {quoted(line)} with title {quoted(title)}"],
        check=True, capture_output=True, text=True, timeout=10,
    )


def send(config: Config, title: str, line: str, link: str | None = None) -> bool:
    """Deliver or enqueue an alert, returning whether delivery succeeded (see ``deliver``).

    The kind comes from the enclosing ``sending`` block; outside one an alert is untyped.
    """
    current = _SENDING.get() or Sending(None)
    error = deliver(config, title, line, link, kind=current.kind, root=current.root)
    current.error = error
    return error is None


def deliver(config: Config, title: str, line: str, link: str | None = None, *,
            kind: str | None = None, root: Path | None = None) -> str | None:
    """Deliver or enqueue an alert; None on success, else why it failed.

    A missing/null setting uses claude-push for older configuration files. Without
    ``config.repo`` the origin of the checkout holding ``root`` (default: the working
    folder) names the repository. Under ``notify_filter: decisions`` only decision kinds
    are pushed; any other alert, and any non-decision alert under ``notify: none``, is
    kept as a notice the planning context reads. A failure is logged and also kept as a
    notice when the repository is known.
    """
    target = config.notify if config.notify is not None else "claude-push"
    resolved: list[str | None] = []

    def repository() -> str | None:
        """Resolved once, and only on a path that needs it (asking Git costs a process)."""
        if not resolved:
            resolved.append(config.repo or checkout_repo(root))
        return resolved[0]

    if target == "none":
        # Pushes are off; a status fact still reaches the planning context. Decision
        # kinds already live in their owner's record (the box or GitHub). Without any
        # repository there is no planning context to keep a fact for, so none stays a no-op.
        if kind in DECISION_KINDS or not repository():
            return None
        return _notice(repository(), "not pushed", title, line, kind)
    if not pushes(config, kind):
        error = _notice(repository(), "not pushed", title, line, kind)
        return None if error is None else f"the alert is not pushed under notify_filter decisions, and {error}"
    try:
        if target == "claude-push":
            repo = repository()
            if not repo:
                raise ValueError("claude-push requires a repository: set repo in .foragent.yml or an origin remote")
            store = StateStore.for_repo(repo)
            store.root.mkdir(parents=True, exist_ok=True)
            record = json.dumps({"title": title, "line": line, "link": link}, ensure_ascii=False)
            # Use the outbox itself: inspect already holds the notification lock.
            with (store.root / "push-outbox.jsonl").open("a", encoding="utf-8") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                stream.write(record + "\n")
                stream.flush()
        elif target == "macos":
            macos_notifier("\n".join(filter(None, (line, link))), title)
        elif isinstance(target, str) and target.startswith("command:"):
            path = target.removeprefix("command:")
            if not path:
                raise ValueError("notification command path is empty")
            subprocess.run(
                [path], env={**os.environ, "FA_TITLE": title, "FA_LINE": line, "FA_LINK": link or ""},
                check=True, capture_output=True, text=True, timeout=10,
            )
        elif isinstance(target, str) and target.startswith(("slack:", "ntfy:")):
            channel, destination = target.split(":", 1)
            if not destination:
                raise ValueError("notification destination is empty")
            message = "\n".join(filter(None, (title, line, link)))
            if channel == "slack":
                url = destination
                data = json.dumps({"text": message}, ensure_ascii=False).encode("utf-8")
                content_type = "application/json; charset=utf-8"
            else:
                url = f"https://ntfy.sh/{destination}"
                data = message.encode("utf-8")
                content_type = "text/plain; charset=utf-8"
            request = urllib.request.Request(url, data=data, headers={"Content-Type": content_type}, method="POST")
            with urllib.request.urlopen(request, timeout=10):
                pass
        else:
            raise ValueError("unsupported notification channel")
        return None
    except Exception as exc:
        logger.warning("Notification delivery failed", exc_info=True)
        error = " ".join(f"{type(exc).__name__}: {exc}".split())
        _notice(repository(), "failed", title, f"{line} ({error})", kind)
        return error
