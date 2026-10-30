"""Lane memory's two writers: migration, readers and the dream's base check (D95)."""

from pathlib import Path

import pytest

from foragent import lane_memory, orch_dream_publish, run_lane_skills
from foragent.config import Config
from test_init import setup  # noqa: F401

OLD = ("---\nname: fa-api\ndescription: 'Lane api'\nmemory: project\nskills:\n- x\nmodel: m\n---\n"
       "Body first.\n" + lane_memory.OLD_WRITER + "\nBody last.\n")


def test_migrate_agent_replaces_only_managed_lines_and_is_idempotent():
    new = lane_memory.migrate_agent(OLD, "fa-api")
    assert "memory: project" not in new and lane_memory.OLD_WRITER not in new
    assert new == OLD.replace("memory: project\n", "").replace(lane_memory.OLD_WRITER, lane_memory.instruction("fa-api"))
    assert new.count(lane_memory.instruction("fa-api")) == 1
    assert lane_memory.migrate_agent(new, "fa-api") == new
    custom = "---\nname: fa-api\n---\nMine."
    assert lane_memory.migrate_agent(custom, "fa-api") == custom + "\n" + lane_memory.instruction("fa-api") + "\n"


def test_migrate_skips_roles_and_missing_agents_and_moves_union(tmp_path):
    agents = tmp_path / ".claude/agents"
    agents.mkdir(parents=True)
    (agents / "fa-api.md").write_text(OLD)
    role = "---\nname: fa-docs\nmemory: project\n---\n" + lane_memory.OLD_WRITER + "\n"
    (agents / "fa-docs.md").write_text(role)
    (tmp_path / ".gitattributes").write_text("*.txt text\n" + lane_memory.OLD_UNION + "\n*.md diff\n")
    config = Config(lanes={"api": {"agent": "fa-api"}, "web": {"agent": "fa-web"}, "docs": {"agent": "fa-docs"}},
                    roles={"docs": {"agent": "fa-docs"}})
    changes = lane_memory.migrate(tmp_path, config)
    assert sorted(changes) == [".claude/agents/fa-api.md", ".gitattributes"]
    assert changes[".gitattributes"] == "*.txt text\n" + lane_memory.UNION + "\n*.md diff\n"
    lane_memory.apply(tmp_path, changes)
    assert lane_memory.migrate(tmp_path, config) == {}
    assert (agents / "fa-docs.md").read_text() == role and not (agents / "fa-web.md").exists()
    assert not (tmp_path / ".claude/agent-memory").exists(), "Pending files are created lazily"


@pytest.mark.parametrize("lanes", [{}, {"api": {"paths": ["api/**"]}}, {"api": {"agent": "fa-api"}}],
                         ids=["no-lanes", "agentless-lane", "missing-agent-file"])
def test_every_project_gets_the_pending_union_line_once(tmp_path, lanes):
    expected = {".gitattributes": f"{lane_memory.UNION_COMMENT}\n{lane_memory.UNION}\n"}
    assert lane_memory.migrate(tmp_path, Config(lanes=lanes)) == expected
    lane_memory.apply(tmp_path, expected)
    assert lane_memory.union_change(tmp_path) is None and lane_memory.migrate(tmp_path, Config(lanes=lanes)) == {}


@pytest.mark.parametrize("native", [False, True])
def test_claude_notice_carries_curated_unless_loaded_natively(tmp_path, native):
    config = Config(lanes={"api": {"agent": "fa-api"}})
    agent = tmp_path / ".claude/agents/fa-api.md"
    agent.parent.mkdir(parents=True)
    agent.write_text(OLD if native else lane_memory.migrate_agent(OLD, "fa-api"))
    folder = tmp_path / ".claude/agent-memory/fa-api"
    folder.mkdir(parents=True)
    (folder / "MEMORY.md").write_text("Curated lesson.\n")
    (folder / "MEMORY.new.md").write_text("Pending lesson.\n")
    notice = run_lane_skills.lane_memory_notice(config, "api", tmp_path)
    assert notice.count("Pending lesson.") == 1
    assert notice.count("Curated lesson.") == (0 if native else 1)
    _, notes = run_lane_skills.lane_notes(config, "api", tmp_path)
    assert notes.count("Curated lesson.") == notes.count("Pending lesson.") == 1


def test_detail_shows_three_lines_of_each_file_with_paths(tmp_path):
    from foragent.watch_sections.detail import render_detail

    folder = tmp_path / ".claude/agent-memory/fa-api"
    folder.mkdir(parents=True)
    (folder / "MEMORY.new.md").write_text("n1\nn2\nn3\nn4\n")
    config = Config(language="en", lanes={"api": {"agent": "fa-api"}})
    text = render_detail(config, "api", tmp_path)
    assert "n3" in text and "n4" not in text and str((folder / "MEMORY.new.md").absolute()) in text
    assert "MEMORY.md\n" not in text + "\n" and "no memory yet" not in text


class _Git:
    def __init__(self, files):
        self.files, self.fetched = files, []

    def fetch(self, remote, ref):
        self.fetched.append((remote, ref))

    def blob_sha(self, ref, path):
        return path if path in self.files else None

    def blob_bytes(self, sha):
        return self.files[sha.decode()]


@pytest.mark.parametrize("base, ok", [
    ({"m/MEMORY.md": b"C\n", "m/MEMORY.new.md": b"P\n"}, True),
    ({"m/MEMORY.md": b"C\n", "m/MEMORY.new.md": b"P\nLate\n"}, False),
    ({"m/MEMORY.md": b"Hand edit\n", "m/MEMORY.new.md": b"P\n"}, False),
    ({"m/MEMORY.md": b"C\n"}, False),
])
def test_dream_publishes_only_while_the_base_holds_what_it_read(base, ok):
    git = _Git(base)
    captured = {"MEMORY.md": b"C\n", "MEMORY.new.md": b"P\n"}
    if ok:
        orch_dream_publish.check_base(git, "main", "m/MEMORY.md", captured)
    else:
        with pytest.raises(RuntimeError, match="changed on main"):
            orch_dream_publish.check_base(git, "main", "m/MEMORY.md", captured)
    assert git.fetched == [("origin", "main")]


def test_this_repository_lane_agents_are_migrated():
    from foragent.config import load_config

    root = Path(__file__).resolve().parents[1]
    assert lane_memory.migrate(root, load_config(root)) == {}


@pytest.mark.parametrize("base_change, rebased, stale", [
    (None, False, []),
    ({"MEMORY.new.md": "P\nLate\n"}, False, ["m/MEMORY.new.md on main"]),  # A union merge would bring P back.
    # The branch rebased over the late append: merge-base is the new base, and union kept P.
    ({"MEMORY.new.md": "P\nLate\n"}, True, ["m/MEMORY.new.md on main", "m/MEMORY.new.md on foragent/dream-api"]),
    ({"MEMORY.md": "Hand edit\n"}, False, ["m/MEMORY.md on main"]),
    ({"other.txt": "unrelated\n"}, False, []),
    ({"other.txt": "unrelated\n"}, True, []),  # Rebased over an unrelated change: the memory still matches.
], ids=["unchanged", "late-append", "rebased-over-late-append", "hand-edit", "unrelated", "rebased-unrelated"])
def test_merge_refuses_a_dream_pr_whose_recorded_inputs_or_outputs_moved(git_history, base_change, rebased, stale):
    from conftest import git_command
    from foragent.git import GitRepo

    def files(changes):
        return {("m/" + name if name.startswith("MEMORY") else name): text for name, text in changes.items()}

    base = {"MEMORY.md": "C\n", "MEMORY.new.md": "P\n"}
    dream = {"MEMORY.md": "C\nP\n", "MEMORY.new.md": ""}
    git_history.update(files(base))
    fork = git_history.tip
    if not rebased:  # The dream branch leaves the base before it moves.
        git_history.update(files(dream), message="dream")
        git_command(git_history.origin, "update-ref", "refs/heads/foragent/dream-api", git_history.tip)
        git_command(git_history.origin, "update-ref", "refs/heads/main", fork)
        git_history.tip = fork
    if base_change:
        git_history.update(files(base_change), message="base moves")
    moved_base = git_history.tip
    if rebased:  # What a union rebase leaves: the base's late appends kept, the dream's curated text.
        rebased_files = {**dream, "MEMORY.new.md": (base_change or {}).get("MEMORY.new.md", "")}
        git_history.update(files(rebased_files), message="dream, rebased")
        git_command(git_history.origin, "update-ref", "refs/heads/foragent/dream-api", git_history.tip)
        git_command(git_history.origin, "update-ref", "refs/heads/main", moved_base)
    digest = orch_dream_publish.fingerprints
    inputs = digest({name: text.encode() for name, text in base.items()})
    outputs = digest({name: text.encode() for name, text in dream.items()})
    found = orch_dream_publish.memory_moved(GitRepo(git_history.clone), "main", "foragent/dream-api", "m",
                                            inputs, outputs)
    assert found == stale


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("bom", ["", "\ufeff"])
def test_migration_handles_crlf_and_bom_agent_files(newline, bom):
    old = bom + OLD.replace("\n", newline)
    new = lane_memory.migrate_agent(old, "fa-api")
    assert "memory: project" not in new and not lane_memory.native(new) and lane_memory.native(old)
    assert new == old.replace("memory: project" + newline, "").replace(lane_memory.OLD_WRITER, lane_memory.instruction("fa-api"))
    assert lane_memory.migrate_agent(new, "fa-api") == new
    custom = bom + newline.join(["---", "name: fa-api", "memory: 'project'  # native", "---", "Mine."])
    migrated = lane_memory.migrate_agent(custom, "fa-api")
    assert migrated == custom.replace("memory: 'project'  # native" + newline, "") + newline + lane_memory.instruction("fa-api") + newline
    assert lane_memory.migrate_attributes("*.md diff" + newline + lane_memory.OLD_UNION + newline) == (
        "*.md diff" + newline + lane_memory.UNION + newline)


def test_a_lane_without_an_agent_shares_one_memory_directory_across_readers_and_the_dream(tmp_path):
    from foragent import orch_dream
    from foragent.watch_sections.detail import render_detail

    config = Config(language="en", lanes={"api": {"paths": ["api/**"]}})
    folder = tmp_path / ".claude/agent-memory/api"
    folder.mkdir(parents=True)
    (folder / "MEMORY.new.md").write_text("Pending lesson.\n")
    (tmp_path / ".claude/agents").mkdir(parents=True)
    (tmp_path / ".claude/agents/fa-api.md").write_text(OLD)  # Not the lane's agent: no executor reads it.
    assert orch_dream.memory_path(config, "api") == ".claude/agent-memory/api/MEMORY.md"
    assert list(lane_memory.migrate(tmp_path, config)) == [".gitattributes"], "Only an explicit lane agent gets the instruction"
    agent, notes = run_lane_skills.lane_notes(config, "api", tmp_path)
    assert agent is None and notes.count("Pending lesson.") == 1
    assert run_lane_skills.lane_memory_notice(config, "api", tmp_path).count("Pending lesson.") == 1
    assert render_detail(config, "api", tmp_path).count("Pending lesson.") == 1


def test_rerun_publishes_a_migration_whose_commit_failed(setup):
    import yaml
    from conftest import git_command
    from foragent.git import GitError

    settings = {"repo": "owner/repo", "language": "en", "skill_libraries": [],
                "lanes": {"api": {"agent": "fa-api", "paths": ["api/**"], "skills": []}}}
    (setup.root / ".foragent.yml").write_text(yaml.safe_dump(settings))
    (setup.root / ".claude/agents").mkdir(parents=True)
    (setup.root / ".claude/agents/fa-api.md").write_text(OLD)
    run = dict(no_push=True, no_launchd=True, no_board=True, approvers=["human"], executor="codex")
    setup.commit_fails = True
    with pytest.raises(GitError):
        setup.init(**run)
    assert lane_memory.migrate(setup.root, Config(lanes=settings["lanes"])) == {}, "The migration wrote its files"
    setup.commit_fails = False
    report = setup.init(**run)
    assert report.committed
    assert git_command(setup.root, "status", "--porcelain", "--", ".claude/agents", ".gitattributes") == ""
    assert lane_memory.instruction("fa-api") in git_command(setup.root, "show", "HEAD:.claude/agents/fa-api.md")
    assert lane_memory.UNION in git_command(setup.root, "show", "HEAD:.gitattributes")


@pytest.mark.parametrize("fork, ours, theirs, merged", [
    (b"P\n", b"P\nLate\n", b"", b"Late\n"),  # The dream consumed P; the worker's append stays.
    (b"P\n", b"P\nL\n", b"P\nB\n", b"P\nB\nL\n"),  # Both appended: the base's order, then the branch's.
    (b"P\n", b"P\n", b"", b""),  # Nothing appended on the branch.
    (b"P\nQ\n", b"P\nX\nR\n", b"", b"X\nR\n"),  # A branch that edited earlier lines keeps what follows the common start.
    (b"P", b"PLate\n", b"B", b"B\nLate\n"),
])
def test_merge_pending_keeps_the_base_buffer_and_only_the_branch_appends(fork, ours, theirs, merged):
    assert lane_memory.merge_pending(fork, ours, theirs) == merged


@pytest.mark.parametrize("union", [True, False], ids=["union", "no-attributes"])
@pytest.mark.parametrize("commit", [True, False])
@pytest.mark.parametrize("branch_appends", [True, False])
def test_merging_the_base_into_a_worker_branch_never_restores_consumed_pending_lines(
        git_history, tmp_path, commit, branch_appends, union):
    from conftest import git_command
    from foragent.git import GitRepo

    pending, other = ".claude/agent-memory/fa-api/MEMORY.new.md", "src/feature.py"
    attributes = {".gitattributes": lane_memory.UNION + "\n"} if union else {}
    git_history.update({pending: "P\n", other: "A = 1\n", **attributes})
    fork = git_history.tip
    worker = {other: "A = 2\n", **({pending: "P\nLate\n"} if branch_appends else {})}
    git_history.update(worker, message="worker")
    git_command(git_history.origin, "update-ref", "refs/heads/foragent/issue-7", git_history.tip)
    git_command(git_history.origin, "update-ref", "refs/heads/main", fork)
    git_history.tip = fork
    git_history.update({pending: "", ".claude/agent-memory/fa-api/MEMORY.md": "P\n"}, message="dream")
    clone = GitRepo(git_history.clone)
    clone.fetch("origin", "main")
    clone.fetch("origin", "foragent/issue-7")
    assert clone.pending_both_changed("origin/foragent/issue-7", "origin/main") == ([pending] if branch_appends else [])
    worktree = tmp_path / "worker"
    clone.add_detached_worktree(worktree, "origin/foragent/issue-7")
    branch = GitRepo(worktree)
    branch.merge_base("origin/main", commit=commit)
    expected = "Late\n" if branch_appends else ""
    assert (worktree / pending).read_text() == expected
    assert git_command(worktree, "show", f":{pending}") + ("\n" if expected else "") == expected
    assert branch.merge_in_progress() is (not commit)
    if commit:
        assert git_command(worktree, "show", f"HEAD:{pending}") + ("\n" if expected else "") == expected
        assert len(git_command(worktree, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3, "A merge commit"
    assert (worktree / other).read_text() == "A = 2\n"


@pytest.mark.parametrize("lane, agent_text", [
    ({"paths": ["api/**"]}, None),  # No agent at all.
    ({"agent": "fa-api"}, None),  # The named agent file is missing.
    ({"agent": "fa-api"}, OLD),  # Not migrated yet.
    ({"agent": "fa-api"}, lane_memory.migrate_agent(OLD, "fa-api")),  # Already carries the contract.
], ids=["agentless", "missing-file", "unmigrated", "migrated"])
def test_every_lane_executor_gets_the_writer_contract_exactly_once(tmp_path, lane, agent_text):
    config = Config(lanes={"api": lane})
    if agent_text is not None:
        (tmp_path / ".claude/agents").mkdir(parents=True)
        (tmp_path / ".claude/agents/fa-api.md").write_text(agent_text)
    contract = lane_memory.instruction(lane_memory.memory_dir(config, "api"))
    _, notes = run_lane_skills.lane_notes(config, "api", tmp_path)
    assert notes.count(contract) == 1, "Codex: the agent body or the reader carries it, never both"
    notice = run_lane_skills.lane_memory_notice(config, "api", tmp_path)
    # Claude reads the agent file itself; the notice adds the contract only when that file lacks it.
    carried = agent_text is not None and contract in agent_text
    assert notice.count(contract) == (0 if carried else 1)
