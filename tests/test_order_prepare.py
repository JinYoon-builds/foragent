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
    # The state directory holds the tree; a link from tool output to its records still escapes.
    (tree / ".venv/state").symlink_to(tmp_path / "scratch" / "records.json")
    (tree / ".venv/inside").symlink_to(tree / "tests")
    (tree / "tests/acceptance/__pycache__/conftest.py").write_text("pytest_plugins = []\n")
    (tree / "src").mkdir()
    (tree / "src/_build.py").write_text("SOURCE = 1\n")
    (tree / "tests/acceptance/run.log").write_text("log\n")
    candidates, refused, ignored = order_scratch.changes(
        git, tree, manifest, "tests/acceptance/", (tmp_path / "checkout", tmp_path / "scratch"))
    assert candidates == {"tests/acceptance/test_new.py": b"assert False\n",
                          "tests/acceptance/helpers/fake.py": b"TABLE = []\n"}
    assert refused == [".venv/checkout", ".venv/state", "app.py", "src/_build.py",
                       "tests/acceptance/__pycache__/conftest.py", "tests/acceptance/conftest.py",
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
    assert files[".claude/skills/tdd"] is None, "main's older copy of the skill is removed first"
    for name, data in files.items():
        if data is not None:
            (tree / name).parent.mkdir(parents=True, exist_ok=True)
            (tree / name).write_bytes(data)
    start = order_prepare_setup.writer_setup(replace(config, executor="codex"), {"lane": "web", "size": "s"}, tree)
    assert start.missing_skills == [] and ".claude/skills/tdd/SKILL.md" in start.notes


def test_writers_ignore_inherited_authority_and_may_edit_only_the_tree_tests(tmp_path):
    state, root, gitdir = tmp_path / "state", tmp_path / "checkout", tmp_path / "checkout/.git"
    tree = state / "prepare/o/a/attempt-1/tree"
    protected = (root, state, gitdir)
    assert order_prepare_setup.permissions("codex", tree, protected, "tests/acceptance/", tmp_path) == {"isolated": True}
    options = order_prepare_setup.permissions("claude", tree, protected, "tests/acceptance/", tmp_path)
    assert options["permission_mode"] == "dontAsk" and options["setting_sources"] == "project"
    settings = json.loads(options["settings_path"].read_text())
    edits = [rule for rule in settings["permissions"]["allow"] if rule.startswith(("Edit", "Write"))]
    assert edits == [f"Edit(/{tree}/tests/acceptance/**)", f"Write(/{tree}/tests/acceptance/**)"]
    denied = settings["permissions"]["deny"]
    assert f"Edit(/{root}/**)" in denied and f"Write(/{gitdir}/**)" in denied and f"Edit(/{tree}/.claude/**)" in denied
    assert not any(str(state) + "/**" in rule for rule in denied), "the state directory holds the tree"
    sandbox = settings["sandbox"]
    assert sandbox["enabled"] is True and sandbox["allowUnsandboxedCommands"] is False
    assert sandbox["filesystem"]["allowWrite"] == [str(tree)] and sandbox["network"]["allowedDomains"] == []
    assert str(root) in sandbox["filesystem"]["denyWrite"]


def test_isolated_executor_commands_drop_user_configuration(tmp_path):
    from foragent.executors import claude_command, codex_command

    codex = codex_command(tmp_path, tmp_path / "out", isolated=True)
    assert {"--ignore-user-config", "--ignore-rules", "sandbox_workspace_write.writable_roots=[]",
            "sandbox_workspace_write.network_access=false"} <= set(codex)
    assert codex[codex.index("-s") + 1] == "workspace-write"
    assert "--ignore-user-config" not in codex_command(tmp_path, tmp_path / "out")
    claude = claude_command(permission_mode="dontAsk", setting_sources="project")
    assert claude[claude.index("--permission-mode") + 1] == "dontAsk"
    assert claude[claude.index("--setting-sources") + 1] == "project" and "--strict-mcp-config" in claude
    assert "--setting-sources" not in claude_command()


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


def test_shared_test_configuration_of_any_tool_is_refused_but_data_fixtures_are_not(git_history, tmp_path):
    git, tree, manifest = scratch(git_history, tmp_path, {
        "app.py": "x = 1\n", "Makefile": "test:\n", "tests/acceptance/test_old.py": "assert True\n"})
    refused_names = ["tests/acceptance/pyproject.toml", "tests/acceptance/setup.cfg", "tests/acceptance/tox.ini",
                     "tests/acceptance/.pytest.ini", "tests/acceptance/jest.config.js",
                     "tests/acceptance/web/package.json", "tests/acceptance/.tools/helper.py",
                     "tests/acceptance/Makefile", "tests/acceptance/app.py"]
    for name in [*refused_names, "tests/acceptance/data/sample.json", "tests/acceptance/test_new.py"]:
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        (tree / name).write_text("x\n")
    candidates, refused, _ = order_scratch.changes(git, tree, manifest, "tests/acceptance/")
    assert refused == sorted(refused_names)
    assert sorted(candidates) == ["tests/acceptance/data/sample.json", "tests/acceptance/test_new.py"]


def test_a_lane_without_an_agent_still_brings_its_checkout_memory(tmp_path):
    from foragent import lane_memory

    config = Config(lanes={"api": {"paths": ["api/**"]}})
    memory = tmp_path / lane_memory.lane_directory(config, "api") / "MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("- lesson\n")
    _config, files = order_prepare_setup.lane_context({}, {"lane": "api"}, config, tmp_path)
    folder = memory.parent.relative_to(tmp_path).as_posix()
    assert files == {folder: None, memory.relative_to(tmp_path).as_posix(): b"- lesson\n"}


def test_scratch_is_the_checkout_view_and_drops_context_the_checkout_deleted(git_history, tmp_path):
    from foragent import lane_memory

    git_history.update({
        ".gitattributes": "skipped.txt export-ignore\nversion.txt export-subst\n",
        "skipped.txt": "kept in a checkout\n", "version.txt": "$Format:%H$\n",
        ".claude/agents/fa-api.md": "---\nname: fa-api\n---\nSTALE MAIN NOTES\n",
        ".claude/agent-memory/fa-api/MEMORY.md": "- old lesson\n",
        ".claude/agent-memory/fa-api/gone.md": "- deleted in the checkout\n",
        ".claude/skills/tdd/SKILL.md": "---\nname: tdd\ndescription: d\n---\nold\n",
        ".claude/skills/tdd/old.md": "removed later\n"})
    root = git_history.clone
    git_command(root, "fetch", "origin", "main")
    git = GitRepo(root)
    base = git.ref_sha("refs/remotes/origin/main")
    # The checkout deleted the agent file and one memory file, and replaced the skill.
    config = Config(lanes={"api": {"paths": ["api/**"], "agent": "fa-api", "skills": ["tdd"]}})
    (root / lane_memory.lane_directory(config, "api")).mkdir(parents=True, exist_ok=True)
    (root / lane_memory.lane_directory(config, "api") / "MEMORY.md").write_text("- lesson\n")
    (root / ".claude/skills/tdd").mkdir(parents=True, exist_ok=True)
    (root / ".claude/skills/tdd/SKILL.md").write_text("---\nname: tdd\ndescription: d\n---\nnew\n")
    _config, overlay = order_prepare_setup.lane_context({}, {"lane": "api"}, config, root)
    tree = tmp_path / "attempt/tree"
    manifest = order_scratch.build(git, base, tree, overlay)
    assert (tree / "skipped.txt").read_text() == "kept in a checkout\n"
    assert (tree / "version.txt").read_text() == "$Format:%H$\n"
    assert not (tree / ".claude/agents/fa-api.md").exists()
    assert sorted(path.name for path in (tree / ".claude/agent-memory/fa-api").iterdir()) == ["MEMORY.md"]
    assert sorted(path.name for path in (tree / ".claude/skills/tdd").iterdir()) == ["SKILL.md"]
    assert "STALE MAIN NOTES" not in order_prepare_setup.writer_setup(
        replace(config, executor="codex"), {"lane": "api", "size": "s"}, tree).notes
    # Nothing the writer did not touch is reported, export attributes included.
    assert order_scratch.changes(git, tree, manifest, "tests/acceptance/") == ({}, [], [])


def test_a_failed_writer_still_reports_the_paths_it_changed(git_history, tmp_path):
    from foragent.executors import ExecResult

    git, tree, manifest = scratch(git_history, tmp_path, {"app.py": "x = 1\n"})
    (tree / "app.py").write_text("x = 2\n")
    (tree / "tests/acceptance").mkdir(parents=True, exist_ok=True)
    (tree / "tests/acceptance/test_partial.py").write_text("assert")
    config = Config(language="en")
    for result in (ExecResult(1, False, "", tmp_path / "log"), ExecResult(0, True, "", tmp_path / "log"),
                   ExecResult(1, False, "", tmp_path / "log", error_kind="limit")):
        failure = order_prepare._failure(result, config)
        outcome = order_prepare._judge(result, failure, tree, git, manifest, config, ())
        assert outcome["status"] == "question" and "app.py" in outcome["questions"][0]
    # Without an offending path the failure stands; the partial test is a fact, never promoted.
    (tree / "app.py").unlink()
    (tree / "app.py").write_text("x = 1\n")
    stopped = {"status": "interrupted", "reason": "stopped"}
    outcome = order_prepare._judge(None, stopped, tree, git, manifest, config, ())
    assert outcome["status"] == "interrupted" and outcome["refused"] == []
    assert outcome["written"] == {"tests/acceptance/test_partial.py": "assert"}
