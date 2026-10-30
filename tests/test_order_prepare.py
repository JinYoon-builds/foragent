"""Unit checks for `fa order prepare`'s scratch tree comparison and status projection."""

from dataclasses import replace
import json
from types import SimpleNamespace

from conftest import git_command
from foragent import order_prepare, order_prepare_setup, order_scratch
from foragent.config import Config
from foragent.git import GitRepo


def scratch(git_history, tmp_path, files):
    git_history.update(files)
    git = GitRepo(git_history.clone)
    git_command(git_history.clone, "fetch", "origin", "main")
    base = git.ref_sha("refs/remotes/origin/main")
    tree = tmp_path / "scratch" / "tree"
    return git, tree, order_scratch.build(git, base, tree, {".claude/agents/fa-api.md": b"agent\n"})


def test_only_new_files_under_the_acceptance_directory_are_candidates(git_history, tmp_path):
    git, tree, manifest = scratch(git_history, tmp_path, {
        "tests/acceptance/test_old.py": "assert True\n", "app.py": "x = 1\n",
        ".gitignore": "__pycache__/\n.venv/\n/src/_build.py\n*.log\n"})
    assert (tree / ".claude/agents/fa-api.md").read_bytes() == b"agent\n"
    (tree / "tests/acceptance/test_new.py").write_text("assert False\n")
    (tree / "tests/acceptance/helpers").mkdir()
    (tree / "tests/acceptance/helpers/fake.py").write_text("TABLE = []\n")
    (tree / "tests/acceptance/__pycache__").mkdir()
    (tree / "tests/acceptance/__pycache__/x.pyc").write_bytes(b"\0")
    (tree / "tests/acceptance/test_old.py").write_text("assert False\n")
    (tree / "tests/acceptance/conftest.py").write_text("pytest_plugins = []\n")
    (tree / "app.py").unlink()
    (tree / "tests/acceptance/test_link.py").symlink_to(tree / "app.py")
    # Ignore rules exempt only whole new ignored directories, never single files or escapes.
    (tree / ".venv/bin").mkdir(parents=True)
    (tree / ".venv/bin/python").symlink_to("/usr/bin/python3")
    (tree / ".venv/checkout").symlink_to(tmp_path / "checkout")
    (tree / "src").mkdir()
    (tree / "src/_build.py").write_text("SOURCE = 1\n")
    (tree / "tests/acceptance/run.log").write_text("log\n")
    candidates, refused, ignored = order_scratch.changes(
        git, tree, manifest, "tests/acceptance/", (tmp_path / "checkout",))
    assert candidates == {"tests/acceptance/test_new.py": b"assert False\n",
                          "tests/acceptance/helpers/fake.py": b"TABLE = []\n"}
    assert refused == [".venv/checkout", "app.py", "src/_build.py", "tests/acceptance/conftest.py",
                       "tests/acceptance/run.log", "tests/acceptance/test_link.py",
                       "tests/acceptance/test_old.py"]
    assert ignored == [".venv", "tests/acceptance/__pycache__"]


def test_an_unchanged_tree_has_no_candidates(git_history, tmp_path):
    git, tree, manifest = scratch(git_history, tmp_path, {"app.py": "x = 1\n"})
    assert order_scratch.changes(git, tree, manifest, "tests/acceptance/") == ({}, [], [])


def test_status_reads_the_record_without_starting_an_agent(tmp_path, monkeypatch):
    plan = {"id": "o/1", "title": "T", "summary": "", "spec_markdown": "", "issues": [
        {"key": "a", "title": "A", "goal": "g", "statements": ["s"], "out_of_scope": "x",
         "done_when": ["true"], "lane": "main", "size": "s", "blocked_by": []},
        {"key": "b", "title": "B", "goal": "g", "out_of_scope": "x", "done_when": ["true"],
         "lane": "main", "size": "s", "blocked_by": []}]}
    store = SimpleNamespace(root=tmp_path / "state")
    record = store.root / "prepare" / "o-1.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"plan": "o/1", "issues": {"a": {
        "status": "written", "phase": "writing_tests", "questions": [], "holds": 0, "reason": "r",
        "updated_at": "2026-10-31T00:00:00+00:00", "base_revision": "abc",
        "written": {"tests/acceptance/test_a.py": "x"}, "attempts": [{"attempt": 1}]}}}))
    monkeypatch.setattr(order_prepare, "executor_for", lambda name: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr("foragent.order.validate_plan", lambda plan, root, config: {})
    rows = order_prepare.prepare(plan, tmp_path, Config(), None, store, status=True)
    assert rows[0] == {"key": "a", "status": "written", "phase": "writing_tests", "questions": [],
                       "holds": 0, "reason": "r", "updated_at": "2026-10-31T00:00:00+00:00",
                       "base_revision": "abc", "written": ["tests/acceptance/test_a.py"], "attempts": 1}
    assert rows[1]["status"] == "legacy" and rows[1]["phase"] == "preread"


def test_writer_reply_is_read_from_a_fenced_block():
    reply = 'Done.\n```json\n{"acceptance_files": {"t.py": "x"}, "done_when": ["c"], "questions": []}\n```\n'
    assert order_prepare._envelope(reply)["done_when"] == ["c"]
    assert order_prepare._envelope('{"acceptance_files": [], "done_when": [], "questions": []}') is None


def test_a_proposed_lane_gets_the_agent_and_library_skills_add_lane_would_install(tmp_path, monkeypatch):
    skill = tmp_path / "library/tdd"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: tdd\n---\nWrite the test first.\n")
    seen = []

    class Manager:
        def __init__(self, config):
            pass

        def check_install(self, identity, root):
            seen.append(identity)
            return SimpleNamespace(path=skill)

    monkeypatch.setattr(order_prepare_setup, "SkillManager", Manager)
    monkeypatch.setattr(order_prepare_setup, "with_vendored_library", lambda config, cwd, root: config)
    plan = {"new_lanes": [{"name": "web", "paths": ["web/**"], "skills": ["lib:skills/tdd"]}]}
    config, files = order_prepare_setup.lane_context(plan, {"lane": "web"}, Config(), tmp_path)
    assert seen == ["lib:skills/tdd"]
    assert config.lanes["web"] == {"paths": ["web/**"], "agent": "fa-web", "skills": ["tdd"]}
    assert files[".claude/skills/tdd/SKILL.md"].endswith(b"Write the test first.\n")
    assert b"web lane (web/**)" in files[".claude/agents/fa-web.md"]
    tree = tmp_path / "tree"
    tree.mkdir()
    for name, data in files.items():
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        (tree / name).write_bytes(data)
    start = order_prepare_setup.writer_setup(replace(config, executor="codex"), {"lane": "web", "size": "s"}, tree)
    assert start.missing_skills == [] and ".claude/skills/tdd/SKILL.md" in start.notes


def test_claude_writer_may_edit_only_the_tree_acceptance_directory(tmp_path):
    tree, root = tmp_path / "attempt/tree", tmp_path / "checkout"
    assert order_prepare_setup.permissions("codex", tree, root, "tests/acceptance/", tmp_path) == {}
    options = order_prepare_setup.permissions("claude", tree, root, "tests/acceptance/", tmp_path)
    assert options["permission_mode"] == "dontAsk"
    settings = json.loads(options["settings_path"].read_text())
    edits = [rule for rule in settings["permissions"]["allow"] if rule.startswith(("Edit", "Write"))]
    assert edits == [f"Edit(/{tree}/tests/acceptance/**)", f"Write(/{tree}/tests/acceptance/**)"]
    assert f"Edit(/{root}/**)" in settings["permissions"]["deny"]
    assert settings["sandbox"]["enabled"] is True and settings["sandbox"]["allowUnsandboxedCommands"] is False


def test_risk_guidance_follows_the_proposed_lane_paths_without_listed_files(tmp_path):
    (tmp_path / ".foragent-risks.yml").write_text("- paths: ['web/**']\n  guidance: web-lane-risk-note\n")
    plan = {"id": "o", "title": "T", "summary": "", "spec_markdown": "", "issues": [],
            "new_lanes": [{"name": "web", "paths": ["web/**"], "skills": []}]}
    item = {"key": "a", "title": "A", "goal": "g", "statements": ["s"], "out_of_scope": "x",
            "lane": "web", "size": "s", "blocked_by": []}
    plan["issues"].append(item)
    config, _files = order_prepare_setup.lane_context(plan, item, Config(), tmp_path)
    prompt = order_prepare.writer_prompt(plan, item, config, tmp_path, tmp_path / "tree", "abc")
    assert "web-lane-risk-note" in prompt
    assert "web-lane-risk-note" not in order_prepare.writer_prompt(
        plan, item, Config(), tmp_path, tmp_path / "tree", "abc")


def test_a_source_file_edited_and_restored_during_the_run_is_refused(git_history, tmp_path):
    git, tree, manifest = scratch(git_history, tmp_path, {"app.py": "x = 1\n", "tests/acceptance/conftest.py": "\n"})
    before = order_scratch.stamps(tree, manifest)
    (tree / "tests/acceptance/test_new.py").write_text("assert False\n")
    for name in ("app.py", "tests/acceptance/conftest.py"):
        original = (tree / name).read_bytes()
        (tree / name).write_text("patched for the run\n")
        (tree / name).write_bytes(original)
    candidates, refused, _ = order_scratch.changes(git, tree, manifest, "tests/acceptance/", (), before)
    assert refused == ["app.py", "tests/acceptance/conftest.py"]
    assert list(candidates) == ["tests/acceptance/test_new.py"]
    assert order_scratch.changes(git, tree, manifest, "tests/acceptance/")[1] == []
