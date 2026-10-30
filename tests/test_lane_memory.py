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
    (folder / "MEMORY.new.md").write_text("- [id:0] n0\nn1\nn2\nn3\nn4\nn5\n")
    (folder / "MEMORY.consumed.ids").write_text("0\n")
    config = Config(language="en", lanes={"api": {"agent": "fa-api"}})
    text = render_detail(config, "api", tmp_path)
    assert "n3" in text and "n4" not in text and "n0" not in text and "2 more new lessons" in text
    # The raw buffer still holds consumed lessons, so the view names no path to it.
    assert "MEMORY.new.md" not in text and "no memory yet" not in text


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
    ({"m/MEMORY.md": b"C\n", "m/MEMORY.new.md": b"P\n", "m/MEMORY.consumed.ids": b"a\n"}, True),
    # Lessons appended meanwhile only add pending ids; the dream consumed ids, not positions.
    ({"m/MEMORY.md": b"C\n", "m/MEMORY.new.md": b"P\nLate\n", "m/MEMORY.consumed.ids": b"a\n"}, True),
    ({"m/MEMORY.md": b"Hand edit\n", "m/MEMORY.new.md": b"P\n", "m/MEMORY.consumed.ids": b"a\n"}, False),
    ({"m/MEMORY.md": b"C\n", "m/MEMORY.new.md": b"P\n", "m/MEMORY.consumed.ids": b"a\nb\n"}, False),
    ({"m/MEMORY.md": b"C\n"}, False),
], ids=["unchanged", "late-append", "hand-edit", "consumed-moved", "consumed-missing"])
def test_dream_publishes_only_while_the_base_holds_its_curated_and_consumed_inputs(base, ok):
    git = _Git(base)
    captured = {"MEMORY.md": b"C\n", "MEMORY.new.md": b"P\n", "MEMORY.consumed.ids": b"a\n"}
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
    ({"MEMORY.new.md": "- [id:a] P\n- [id:b] Late\n"}, False, []),  # Appends are harmless: ids, not positions.
    ({"MEMORY.new.md": "- [id:a] P\n- [id:b] Late\n"}, True, []),  # Rebased: the dream's own files still match.
    ({"MEMORY.md": "Hand edit\n"}, False, ["m/MEMORY.md on main"]),
    ({"MEMORY.consumed.ids": "z\n"}, False, ["m/MEMORY.consumed.ids on main"]),
    ({"MEMORY.md": "Hand edit\n"}, True, ["m/MEMORY.md on main", "m/MEMORY.md on foragent/dream-api"]),
], ids=["unchanged", "late-append", "rebased-over-late-append", "hand-edit", "consumed-moved", "rebased-over-hand-edit"])
def test_merge_refuses_a_dream_pr_whose_recorded_inputs_or_outputs_moved(git_history, base_change, rebased, stale):
    from conftest import git_command
    from foragent.git import GitRepo

    def files(changes):
        return {("m/" + name if name.startswith("MEMORY") else name): text for name, text in changes.items()}

    base = {"MEMORY.md": "C\n", "MEMORY.new.md": "- [id:a] P\n", "MEMORY.consumed.ids": ""}
    dream = {"MEMORY.md": "C\nP\n", "MEMORY.consumed.ids": "a\n"}
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
    if rebased:  # A rebase replays the dream over the moved base; a conflicting MEMORY.md takes the base's text.
        replayed = {**dream, **({"MEMORY.md": base_change["MEMORY.md"]} if "MEMORY.md" in base_change else {})}
        git_history.update(files(replayed), message="dream, rebased")
        git_command(git_history.origin, "update-ref", "refs/heads/foragent/dream-api", git_history.tip)
        git_command(git_history.origin, "update-ref", "refs/heads/main", moved_base)
    digest = orch_dream_publish.fingerprints
    inputs = digest({name: text.encode() for name, text in base.items()})
    outputs = digest({name: text.encode() for name, text in {**base, **dream}.items()})
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


@pytest.mark.parametrize("pending, consumed, view", [
    ("- [id:a] A\n- [id:b] B\n", "a\n", "- [id:b] B\n"),
    ("- [id:a] A\n- [id:b] B\n- [id:b] B\n", "", "- [id:a] A\n- [id:b] B\n"),  # A union merge repeated b.
    ("- [id:b] B\n- [id:a] A\n", "a\nb\n", ""),  # Order does not matter; all consumed.
    ("Untagged lesson.\n- [id:a] A\n", "Untagged lesson.\n", "- [id:a] A\n"),  # Its text is its id.
    ("```diff\n-x\n```\n```diff\n-y\n```\n", "", "```diff\n-x\n```\n```diff\n-y\n```\n"),  # Untagged blocks stay whole.
    ("- [id:a] A\n", None, "- [id:a] A\n"),
    (None, "a\n", ""),
])
def test_pending_view_is_the_buffer_minus_consumed_ids(pending, consumed, view):
    assert lane_memory.pending_view(pending, consumed) == view


def test_the_dream_cannot_rewrite_pending_or_consumed_records(tmp_path):
    from foragent.orch_dream_worker import _keep_worker_files

    before = {"MEMORY.new.md": b"- [id:a] A\n", "MEMORY.consumed.ids": b"x\n"}
    (tmp_path / "MEMORY.new.md").write_bytes(b"")  # The dream truncated the buffer.
    (tmp_path / "MEMORY.consumed.ids").write_bytes(b"x\na\n")
    _keep_worker_files(tmp_path, before)
    assert (tmp_path / "MEMORY.new.md").read_bytes() == before["MEMORY.new.md"]
    (tmp_path / "MEMORY.consumed.ids").write_bytes(b"a\n")  # Lost the earlier id.
    with pytest.raises(RuntimeError, match="may only append"):
        _keep_worker_files(tmp_path, before)
    (tmp_path / "MEMORY.new.md").unlink()
    (tmp_path / "MEMORY.consumed.ids").write_bytes(b"x\n")
    _keep_worker_files(tmp_path, before)  # A deleted buffer comes back too.
    assert (tmp_path / "MEMORY.new.md").read_bytes() == before["MEMORY.new.md"]
    (tmp_path / "MEMORY.new.md").unlink()
    (tmp_path / "MEMORY.new.md").write_bytes(b"- [id:z] made up\n")
    _keep_worker_files(tmp_path, {})  # The dream created a buffer the base did not have.
    assert not (tmp_path / "MEMORY.new.md").exists()


def test_migration_replaces_the_first_split_instruction_once():
    first = lane_memory.migrate_agent(OLD, "fa-api").replace(lane_memory.instruction("fa-api"),
                                                             lane_memory._instruction_v1("fa-api"))
    new = lane_memory.migrate_agent(first, "fa-api")
    assert new.count(lane_memory.instruction("fa-api")) == 1 and lane_memory._instruction_v1("fa-api") not in new
    assert "MEMORY.consumed.ids" in new and lane_memory.migrate_agent(new, "fa-api") == new


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


def test_run_dream_publishes_the_pending_buffer_unchanged_even_when_the_dream_truncates_it(tmp_path, monkeypatch):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from foragent import orch_dream, orch_dream_worker, usage_events
    from foragent.executors import ExecResult
    from foragent.state import StateStore

    config = Config(executor="codex", lanes={"orch": {"agent": "fa-orch", "paths": ["src/**"]}})
    store = StateStore(tmp_path / "state")
    worktree = orch_dream.worktree_path(store, "orch")
    store.start_dream("orch", worktree=worktree)
    memory = worktree / ".claude/agent-memory/fa-orch"
    pending = b"- [id:a] Lesson A.\n"
    commits = []

    def prepare(*args):
        memory.mkdir(parents=True)
        (memory / "MEMORY.new.md").write_bytes(pending)
        return SimpleNamespace(commit_paths=lambda paths, title: commits.append(
            {file.name: file.read_bytes() for file in memory.iterdir()}))

    def execute(**kw):
        (memory / "MEMORY.md").write_text("A.\n")
        (memory / "MEMORY.new.md").write_bytes(b"")  # Against its instructions.
        (memory / "MEMORY.consumed.ids").write_bytes(b"a\n")
        return ExecResult(0, False, "Folded a.", kw["log_path"])

    monkeypatch.setattr(orch_dream_worker, "_dream_worktree", prepare)
    monkeypatch.setattr(orch_dream_worker, "_publish", lambda *args: 5)
    monkeypatch.setattr(usage_events, "record_from_log", lambda *a: None)
    ctx = SimpleNamespace(store=store, config=config, now=lambda: datetime.now(UTC),
                          git=SimpleNamespace(remove_worktree=lambda _: None),
                          executor_factory=lambda _: SimpleNamespace(run=execute), poll_hook=lambda execute, poll: execute())
    assert orch_dream_worker.run_dream("orch", ctx) == 0
    assert commits == [{"MEMORY.md": b"A.\n", "MEMORY.new.md": pending, "MEMORY.consumed.ids": b"a\n"}]


@pytest.mark.parametrize("saved", [False, True], ids=["no-full-text-dir", "full-text-dir"])
def test_long_pending_lessons_never_link_the_raw_buffer(tmp_path, saved):
    config = Config(lanes={"api": {"agent": "fa-api"}})
    folder = tmp_path / ".claude/agent-memory/fa-api"
    folder.mkdir(parents=True)
    (folder / "MEMORY.new.md").write_text("- [id:old] Consumed lesson.\n"
                                          + "".join(f"- [id:n{i}] Lesson {i}.\n" for i in range(250)))
    (folder / "MEMORY.consumed.ids").write_text("old\n")
    full = tmp_path / "full" if saved else None
    for text in (run_lane_skills.lane_notes(config, "api", tmp_path, full)[1],
                 run_lane_skills.lane_memory_notice(config, "api", tmp_path, full)):
        assert "MEMORY.new.md" not in text.split("Pending memory", 1)[1].split("\n", 1)[1]
        assert "Consumed lesson." not in text
        if saved:
            assert "Lesson 199." in text and "Lesson 200." not in text and "[cut: 50 more lines" in text
            copy = Path(text.split("full text: ", 1)[1].split("]", 1)[0])
            assert copy.parent == full and "Consumed lesson." not in copy.read_text()
            assert copy.read_text().count("- [id:") == 250
        else:
            assert "Lesson 249." in text and "[cut:" not in text


def test_migration_replaces_the_second_split_instruction_once():
    second = lane_memory.migrate_agent(OLD, "fa-api").replace(lane_memory.instruction("fa-api"),
                                                              lane_memory._instruction_v2("fa-api"))
    new = lane_memory.migrate_agent(second, "fa-api")
    assert new.count(lane_memory.instruction("fa-api")) == 1 and lane_memory._instruction_v2("fa-api") not in new
    assert lane_memory.migrate_agent(new, "fa-api") == new


@pytest.mark.parametrize("attributes", [
    "*.txt text\n.claude/agent-memory/**/MEMORY.md merge=union\n",
    ".claude/agent-memory/**/MEMORY.md text eol=lf merge=union\n",
    ".claude/agent-memory/**/MEMORY.md\t  merge=union  \n",
    "/.claude/agent-memory/*/MEMORY.md merge=union\n",
    "MEMORY.md merge=union\n",
    ".claude/agent-memory/** merge=union\n",
    ".claude/agent-memory/**/MEMORY.md merge=union\r\n*.md text\r\n",
    "",
], ids=["old-line", "more-attributes", "whitespace", "single-star", "basename", "whole-directory", "crlf", "empty"])
def test_attribute_migration_matches_git_for_every_spelling(git_history, attributes):
    from conftest import git_command

    root = git_history.clone
    (root / ".gitattributes").write_bytes(attributes.encode())
    migrated = lane_memory.migrate_attributes(attributes or None)
    (root / ".gitattributes").write_bytes(migrated.encode())

    def attribute(name, path):
        return git_command(root, "check-attr", name, "--", path).rsplit(": ", 1)[1]

    for lane in ("fa-api", "api"):
        assert attribute("merge", f".claude/agent-memory/{lane}/MEMORY.md") != "union"
        assert attribute("merge", f".claude/agent-memory/{lane}/MEMORY.new.md") == "union"
    if "eol=lf" in attributes:
        assert attribute("eol", ".claude/agent-memory/fa-api/MEMORY.md") == "lf", "Other attributes stay"
    if attributes.startswith("MEMORY.md"):
        assert attribute("merge", "docs/MEMORY.md") == "union", "Files outside lane memory keep their merge"
    assert lane_memory.migrate_attributes(migrated) == migrated
    assert ("\r\n" in migrated) == ("\r\n" in attributes) and ("\r\n" not in migrated or "\n" not in migrated.replace("\r\n", ""))


@pytest.mark.parametrize("header", [
    '"memory": project\nname: fa-api\nmodel: m\n',
    "memory:\n  project\nname: fa-api\nmodel: m\n",
    "name: fa-api\nmemory: !!str project # native\nmodel: m\n",
    "name: fa-api\nmemory: 'project'\nmodel: m\n",
    "{name: fa-api, memory: project, model: m}\n",
], ids=["quoted-key", "block-value", "tagged", "quoted-value", "flow"])
def test_native_memory_is_removed_in_every_yaml_spelling(header):
    import yaml

    text = "---\n" + header + "---\nBody.\n"
    assert lane_memory.native(text)
    new = lane_memory.migrate_agent(text, "fa-api")
    assert not lane_memory.native(new) and lane_memory.migrate_agent(new, "fa-api") == new
    data = yaml.safe_load(new.split("---\n")[1])
    assert data == {"name": "fa-api", "model": "m"}
    if not header.startswith("{"):
        assert "name: fa-api\n" in new and "model: m\n" in new, "Other entries keep their bytes"


def test_other_memory_scopes_are_not_native_lane_memory():
    text = "---\nname: fa-api\nmemory: user\n---\nBody.\n"
    assert not lane_memory.native(text) and "memory: user" in lane_memory.migrate_agent(text, "fa-api")
