"""Pluggable notification delivery; failures are logged and isolated."""

import fcntl
import json
import logging
import os
import subprocess
import urllib.request

from foragent.config import Config
from foragent.state import StateStore


logger = logging.getLogger(__name__)


def macos_notifier(line: str, title: str) -> None:
    def quoted(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    subprocess.run(
        ["osascript", "-e", f"display notification {quoted(line)} with title {quoted(title)}"],
        check=True, capture_output=True, text=True, timeout=10,
    )


def send(config: Config, title: str, line: str, link: str | None = None) -> bool:
    """Deliver or enqueue an alert, returning whether delivery succeeded.

    A missing/null setting uses claude-push for older configuration files.
    """
    try:
        target = config.notify if config.notify is not None else "claude-push"
        if target == "none":
            return True
        if target == "claude-push":
            if not config.repo:
                raise ValueError("claude-push requires a repository")
            store = StateStore.for_repo(config.repo)
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
        return True
    except Exception:
        logger.warning("Notification delivery failed", exc_info=True)
        return False
