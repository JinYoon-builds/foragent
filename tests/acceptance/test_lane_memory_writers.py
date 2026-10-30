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
    base = tmp_path / "base"
    agent = base / ".claude/agents/fa-orch.md"
    agent.parent.mkdir(parents=True)
    agent.write_text("---\nname: fa-orch\nmemory: project\n---\nRead both memory files.\n")
    source = base / ".claude/agent-memory/fa-orch"
    source.mkdir(parents=True)
    (source / "MEMORY.md").write_text("Curated.\n")
    # Fixture format: stable ids on appended lines; only the dream appends consumed ids.
    pending = (b"- [id:prior-run-0] Retired captured lesson.\n"
               b"- [id:locale-run-1] Pending locale lesson.\n"
               b"- [id:deferred-run-2] Keep this for another dream.\n")
    (source / "MEMORY.new.md").write_bytes(pending)
    (source / "MEMORY.consumed.ids").write_bytes(b"prior-run-0\n")
    before = {file.name: file.read_bytes() for file in source.iterdir()}
    commits, publications, turns = [], [], []
    memory = worktree / ".claude/agent-memory/fa-orch"

    def commit_paths(paths, title):
        # Capture only what is staged: curated text and consumed ids must publish together.
        staged = {}
        for path in paths:
            target = worktree / path
            files = [target] if target.is_file() else target.rglob("*")
            staged.update({file.relative_to(memory).as_posix(): file.read_bytes()
                           for file in files if file.is_file()})
        commits.append(staged)

    def prepare(*args):
        memory.mkdir(parents=True)
        for file in source.iterdir():
            (memory / file.name).write_bytes(file.read_bytes())
        destination = worktree / agent.relative_to(base)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(agent.read_bytes())
        return SimpleNamespace(commit_paths=commit_paths)

    def execute(**kw):
        turns.append(kw)
        assert "MEMORY.new.md" in kw["prompt"], "Dream lacks its append-only pending-memory input"
        assert "MEMORY.consumed.ids" in kw["prompt"], "Dream lacks authority to append consumed ids"
        assert (memory / "MEMORY.new.md").read_bytes() == pending
        (memory / "MEMORY.md").write_text("Curated.\nLocale guidance consolidated.\n")
        with (memory / "MEMORY.consumed.ids").open("ab") as record:
            record.write(b"locale-run-1\n")
        # The executor leaves the append-only input untouched, even on a failed turn.
        return ExecResult(7 if fails else 0, False, "Curated captured id locale-run-1.", kw["log_path"])

    def publish(*args):
        assert len(commits) == 1
        publications.append(commits[-1].copy())
        return 123

    monkeypatch.setattr(orch_dream_worker, "_dream_worktree", prepare)
    monkeypatch.setattr(orch_dream_worker, "_publish", publish)
    monkeypatch.setattr(usage_events, "record_from_log", lambda *a: None)
    ctx = SimpleNamespace(store=store, config=config, now=lambda: datetime.now(UTC),
        git=SimpleNamespace(remove_worktree=lambda _: None), executor_factory=lambda _: SimpleNamespace(run=execute),
        poll_hook=lambda execute, poll: execute())
    assert orch_dream_worker.run_dream("orch", ctx) == (1 if fails else 0)
    assert len(turns) == 1 and len(publications) == len(commits) == (0 if fails else 1)
    assert (memory / "MEMORY.new.md").read_bytes() == pending, "Dream must never truncate or rewrite pending"
    if not fails:
        assert commits[0]["MEMORY.md"] == b"Curated.\nLocale guidance consolidated.\n"
        assert commits[0]["MEMORY.consumed.ids"] == b"prior-run-0\nlocale-run-1\n"
        assert (memory / "MEMORY.consumed.ids").read_bytes() == commits[0]["MEMORY.consumed.ids"]
        # An unchanged pending file need not be staged, but cannot be published rewritten.
        assert commits[0].get("MEMORY.new.md", pending) == pending
        assert publications == commits
        assert store.get_dream("orch").outcome == "dreamed"
    else:
        assert store.get_dream("orch").outcome == "error"
        assert commits == publications == [], "A failed dream must publish neither curation nor consumption"
    assert before == {file.name: file.read_bytes() for file in source.iterdir()}, "Base bytes must remain intact"

    # Observe the production readers, not a test-side implementation of pending = ids - consumed.
    visible = base if fails else worktree
    for text in (run_lane_skills.lane_notes(config, "orch", visible)[1],
                 run_lane_skills.lane_memory_notice(config, "orch", visible),
                 render_detail(config, "orch", visible)):
        assert "Retired captured lesson." not in text, "Previously consumed ids must stay consumed"
        assert ("Pending locale lesson." in text) is fails, "Consumption is visible only after a successful dream"
        assert text.count("Keep this for another dream.") == 1, "Unconsumed ids must remain pending"
    assert (memory / "MEMORY.new.md").read_bytes() == pending
    assert before == {file.name: file.read_bytes() for file in source.iterdir()}


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


def test_worker_branch_forked_before_consumption_cannot_resurrect_consumed_lesson_after_union_merge(
    git_history, tmp_path,
):
    import subprocess
    from conftest import git_command

    config = Config(language="en", lanes={"orch": {"agent": "fa-orch", "paths": ["src/**"]}})
    directory = ".claude/agent-memory/fa-orch"
    pending_path = f"{directory}/MEMORY.new.md"
    consumed_path = f"{directory}/MEMORY.consumed.ids"
    curated_path = f"{directory}/MEMORY.md"
    agent_path = ".claude/agents/fa-orch.md"
    captured = "- [id:locale-run-1] Retain the selected locale.\n"
    same_text_new_id = "- [id:locale-run-2] Retain the selected locale.\n"
    branch_lesson = "- [id:branch-run-3] Keep labels on the active branch.\n"
    git_history.update({
        ".gitattributes": ".claude/agent-memory/**/MEMORY.new.md merge=union\n",
        agent_path: "---\nname: fa-orch\nmemory: project\n---\nRead both memory files.\n",
        curated_path: "Curated.\n", pending_path: captured, consumed_path: "",
    })
    fork = git_history.tip
    git_command(git_history.origin, "update-ref", "refs/heads/worker", fork)

    # Main publishes the dream without changing pending, then another worker appends a new id.
    git_history.update({curated_path: "Curated locale guidance.\n", consumed_path: "locale-run-1\n"})
    def blob(ref, path):
        return subprocess.run(["git", "-C", str(git_history.origin), "show", f"{ref}:{path}"],
                              check=True, capture_output=True).stdout

    assert blob("main", pending_path) == blob(fork, pending_path) == captured.encode()
    git_history.update({pending_path: captured + same_text_new_id})

    # The old branch retains the consumed entry and retries the same new append with its stable id.
    worker_pending = captured + branch_lesson + branch_lesson
    stream = ("commit refs/heads/worker\n"
              "committer Offline Tests <tests@example.com> 1792483200 +0000\n"
              "data 13\nWorker append\n"
              f"from {fork}\n"
              f"M 100644 inline {pending_path}\ndata {len(worker_pending.encode())}\n{worker_pending}\n"
              "\ndone\n")
    git_command(git_history.origin, "fast-import", "--quiet", input=stream)
    assert git_command(git_history.origin, "merge-base", "main", "worker") == fork
    assert blob("worker", consumed_path) == b"", "The worker forked before consumption"
    # Bare histories need an explicit attribute source; this uses Git's real union driver.
    merged = git_command(git_history.origin, "--attr-source=main", "merge-tree", "--write-tree",
                         "main", "worker").splitlines()[0]
    assert blob(merged, consumed_path) == b"locale-run-1\n", "A worker cannot undo the dream's record"
    raw = blob(merged, pending_path)
    assert captured.encode() in raw and same_text_new_id.encode() in raw and branch_lesson.encode() in raw
    assert raw.count(branch_lesson.encode()) == 2, "The union fixture must retain a repeated stable id"

    merged_root = tmp_path / "merged"
    for path in (agent_path, curated_path, pending_path, consumed_path):
        target = merged_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob(merged, path))
    before = {path: (merged_root / path).read_bytes() for path in (curated_path, pending_path, consumed_path)}
    for text in (run_lane_skills.lane_notes(config, "orch", merged_root)[1],
                 run_lane_skills.lane_memory_notice(config, "orch", merged_root),
                 render_detail(config, "orch", merged_root)):
        assert "[id:locale-run-1]" not in text, "Union merge must not resurrect the consumed id"
        assert text.count("Retain the selected locale.") == 1, "A different id with the same text remains pending"
        assert text.count("Keep labels on the active branch.") == 1, "Pending is a union by id, including retried appends"
    assert before == {path: (merged_root / path).read_bytes() for path in before}, "Deriving pending must be read-only"
