"""Bounded retry for Git and GitHub authentication failures (#1208).

A credential helper, SSH agent or token refresh can fail once and then work.
Transport adapters retry such failures here, below the runner, so one run
continues without repeating implementation work or publication effects.
When the budget is used up the last error propagates and the runner records
``auth-failed`` without blocking the issue.
"""

from __future__ import annotations

from collections.abc import Callable
import re
import time
from typing import TypeVar

ATTEMPTS = 3
WAITS = (2.0, 5.0)
OUTCOME = "auth-failed"

# Facts Git, SSH and gh print when the credential itself was refused or missing.
# Rate limits (HTTP 429/403 with a limit message) and a bare 403 stay outside.
_CREDENTIAL = re.compile(
    r"Permission denied \(publickey|Authentication failed|Bad credentials"
    r"|\bHTTP(?:/\S+)?\s+401\b|returned error: 401\b"
    r"|could not read (?:Username|Password) for"  # Git asked for a credential nobody can type
    r"|agent refused operation|sign_and_send_pubkey: signing failed"  # SSH agent hiccups
    r"|gh auth login|not logged in(?:to)? (?:to )?any (?:GitHub )?hosts",  # gh has no usable login
    re.I,
)
# Git's generic trailer after any failed SSH transport says only "no access".
# It names a credential failure when nothing else was printed: any other
# diagnostic (DNS, connection, "does not appear to be a git repository",
# "Repository not found", ...) is the real cause and is not a credential.
_TRAILER = re.compile(r"Could not read from remote repository", re.I)
_TRAILER_LINES = re.compile(
    r"(?:fatal: )?Could not read from remote repository\.?|Please make sure you have the correct access rights"
    r"|and the repository exists\.?|warning: permanently added .*",
    re.I,
)

T = TypeVar("T")


def is_auth_failure(text: str) -> bool:
    text = text or ""
    if _CREDENTIAL.search(text):
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return bool(_TRAILER.search(text)) and all(_TRAILER_LINES.fullmatch(line) for line in lines)


def first_line(text: str) -> str:
    """The first stderr line that names the failure, for a one-line report."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    named = [line for line in lines if _CREDENTIAL.search(line) or _TRAILER.search(line)]
    return (named or lines or ["authentication failed"])[0]


def describe(exc: BaseException) -> str:
    return f"authentication failed after {ATTEMPTS} attempts: {first_line(str(exc))}"


def retry(call: Callable[[], T], is_auth: Callable[[Exception], bool],
          sleep: Callable[[float], None] | None = None) -> T:
    """Run ``call``; retry authentication failures up to ``ATTEMPTS`` times with growing waits."""
    for attempt in range(ATTEMPTS):
        try:
            return call()
        except Exception as exc:
            if attempt == ATTEMPTS - 1 or not is_auth(exc):
                raise
        # Looked up at call time so a patched or injected sleep applies.
        (sleep or time.sleep)(WAITS[min(attempt, len(WAITS) - 1)])
    raise AssertionError("unreachable")
