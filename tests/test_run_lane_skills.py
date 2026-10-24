from dataclasses import replace
from datetime import timedelta

import pytest

from foragent.git import GitRepo
from foragent.runner import _lane_skills, goal_condition, run_issue
from foragent.scheduler import TickContext, TickReport, _kill_run, _recover_orphan, _recover_runs

from conftest import git_command
from runner_support import run_case, skill_case


@pytest.mark.parametrize("cleanup", ["kill", "crash", "orphan"])
def test_scheduler_excludes_injected_lane_skills_from_saved_and_resumed_work(run_case, cleanup):
    from foragent.runner import _inject_lane_skills

    c = run_case
    relative = ".claude/skills/testing"
    skill = c.ctx.repo_root / relative / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\ndescription: Focused tests\n---\nRun useful checks.\n")
    c.ctx.config = replace(c.ctx.config, lanes={"api": {"skills": ["testing"]}}, skill_libraries=[])
    c.store.update_run(1, lane="api", pid=None)
    worktree = c.store.worktree_path(1)
    c.ctx.git.add_worktree(worktree, "foragent/issue-1", "origin/main")
    git = c.ctx.git.at(worktree)
    _inject_lane_skills(c.ctx, 1, git)
    (worktree / "feature.txt").write_text("unfinished")
    handmade = ".claude/skills/handmade/SKILL.md"
    (worktree / handmade).parent.mkdir(parents=True)
    (worktree / handmade).write_text("User-authored skill\n")
    git_command(worktree, "add", "-f", "--", relative)
    ctx = TickContext(
        repo=c.ctx.repo, repo_root=c.ctx.repo_root, tracker=c.tracker, store=c.store,
        config=c.ctx.config, git=c.ctx.git, now=c.ctx.now, launcher=lambda n: None,
        kill_runner=lambda pid: None, runner_process_exists=lambda n: False,
    )
    report = TickReport()
    if cleanup == "kill":
        _kill_run(ctx, 1, report)
    elif cleanup == "crash":
        c.now[0] += timedelta(minutes=3)
        _recover_runs(ctx, report)
    else:
        c.store.finish_run(1, "error")
        c.now[0] += timedelta(seconds=1)
        _recover_orphan(ctx, c.tracker.get_issue(1), report)
    c.tracker.remove_labels(1, ["running"])
    for root in (c.history.clone, c.history.origin):
        published = git_command(root, "ls-tree", "-r", "--name-only", "foragent/issue-1").splitlines()
        assert relative + "/SKILL.md" not in published
        assert handmade in published
    assert c.store.get_run(1).local_skill_paths == [relative]
    c.now[0] += timedelta(minutes=1)
    c.store.start_run(1, branch="foragent/issue-1", worktree=worktree, lane="api")
    assert c.store.get_run(1).local_skill_paths == [relative]
    assert run_issue(1, c.ctx) == "merged"
    published = git_command(c.history.origin, "ls-tree", "-r", "--name-only", "foragent/issue-1").splitlines()
    assert relative + "/SKILL.md" not in published
    assert handmade in published


@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_lane_executor_model_agent_notes_and_outside_paths(run_case, kind):
    c = run_case
    definition = "---\nname: fa-ui\nmemory: project\n---\nUse the UI skills.\n"
    memory = "".join(f"memory line {line}\n" for line in range(1, 202))
    c.history.update({".claude/agents/fa-ui.md": definition,
                      ".claude/agent-memory/fa-ui/MEMORY.md": memory})
    c.store.update_run(1, lane="ui")
    c.ctx.config = replace(c.ctx.config, executor_model="project-model", lanes={
        "ui": {"paths": ["src/**"], "agent": "fa-ui", "executor": kind, "model": "lane-model"},
    })
    factories = []
    c.ctx.executor_factory = lambda name: factories.append(name) or c.executor
    assert run_issue(1, c.ctx) == "merged"
    assert factories == [kind]
    call = c.executor.calls[0]
    assert call["model"] == "lane-model"
    if kind == "claude":
        assert call["agent"] == "fa-ui" and "settings_path" in call
        assert call["goal"] == goal_condition(c.ctx.config.acceptance_dir)
        assert call["prompt"].startswith("You are implementing GitHub issue #1")
    else:
        assert "agent" not in call
        assert call["prompt"].startswith("Lane notes:\nUse the UI skills.")
        assert "memory: project" not in call["prompt"]
        assert "memory line 200\n" in call["prompt"]
        assert "memory line 201" not in call["prompt"]
    assert "## Outside lane\n\n- `feature.txt`" in c.tracker.created_prs[0].body
    assert git_command(c.history.origin, "show", "foragent/issue-1:.claude/agent-memory/fa-ui/MEMORY.md") + "\n" == memory


@pytest.mark.parametrize("lane", ["main", "ui"])
def test_lane_null_overrides_and_main_use_project_defaults(run_case, lane):
    c = run_case
    c.store.update_run(1, lane=lane)
    c.ctx.config = replace(c.ctx.config, executor_model="project-model", lanes={
        "main": {"executor": "claude", "model": "ignored", "agent": "fa-main"},
        "ui": {"executor": None, "model": None, "agent": "fa-missing"},
    })
    assert run_issue(1, c.ctx) == "merged"
    call = c.executor.calls[0]
    assert call["model"] == "project-model" and "agent" not in call
    assert not call["prompt"].startswith("Lane notes:")
    if lane == "main":
        assert "## Outside lane" not in c.tracker.created_prs[0].body


@pytest.mark.parametrize("kind", ["codex", "claude"])
@pytest.mark.parametrize("source", ["config", "frontmatter", "empty-config"])
def test_lane_skills_come_from_worktree_and_only_extend_codex_prompts(run_case, source, kind):
    c = run_case
    definition = "---\nname: fa-ui\nskills:\n- agent-skill\n- missing\n---\nLane instructions.\n"
    c.history.update({
        ".claude/agents/fa-ui.md": definition,
        ".claude/skills/configured/SKILL.md": "---\nname: configured\ndescription: Skill metadata\n---\nConfigured strategy.\n",
        ".claude/skills/agent-skill/SKILL.md": "---\nname: agent-skill\ndescription: Skill metadata\n---\nAgent strategy.\n",
    })
    # The coordinator's local file must not replace the remote worktree's skill.
    local = c.ctx.repo_root / ".claude/skills/configured/SKILL.md"
    local.parent.mkdir(parents=True)
    local.write_text("Wrong local strategy.\n")
    lane = {"agent": "fa-ui", "executor": kind}
    if source != "frontmatter":
        lane["skills"] = ["configured", "missing"] if source == "config" else []
    c.ctx.config = replace(c.ctx.config, lanes={"ui": lane})
    c.store.update_run(1, lane="ui")
    assert run_issue(1, c.ctx) == "merged"
    call = c.executor.calls[0]
    pr = c.tracker.created_prs[0]
    if kind == "codex":
        name = "configured" if source == "config" else "agent-skill"
        assert f"- {name}: Skill metadata (.claude/skills/{name}/SKILL.md)\n\n" in call["prompt"]
        assert call["prompt"].startswith("Lane notes:\nLane instructions.\n\nLane skills (read the file when relevant and follow it):\n")
        assert "Configured strategy." not in call["prompt"] and "Agent strategy." not in call["prompt"]
        assert "description: Skill metadata" not in call["prompt"]
        assert "Wrong local strategy." not in call["prompt"]
        assert "missing skill: missing" in pr.body
        assert "agent" not in call
    else:
        assert call["agent"] == "fa-ui"
        assert call["goal"] == goal_condition(c.ctx.config.acceptance_dir)
        assert call["prompt"].startswith("You are implementing GitHub issue #1")
        assert "Lane skills (read the file" not in call["prompt"]
        assert "Configured strategy." not in call["prompt"] and "Agent strategy." not in call["prompt"]
        assert "missing skill:" not in pr.body
    assert local.read_text() == "Wrong local strategy.\n"


@pytest.mark.parametrize("agent", [None, "fa-missing"])
def test_codex_configured_skills_do_not_require_an_agent_file(run_case, agent):
    c = run_case
    c.history.update({".claude/skills/testing/SKILL.md": "---\ndescription: Focused checks\n---\nUse focused checks.\n"})
    c.ctx.config = replace(c.ctx.config, lanes={"ui": {"agent": agent, "skills": ["testing"]}})
    c.store.update_run(1, lane="ui")
    assert run_issue(1, c.ctx) == "merged"
    assert c.executor.calls[0]["prompt"].startswith("Lane skills (read the file when relevant and follow it):\n"
                                                "- testing: Focused checks (.claude/skills/testing/SKILL.md)\n\n")
    assert "Use focused checks." not in c.executor.calls[0]["prompt"]
    assert "missing skill:" not in c.tracker.created_prs[0].body


@pytest.mark.parametrize("kind", ["codex", "claude"])
@pytest.mark.parametrize("source", ["local", "cache"])
def test_local_lane_skill_is_available_on_retries_and_never_published(run_case, tmp_path, monkeypatch, kind, source):
    c = run_case
    relative = ".claude/skills/testing"
    library = tmp_path / "library"
    skill = (c.ctx.repo_root / relative) if source == "local" else (library / "skills/testing")
    skill.mkdir(parents=True)
    content = "---\nname: testing\ndescription: Focused tests\n---\nRun useful checks.\n"
    (skill / "SKILL.md").write_text(content)
    (skill / "references").mkdir()
    (skill / "references/example.md").write_text("Reference\n")
    c.ctx.config = replace(c.ctx.config, executor=kind, lanes={"api": {"skills": ["testing"]}},
                           skill_libraries=[{"name": "fixture", "repo": "local", "path": str(library)}])
    c.store.update_run(1, lane="api")
    c.executor.steps = [{"feature.txt": "broken"}, {"feature.txt": "done"}]
    execute = c.executor.run

    def implement(**kwargs):
        worktree = kwargs["worktree"]
        assert (worktree / relative / "SKILL.md").read_text() == content
        assert (worktree / relative / "references/example.md").read_text() == "Reference\n"
        # Even explicitly staged copies must stay out of the published tree.
        git_command(worktree, "add", "-f", "--", relative)
        handmade = worktree / ".claude/skills/handmade/SKILL.md"
        handmade.parent.mkdir(parents=True, exist_ok=True)
        handmade.write_text("User-authored skill\n")
        return execute(**kwargs)

    monkeypatch.setattr(c.executor, "run", implement)
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 2
    published = git_command(c.history.origin, "ls-tree", "-r", "--name-only", "foragent/issue-1").splitlines()
    assert not any(name.startswith(relative + "/") for name in published)
    assert ".claude/skills/handmade/SKILL.md" in published


@pytest.mark.parametrize("length", [5999, 6000, 6001, 12000])
def test_lane_skill_list_includes_one_line_description_without_any_body(skill_case, length):
    ctx, worktree = skill_case
    path = worktree / ".claude/skills/testing/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: testing\ndescription: |\n  Useful metadata\n  on two lines.\n---\n" + "가" * length)
    notes, missing = _lane_skills(ctx, 1, worktree, None)
    assert missing == []
    assert notes == ("Lane skills (read the file when relevant and follow it):\n"
                     "- testing: Useful metadata on two lines. (.claude/skills/testing/SKILL.md)\n\n")
    assert "가" not in notes and "[truncated]" not in notes


@pytest.mark.parametrize("missing_name", [None, "first", "sixth"])
def test_lane_skills_list_every_entry_in_order_with_no_count_limit(skill_case, missing_name):
    ctx, worktree = skill_case
    names = ["third", "first", "second", "fourth", "fifth", "sixth"]
    ctx.config.lanes["ui"]["skills"] = names
    for name in names:
        if name == missing_name:
            continue
        path = worktree / ".claude/skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\ndescription: About {name}\n---\nInstructions for {name}.")
    notes, missing = _lane_skills(ctx, 1, worktree, None)
    selected = [name for name in names if name != missing_name]
    assert [line.split(":", 1)[0].removeprefix("- ") for line in notes.splitlines() if line.startswith("- ")] == selected
    assert missing == ([missing_name] if missing_name else [])
    assert ("- sixth: About sixth (.claude/skills/sixth/SKILL.md)" in notes) == (missing_name != "sixth")


@pytest.mark.parametrize("header", ["", "---\n---\n", "--- \nname: metadata\n--- \n"])
def test_lane_skills_without_a_description_keep_the_name_and_path(skill_case, header):
    ctx, worktree = skill_case
    path = worktree / ".claude/skills/testing/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(header + "Testing instructions.\n")
    assert _lane_skills(ctx, 1, worktree, None) == (
        "Lane skills (read the file when relevant and follow it):\n- testing:  (.claude/skills/testing/SKILL.md)\n\n", [],
    )


@pytest.mark.parametrize("existing_pr", [False, True])
def test_missing_lane_skill_is_recorded_once_in_created_or_updated_pr_after_retry(run_case, existing_pr):
    c = run_case
    if existing_pr:
        c.tracker.create_pr(head="foragent/issue-1", base="main", title="Existing", body="old")
    c.ctx.config = replace(c.ctx.config, lanes={"ui": {"skills": ["missing"]}})
    c.store.update_run(1, lane="ui")
    c.executor.steps = [{"feature.txt": "not ready"}, {"feature.txt": "done"}]
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 2
    assert all("Lane skills (read the file" not in call["prompt"] for call in c.executor.calls)
    assert len(c.tracker.created_prs) == 1
    assert c.tracker.created_prs[0].body.count("missing skill: missing") == 1


def test_lane_skills_do_not_read_a_symlink_outside_the_worktree(skill_case, tmp_path):
    ctx, worktree = skill_case
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("Outside instructions.")
    directory = worktree / ".claude/skills"
    directory.mkdir(parents=True)
    (directory / "testing").symlink_to(outside, target_is_directory=True)
    assert _lane_skills(ctx, 1, worktree, None) == ("", ["testing"])


@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("merge_fails", [False, True])
def test_active_injected_skill_becomes_target_owned_without_leaking_to_wip(run_case, monkeypatch, staged, merge_fails):
    from foragent.git import GitError

    c = run_case
    relative = ".claude/skills/testing"
    path = relative + "/SKILL.md"
    local = c.ctx.repo_root / path
    local.parent.mkdir(parents=True)
    local.write_text("Local injected skill\n")
    (local.parent / "injected-helper.py").write_text("# local dependency\n")
    c.ctx.config = replace(c.ctx.config, lanes={"api": {"skills": ["testing"]}}, skill_libraries=[])
    c.store.update_run(1, lane="api")
    execute = c.executor.run
    official = "Official repository skill\n"

    def implement(**kwargs):
        assert (kwargs["worktree"] / path).read_text() == "Local injected skill\n"
        if staged:
            git_command(kwargs["worktree"], "add", "--", relative)
        result = execute(**kwargs)
        c.history.update({path: official})
        return result

    monkeypatch.setattr(c.executor, "run", implement)
    merge_base = GitRepo.merge_base

    def refresh(git, target, **kwargs):
        if merge_fails:
            raise GitError("unrelated refresh failure")
        return merge_base(git, target, **kwargs)

    monkeypatch.setattr(GitRepo, "merge_base", refresh)
    assert run_issue(1, c.ctx) == ("error" if merge_fails else "merged")
    branch = "foragent/issue-1"
    if merge_fails:
        assert c.ctx.git.blob_sha(branch, path) is None
        assert git_command(c.history.origin, "ls-tree", "-r", "--name-only", branch, "--", relative) == ""
        assert not c.tracker.created_prs
    else:
        assert git_command(c.history.origin, "show", f"{branch}:{path}") == official.strip()
        assert c.ctx.git.blob_sha(branch, relative + "/injected-helper.py") is None
    assert c.store.get_run(1).local_skill_paths == []


@pytest.mark.parametrize("selected", [False, True])
def test_resumed_injected_skill_becomes_target_owned(run_case, monkeypatch, selected):
    c = run_case
    relative = ".claude/skills/testing"
    path = relative + "/SKILL.md"
    local = c.ctx.repo_root / path
    local.parent.mkdir(parents=True)
    local.write_text("Local injected skill\n")
    c.ctx.config = replace(c.ctx.config, lanes={"api": {"skills": ["testing"]}}, skill_libraries=[])
    c.store.update_run(1, lane="api")
    execute = c.executor.run

    def interrupt(**kwargs):
        return replace(execute(**kwargs), timed_out=True)

    monkeypatch.setattr(c.executor, "run", interrupt)
    assert run_issue(1, c.ctx) == "timeout"
    assert c.store.get_run(1).local_skill_paths == [relative]
    official = "Official repository skill\n"
    c.history.update({path: official})
    c.ctx.config = replace(c.ctx.config, lanes={"api": {"skills": ["testing"] if selected else []}})
    c.now[0] += timedelta(minutes=1)
    c.store.start_run(1, branch="foragent/issue-1", worktree=c.store.worktree_path(1), lane="api")
    c.executor.steps = [{"feature.txt": "done"}]

    def finish(**kwargs):
        assert (kwargs["worktree"] / path).read_text() == official
        return execute(**kwargs)

    monkeypatch.setattr(c.executor, "run", finish)
    assert run_issue(1, c.ctx) == "merged"
    assert c.store.get_run(1).local_skill_paths == []
    assert git_command(c.history.origin, "show", f"foragent/issue-1:{path}") == official.strip()


def test_lane_matched_paths_are_not_reported_outside(run_case):
    c = run_case
    c.store.update_run(1, lane="ui")
    c.ctx.config = replace(c.ctx.config, lanes={"ui": {"paths": ["**/*.txt"]}})
    assert run_issue(1, c.ctx) == "merged"
    assert "## Outside lane" not in c.tracker.created_prs[0].body
