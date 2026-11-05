"""The ordered, read-only setup checklist shared by doctor, init and /fa.

Checks evaluate collected facts; they never repair credentials or persist results.
Add later checks here in setup order (tools, login, scopes, repository, ...).
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import NotRequired, TypedDict

from foragent.git import GitError, GitRepo
from foragent.i18n import text


class SetupItem(TypedDict):
    id: str
    ok: bool
    fix: NotRequired[str]


def gh_project_scope(auth_status: str) -> SetupItem:
    """Check the active github.com account; an undisclosed scope list is unknown.

    Environment tokens don't expose scopes. Tool and login prerequisites are
    checked separately before this parser is called.
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


@dataclass(frozen=True)
class Facts:
    """Read-only machine observations shared by every checklist reader."""

    installed: dict[str, bool]
    auth_status: str = ""
    logged_in: bool = False
    auth_error: str = ""
    language: str = "en"


def installed_tools() -> dict[str, bool]:
    return {tool: shutil.which(tool) is not None for tool in ("git", "gh", "uv")}


def _auth_error(exc: OSError | subprocess.SubprocessError) -> str:
    """Keep the process failure and gh's explanation, including partial timeout output."""
    parts = [str(exc)]
    if isinstance(exc, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
        for output in (exc.stdout, exc.stderr):
            if output:
                parts.append(output.decode(errors="replace") if isinstance(output, bytes) else output)
    return "\n".join(parts)


def collect(read_auth: Callable[[], str], *, installed: dict[str, bool] | None = None,
            language: str = "en") -> Facts:
    """Observe prerequisites once, without invoking gh until it is installed."""
    tools = installed if installed is not None else installed_tools()
    if not tools["gh"]:
        return Facts(tools, auth_error="gh not found on PATH", language=language)
    try:
        return Facts(tools, auth_status=read_auth(), logged_in=True, language=language)
    except FileNotFoundError as exc:
        return Facts({**tools, "gh": False}, auth_error=_auth_error(exc), language=language)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return Facts(tools, auth_error=_auth_error(exc), language=language)


INSTALL_PAGES = {
    "git": "https://git-scm.com/downloads/linux",
    "gh": "https://github.com/cli/cli/blob/trunk/docs/install_linux.md#installing-gh-on-linux-and-bsd",
    "uv": "https://docs.astral.sh/uv/getting-started/installation/",
}


def installed_item(facts: Facts, tool: str) -> SetupItem:
    item: SetupItem = {"id": f"{tool}-installed", "ok": facts.installed[tool]}
    if not item["ok"]:
        item["fix"] = f"brew install {tool}" if sys.platform == "darwin" else INSTALL_PAGES[tool]
    return item


def git_installed(facts: Facts) -> SetupItem:
    return installed_item(facts, "git")


def gh_installed(facts: Facts) -> SetupItem:
    return installed_item(facts, "gh")


def uv_installed(facts: Facts) -> SetupItem:
    return installed_item(facts, "uv")


def waiting_item(item_id: str, prerequisite: str, language: str) -> SetupItem:
    return {"id": item_id, "ok": False, "fix": text("setup_wait_for", language, prerequisite=prerequisite)}


def gh_logged_in(facts: Facts) -> SetupItem:
    if not facts.installed["gh"]:
        return waiting_item("gh-logged-in", "gh-installed", facts.language)
    item: SetupItem = {"id": "gh-logged-in", "ok": facts.logged_in}
    if not item["ok"]:
        item["fix"] = "gh auth login"
    return item


def project_scope(facts: Facts) -> SetupItem:
    for prerequisite in (gh_installed(facts), gh_logged_in(facts)):
        if not prerequisite["ok"]:
            return waiting_item("gh-project-scope", prerequisite["id"], facts.language)
    return gh_project_scope(facts.auth_status)


CHECKS = (git_installed, gh_installed, uv_installed, gh_logged_in, project_scope)


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


def checklist(facts: Facts, *, root: Path | None = None,
              git: GitRepo | None = None) -> list[SetupItem]:
    """Return fresh items in check order from the same facts for every reader."""
    items = [check(facts) for check in CHECKS]
    if facts.installed["git"]:
        items.append(repository_linked(root if root is not None else Path.cwd(), git=git, language=facts.language))
    else:
        items.append(waiting_item("repository-linked", "git-installed", facts.language))
    return items
