"""The ordered, read-only setup checklist shared by doctor, init and /fa.

Checks evaluate collected facts; they never repair credentials or persist results.
Add later checks here in setup order (tools, login, scopes, repository, ...).
"""

import re
from pathlib import Path
from typing import NotRequired, TypedDict

from foragent.git import GitError, GitRepo
from foragent.i18n import text


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


def repository_fix(key: str, error: GitError, language: str = "en") -> str:
    """Share recovery text with init's early, before-auth repository preflight."""
    return text(key, language, error=error, choices=text("init_repository_choices", language))


def repository_linked(root: Path, *, git: GitRepo | None = None, language: str = "en") -> SetupItem:
    """Describe the repository's next step without creating or linking anything."""
    git = git or GitRepo(root)
    try:
        git.check_worktree()
    except GitError as exc:
        return {"id": "repository-linked", "ok": False,
                "fix": repository_fix("init_git_missing", exc, language)}
    try:
        git.remote_slug()
    except GitError as exc:
        key = "init_origin_missing" if exc.__cause__ is not None else "init_origin_not_github"
        return {"id": "repository-linked", "ok": False,
                "fix": repository_fix(key, exc, language)}
    return {"id": "repository-linked", "ok": True}


def checklist(auth_status: str, *, root: Path | None = None,
              git: GitRepo | None = None, language: str = "en") -> list[SetupItem]:
    """Return fresh items in check order from the same facts for every reader."""
    items = [check(auth_status) for check in CHECKS]
    items.append(repository_linked(root if root is not None else Path.cwd(), git=git, language=language))
    return items
