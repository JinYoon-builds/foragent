"""The ordered, read-only setup checklist shared by doctor, init and /fa.

Checks evaluate collected facts; they never repair credentials or persist results.
Add later checks here in setup order (tools, login, scopes, repository, ...).
"""

import re
from typing import NotRequired, TypedDict


class SetupItem(TypedDict):
    id: str
    ok: bool
    fix: NotRequired[str]


def gh_project_scope(auth_status: str) -> SetupItem:
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


CHECKS = (gh_project_scope,)


def checklist(auth_status: str) -> list[SetupItem]:
    """Return fresh items in check order from the same facts for every reader."""
    return [check(auth_status) for check in CHECKS]
