"""Acceptance test for lanes-skills-keeps-comments. Locked by Foragent; do not edit."""

import pytest
import yaml

from test_skills_add import skill_pack
from test_skills_autosync import cli_exit, isolated_home, write_global_config


SETTINGS = (
    b"# board: PVT_x  # paused while the board is reorganized  \n"
    b"repo: owner/repo\n"
    b"language: ko\n"
    b"\n"
    b"lanes:\n"
    b"  api:  # API reviews stay here  \n"
    b"    paths: ['api/**']\n"
    b"    # Keep this lane's review context.  \n"
    b"    skills: [tone]\n"
    b"    agent: fa-api\n"
    b"  web:\n"
    b"    paths: ['web/**']\n"
    b"    skills: [handmade]\n"
    b"    agent: fa-web\n"
    b"\n"
    b"# Keep this trailing note.\n"
    b"max_parallel: 3\n"
)
OLD_SKILLS_LINE = b"    skills: [tone]\n"


@pytest.fixture
def commented_lane(git_history, tmp_path, monkeypatch):
    source = skill_pack(git_history, tmp_path)
    write_global_config({"skill_libraries": [{"name": "local", "repo": str(source), "path": str(source)}],
                         "skill_roles": {"local:polish": "review", "local:tone": "review"}})
    root = git_history.clone
    settings = root / ".foragent.yml"
    settings.write_bytes(SETTINGS)
    agent = root / ".claude/agents/fa-api.md"
    agent.parent.mkdir(parents=True)
    agent.write_text("---\nname: fa-api\ndescription: API lane\nskills: [tone]\n---\nReview API work.\n")
    # Exercise real CLI file updates without committing or pushing lane setup.
    monkeypatch.setattr("foragent.git.GitRepo.commit_paths", lambda *a, **kw: None)
    monkeypatch.setattr("foragent.git.GitRepo.push_base", lambda *a, **kw: None)
    return root, settings


def assert_existing_lines_preserved(before, after, *, replaced_line=None):
    """Allow new list formatting or an added lane, preserving every other byte line in order."""
    remaining = iter(after.splitlines(keepends=True))
    for line in before.splitlines(keepends=True):
        if line == replaced_line:
            continue
        assert any(candidate == line for candidate in remaining), f"existing line lost or moved: {line!r}"


def test_lanes_skills_replacement_preserves_comments_and_other_lines(commented_lane):
    from foragent.cli import main

    root, settings = commented_lane
    before = settings.read_bytes()
    expected = yaml.safe_load(before)
    expected["lanes"]["api"]["skills"] = ["polish"]
    assert cli_exit(main, ["lanes", "skills", "api", "local:polish", "--repo-root", str(root)]) == 0
    after = settings.read_bytes()
    assert yaml.safe_load(after) == expected
    assert_existing_lines_preserved(before, after, replaced_line=OLD_SKILLS_LINE)


def test_lanes_skills_clear_preserves_comments_and_other_lines(commented_lane):
    from foragent.cli import main

    root, settings = commented_lane
    before = settings.read_bytes()
    expected = yaml.safe_load(before)
    expected["lanes"]["api"]["skills"] = []
    assert cli_exit(main, ["lanes", "skills", "api", "--repo-root", str(root)]) == 0
    after = settings.read_bytes()
    assert yaml.safe_load(after) == expected
    assert_existing_lines_preserved(before, after, replaced_line=OLD_SKILLS_LINE)


def test_lanes_add_preserves_existing_comments_and_line_order(commented_lane):
    from foragent.cli import main

    root, settings = commented_lane
    before = settings.read_bytes()
    expected = yaml.safe_load(before)
    expected["lanes"]["docs"] = {"paths": ["docs/**"], "agent": "fa-docs", "skills": ["polish"]}
    assert cli_exit(main, ["lanes", "add", "docs", "--paths", "docs/**", "--skill", "local:polish",
                           "--repo-root", str(root)]) == 0
    after = settings.read_bytes()
    assert yaml.safe_load(after) == expected
    assert_existing_lines_preserved(before, after)
