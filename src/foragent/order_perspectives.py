"""`fa order perspectives`: read-only agents question a forming spec from fixed seats (#414).

Each seat runs once per plan, in parallel, on its configured executor (codex or claude,
falling back to the other when the configured one is not installed). Every seat's
questions go into a ledger keyed by the plan's content hash under the state folder;
the interviewer merges duplicates and marks whether a question changed the spec.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any

from foragent import executors
from foragent.i18n import text

SEATS = ("skeptic", "first_time_user", "attacker", "operator", "maintainer", "incidents", "other_users")
# With both tools installed, at least two default seats run on Codex and the rest on Claude.
DEFAULT_EXECUTORS = {"skeptic": "claude", "first_time_user": "claude", "attacker": "codex",
                     "operator": "claude", "maintainer": "codex", "incidents": "claude",
                     "other_users": "claude"}
QUESTION = ("What would you ask that the person has not thought of, "
            "and how does this fail from your seat?")
SEAT_FOCUS = {
    "skeptic": "You are the skeptic. Ask whether this is needed at all and whether a simpler "
               "approach would reach the same goal with less work or less risk. "
               "Reuse: does existing code already do this or most of it? If so, why is reusing it "
               "not enough? Name the measured gap. "
               "Bar: is the Done-when bar stricter than what the existing proven path meets? If so, "
               "justify it with an incident. "
               "Reversibility (principle 10): if this fails, can it be undone? If yes, the Goal asks "
               "for detection and recovery, not zero races; only irreversible failures (uncommitted "
               "work deleted, permanent deletion, sending outside) are prevented structurally.",
    "first_time_user": "You are a first-time user who has never seen this project. Ask what is "
                       "confusing, what you would try first, and where you would get lost or stuck.",
    "attacker": "You are the attacker. Look for abuse: inputs, paths and permissions someone could "
                "misuse to break, leak, forge or escalate something.",
    "operator": "You are the operator who runs this unattended. Ask what happens when the machine "
                "goes to sleep, a quota or rate limit runs out, a dependency is slow or down, or a "
                "step fails halfway, and how the person would notice and recover.",
    "maintainer": "You are the maintainer in six months who did not write this. Ask what will be "
                  "hard to understand, change or debug, and which choices need recording now.",
    "incidents": "You are the incidents reviewer. Compare the plan with the risk map and the "
                 "rules or decisions below; ask where it repeats a known failure or contradicts them.",
    "other_users": "Other users: does this assume the maintainer's tools, accounts, OS, language or "
                   "habits? Does it still work for a Claude-only, Codex-only or API-key user, a "
                   "non-Korean user, or a project without a screen? Should this be a setting asked "
                   "once at init instead of a constant?",
}
RULES_FILE = "docs/rules.md"
DECISIONS_FILE = "docs/decisions.md"
RISK_FILE = ".foragent-risks.yml"
LATEST_DECISIONS = 20
LEDGER_DIR = "perspectives"
LANGUAGE_NAMES = {"en": "English", "ko": "Korean"}


def language_code(language: Any) -> str:
    """The configured language as i18n reads it ("ko-KR" -> "ko"), English when unset."""
    return language.lower().split("-")[0] if isinstance(language, str) and language.strip() else "en"


def language_instruction(language: str) -> str:
    code = language_code(language)
    name = LANGUAGE_NAMES.get(code, f"the language with code {code!r}")
    return (f"The person reads {name}. Write every question in {name}, even though these "
            "instructions are in English. Keep the word NONE in English.\n")


def plan_hash(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def decisions_text(root: Path) -> str:
    """All decision titles plus the latest entries in full, with a notice when cut."""
    try:
        text = (root / DECISIONS_FILE).read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        return f"({DECISIONS_FILE} does not exist)"
    starts = [match.start() for match in re.finditer(r"^### D\d+", text, re.MULTILINE)]
    if len(starts) <= LATEST_DECISIONS:
        return text.strip()
    entries = [text[start:end].strip() for start, end in zip(starts, starts[1:] + [len(text)])]
    titles = "\n".join(entry.splitlines()[0] for entry in entries)
    latest = "\n\n".join(entries[-LATEST_DECISIONS:])
    return (f"Cut notice: {DECISIONS_FILE} has {len(entries)} decisions. All titles follow; only the "
            f"latest {LATEST_DECISIONS} are shown in full. Read {DECISIONS_FILE} for the rest.\n\n"
            f"## All decision titles\n{titles}\n\n## Latest {LATEST_DECISIONS} decisions\n{latest}")


def prompt(seat: str, plan: dict, root: Path, language: str = "en") -> str:
    body = (
        "Read the order plan below and the repository read-only. Do not change any file.\n"
        f"Seat: {seat.replace('_', ' ')}. {SEAT_FOCUS[seat]}\n\n"
        f"{QUESTION}\n"
        "Reply with one question per line and nothing else. If you have none, reply with exactly NONE.\n"
        f"{language_instruction(language)}\n"
        "## Order plan (PLAN.json)\n```json\n"
        f"{json.dumps(plan, ensure_ascii=False, indent=2)}\n```\n"
    )
    if seat == "incidents":
        try:
            risks = (root / RISK_FILE).read_text(encoding="utf-8").strip()
        except (FileNotFoundError, UnicodeDecodeError):
            risks = f"({RISK_FILE} does not exist)"
        body += f"\n## Risk map ({RISK_FILE})\n```yaml\n{risks}\n```\n\n"
        try:
            rules = (root / RULES_FILE).read_text(encoding="utf-8").strip() or "(empty)"
        except FileNotFoundError:
            body += f"## Decisions ({DECISIONS_FILE})\n{decisions_text(root)}\n"
        except (OSError, UnicodeDecodeError) as exc:
            body += f"## Current rules ({RULES_FILE})\n({RULES_FILE} exists but cannot be read: {exc})\n"
        else:
            body += f"## Current rules ({RULES_FILE})\n{rules}\n"
    return body


def _installed(name: str) -> bool:
    return shutil.which(name) is not None


def seat_executors(config: Any) -> dict[str, str | None]:
    """Each seat's executor: configured, else default; the other tool when it is not installed."""
    configured = getattr(config, "perspectives", None) or {}
    chosen = {}
    for seat in SEATS:
        wanted = configured.get(seat, DEFAULT_EXECUTORS[seat])
        other = "claude" if wanted == "codex" else "codex"
        chosen[seat] = wanted if _installed(wanted) else other if _installed(other) else None
    return chosen


def _ledger_dir(store) -> Path:
    return Path(store.root) / LEDGER_DIR


def _ledgers(store) -> list[tuple[Path, dict]]:
    found = []
    for path in sorted(_ledger_dir(store).glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("questions"), list):
            found.append((path, value))
    return found


def _lock(store):
    from foragent.state_locks import _file_lock

    _ledger_dir(store).mkdir(parents=True, exist_ok=True)
    return _file_lock(_ledger_dir(store) / ".lock")


def _write(store, path: Path, value: dict) -> None:
    store._write_json(path, value)


def _ask(seat: str, executor: str | None, plan: dict, root: Path, logs: Path, digest: str,
         language: str = "en"):
    from foragent.order import PREREAD_TIMEOUT_S, _unknowns

    if executor is None:
        return seat, executor, None, text("perspectives_error_missing", language)
    log = logs / f"{digest[:12]}-{seat}.log"
    try:
        # The executor's own default model and effort; only the timeout follows preread.
        result = executors.executor_for(executor).run(
            worktree=root, prompt=prompt(seat, plan, root, language), model=None,
            timeout_s=PREREAD_TIMEOUT_S, log_path=log, read_only=True)
    except Exception as exc:  # One seat's failure must not lose the others.
        return seat, executor, None, text("perspectives_error_raised", language,
                                          kind=type(exc).__name__, message=exc)
    message = result.last_message or ""
    if result.timed_out:
        return seat, executor, None, text("perspectives_error_timeout", language, seconds=PREREAD_TIMEOUT_S,
                                          log=log, message=message.strip())
    if result.exit_code != 0 or not message.strip():
        return seat, executor, None, text("perspectives_error_exit", language, code=result.exit_code,
                                          log=log, message=message.strip())
    return seat, executor, _unknowns(message), None


def run(plan: dict, root: Path, config: Any, store) -> dict:
    """Run every seat in parallel and replace this plan hash's questions, keeping marks."""
    digest = plan_hash(plan)
    logs = _ledger_dir(store) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    chosen = seat_executors(config)
    language = language_code(getattr(config, "language", "en"))
    with ThreadPoolExecutor(max_workers=len(SEATS)) as pool:
        answers = list(pool.map(lambda seat: _ask(seat, chosen[seat], plan, root, logs, digest, language), SEATS))
    questions, failures = [], []
    for seat, executor, texts, error in answers:
        if error is not None:
            failures.append({"perspective": seat, "executor": executor, "error": error})
            continue
        questions += [{"id": f"{digest[:12]}-{seat}-{number}", "perspective": seat,
                       "executor": executor, "language": language, "text": text}
                      for number, text in enumerate(texts, start=1)]
    path = _ledger_dir(store) / f"{digest}.json"
    with _lock(store):
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            previous = {}
        marks = {item["id"]: item["changed"] for item in previous.get("questions", [])
                 if isinstance(item, dict) and isinstance(item.get("changed"), bool)}
        saved = [{**question, **({"changed": marks[question["id"]]} if question["id"] in marks else {})}
                 for question in questions]
        _write(store, path, {"plan_hash": digest, "plan_id": plan.get("id"), "language": language, "questions": saved,
                             "failures": failures})
    return {"plan_hash": digest, "language": language, "questions": questions, "failures": failures}


def mark(store, identity: str, changed: bool, language: str = "en") -> dict:
    from foragent.order import PlanError

    with _lock(store):
        for path, value in _ledgers(store):
            for question in value["questions"]:
                if isinstance(question, dict) and question.get("id") == identity:
                    question["changed"] = changed
                    _write(store, path, value)
                    return question
    raise PlanError(text("perspectives_unknown_id", language, id=identity))


def stats(store) -> dict:
    rows = {seat: {"asked": 0, "changed": 0} for seat in SEATS}
    for _path, value in _ledgers(store):
        for question in value["questions"]:
            if not isinstance(question, dict) or question.get("perspective") not in rows:
                continue
            rows[question["perspective"]]["asked"] += 1
            rows[question["perspective"]]["changed"] += question.get("changed") is True
    return {"perspectives": rows}
