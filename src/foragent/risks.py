"""Per-project risk map (.foragent-risks.yml): review context for risky paths.

The map is a root YAML sequence. Each entry names repo-relative review-policy
globs (`paths`), why they are risky, the incident that showed it, guidance for
the reviewer, and an `added` trace. `always_human`, `fixed_by` and
`guard_tests` are optional. Entries are only ever appended by `fa risk add`;
candidate discovery reads source statically and never writes the map.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

import yaml

from foragent.policy import touched

if TYPE_CHECKING:
    from foragent.config import Config
    from foragent.git import GitRepo

RISK_FILE = ".foragent-risks.yml"
ADDITIVE_REVIEW = (
    "Review all changed files in full; unmapped code receives the same full review as always, "
    "and risk-map notes only add context."
)


class RiskError(ValueError):
    """A map or entry problem; `key` and `values` name its message in foragent.messages.risk."""

    def __init__(self, key: str, **values: Any):
        from foragent.i18n import text

        self.key, self.values = key, values
        super().__init__(text(key, "en", **values))


def parse(text: str) -> list[dict[str, Any]]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RiskError("risk_invalid_yaml", file=RISK_FILE, error=" ".join(str(exc).split())) from exc
    if data is None:
        return []
    if not isinstance(data, list):
        raise RiskError("risk_not_a_list", file=RISK_FILE)
    for index, risk in enumerate(data, 1):
        paths = risk.get("paths") if isinstance(risk, dict) else None
        if not isinstance(paths, list) or not paths or not all(isinstance(path, str) and path for path in paths):
            raise RiskError("risk_entry_paths", file=RISK_FILE, index=index)
    return data


def load(root: Path) -> list[dict[str, Any]]:
    path = Path(root) / RISK_FILE
    return parse(path.read_text(encoding="utf-8")) if path.is_file() else []


def load_at(git: GitRepo, ref: str) -> list[dict[str, Any]]:
    """The map committed at `ref`; a branch cannot rewrite the risks that review it."""
    sha = git.blob_sha(ref, RISK_FILE)
    return parse(git._call(["cat-file", "blob", sha])) if sha else []


def matching(risks: list[dict[str, Any]], paths: list[str]) -> list[dict[str, Any]]:
    return [risk for risk in risks if touched(paths, risk["paths"])]


def always_human_globs(git: GitRepo, ref: str) -> list[str]:
    return [glob for risk in load_at(git, ref) if risk.get("always_human") is True for glob in risk["paths"]]


def review_notes(matched: list[dict[str, Any]], paths: list[str]) -> str:
    """Reviewer context for the matched entries; empty when nothing matched."""
    if not matched:
        return ""
    lines = ["Risk map (.foragent-risks.yml) notes for paths this change touches.", ADDITIVE_REVIEW]
    for risk in matched:
        incident = risk.get("incident") if isinstance(risk.get("incident"), dict) else {}
        lines += [
            "",
            f"- Paths: {', '.join(risk['paths'])} (changed here: {', '.join(touched(paths, risk['paths']))})",
            f"  Why: {risk.get('why', '')}",
            f"  Incident {incident.get('date', '?')}: {incident.get('summary', '')}",
            f"  Guidance: {risk.get('guidance', '')}",
        ]
        if risk.get("fixed_by"):
            lines.append(f"  Fixed by: {risk['fixed_by']} (the incident context still applies)")
        if risk.get("guard_tests"):
            lines.append(f"  Guard tests: {', '.join(map(str, risk['guard_tests']))}")
        lines.append("  Always human: " + ("yes; a person reviews this PR before merge"
                                            if risk.get("always_human") is True else "no"))
        added = risk.get("added") if isinstance(risk.get("added"), dict) else {}
        lines.append(f"  Added by {added.get('by', '?')} at {added.get('at', '?')}"
                     f" (event: {added.get('event') or 'none'})")
    return "\n".join(lines)


NOTES_START, NOTES_END = "<!-- foragent:risk-notes -->", "<!-- /foragent:risk-notes -->"


def notes_block(notes: str) -> str:
    return f"{NOTES_START}\n{notes}\n{NOTES_END}" if notes else ""


def refresh_notes(body: str, notes: str) -> str:
    """Replace the PR body's risk-notes block with current notes (append, or drop when none)."""
    block = notes_block(notes)
    pattern = re.compile(rf"(?:\n\n)?{re.escape(NOTES_START)}.*?{re.escape(NOTES_END)}", re.S)
    if pattern.search(body):
        return pattern.sub(lambda match: f"\n\n{block}" if block else "", body, count=1)
    return f"{body}\n\n{block}" if block else body


def review_options(config: Config, reviewer: str, model: str | None) -> dict[str, Any]:
    """Matched reviews run at tier l: effort for Codex, the tier model for Claude."""
    tier = config.tiers["l"]
    if reviewer == "claude":
        return {"model": tier["claude_model"] or model}
    return {"model": model, "effort": tier["codex_effort"]}


def entry(*, paths: list[str], why: str, incident_date: str, incident: str, guidance: str, by: str,
          always_human: bool = False, event: str | None = None, fixed_by: str | None = None,
          guard_tests: list[str] | None = None, root: Path | None = None,
          now: datetime | None = None) -> dict[str, Any]:
    values = {"why": why, "incident": incident, "guidance": guidance, "by": by}
    blank = [name for name, value in values.items() if not value.strip()]
    if not paths or any(not path.strip() for path in paths):
        blank.insert(0, "path")
    if blank:
        raise RiskError("risk_blank_options", options=", ".join(f"--{name}" for name in blank))
    try:
        date.fromisoformat(incident_date)
    except ValueError as exc:
        raise RiskError("risk_bad_date", value=incident_date) from exc
    missing = [test for test in guard_tests or [] if root is not None and not (root / test).is_file()]
    if missing:
        raise RiskError("risk_guard_missing", tests=", ".join(missing))
    risk: dict[str, Any] = {
        "paths": list(paths), "why": why.strip(),
        "incident": {"date": incident_date, "summary": incident.strip()},
        "guidance": guidance.strip(),
    }
    if always_human:
        risk["always_human"] = True
    if fixed_by:
        risk["fixed_by"] = fixed_by
    if guard_tests:
        risk["guard_tests"] = list(guard_tests)
    risk["added"] = {"by": by.strip(), "at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
                     "event": event}
    return risk


def append(root: Path, risk: dict[str, Any]) -> None:
    """Append one entry, keeping every existing byte (comments, line endings).

    A flow-style sequence (`[]` or `[...]`) cannot take a block item after it, so
    that layout is rewritten in block style, keeping its comment lines first.
    """
    path = Path(root) / RISK_FILE
    before = path.read_bytes() if path.exists() else b""
    existing = parse(before.decode("utf-8"))
    newline = b"\r\n" if b"\r\n" in before else b"\n"

    def dump(entries: list[dict[str, Any]]) -> bytes:
        text = yaml.safe_dump(entries, sort_keys=False, allow_unicode=True).encode("utf-8")
        return text.replace(b"\n", newline) if newline != b"\n" else text

    def appended(after: bytes) -> bool:
        try:
            return len(parse(after.decode("utf-8"))) == len(existing) + 1
        except RiskError:
            return False

    separator = newline if before and not before.endswith((b"\n", b"\r")) else b""
    after = before + separator + dump([risk])
    if not appended(after):
        comments = [line.rstrip(b"\r\n") + newline for line in before.splitlines()
                    if line.lstrip().startswith(b"#")]
        after = b"".join(comments) + dump([*existing, risk])
        if not appended(after):
            raise RiskError("risk_append_failed", file=RISK_FILE)
    path.write_bytes(after)


# Fixed inventory: (family, label, pattern). Command labels match shell words and
# Python argv lists. Guard functions are known safety checks in this project.
_CALLS = {
    "signals": ("os.kill", "os.killpg", "signal.raise_signal", "signal.pthread_kill"),
    "deletion": ("shutil.rmtree",),
}
_COMMANDS = {
    "deletion": ("rm -rf",),
    "git-write": ("git push", "git merge"),
    "github-write": ("gh pr merge", "gh issue edit", "gh api -X POST", "gh api -X PATCH", "gh api -X DELETE"),
}
GUARD_FUNCTIONS = {
    "src/foragent/policy.py": ("merge-policy", "merge_decision"),
    "src/foragent/run_baseline.py": ("baseline", "_capture_acceptance_baseline"),
    "src/foragent/scheduler_approval.py": ("approval", "_check_integrity"),
}


def _command_pattern(label: str) -> re.Pattern[str]:
    words = label.split()
    shell = r"\s+".join(map(re.escape, words))
    argv = r"""['"]\s*,\s*['"]""".join(map(re.escape, words))
    return re.compile(rf"(?<![\w./-]){shell}(?![\w-])|['\"]{argv}['\"]")


CATALOG = [
    *((label, re.compile(rf"(?<![\w.]){re.escape(label)}\s*\(")) for labels in _CALLS.values() for label in labels),
    *((label, _command_pattern(label)) for labels in _COMMANDS.values() for label in labels),
]


def scan(root: Path, files: list[str]) -> list[dict[str, Any]]:
    """Report fixed-pattern locations in tracked files without executing anything."""
    found = []
    for name in files:
        if name == RISK_FILE or name.endswith(".md"):  # prose mentions are not code
            continue
        try:
            text = (Path(root) / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        guard = GUARD_FUNCTIONS.get(name)
        for number, line in enumerate(text.splitlines(), 1):
            labels = [label for label, pattern in CATALOG if pattern.search(line)]
            if guard and re.match(rf"\s*(?:async\s+)?def\s+{guard[1]}\b", line):
                labels.append(guard[0])
            found.extend({"path": name, "line": number, "pattern": label} for label in labels)
    return found


def candidates(root: Path, files: list[str]) -> dict[str, list[dict[str, Any]]]:
    globs = [glob for risk in load(root) for glob in risk["paths"]]
    items = [{**item, "covered": bool(touched([item["path"]], globs))} for item in scan(root, files)]
    uncovered: dict[str, list[str]] = {}
    for item in items:
        if not item["covered"]:
            patterns = uncovered.setdefault(item["path"], [])
            if item["pattern"] not in patterns:
                patterns.append(item["pattern"])
    proposals = [{"paths": [path], "patterns": patterns} for path, patterns in uncovered.items()]
    return {"candidates": items, "proposals": proposals}
