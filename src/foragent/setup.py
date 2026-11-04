"""The ordered, read-only setup checklist shared by doctor, init and /fa.

Checks read facts; they never repair credentials or persist results.
Add later checks here in setup order (tools, login, scopes, repository, ...).
"""

import re
import subprocess
import sys
from typing import NotRequired, TypedDict

from foragent.i18n import text


class SetupItem(TypedDict):
    id: str
    ok: bool
    fix: NotRequired[str]
    advice: NotRequired[bool]


def gh_project_scope(auth_status: str, *, language: str = "en") -> SetupItem:
    """Check the active github.com account; an undisclosed scope list is unknown.

    Environment tokens don't expose scopes. Missing gh and login failures belong
    to future checklist items, so neither is treated as a missing project scope.
    """
    accounts = []
    host = ""
    account = None
    for line in auth_status.splitlines():
        if line and not line[0].isspace():
            host, account = line.strip(), None
        elif host == "github.com":
            if "Logged in to github.com account " in line:
                account = {"active": None, "scopes": None}
                accounts.append(account)
            elif account is not None:
                if "Active account:" in line:
                    account["active"] = line.partition("Active account:")[2].strip() == "true"
                elif "Token scopes:" in line:
                    account["scopes"] = set(re.split(r"[\s,'\"]+", line.partition("Token scopes:")[2].strip()))
    selected = next((a for a in accounts if a["active"] is True),
                    next((a for a in accounts if a["active"] is None), None))
    ok = selected is None or selected["scopes"] is None or "project" in selected["scopes"]
    item: SetupItem = {"id": "gh-project-scope", "ok": ok}
    if not ok:
        item["fix"] = "gh auth refresh -h github.com -s project"
    return item


def _standing_blocker(assertions: str) -> bool:
    """Only an indefinite sleep assertion counts, not aggregate assertion counts.

    pmset groups each assertion's Details/Timeout lines beneath its owning pid.
    Require explicit indefinite lifetime: display activity and other temporary
    system assertions do not establish that the gate stays reachable between runs.
    """
    for block in re.split(r"(?m)(?=^[ \t]*pid \d+\()", assertions)[1:]:
        header = block.splitlines()[0]
        if not re.search(r"\bPrevent(?:UserIdleSystem|System)Sleep\b", header):
            continue
        if ("asserting forever" in block
                and not re.search(r"Process ID|Created for PID|asserting for \d|Timeout", block, re.IGNORECASE)):
            return True
    return False


def no_idle_sleep(auth_status: str, *, language: str = "en") -> SetupItem | None:
    """Read macOS power configuration through the same subprocess probe as init tools."""
    item: SetupItem = {"id": "no-idle-sleep", "ok": True, "advice": True}
    if sys.platform != "darwin":
        return None
    try:
        settings = subprocess.run(["pmset", "-g"], capture_output=True, text=True,
                                  timeout=10, check=False)
        if settings.returncode != 0:
            return None
        sleep = re.search(r"(?m)^\s*sleep\s+(\d+)\b", settings.stdout)
        if sleep is None:
            return None
        if int(sleep[1]) == 0:
            return item
        assertions = subprocess.run(["pmset", "-g", "assertions"], capture_output=True,
                                   text=True, timeout=10, check=False)
        if assertions.returncode != 0:
            return None
    except (OSError, subprocess.TimeoutExpired):
        return None
    item["ok"] = _standing_blocker(assertions.stdout)
    if not item["ok"]:
        item["fix"] = text("setup_no_idle_sleep_fix", language)
    return item


CHECKS = (gh_project_scope, no_idle_sleep)


def checklist(auth_status: str, *, language: str = "en") -> list[SetupItem]:
    """Return fresh items in check order; missing advice marks mean blocking."""
    return [item for check in CHECKS if (item := check(auth_status, language=language)) is not None]
