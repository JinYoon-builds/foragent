"""Acceptance test for skills-local. Locked by Foragent; do not edit."""


def library_manager(tmp_path, *, license="MIT"):
    from foragent.config import Config
    from foragent.skills import SkillManager

    library = tmp_path / "library"
    skill = library / "skills/testing-strategy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: testing-strategy\ndescription: Offline test skill\n---\nUse sharp tests.\n")
    (library / "LICENSE").write_text(f"SPDX-License-Identifier: {license}\n")
    config = Config(skill_libraries=[{"name": "fixture", "repo": "https://example.test/library", "path": str(library)}])
    manager = SkillManager(config, run=lambda argv: "a" * 40)
    identity = manager.catalog()[0]["id"]
    return manager, identity, config


def test_install_is_local_ignored_once_and_preserves_user_skills(git_history, tmp_path, monkeypatch):
    from conftest import git_command
    from foragent import cli
    from test_orch_review_handoff import bind_cli, make_case

    case = make_case(git_history, tmp_path, monkeypatch)
    main = bind_cli(case, monkeypatch)
    manager, identity, config = library_manager(tmp_path)
    monkeypatch.setattr(cli, "SkillManager", lambda *a, **kw: manager)
    monkeypatch.setattr(cli, "load_global_config", lambda: config)
    handmade = case.root / ".claude/skills/handmade/SKILL.md"
    handmade.parent.mkdir(parents=True)
    handmade.write_text("User authored skill\n")
    head = git_command(case.root, "rev-parse", "HEAD")
    for _ in range(2):
        assert main(["skills", "install", identity, "--repo-root", str(case.root)]) == 0
    installed = case.root / ".claude/skills/testing-strategy/SKILL.md"
    assert installed.read_text().endswith("Use sharp tests.\n")
    assert handmade.read_text() == "User authored skill\n"
    assert git_command(case.root, "rev-parse", "HEAD") == head
    assert git_command(case.root, "ls-files", "--", ".claude/skills/testing-strategy") == ""
    assert git_command(case.root, "check-ignore", ".claude/skills/testing-strategy/SKILL.md")
    rules = (case.root / ".gitignore").read_text().splitlines()
    assert len([rule for rule in rules if "testing-strategy" in rule]) == 1


def test_lane_add_keeps_library_skill_out_of_setup_commit(git_history, tmp_path, monkeypatch):
    from foragent import cli
    from foragent.git import GitRepo
    from test_orch_review_handoff import bind_cli, make_case

    import yaml

    case = make_case(git_history, tmp_path, monkeypatch)
    main = bind_cli(case, monkeypatch)
    manager, identity, config = library_manager(tmp_path)
    staged = []
    monkeypatch.setattr("foragent.lanes.SkillManager", lambda *a, **kw: manager)
    monkeypatch.setattr(GitRepo, "commit_paths", lambda self, paths, title: staged.extend(paths))
    monkeypatch.setattr(GitRepo, "push_base", lambda *a, **kw: None)
    assert main(["lanes", "add", "api", "--paths", "api/**", "--skill", identity]) == 0
    lane = yaml.safe_load((case.root / ".foragent.yml").read_text())["lanes"]["api"]
    assert lane["skills"] == ["testing-strategy"]
    assert (case.root / ".claude/skills/testing-strategy/SKILL.md").is_file()
    assert staged and not any(path.startswith(".claude/skills/testing-strategy") for path in staged)
    assert "testing-strategy" in (case.root / ".gitignore").read_text()


def test_run_injects_local_skill_before_executor_without_staging_it(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue
    from test_orch_review_handoff import make_case

    case = make_case(git_history, tmp_path, monkeypatch, lanes={"api": {
        "paths": ["feature.txt"], "skills": ["testing-strategy"]}})
    case.store.update_run(1, lane="api")
    skill = case.root / ".claude/skills/testing-strategy/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: testing-strategy\ndescription: Local only\n---\nLocal skill content\n")
    seen = []

    def execute(kwargs):
        path = kwargs["worktree"] / ".claude/skills/testing-strategy/SKILL.md"
        seen.append(path.read_text() if path.exists() else None)
        case.implement(kwargs)

    case.implementer.steps = [execute]
    assert run_issue(1, case.ctx) == "merged"
    assert seen == [skill.read_text()]
    assert case.snapshots and all(".claude/skills/testing-strategy/SKILL.md" not in files for files in case.snapshots)


def test_nonopen_license_warns_locally_but_commit_opt_in_refuses(git_history, tmp_path, monkeypatch, capsys):
    from foragent import cli
    from test_orch_review_handoff import bind_cli, make_case

    case = make_case(git_history, tmp_path, monkeypatch)
    main = bind_cli(case, monkeypatch)
    manager, identity, config = library_manager(tmp_path, license="Proprietary")
    monkeypatch.setattr(cli, "SkillManager", lambda *a, **kw: manager)
    monkeypatch.setattr(cli, "load_global_config", lambda: config)
    assert main(["skills", "install", identity, "--repo-root", str(case.root)]) == 0
    output = capsys.readouterr()
    assert "Proprietary" in output.out + output.err
    assert (case.root / ".claude/skills/testing-strategy/SKILL.md").exists()
    destination = tmp_path / "commit-opt-in"
    destination.mkdir()
    assert main(["skills", "install", identity, "--repo-root", str(destination), "--commit"]) != 0
    assert not (destination / ".claude/skills/testing-strategy").exists()
