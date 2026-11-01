"""The prompt a role session starts with, built from its entry in the roles source (D95).

The architect keeps its measurement prompt; every other role shares ``ROLE_PROMPT``.
"""

from pathlib import Path

from foragent.config import Config
from foragent.roles import load_roles

REFACTOR = "skills/fa-orchestrator/references/refactor.md"

# The procedure lives in the architect's skills; the prompt keeps the evidence, the why and a pointer.
PROMPT = """You are the architect for this repository. The orchestrator started you with this evidence:

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
Put each proposal's expected effect and `measured: pending` in that request or issue. On later runs,
read earlier proposals in `fa orch inbox` and linked issue history, then check their measured effect
with `fa orch scorecard` or `fa orch facts`. Record the measurement on its request or issue through
the orchestrator, and append only the reusable lesson to memory.

End with a one-line summary of what you proposed, or "Nothing to change.".
"""

ARCHITECT = "architect"

# Every other role: the evidence, the role's skills and memory, and the one way out (the orchestrator).
ROLE_PROMPT = """You are the {role} role for this repository. The orchestrator started you with this evidence:

{why}

Your role is only to propose. You never publish, edit or approve orders, and you never message a
person. The orchestrator judges each proposal and decides whether it becomes an order.

{procedure}

Deliver each proposal through `fa orch request --from {role} --text "<proposal>"`. "Nothing to change"
is a valid outcome.

## Memory

Your project memory is {memory}. Read {memory_file} first. Append only reusable lessons there;
leave the change uncommitted. Keep proposals, decisions and waits on the owning GitHub issue or
linked unpublished request, delivered through `fa orch request`. Include the responsible role and
next condition with each wait so the owner can continue it after a restart.

End with a one-line summary of what you proposed, or "Nothing to change.".
"""



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
