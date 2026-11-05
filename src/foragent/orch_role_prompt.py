"""The prompt a role session starts with, built from its entry in the roles source (D95).

The architect keeps its measurement prompt; every other role shares ``ROLE_PROMPT``.

A user-view role (one the gate calls when an order finishes or once a day, such as the UX
researcher) sees what a person sees: its session runs in a fresh folder outside the checkout
holding the README, its agent, skills and memory, and never the source or the state directory.
"""

import json
import os
from pathlib import Path
import shutil

from foragent.config import Config
from foragent.roles import load_roles

REFACTOR = "skills/fa-orchestrator/references/refactor.md"

# The procedure lives in the architect's skills; the prompt keeps the evidence, the why and a pointer.
PROMPT = """You are the architect for this repository. You were started with this evidence:

{why}

Your role is only to propose. You never publish, edit or approve orders. The orchestrator judges each
proposal and decides whether it becomes an order.

{procedure}

## Measure first

Run `fa orch facts` for the hot files, and the project's tests with `pytest --durations=25` (for
example `fa test --durations=25`) for the slow tests.

"Nothing to change" is a valid and often the best outcome.

Every proposal cites fa orch facts numbers or a real incident, and states its expected measurable
effect. Deliver each proposal through `fa orch request --from architect --text "<proposal>"` and mark
each proposal with its decision owner on its first line: `Decision: orchestrator` for an orchestrator
decision, or `Decision: person` for a person decision (lane boundary changes, anything irreversible,
scope, or taste), which the orchestrator forwards to the person as a multiple-choice question.

## Memory

Read {memory} first. Append only reusable lessons there; leave the change uncommitted.
Keep proposals, decisions and waits on the owning GitHub issue or linked unpublished request.
Track each proposal by its request id and date in `fa orch inbox`. Keep its expected effect and
`measured: pending` in that request or issue. On later runs, read earlier proposals in `fa orch inbox`
and linked issue history, then check their measured effect with `fa orch scorecard` or `fa orch facts`.
Record the measurement on its request or issue through the orchestrator, and append only the
reusable lesson to memory.

End with a one-line summary of what you proposed, or "Nothing to change.".
"""

ARCHITECT = "architect"

# Every other role: the evidence, the role's skills and memory, and the one way out (the orchestrator).
ROLE_PROMPT = """You are the {role} role for this repository. You were started with this evidence:

{why}

Your role is only to propose. You never publish, edit or approve orders, and you never message a
person. The orchestrator judges each proposal and decides whether it becomes an order.

{procedure}

Deliver each proposal through `fa orch request --from {role} --text "<proposal>"`. "Nothing to change"
is a valid outcome.

## Memory

Your project memory is {memory}. Read {memory_file} first. Append only reusable lessons there;
leave the change uncommitted. Keep proposals, decisions and waits on the owning GitHub issue or
linked unpublished request, delivered through `fa orch request`. Track each proposal by its
request id and date in `fa orch inbox`. Include the responsible role and next condition with each
wait so the owner can continue it after a restart.

End with a one-line summary of what you proposed, or "Nothing to change.".
"""



# Signal kinds the gate schedules itself: one pass per finished order, one pass a day (#1874).
USER_VIEW_KINDS = ("order_finished", "daily")
# The file in a user-view session's folder where `fa orch request` leaves its reports (#1874).
REPORTS = ".foragent-reports.jsonl"
REPORTS_ENV = "FORAGENT_ROLE_REPORTS"

USER_VIEW_NOTE = """## What you see

You work as a person would: your working folder holds only the project's README, your agent,
skills and memory. Use the product as the README tells a person to; never read its source.
Report with `fa orch request --repo-root {root} --from {role} --text "<find>"`; a separate session
confirms each find before it reaches the orchestrator.
{readme}"""


def readme_section(workspace: Path) -> str:
    """The README a person starts from, also kept as a file in the session's folder."""
    for name in ("README.md", "README.rst", "README.txt", "README"):
        path = workspace / name
        if path.is_file():
            return f"\n## {name}\n\n{path.read_text(encoding='utf-8', errors='replace').strip()}\n"
    return ""


def user_view_kinds(entry: dict | None) -> set[str]:
    """The gate-scheduled kinds the role's signals declare."""
    kinds = {kind for signal in (entry or {}).get("signals") or [] for kind in signal.get("kinds") or []}
    return kinds & set(USER_VIEW_KINDS)


def is_user_view(entry: dict | None) -> bool:
    return bool(user_view_kinds(entry))


def _copy(source: Path, target: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    elif source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def prepare_workspace(root: Path, workspace: Path, entry: dict) -> None:
    """Fill a user-view session's folder: README files, the role's agent, skills and memory."""
    for readme in sorted(root.glob("README*")):
        _copy(readme, workspace / readme.name)
    _copy(root / ".claude/agents" / f"{entry['agent']}.md", workspace / ".claude/agents" / f"{entry['agent']}.md")
    for skill in entry["skills"]:
        source = root / ".claude/skills" / skill
        _copy(source if source.is_dir() else root / "skills" / skill, workspace / ".claude/skills" / skill)
    _copy(root / entry["memory"], workspace / entry["memory"])
    (workspace / REPORTS).touch()


def keep_memory(root: Path, workspace: Path, entry: dict) -> None:
    """Bring the session's memory edits back to the checkout, where role memory is published."""
    memory = workspace / entry["memory"]
    if memory.is_dir():
        shutil.copytree(memory, root / entry["memory"], dirs_exist_ok=True)


def reports(workspace: Path) -> list[str]:
    """The texts the session reported through `fa orch request`, in order."""
    try:
        lines = (workspace / REPORTS).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    found = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip():
            found.append(item["text"].strip())
    return found


def spool(source: str, text: str) -> Path | None:
    """Inside a user-view session of ``source``, append the report to its folder and return the file."""
    target = os.environ.get(REPORTS_ENV)
    role, _, path = (target or "").partition("=")
    if not path or role != source:
        return None
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"text": text}, ensure_ascii=False) + "\n")
    return Path(path)


def _entry(config: Config, role: str) -> dict | None:
    """The role's agent, skills and memory, read only from the roles source (D95)."""
    return load_roles(config).get(role)


def _memory_file(entry: dict) -> str:
    return entry["memory"].rstrip("/") + "/MEMORY.md"


def _has_agent(root: Path | None, entry: dict) -> bool:
    return root is not None and (root / ".claude/agents" / f"{entry['agent']}.md").is_file()


def _skill_text(root: Path, skill: str) -> str | None:
    """The installed copy first, then the project's own skills directory."""
    for path in (root / ".claude/skills" / skill / "SKILL.md", root / "skills" / skill / "SKILL.md"):
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    return None


def _procedure(entry: dict, root: Path | None, executor: str, note: str) -> str:
    """Claude loads the skills through the agent; Codex gets their text; otherwise a pointer to them."""
    skills = entry["skills"]
    if not skills:
        return f"Your role has no skills configured ({note})." if note else ""
    names = ", ".join(skills)
    paths = ", ".join(f"skills/{skill}/SKILL.md" for skill in skills)
    detail = f"{paths}; {note}" if note else paths
    texts = [text for skill in skills
             if root is not None and executor != "claude" and (text := _skill_text(root, skill))]
    if texts:
        return f"Follow the {names} skill below ({detail}).\n\n" + "\n\n".join(texts)
    if executor == "claude" and _has_agent(root, entry):
        return f"Follow your {names} skill ({detail})."
    return f"Follow the {names} skill in {detail}."


def build_prompt(why: str, root: Path | None = None, executor: str = "claude",
                 role: str = ARCHITECT, entry: dict | None = None, *, review_notes: str = "") -> str:
    """The prompt for ``role``; ``entry`` defaults to the derived roles source of a bare config.

    ``review_notes`` is the architect's section of recorded reviewer NOTE lines, appended when present.
    """
    entry = entry if entry is not None else _entry(Config(), role)
    if role == ARCHITECT:
        procedure = _procedure(entry, root, executor, f"the old link {REFACTOR} points there")
        prompt = PROMPT.format(why=why, procedure=procedure, memory=_memory_file(entry))
        return f"{prompt}\n{review_notes}\n" if review_notes else prompt
    return ROLE_PROMPT.format(role=role, why=why, procedure=_procedure(entry, root, executor, ""),
                              memory=entry["memory"], memory_file=_memory_file(entry))
