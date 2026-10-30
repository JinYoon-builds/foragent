"""Readers see curated and pending memory; the dream receives both inputs."""

from pathlib import Path
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from test_init import setup  # noqa: F401
from foragent import orch_dream_worker, run_lane_skills
from foragent.config import Config
from foragent.state import StateStore
from foragent.executors import ExecResult
from foragent.watch_sections.detail import render_detail


@pytest.mark.parametrize("curated", [False, True])
def test_lane_prompt_native_notice_and_view_read_pending_memory(tmp_path, curated):
    config = Config(language="en", lanes={"orch": {"agent": "fa-orch", "paths": ["src/**"]}})
    agent = tmp_path / ".claude/agents/fa-orch.md"
    agent.parent.mkdir(parents=True)
    agent.write_text("---\nname: fa-orch\nmemory: project\n---\nRead both memory files.\n")
    memory = tmp_path / ".claude/agent-memory/fa-orch"
    memory.mkdir(parents=True)
    if curated:
        (memory / "MEMORY.md").write_text("Existing curated lesson.\n")
    pending = "A new lesson on preserving the selected locale.\n"
    (memory / "MEMORY.new.md").write_text(pending)
    name, notes = run_lane_skills.lane_notes(config, "orch", tmp_path)
    assert name == "fa-orch" and notes.count(pending.strip()) == 1
    assert ("Existing curated lesson." in notes) is curated
    native = run_lane_skills.lane_memory_notice(config, "orch", tmp_path)
    assert pending.strip() in native, "Native MEMORY.md loading does not include the pending buffer"
    detail = render_detail(config, "orch", tmp_path)
    assert detail.count(pending.strip()) == 1
    assert (memory / "MEMORY.new.md").read_text() == pending, "Readers cannot clear pending lessons"


def test_dream_prompt_and_existing_lane_agents_assign_separate_memory_writers(tmp_path):
    config = Config(lanes={"orch": {"agent": "fa-orch", "paths": ["src/**"]}})
    store = StateStore(tmp_path / "state")
    prompt = orch_dream_worker.prompt(store, config, "orch", {})
    assert ".claude/agent-memory/fa-orch/MEMORY.new.md" in prompt
    assert "MEMORY.md" in prompt and any(word in prompt.lower() for word in ("fold", "incorporate", "consolidate"))
    root = Path(__file__).resolve().parents[2]
    import yaml
    lanes = yaml.safe_load((root / ".foragent.yml").read_text())["lanes"]
    for lane, settings in lanes.items():
        agent = root / ".claude/agents" / (settings.get("agent", f"fa-{lane}") + ".md")
        text = agent.read_text()
        assert "MEMORY.new.md" in text, f"Existing {lane} agent still writes the curated memory"
        assert "dream" in text.lower() and "MEMORY.md" in text
    attributes = (root / ".gitattributes").read_text().splitlines()
    assert ".claude/agent-memory/**/MEMORY.new.md merge=union" in attributes
    assert ".claude/agent-memory/**/MEMORY.md merge=union" not in attributes


@pytest.mark.parametrize("fails", [False, True])
def test_dream_publishes_curated_and_consumed_pending_lines_together_or_keeps_base(tmp_path, monkeypatch, fails):
    from foragent import orch_dream, usage_events

    config = Config(executor="codex", lanes={"orch": {"agent": "fa-orch", "paths": ["src/**"]}})
    store = StateStore(tmp_path / "state")
    worktree = orch_dream.worktree_path(store, "orch")
    store.start_dream("orch", worktree=worktree)
    source = tmp_path / "base/.claude/agent-memory/fa-orch"
    source.mkdir(parents=True)
    (source / "MEMORY.md").write_text("Curated.\n")
    (source / "MEMORY.new.md").write_text("Pending locale lesson.\n")
    commits, publications, turns = [], [], []
    memory = worktree / ".claude/agent-memory/fa-orch"

    def prepare(*args):
        memory.mkdir(parents=True)
        for file in source.iterdir():
            (memory / file.name).write_bytes(file.read_bytes())
        return SimpleNamespace(commit_paths=lambda paths, title: commits.append(
            {file.name: file.read_text() for file in memory.iterdir()}))

    def execute(**kw):
        turns.append(kw)
        assert "MEMORY.new.md" in kw["prompt"], "Dream lacks its pending-memory input and fold/clear authority"
        (memory / "MEMORY.md").write_text("Curated.\nPending locale lesson.\n")
        (memory / "MEMORY.new.md").write_text("")
        return ExecResult(7 if fails else 0, False, "Folded captured pending input.", kw["log_path"])

    monkeypatch.setattr(orch_dream_worker, "_dream_worktree", prepare)
    monkeypatch.setattr(orch_dream_worker, "_publish", lambda *args: publications.append(args) or 123)
    monkeypatch.setattr(usage_events, "record_from_log", lambda *a: None)
    ctx = SimpleNamespace(store=store, config=config, now=lambda: datetime.now(UTC),
        git=SimpleNamespace(remove_worktree=lambda _: None), executor_factory=lambda _: SimpleNamespace(run=execute),
        poll_hook=lambda execute, poll: execute())
    assert orch_dream_worker.run_dream("orch", ctx) == (1 if fails else 0)
    assert len(turns) == 1 and len(publications) == len(commits) == (0 if fails else 1)
    if not fails:
        assert commits == [{"MEMORY.md": "Curated.\nPending locale lesson.\n", "MEMORY.new.md": ""}]
        assert store.get_dream("orch").outcome == "dreamed"
    assert (source / "MEMORY.md").read_text() == "Curated.\n"
    assert (source / "MEMORY.new.md").read_text() == "Pending locale lesson.\n"


def test_reinit_migrates_other_project_lane_memory_idempotently(setup, monkeypatch):
    from foragent.git import GitRepo
    import yaml
    monkeypatch.setattr(GitRepo, "commit_paths", lambda *a, **kw: None)
    monkeypatch.setattr(GitRepo, "push_base", lambda *a, **kw: pytest.fail("No publication"))
    settings = {"repo": "owner/repo", "language": "en", "skill_libraries": [],
                "lanes": {"custom": {"agent": "fa-custom", "paths": ["src/**"], "skills": []}}}
    (setup.root / ".foragent.yml").write_text(yaml.safe_dump(settings))
    agents = setup.root / ".claude/agents"
    agents.mkdir(parents=True)
    agent = agents / "fa-custom.md"
    agent.write_text("---\nname: fa-custom\nmemory: project\nmodel: custom-model\n---\nKeep my custom instructions.\nRecord lessons in your memory.\n")
    curated = setup.root / ".claude/agent-memory/fa-custom/MEMORY.md"
    curated.parent.mkdir(parents=True)
    curated.write_text("Keep existing curated bytes.\n")
    (setup.root / ".gitattributes").write_text("*.txt text\n.claude/agent-memory/**/MEMORY.md merge=union\n")
    def run():
        setup.init(no_push=True, no_launchd=True, no_board=True, approvers=["human"], executor="codex")
    run()
    assert "MEMORY.new.md" in agent.read_text(), "Other projects need a supported migration entrypoint"
    assert "Keep my custom instructions." in agent.read_text() and "custom-model" in agent.read_text()
    _, codex_notes = run_lane_skills.lane_notes(Config(lanes=settings["lanes"]), "custom", setup.root)
    assert "MEMORY.new.md" in codex_notes, "Codex must receive the same writer instruction"
    assert curated.read_text() == "Keep existing curated bytes.\n"
    paths = [agent, curated, setup.root / ".gitattributes"]
    before = {str(p): p.read_bytes() for p in paths}
    run()
    assert before == {str(p): p.read_bytes() for p in paths}
