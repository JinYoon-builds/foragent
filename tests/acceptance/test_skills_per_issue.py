"""Acceptance test for skills-per-issue. Locked by Foragent; do not edit."""

import json
import pytest

from test_skills_autosync import isolated_home, write_global_config


def issue_skills_case(git_history, tmp_path, monkeypatch):
    from foragent.config import load_config
    from test_orch_review_handoff import make_case

    library = tmp_path / "library"
    descriptions = {"tone": "Check tone with a script", "polish-x": "Review prose consistency",
                    "house-style": "Apply the house style", "unused-style": "A different style",
                    "domain": "Understand this business"}
    for name, description in descriptions.items():
        skill = library / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(f"---\nname: {name}\ndescription: {description}\n---\nRead me.\n")
    (library / "LICENSE").write_text("SPDX-License-Identifier: MIT\n")
    write_global_config({"skill_libraries": [{"name": "local", "repo": str(git_history.origin),
                                             "path": str(library)}], "skill_roles": {
        "local:tone": "tool", "local:polish-x": "review", "local:house-style": "style",
        "local:unused-style": "style", "local:domain": "domain"}})
    case = make_case(git_history, tmp_path, monkeypatch, executor="codex", reviewer="claude",
                     skill_libraries=[], lanes={"api": {"paths": ["feature.txt"], "skills": ["house-style"]}})
    case.store.update_run(1, lane="api")
    # Load through the real configuration path: roles are global, lane settings local.
    import yaml

    (case.root / ".foragent.yml").write_text(yaml.safe_dump({
        "executor": "codex", "reviewer": "claude", "review": "fast", "approvers": ["human"],
        "repo": "owner/repo", "lanes": case.config.lanes,
    }))
    case.ctx.config = load_config(case.root)
    # The Claude reviewer answers with a /code-review result event, as the real CLI does.
    case.reviewer.steps = [json.dumps({"result": "(none)"})]
    return case, library, descriptions


def test_skills_per_issue_prompt_offers_nonstyle_catalog_and_preserves_fixed_skills(
    git_history, tmp_path, monkeypatch,
):
    from foragent.runner import run_issue

    case, library, descriptions = issue_skills_case(git_history, tmp_path, monkeypatch)
    seen = []

    def execute(kwargs):
        seen.append((kwargs["prompt"], (kwargs["worktree"] / ".claude/skills/house-style/SKILL.md").read_text()))
        return case.implement(kwargs)

    case.implementer.steps = [execute]
    assert run_issue(1, case.ctx) == "merged"
    prompt, copied = seen[0]
    assert "Available skills" in prompt
    assert "Lane skills" in prompt and "house-style" in prompt and "Apply the house style" in copied
    for name in ("tone", "polish-x", "domain"):
        assert name in prompt and descriptions[name] in prompt
        assert str((library / "skills" / name / "SKILL.md").resolve()) in prompt
    assert "unused-style" not in prompt
    assert "Skills used: <names>" in prompt and "Skills used: none" in prompt
    available = prompt.split("Available skills", 1)[1].split("Lane skills", 1)[0].split("\n## ", 1)[0]
    assert "house-style" not in available and "unused-style" not in available


@pytest.mark.parametrize("report,names,body_line", [
    ("Skills used: tone, polish-x", ["tone", "polish-x"], "Skills used: tone, polish-x"),
    ("Skills used: none", [], "Skills used: none"),
    ("", [], "Skills used: not reported"),
])
def test_skills_per_issue_final_report_is_recorded_in_state_and_pr(
    git_history, tmp_path, monkeypatch, report, names, body_line,
):
    from foragent.runner import run_issue

    case, _, _ = issue_skills_case(git_history, tmp_path, monkeypatch)

    def execute(kwargs):
        case.implement(kwargs)
        return "Implemented feature.\n" + report

    case.implementer.steps = [execute]
    assert run_issue(1, case.ctx) == "merged"
    assert case.store.get_run(1).skills_used == names
    assert body_line in case.tracker.created_prs[0].body.splitlines()
