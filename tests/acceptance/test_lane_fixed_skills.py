"""Acceptance test for lane-fixed-skills. Locked by Foragent; do not edit."""

import yaml

from test_skills_autosync import cli_exit, isolated_home, write_global_config


def fixed_lane_case(git_history, tmp_path, monkeypatch):
    from foragent import cli
    from foragent.config import load_config
    from test_orch_review_handoff import bind_cli, make_case

    library = tmp_path / "library"
    for name in ("polish", "other-style"):
        skill = library / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(f"---\nname: {name}\ndescription: Style for {name}\n---\nWrite clearly.\n")
    (library / "LICENSE").write_text("SPDX-License-Identifier: MIT\n")
    write_global_config({"skill_libraries": [{"name": "local", "repo": str(git_history.origin),
                                             "path": str(library)}],
                         "skill_roles": {"local:polish": "style", "local:other-style": "style"}})
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[])
    main = bind_cli(case, monkeypatch)
    config = case.root / ".foragent.yml"
    config.write_text("repo: owner/repo\nlanes:\n  api:\n    paths: ['api/**']\n    agent: fa-api\n    skills: []\n")
    agent = case.root / ".claude/agents/fa-api.md"
    agent.parent.mkdir(parents=True)
    agent.write_text("---\nname: fa-api\ndescription: API lane\nskills: []\n---\nImplement API work.\n")
    monkeypatch.setattr(cli, "load_config", load_config)
    # The new command may publish lane setup, as lanes add does; never run Git commit/push.
    monkeypatch.setattr("foragent.git.GitRepo.commit_paths", lambda *a, **kw: None)
    monkeypatch.setattr("foragent.git.GitRepo.push_base", lambda *a, **kw: None)
    return case, main, config, agent


def test_lane_fixed_skills_set_installs_and_updates_both_declarations(git_history, tmp_path, monkeypatch):
    case, main, config, agent = fixed_lane_case(git_history, tmp_path, monkeypatch)
    assert main(["lanes", "skills", "api", "local:polish", "--repo-root", str(case.root)]) == 0
    assert yaml.safe_load(config.read_text())["lanes"]["api"]["skills"] == ["polish"]
    assert yaml.safe_load(agent.read_text().split("---", 2)[1])["skills"] == ["polish"]
    assert (case.root / ".claude/skills/polish/SKILL.md").is_file()


def test_lane_fixed_skills_empty_ids_clear_both_declarations(git_history, tmp_path, monkeypatch):
    case, main, config, agent = fixed_lane_case(git_history, tmp_path, monkeypatch)
    # Seed a nonempty lane so this cannot pass as a no-op.
    config.write_text(config.read_text().replace("skills: []", "skills: [polish]"))
    agent.write_text(agent.read_text().replace("skills: []", "skills: [polish]"))
    assert main(["lanes", "skills", "api", "--repo-root", str(case.root)]) == 0
    assert yaml.safe_load(config.read_text())["lanes"]["api"]["skills"] == []
    assert yaml.safe_load(agent.read_text().split("---", 2)[1])["skills"] == []


def test_lane_fixed_skills_two_styles_fail_atomically(git_history, tmp_path, monkeypatch, capsys):
    case, main, config, agent = fixed_lane_case(git_history, tmp_path, monkeypatch)
    before = (config.read_bytes(), agent.read_bytes())
    assert cli_exit(main, ["lanes", "skills", "api", "local:polish", "local:other-style",
                          "--repo-root", str(case.root)]) != 0
    assert "style" in capsys.readouterr().err.lower()
    assert (config.read_bytes(), agent.read_bytes()) == before
    assert not (case.root / ".claude/skills").exists()


def test_lane_fixed_skills_unknown_id_changes_no_files(git_history, tmp_path, monkeypatch, capsys):
    case, main, config, agent = fixed_lane_case(git_history, tmp_path, monkeypatch)
    before = {path.relative_to(case.root): path.read_bytes() for path in case.root.rglob("*")
              if path.is_file() and ".git" not in path.relative_to(case.root).parts}
    assert cli_exit(main, ["lanes", "skills", "api", "local:missing", "--repo-root", str(case.root)]) != 0
    assert "missing" in capsys.readouterr().err
    after = {path.relative_to(case.root): path.read_bytes() for path in case.root.rglob("*")
             if path.is_file() and ".git" not in path.relative_to(case.root).parts}
    assert after == before
