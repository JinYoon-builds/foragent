"""Defect-class procedure: vendor pstack `correct` and `blast-radius`, point the roles at them (order defect-class).

Statements checked here:
1. skills/correct/SKILL.md and skills/blast-radius/SKILL.md equal the upstream texts at revision
   9b93e81fb2921b9263d9463c15bafeabfe3903aa (attached under docs/design/pstack-vendor/), apart from the
   removed `disable-model-invocation: true` line, dropped references to skills Foragent does not ship
   (blast-radius: how, why, arena, unslop) and the added Source:/Changed: lines.
2. skills/pstack/library.yml lists both with their upstream paths, each folder carries the LICENSE, and
   `fa skills catalog` shows pstack:correct and pstack:blast-radius with license MIT.
3. The orchestrator's bug-fix and retrospective references and the architect's survey name `correct`,
   `blast-radius` and principle-fix-root-causes, and do not restate their procedure steps.
4. The orchestrator's references state the two Foragent rules: out-of-scope findings become cleanup
   issues through the order path; on the third patch to one path, stop and redesign with simulation
   tests first.

How the text checks read the files:
- A dropped reference may delete a whole line or only words of it; no word may be added or reordered,
  and every upstream line without a dropped reference must stay verbatim and in order. Runs of blank
  lines are compared as one blank line.
- A skill is named by `correct` / `blast-radius` in backticks, a link or path to its folder, or its
  `pstack:` catalog id; principle-fix-root-causes by its name.
- The architect's survey is every section of skills/fa-architect/SKILL.md whose heading names
  measuring, surveying, a sweep or a review (main: "## Measure first", "## Project review"), plus the
  files those sections link to. No other heading of the shipped file is assumed.
- Restating means sharing a run of eight words with the vendored procedure texts, or one of their
  signature steps (the architecture-then-types-then-lint ladder, "the one fact it's safe because of").
- A rule's parts are searched in a window of lines around its anchor word inside one heading section.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

from foragent import cli
from test_skills_autosync import cli_exit, isolated_home, write_global_config  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
REVISION = "9b93e81fb2921b9263d9463c15bafeabfe3903aa"
UPSTREAM_DIR = "docs/design/pstack-vendor"
VENDORED = {"correct": (), "blast-radius": ("how", "why", "arena", "unslop")}
BUG_FIX = "skills/fa-orchestrator/references/bug-fix.md"
RETRO = "skills/fa-orchestrator/references/retrospective.md"
ORCH_REFS = "skills/fa-orchestrator/references"
ARCHITECT_SKILL = "skills/fa-architect/SKILL.md"
FIX_ROOT = "principle-fix-root-causes"
WINDOW = 8
SHINGLE = 8

HEADING = re.compile(r"^#{1,6}\s")
SURVEY_HEADING = re.compile(r"^#{1,6}\s.*\b(measur\w*|survey\w*|sweep\w*|review\w*)\b", re.I)
MD_LINK = re.compile(r"\]\(([^)#\s]+\.md)(?:#[^)]*)?\)")
DISABLE = re.compile(r"^disable-model-invocation:\s*true\s*$")
NAMES = {
    "correct": re.compile(r"`correct`|pstack:correct\b|(?<![\w-])correct/SKILL\.md|skills/correct\b", re.I),
    "blast-radius": re.compile(r"\bblast-radius\b", re.I),
    FIX_ROOT: re.compile(re.escape(FIX_ROOT)),
}
SIGNATURE_STEPS = (
    re.compile(r"architecture[^.\n]{0,80}\btypes?\b[^.\n]{0,80}\blint", re.I),
    re.compile(r"one fact (it'?s|it is|the change is) safe because of", re.I),
    re.compile(r"eliminate it with architecture|enforce it with types|look where grep stops|how sure are you", re.I),
)
CLEANUP = re.compile(r"clean-?up issues?", re.I)
SCOPE = re.compile(r"\bscope\b", re.I)
ORDER_PATH = re.compile(r"\border\b|fa-order|fa order", re.I)
THIRD = re.compile(r"\bthird\b|\b3rd\b", re.I)
PATCH = re.compile(r"\bpatch(es|ed)?\b|\bfix(es)?\b", re.I)
ONE_PATH = re.compile(r"\b(one|same|single) (code )?path\b", re.I)
STOP = re.compile(r"\bstop(s|ped|ping)?\b", re.I)
REDESIGN = re.compile(r"\bre-?design", re.I)
SIMULATION = re.compile(r"simulation[- ]tests?", re.I)
FIRST = re.compile(r"\bfirst\b|\bbefore\b", re.I)


# ---------------------------------------------------------------- helpers

def _read(root: Path, relative: str) -> str:
    path = root / relative
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.casefold().replace("’", "'").replace("'", ""))


def _collapse(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in (line.rstrip() for line in lines):
        if line == "" and out and out[-1] == "":
            continue
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    while out and out[0] == "":
        out.pop(0)
    return out


def _mentions(line: str, dropped: tuple[str, ...]) -> bool:
    return any(f"`{name}`" in line for name in dropped)


def _subsequence(small: list[str], big: list[str]) -> bool:
    it = iter(big)
    return all(word in it for word in small)


def vendor_problems(vendored: str, upstream: str, dropped: tuple[str, ...], name: str) -> list[str]:
    """Differences between a vendored skill text and its upstream, beyond the allowed edits."""
    found: list[str] = []
    if not vendored:
        return [f"skills/{name}/SKILL.md is missing"]
    lines = vendored.splitlines()
    source = [line for line in lines if line.startswith("Source:")]
    changed = [line for line in lines if line.startswith("Changed:")]
    if len(source) != 1:
        found.append(f"{name}: needs exactly one Source: line, found {len(source)}")
    else:
        line = source[0]
        if "pstack" not in line or "MIT" not in line or REVISION[:7] not in line:
            found.append(f"{name}: Source: line must name pstack, revision {REVISION[:7]} and the MIT license")
    if len(changed) != 1:
        found.append(f"{name}: needs exactly one Changed: line, found {len(changed)}")
    else:
        line = changed[0]
        if "disable-model-invocation" not in line:
            found.append(f"{name}: Changed: line must state the removed disable-model-invocation line")
        for skill in dropped:
            if not re.search(rf"\b{re.escape(skill)}\b", line):
                found.append(f"{name}: Changed: line must state the dropped reference to {skill}")
    if any(DISABLE.match(line) for line in lines):
        found.append(f"{name}: still has disable-model-invocation: true")
    # Source:/Changed: are metadata: they are neither compared with upstream nor scanned for
    # forbidden references, so the Changed: line may name the dropped skills backticked.
    body = _collapse([line for line in lines if not line.startswith(("Source:", "Changed:"))])
    for skill in dropped:
        if any(f"`{skill}`" in line for line in body):
            found.append(f"{name}: still refers to the unshipped skill `{skill}`")
    up = _collapse([line for line in upstream.splitlines() if not DISABLE.match(line)])
    position = 0
    for number, line in enumerate(body, 1):
        match = None
        probe = position
        while probe < len(up):
            if up[probe] == line:
                match = probe
                break
            if not (up[probe] == "" or _mentions(up[probe], dropped)):
                break
            probe += 1
        if match is None and line and position < len(up):
            probe = position
            while probe < len(up):
                if _mentions(up[probe], dropped) and _subsequence(_words(line), _words(up[probe])) and _words(line):
                    match = probe
                    break
                if not (up[probe] == "" or _mentions(up[probe], dropped)):
                    break
                probe += 1
        if match is None:
            found.append(f"{name}: line {number} differs from upstream: {line!r}")
            return found
        position = match + 1
    rest = [line for line in up[position:] if line and not _mentions(line, dropped)]
    if rest:
        found.append(f"{name}: upstream text is missing: {rest[0]!r}")
    return found


def _sections(text: str) -> list[list[str]]:
    sections: list[list[str]] = [[]]
    for line in text.splitlines():
        if HEADING.match(line):
            sections.append([])
        sections[-1].append(line)
    return sections


def _has_rule(text: str, anchor: re.Pattern, *patterns: re.Pattern) -> bool:
    for section in _sections(text):
        for index, line in enumerate(section):
            if anchor.search(line):
                window = "\n".join(section[max(0, index - WINDOW): index + WINDOW + 1])
                if all(pattern.search(window) for pattern in patterns):
                    return True
    return False


def _architect_survey(root: Path) -> str:
    text = _read(root, ARCHITECT_SKILL)
    parts: list[str] = []
    seen: set[Path] = set()
    for section in _sections(text):
        if not section or not SURVEY_HEADING.match(section[0]):
            continue
        chunk = "\n".join(section)
        parts.append(chunk)
        for link in MD_LINK.findall(chunk):
            path = ((root / ARCHITECT_SKILL).parent / link).resolve()
            if path.is_file() and path not in seen:
                seen.add(path)
                parts.append(path.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


def _shingles(text: str) -> set[tuple[str, ...]]:
    lines = [line for line in text.splitlines() if not line.startswith(("Source:", "Changed:"))]
    if lines and lines[0] == "---":  # frontmatter is metadata, not the procedure
        end = next((i for i, line in enumerate(lines[1:], 1) if line == "---"), 0)
        lines = lines[end + 1:]
    words = _words("\n".join(lines))
    return {tuple(words[i:i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)}


def _procedure_texts(root: Path) -> set[tuple[str, ...]]:
    shingles: set[tuple[str, ...]] = set()
    for name in VENDORED:
        shingles |= _shingles(_read(root, f"{UPSTREAM_DIR}/{name}.SKILL.md"))
    shingles |= _shingles(_read(root, f"skills/{FIX_ROOT}/SKILL.md"))
    return shingles


def pointer_problems(root: Path) -> list[str]:
    """Statement 3: each place names the three skills and restates none of their steps."""
    found: list[str] = []
    procedures = _procedure_texts(root)
    places = {BUG_FIX: _read(root, BUG_FIX), RETRO: _read(root, RETRO),
              "the architect's survey": _architect_survey(root)}
    for label, text in places.items():
        for name, pattern in NAMES.items():
            if not pattern.search(text):
                found.append(f"{label}: does not name {name}")
        overlap = _shingles(text) & procedures
        if overlap:
            found.append(f"{label}: restates vendored procedure text: {' '.join(sorted(overlap)[0])!r}")
        for step in SIGNATURE_STEPS:
            hit = step.search(text)
            if hit:
                found.append(f"{label}: restates a procedure step: {hit.group(0)!r}")
    return found


def rule_problems(root: Path) -> list[str]:
    """Statement 4: the two Foragent rules stand in the orchestrator's references."""
    folder = root / ORCH_REFS
    text = "\n\n".join(path.read_text(encoding="utf-8") for path in sorted(folder.glob("*.md")))
    found: list[str] = []
    if not _has_rule(text, CLEANUP, SCOPE, ORDER_PATH):
        found.append("orchestrator references: findings outside the issue's scope must become cleanup issues "
                     "through the order path")
    if not _has_rule(text, THIRD, PATCH, ONE_PATH, STOP, REDESIGN, SIMULATION, FIRST):
        found.append("orchestrator references: on the third patch to one path, stop and redesign with "
                     "simulation tests first")
    return found


def _copy(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(ROOT / "skills", root / "skills")
    shutil.copytree(ROOT / UPSTREAM_DIR, root / UPSTREAM_DIR)
    return root


def _upstream(name: str) -> str:
    return _read(ROOT, f"{UPSTREAM_DIR}/{name}.SKILL.md")


SOURCE = ("Source: vendored from `pstack/skills/{name}/SKILL.md` (pstack by Lauren Tan, MIT License, "
          "Copyright (c) 2026 Lauren Tan, source commit 9b93e81). See LICENSE.")


def _model_vendored(name: str, backticks: bool = False) -> str:
    """A hand-made vendored copy that makes exactly the allowed edits; the checker must accept it."""
    lines = [line for line in _upstream(name).splitlines() if not DISABLE.match(line)]
    if name == "blast-radius":
        edited = []
        for line in lines:
            if line.startswith("Companion to"):
                continue
            if line.startswith("6. For a big"):
                continue
            line = line.replace(" Use `why` step 2 to pull the PR and commits.", "")
            line = line.replace(" Same rules as `why`.", "")
            line = line.replace("Write it through `unslop`, cite real code", "Cite real code")
            edited.append(line)
        lines = edited
        names = ("`how`, `why`, `arena` and `unslop`" if backticks else "how, why, arena and unslop")
        changed = (f"Changed: removed `disable-model-invocation: true`; dropped references to {names}, "
                   "which Foragent does not ship. The text is otherwise unchanged.")
    else:
        changed = "Changed: removed `disable-model-invocation: true`. The text is otherwise unchanged."
    end = lines.index("---", 1)
    return "\n".join(lines[:end + 1] + ["", SOURCE.format(name=name), changed] + lines[end + 1:]) + "\n"


# ---------------------------------------------------------------- statement 1

@pytest.mark.parametrize("name", sorted(VENDORED))
def test_vendored_skill_equals_upstream_apart_from_allowed_edits(name):
    vendored = _read(ROOT, f"skills/{name}/SKILL.md")
    assert vendor_problems(vendored, _upstream(name), VENDORED[name], name) == []


@pytest.mark.parametrize("name", sorted(VENDORED))
def test_checker_accepts_a_copy_with_exactly_the_allowed_edits(name):
    assert vendor_problems(_model_vendored(name), _upstream(name), VENDORED[name], name) == []


def test_checker_accepts_backticked_dropped_names_in_the_changed_line():
    """Planning answer (hold d): `how`, `why`, `arena`, `unslop` backticked in Changed: is compliant."""
    text = _model_vendored("blast-radius", backticks=True)
    changed = next(line for line in text.splitlines() if line.startswith("Changed:"))
    assert all(f"`{skill}`" in changed for skill in VENDORED["blast-radius"])
    assert vendor_problems(text, _upstream("blast-radius"), VENDORED["blast-radius"], "blast-radius") == []


@pytest.mark.parametrize("where", ["Source", "Changed"])
def test_backticked_names_in_metadata_do_not_hide_one_left_in_the_body(where):
    text = _model_vendored("blast-radius", backticks=True)
    text = text.replace("Cite real code", "Write it through `unslop`, cite real code")
    if where == "Source":
        text = text.replace("See LICENSE.", "See LICENSE. Not `how`, `why`, `arena` or `unslop`.")
    problems = vendor_problems(text, _upstream("blast-radius"), VENDORED["blast-radius"], "blast-radius")
    assert any("`unslop`" in problem for problem in problems), problems
    assert not any("`how`" in problem or "`arena`" in problem for problem in problems), problems


@pytest.mark.parametrize("mutation", ["added word", "changed word", "doubled line", "dropped plain line",
                                      "kept disable line", "kept unshipped reference", "no Source line",
                                      "Changed line omits a dropped skill"])
def test_checker_rejects_any_other_edit(mutation):
    name = "blast-radius"
    text = _model_vendored(name)
    target = "Listing the callers is not the job. The agent can grep those in a second."
    if mutation == "added word":
        text = text.replace(target, target.replace("not the job", "not really the job"))
    elif mutation == "changed word":
        text = text.replace(target, target.replace("second", "minute"))
    elif mutation == "doubled line":
        text = text.replace(target, target + "\n\n" + target)
    elif mutation == "dropped plain line":
        text = text.replace(target + " The job is the breakage grep won't show you.\n", "")
    elif mutation == "kept disable line":
        text = text.replace("---\n\nSource:", "disable-model-invocation: true\n---\n\nSource:", 1)
    elif mutation == "kept unshipped reference":
        text = text.replace("Cite real code", "Write it through `unslop`, cite real code")
    elif mutation == "no Source line":
        text = "\n".join(line for line in text.splitlines() if not line.startswith("Source:")) + "\n"
    elif mutation == "Changed line omits a dropped skill":
        text = text.replace("arena ", "")
    assert text != _model_vendored(name), "mutation did not apply"
    assert vendor_problems(text, _upstream(name), VENDORED[name], name), mutation


# ---------------------------------------------------------------- statement 2

def _pstack_license() -> str:
    return _read(ROOT, "skills/pstack/LICENSE")


def test_library_lists_both_with_upstream_paths_and_each_folder_has_the_license():
    data = yaml.safe_load(_read(ROOT, "skills/pstack/library.yml"))
    assert data["revision"] == REVISION and data["license"] == "MIT"
    skills = data["skills"]
    for name in VENDORED:
        assert name in skills, f"library.yml must list {name}"
        assert re.fullmatch(rf"pstack/(?:[\w.-]+/)*{re.escape(name)}/SKILL\.md", skills[name]), (
            f"library.yml must give {name} its upstream pstack path, got {skills[name]!r}")
        assert name not in (data.get("sources") or {}), f"{name} comes from pstack itself, not another source"
        license_text = _read(ROOT, f"skills/{name}/LICENSE")
        assert license_text, f"skills/{name}/LICENSE is missing"
        assert _pstack_license().strip() in license_text, f"skills/{name}/LICENSE must carry the pstack MIT text"
        assert "Lauren Tan" in license_text


def test_catalog_shows_both_as_pstack_skills_with_mit(tmp_path, monkeypatch, capsys, isolated_home):
    root = tmp_path / "project"
    root.mkdir()
    shutil.copytree(ROOT / "skills", root / "skills")
    shutil.copy2(ROOT / ".foragent.yml", root / ".foragent.yml")
    for extra in ("LICENSE-MIT", "THIRD_PARTY_NOTICES.md"):
        if (ROOT / extra).exists():
            shutil.copy2(ROOT / extra, root / extra)
    write_global_config({"skill_libraries": []})
    monkeypatch.chdir(root)
    capsys.readouterr()
    assert cli_exit(cli.main, ["skills", "catalog", "--json"]) == 0
    rows = {row["id"]: row for row in json.loads(capsys.readouterr().out)}
    for name in VENDORED:
        row = rows.get(f"pstack:{name}")
        assert row is not None, f"`fa skills catalog` must show pstack:{name}"
        assert row["name"] == name and row["license"] == "MIT" and not row["runs_scripts"]
    assert not (root / ".claude/skills").exists(), "the catalog must not install anything"


# ---------------------------------------------------------------- statement 3

def test_bug_fix_and_retrospective_and_architect_survey_name_the_skills_without_restating_them():
    assert pointer_problems(ROOT) == []


@pytest.mark.parametrize("place", [BUG_FIX, RETRO, ARCHITECT_SKILL])
@pytest.mark.parametrize("name", sorted(NAMES))
def test_removing_a_name_fails_the_check_and_names_it(tmp_path, place, name):
    root = _copy(tmp_path)
    path = root / place
    if place == ARCHITECT_SKILL:
        _write_architect(root, "1. Run `fa orch facts`.\n2. Load `correct`, `blast-radius` and "
                         f"{FIX_ROOT}.\n", "Keep notes.\n")
    else:
        path.write_text("# Ref\n\nUse `correct`, `blast-radius` and principle-fix-root-causes.\n", encoding="utf-8")
    label = _label(place)
    assert _problems_of(root, label) == []
    path.write_text(NAMES[name].sub("", path.read_text(encoding="utf-8")), encoding="utf-8")
    assert _problems_of(root, label) == [f"{label}: does not name {name}"]


@pytest.mark.parametrize("restated", [
    "Fix each class at the highest level: architecture first, then types, then a lint, then tests.",
    "Find the one fact it's safe because of and prove it.",
    "Give each piece of state one owner and each task one supported way.",
    "Ask whether the state that triggered the symptom is legitimate before you guard it.",
])
@pytest.mark.parametrize("place", [BUG_FIX, RETRO, ARCHITECT_SKILL])
def test_restating_a_procedure_step_fails_the_check(tmp_path, place, restated):
    root = _copy(tmp_path)
    base = f"Use `correct`, `blast-radius` and {FIX_ROOT}.\n"
    if place == ARCHITECT_SKILL:
        _write_architect(root, base, "Keep notes.\n")
    else:
        (root / place).write_text("# Ref\n\n" + base, encoding="utf-8")
    label = _label(place)
    assert _problems_of(root, label) == []
    if place == ARCHITECT_SKILL:
        _write_architect(root, base + restated + "\n", "Keep notes.\n")
    else:
        (root / place).write_text("# Ref\n\n" + base + restated + "\n", encoding="utf-8")
    assert any(p.startswith(f"{label}: restates") for p in _problems_of(root, label)), restated


def _label(place: str) -> str:
    return "the architect's survey" if place == ARCHITECT_SKILL else place


def _problems_of(root: Path, label: str) -> list[str]:
    return [p for p in pointer_problems(root) if p.startswith(f"{label}:")]


def _write_architect(root: Path, survey: str, other: str, linked: str | None = None) -> None:
    """A temporary architect skill whose sections are made here, not taken from the shipped file."""
    folder = root / "skills/fa-architect"
    shutil.rmtree(folder, ignore_errors=True)
    (folder / "references").mkdir(parents=True)
    if linked is not None:
        (folder / "references/class-sweep.md").write_text(linked, encoding="utf-8")
    (folder / "SKILL.md").write_text(
        "---\nname: fa-architect\ndescription: fixture\n---\n\n# Architect\n\n## Measure first\n\n" + survey
        + "\n## Notes\n\n" + other + "\n## Background\n\nSee [sweep](references/class-sweep.md).\n",
        encoding="utf-8")


def _architect_name_problems(root: Path) -> list[str]:
    return [p for p in pointer_problems(root) if p.startswith("the architect's survey")]


def test_architect_names_outside_the_survey_do_not_count(tmp_path):
    root = _copy(tmp_path)
    names = f"Use `correct`, `blast-radius` and {FIX_ROOT}.\n"
    _write_architect(root, "1. Run `fa orch facts`.\n", names)
    assert len(_architect_name_problems(root)) == 3
    _write_architect(root, "1. Run `fa orch facts`.\n", "Keep notes.\n", linked=names)
    assert len(_architect_name_problems(root)) == 3, "a file linked only from a non-survey section does not count"
    _write_architect(root, names, "Keep notes.\n")
    assert _architect_name_problems(root) == []


def test_architect_survey_may_point_through_a_linked_reference(tmp_path):
    root = _copy(tmp_path)
    names = f"Use `correct`, `blast-radius` and {FIX_ROOT}.\n"
    _write_architect(root, "1. Follow [class sweep](references/class-sweep.md).\n", "Keep notes.\n", linked=names)
    assert _architect_name_problems(root) == []


# ---------------------------------------------------------------- statement 4

def test_orchestrator_references_state_the_two_foragent_rules():
    assert rule_problems(ROOT) == []


CLEANUP_RULE = ("Findings outside the current issue's scope become cleanup issues through the order path "
                "(`fa-order`).\n")
THIRD_RULE = ("On the third patch to one path, stop and redesign it with simulation tests first.\n")


@pytest.mark.parametrize("drop, missing", [
    (CLEANUP_RULE, "cleanup issues"),
    (THIRD_RULE, "third patch"),
])
def test_removing_a_rule_fails_the_check_and_names_it(tmp_path, drop, missing):
    root = _copy(tmp_path)
    folder = root / ORCH_REFS
    for path in folder.glob("*.md"):
        path.write_text("# Reference\n\nNothing here.\n", encoding="utf-8")
    (folder / "bug-fix.md").write_text("# Bug fix\n\n" + CLEANUP_RULE + "\n" + THIRD_RULE, encoding="utf-8")
    assert rule_problems(root) == []
    (folder / "bug-fix.md").write_text(("# Bug fix\n\n" + CLEANUP_RULE + "\n" + THIRD_RULE).replace(drop, ""),
                                       encoding="utf-8")
    found = rule_problems(root)
    assert len(found) == 1 and missing in found[0]


@pytest.mark.parametrize("weakened", [
    THIRD_RULE.replace("third", "fifth"),
    THIRD_RULE.replace(" with simulation tests first", ""),
    THIRD_RULE.replace("stop and redesign it", "keep patching it"),
    CLEANUP_RULE.replace("become cleanup issues", "are dropped"),
])
def test_a_weakened_rule_does_not_count(tmp_path, weakened):
    root = _copy(tmp_path)
    folder = root / ORCH_REFS
    for path in folder.glob("*.md"):
        path.write_text("# Reference\n\nNothing here.\n", encoding="utf-8")
    rules = CLEANUP_RULE + "\n" + THIRD_RULE
    (folder / "bug-fix.md").write_text("# Bug fix\n\n" + rules, encoding="utf-8")
    assert rule_problems(root) == []
    original = CLEANUP_RULE if weakened.startswith("Findings") else THIRD_RULE
    (folder / "bug-fix.md").write_text("# Bug fix\n\n" + rules.replace(original, weakened), encoding="utf-8")
    assert len(rule_problems(root)) == 1
