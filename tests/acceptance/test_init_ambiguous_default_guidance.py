"""Acceptance (N66): `fa init`'s hint for an ambiguous default skill is usable guidance.

Today the hint lists only the unpicked matches, repeats the picked id as its switching example,
and points role-default changes at `fa lanes skills`, which changes a lane's fixed skills.
After this issue the hint:
- lists every matching qualified id and identifies the selected one,
- illustrates switching with a different matching id,
- names the role's `default_skills` setting and `fa init` as the command that applies it.
Following it (put the example id in that role's `default_skills` entry, run `fa init`) makes the
role's sessions use the alternative copy, and a lane's fixed skills stay as they were.

Fixture: the locked pick test's machine (fake tools, temp HOME, nothing reaches GitHub) and its
two extra local libraries. principle-prove-it-works then has three qualified ids: alpha-skills,
pstack (vendored) and zeta-skills; alpha-skills is picked by the existing rule.

Wording is the lane's. The test finds the hint as the output lines that mention one of the
name's qualified ids, plus indented continuation lines after them, and the switching example as the matching
id on a line that names `default_skills`, or else the last matching id the hint mentions.
"""

import re

import pytest
import yaml

from test_setup_checklist_tools import machine  # noqa: F401 -- fixture
from test_init_picks_ambiguous_skill import (  # noqa: F401
    ATTACK, HANGUL, INIT_FLAGS, PICKED, PROVE, UNIQUE, add_libraries, run,
)

from foragent import session_skills
from foragent.config import load_config
from foragent.git import GitRepo

IDS = {
    PROVE: ["alpha-skills:" + PROVE, "pstack:" + PROVE, "zeta-skills:" + PROVE],
    ATTACK: ["pstack:" + ATTACK, "zeta-skills:" + ATTACK],
}
# Roles of the default table (init.ROLE_DEFAULT_SKILLS) that assign each ambiguous name.
APPLICABLE = {PROVE: {"workers", "reviewers", "orchestrator"},
              ATTACK: {"workers", "reviewers", "orchestrator", "architect"}}
ROLES = ("workers", "reviewers", "orchestrator", "architect", "docs")
LANE = {"paths": ["web/**"], "agent": "fa-web", "skills": [UNIQUE]}


def hint(output: str, name: str) -> str:
    lines = output.splitlines()
    hits = [i for i, line in enumerate(lines) if any(identity in line for identity in IDS[name])]
    assert hits, f"init prints a hint naming the qualified ids of {name}:\n{output}"
    end = hits[-1] + 1
    while end < len(lines) and lines[end][:1].isspace() and lines[end].strip():
        end += 1  # Indented continuation lines belong to the hint.
    return "\n".join(lines[hits[0]:end])


def mentioned(text: str, name: str) -> list[str]:
    """The qualified ids of ``name`` in the order the text mentions them."""
    pattern = "|".join(re.escape(identity) for identity in IDS[name])
    return re.findall(pattern, text)


def example_id(text: str, name: str) -> str | None:
    picked = PICKED[name]
    for line in text.splitlines():
        if "default_skills" in line and picked not in line:
            others = [identity for identity in mentioned(line, name) if identity != picked]
            if others:
                return others[-1]
    ids = mentioned(text, name)
    return ids[-1] if ids else None


def assert_ids_and_example(text: str, name: str) -> str:
    for identity in IDS[name]:
        assert identity in text, f"The hint lists every matching qualified id ({identity}):\n{text}"
    assert PICKED[name] in text, f"The hint identifies the selected id {PICKED[name]}:\n{text}"
    example = example_id(text, name)
    assert example is not None and example != PICKED[name], (
        f"The switching example uses a different matching id, not the selected {PICKED[name]}:\n{text}")
    return example


def assert_role_guidance(text: str, name: str, applicable: set[str]) -> set[str]:
    assert "default_skills" in text, f"The hint names the default_skills role setting:\n{text}"
    assert re.search(r"\bfa init\b", text), f"The hint names fa init as the command that applies it:\n{text}"
    named = {role for role in ROLES if re.search(rf"\b{role}\b", text)}
    assert named & applicable, f"The hint names a role whose default_skills holds {name} ({applicable}):\n{text}"
    assert named <= applicable, f"The hint names only roles that assign {name}; {named - applicable} do not:\n{text}"
    return named


def test_three_matches_hint_lists_all_selects_one_and_switches_to_another(machine, capsys, tmp_path,  # noqa: F811
                                                                          monkeypatch):
    add_libraries(machine, tmp_path)
    monkeypatch.setattr(GitRepo, "commit_paths", lambda *args, **kwargs: None)

    code, output = run(["init", "--repo-root", str(machine.root), "--language", "en", *INIT_FLAGS], capsys)

    assert "Traceback" not in output, output
    assert code == 0, f"fa init succeeds with an ambiguous default:\n{output}"
    for name in (PROVE, ATTACK):
        text = hint(output, name)
        assert not HANGUL.search(text), text
        assert_ids_and_example(text, name)
        assert_role_guidance(text, name, APPLICABLE[name])
    assert not any(UNIQUE in line and "default_skills" in line for line in output.splitlines()), (
        f"A default with one match gets no ambiguity hint:\n{output}")
    installed = (machine.root / ".claude/skills" / PROVE / "SKILL.md").read_text(encoding="utf-8")
    assert "Copy from alpha-skills." in installed, "The selected copy is the one installed"


def test_korean_dry_run_hint_lists_all_selects_one_and_switches_to_another(machine, capsys, tmp_path):  # noqa: F811
    add_libraries(machine, tmp_path)

    code, output = run(["init", "--repo-root", str(machine.root), "--language", "ko", "--dry-run", *INIT_FLAGS],
                       capsys)

    assert "Traceback" not in output, output
    assert code == 0, output
    for name in (PROVE, ATTACK):
        text = hint(output, name)
        assert HANGUL.search(text), f"Korean hint under --language ko:\n{text}"
        assert_ids_and_example(text, name)
        assert_role_guidance(text, name, APPLICABLE[name])


def test_following_the_hint_moves_the_named_role_to_the_alternative_copy(machine, capsys, tmp_path,  # noqa: F811
                                                                         monkeypatch):
    add_libraries(machine, tmp_path)
    # Only workers assigns the ambiguous name, so one role owns the installed copy.
    config = machine.home / ".config/foragent/config.yml"
    values = yaml.safe_load(config.read_text(encoding="utf-8"))
    values["default_skills"] = {"workers": [PROVE, UNIQUE]}
    config.write_text(yaml.safe_dump(values), encoding="utf-8")
    monkeypatch.setattr(GitRepo, "commit_paths", lambda *args, **kwargs: None)
    argv = ["init", "--repo-root", str(machine.root), "--language", "en", *INIT_FLAGS]

    code, output = run(argv, capsys)
    assert code == 0, output
    text = hint(output, PROVE)
    example = assert_ids_and_example(text, PROVE)
    assert assert_role_guidance(text, PROVE, {"workers"}) == {"workers"}

    # Follow the hint: the example id replaces the bare name in the named role, then fa init.
    project = machine.root / ".foragent.yml"
    written = yaml.safe_load(project.read_text(encoding="utf-8"))
    written["default_skills"]["workers"] = [example if entry == PROVE else entry
                                            for entry in written["default_skills"]["workers"]]
    written["lanes"] = {"web": dict(LANE)}
    project.write_text(yaml.safe_dump(written, sort_keys=False), encoding="utf-8")

    code, output = run(argv, capsys)
    assert "Traceback" not in output, output
    assert code == 0, f"fa init applies the replacement:\n{output}"

    after = yaml.safe_load(project.read_text(encoding="utf-8"))
    assert after["default_skills"]["workers"][0] == example, after["default_skills"]
    assert after.get("lanes") == {"web": LANE}, f"A lane's fixed skills stay unchanged: {after.get('lanes')}"
    library = example.split(":", 1)[0]
    section = session_skills.section(load_config(machine.root), "workers", machine.root)
    paths = [line for line in section.splitlines() if f"{PROVE}/SKILL.md" in line]
    assert paths, f"The workers section lists {PROVE}:\n{section}"
    match = re.search(r"(\S*" + re.escape(f"{PROVE}/SKILL.md") + ")", paths[0])
    path = match.group(1).strip("`()")
    file = machine.root / path if not path.startswith("/") else type(machine.root)(path)
    content = file.read_text(encoding="utf-8")
    assert f"Copy from {library}." in content or (library == "pstack" and "Copy from" not in content), (
        f"After following the hint, workers read the {example} copy, not the selected one:\n{content}")
    installed = (machine.root / ".claude/skills" / PROVE / "SKILL.md").read_text(encoding="utf-8")
    assert "Copy from alpha-skills." not in installed, f"The workers copy is now {example}:\n{installed}"


def workers_only(machine):
    """Only workers assigns the ambiguous name, so one role owns the installed copy."""
    config = machine.home / ".config/foragent/config.yml"
    values = yaml.safe_load(config.read_text(encoding="utf-8"))
    values["default_skills"] = {"workers": [PROVE, UNIQUE]}
    config.write_text(yaml.safe_dump(values), encoding="utf-8")


def workers_copy(machine) -> str:
    """The text of the principle-prove-it-works file the workers session section points at."""
    section = session_skills.section(load_config(machine.root), "workers", machine.root)
    paths = [line for line in section.splitlines() if f"{PROVE}/SKILL.md" in line]
    assert paths, f"The workers section lists {PROVE}:\n{section}"
    match = re.search(r"(\S*" + re.escape(f"{PROVE}/SKILL.md") + ")", paths[0])
    path = match.group(1).strip("`()")
    file = machine.root / path if not path.startswith("/") else type(machine.root)(path)
    return file.read_text(encoding="utf-8")


def test_rerun_after_switching_the_role_setting_replaces_the_copy_init_installed(machine, capsys, tmp_path,  # noqa: F811
                                                                                 monkeypatch):
    """Statement 2, independent of the hint wording: the person puts another matching id in
    default_skills.workers and runs fa init again; init replaces the copy it installed itself."""
    add_libraries(machine, tmp_path)
    workers_only(machine)
    monkeypatch.setattr(GitRepo, "commit_paths", lambda *args, **kwargs: None)
    argv = ["init", "--repo-root", str(machine.root), "--language", "en", *INIT_FLAGS]

    code, output = run(argv, capsys)
    assert code == 0, output
    skills = machine.root / ".claude/skills"
    assert "Copy from alpha-skills." in (skills / PROVE / "SKILL.md").read_text(encoding="utf-8")

    project = machine.root / ".foragent.yml"
    written = yaml.safe_load(project.read_text(encoding="utf-8"))
    alternative = "zeta-skills:" + PROVE
    written.setdefault("default_skills", {})["workers"] = [alternative, UNIQUE]
    written["lanes"] = {"web": dict(LANE)}
    project.write_text(yaml.safe_dump(written, sort_keys=False), encoding="utf-8")
    before = sorted(path.name for path in skills.iterdir())

    code, output = run(argv, capsys)
    assert "Traceback" not in output, output
    assert code == 0, f"fa init applies the replacement:\n{output}"

    installed = (skills / PROVE / "SKILL.md").read_text(encoding="utf-8")
    assert "Copy from zeta-skills." in installed, (
        f"fa init replaced the alpha-skills copy it installed with the zeta-skills copy:\n{installed}")
    assert "Copy from zeta-skills." in workers_copy(machine), "workers read the zeta-skills copy"
    assert sorted(path.name for path in skills.iterdir()) == before, "One copy replaced, none added"
    after = yaml.safe_load(project.read_text(encoding="utf-8"))
    assert after["default_skills"]["workers"] == [alternative, UNIQUE], after["default_skills"]
    assert after.get("lanes") == {"web": LANE}, f"A lane's fixed skills stay unchanged: {after.get('lanes')}"

    # A third run with the same setting keeps the replacement (no flip back to the picked id).
    code, output = run(argv, capsys)
    assert code == 0, output
    assert "Copy from zeta-skills." in (skills / PROVE / "SKILL.md").read_text(encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
