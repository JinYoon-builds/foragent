"""Unit checks for `fa order prepare`'s scratch tree comparison and status projection."""

from dataclasses import replace
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

from conftest import git_command
from foragent import order_prepare, order_prepare_setup, order_scratch, order_scratch_context
from foragent.config import Config
from foragent.git import GitRepo


def scratch(git_history, tmp_path, files):
    git_history.update(files)
    git = GitRepo(git_history.clone)
    git_command(git_history.clone, "fetch", "origin", "main")
    base = git.ref_sha("refs/remotes/origin/main")
    tree = tmp_path / "scratch" / "tree"
    context = order_scratch_context.Context(git_history.clone, files={".claude/agents/fa-api.md": b"agent\n"})
    return git, tree, order_scratch.build(git, base, tree, context)


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
    assert refused == [".venv/checkout", ".venv/state", "app.py", "src", "src/_build.py",
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
    record = order_prepare._record_path(store, plan)
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
    assert settings["disableAllHooks"] is True
    sandbox = settings["sandbox"]
    assert sandbox["enabled"] is True and sandbox["allowUnsandboxedCommands"] is False
    assert sandbox["filesystem"]["allowWrite"] == [str(tree)] and sandbox["network"]["allowedDomains"] == []
    assert str(root) in sandbox["filesystem"]["denyWrite"]


def test_isolated_executor_commands_drop_user_configuration(tmp_path):
    from foragent.executors import claude_command, codex_command

    codex = codex_command(tmp_path, tmp_path / "out", isolated=True)
    assert {"--ignore-user-config", "--ignore-rules", "sandbox_workspace_write.writable_roots=[]",
            "sandbox_workspace_write.network_access=false", "notify=[]", "mcp_servers={}"} <= set(codex)
    assert codex[codex.index("--disable") + 1] == "hooks"
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
    config, _context = order_prepare_setup.lane_context(plan, item, Config(), tmp_path)
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
    assert refused == sorted([*refused_names, "tests/acceptance/.tools"])  # the new dot folder too
    assert sorted(candidates) == ["tests/acceptance/data/sample.json", "tests/acceptance/test_new.py"]






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












def test_a_fifo_or_socket_left_in_the_scratch_is_a_question_and_never_read(git_history, tmp_path, monkeypatch):
    import os
    import socket

    git, tree, manifest = scratch(git_history, tmp_path, {"app.py": "x = 1\n", ".gitignore": ".venv/\n"})
    (tree / "tests/acceptance").mkdir(parents=True, exist_ok=True)
    (tree / "tests/acceptance/test_ok.py").write_text("assert False\n")
    os.mkfifo(tree / "tests/acceptance/pytest.ini")
    os.mkfifo(tree / "tests/acceptance/test_pipe.py")
    (tree / ".venv").mkdir()
    os.mkfifo(tree / ".venv/pipe")
    listener = socket.socket(socket.AF_UNIX)
    try:
        monkeypatch.chdir(tree / "tests/acceptance")  # a short path: Unix socket names are limited
        listener.bind("s.sock")
        candidates, refused, _ = order_scratch.changes(git, tree, manifest, "tests/acceptance/")
    finally:
        listener.close()
    assert list(candidates) == ["tests/acceptance/test_ok.py"]
    assert refused == [".venv/pipe", "tests/acceptance/pytest.ini", "tests/acceptance/s.sock",
                       "tests/acceptance/test_pipe.py"]








def test_folders_made_removed_or_re_permissioned_outside_the_tests_are_questions(git_history, tmp_path):
    git, tree, manifest = scratch(git_history, tmp_path, {
        "src/app.py": "x = 1\n", "lib/util.py": "y = 1\n", ".gitignore": ".venv/\n"})
    (tree / "newpkg").mkdir()                       # empty production folder
    (tree / "src").chmod(0o700)                     # permission change on an existing folder
    (tree / "lib/util.py").unlink()
    (tree / "lib").rmdir()                          # folder removed
    (tree / "tests/acceptance/helpers").mkdir(parents=True)  # new folders for tests are fine
    (tree / ".venv/bin").mkdir(parents=True)        # ignored tool output stays exempt
    candidates, refused, ignored = order_scratch.changes(git, tree, manifest, "tests/acceptance/")
    assert refused == ["lib", "lib/util.py", "newpkg", "src"]
    assert candidates == {} and ignored == [".venv"]





def test_unreadable_entries_left_by_the_writer_are_questions(git_history, tmp_path):
    import os

    git, tree, manifest = scratch(git_history, tmp_path, {"app.py": "x = 1\n"})
    (tree / "hidden").mkdir()
    os.mkfifo(tree / "hidden/pytest.ini")
    (tree / "hidden").chmod(0o000)
    (tree / "tests/acceptance/locked").mkdir(parents=True)
    (tree / "tests/acceptance/locked").chmod(0o000)
    (tree / "tests/acceptance/test_secret.py").write_text("assert False\n")
    (tree / "tests/acceptance/test_secret.py").chmod(0o000)
    try:
        candidates, refused, _ = order_scratch.changes(git, tree, manifest, "tests/acceptance/")
    finally:
        for name in ("hidden", "tests/acceptance/locked", "tests/acceptance/test_secret.py"):
            (tree / name).chmod(0o755)
    assert candidates == {}
    assert refused == ["hidden", "tests/acceptance/locked", "tests/acceptance/test_secret.py"]


def checkout(git_history, files=None):
    git_history.update(files or {"app.py": "x = 1\n"})
    root = git_history.clone
    git_command(root, "fetch", "origin", "main")
    git_command(root, "merge", "--ff-only", "origin/main")
    git = GitRepo(root)
    return root, git, git.ref_sha("refs/remotes/origin/main")


def make_skill(folder, name):
    """A skill with every shape a faithful copy must keep."""
    import os

    (folder / "resources").mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\nUse resources/x.txt\n")
    (folder / "resources/x.txt").write_text("resource\n")
    (folder / "alias").symlink_to("resources")
    (folder / "empty").mkdir()
    (folder / "private").mkdir()
    (folder / "private/note.md").write_text("private\n")
    (folder / "private").chmod(0o700)
    (folder / "run.sh").write_text("#!/bin/sh\necho skill-ran\n")
    (folder / "run.sh").chmod(0o755)
    os.mkfifo(folder / "pipe")
    os.chmod(folder / "pipe", 0o666)


def assert_faithful(tree, source, name):
    import stat

    assert order_scratch_context.signature(tree / name) == order_scratch_context.signature(source)
    assert (tree / name / "empty").is_dir() and not any((tree / name / "empty").iterdir())
    assert stat.S_IMODE((tree / name / "private").stat().st_mode) == 0o700
    assert (tree / name / "alias").is_symlink() and (tree / name / "alias/x.txt").read_text() == "resource\n"
    ran = subprocess.run([str(tree / name / "run.sh")], capture_output=True, text=True, check=True)
    assert ran.stdout == "skill-ran\n"


def test_checkout_context_is_copied_whole_and_faithfully(git_history, tmp_path):
    from foragent import lane_memory

    config = Config(executor="codex", lanes={"api": {"paths": ["api/**"], "agent": "fa-api", "skills": ["tdd"]}})
    folder = lane_memory.lane_directory(config, "api")
    root, git, base = checkout(git_history, {
        ".gitattributes": "skipped.txt export-ignore\n", "skipped.txt": "kept in a checkout\n",
        ".claude/agents/fa-api.md": "---\nname: fa-api\n---\nSTALE MAIN NOTES\n",
        f"{folder}/MEMORY.md": "- old\n", f"{folder}/MEMORY.new.md": "- STALE PENDING\n",
        ".claude/skills/tdd/old.md": "removed later\n"})
    (root / ".claude/agents/fa-api.md").write_text("---\nname: fa-api\n---\nCURRENT NOTES\n")
    (root / f"{folder}/MEMORY.new.md").unlink()
    (root / f"{folder}/MEMORY.md").write_text("- current lesson\n")
    (root / ".claude/skills/tdd/old.md").unlink()
    make_skill(root / ".claude/skills/tdd", "tdd")
    _config, context = order_prepare_setup.lane_context({}, {"lane": "api"}, config, root)
    assert sorted(name for _source, name in context.sources
                  if name not in order_prepare_setup.EXECUTOR_SETTINGS) == sorted([".claude/agents", folder, ".claude/skills/tdd"])
    tree = tmp_path / "attempt/tree"
    manifest = order_scratch.build(git, base, tree, context)
    assert context.refused == []
    assert (tree / "skipped.txt").read_text() == "kept in a checkout\n"
    for name in (".claude/agents", folder):
        assert order_scratch_context.signature(tree / name) == order_scratch_context.signature(root / name)
    assert_faithful(tree, root / ".claude/skills/tdd", ".claude/skills/tdd")
    writer = order_prepare_setup.writer_setup(config, {"lane": "api", "size": "s"}, tree)
    preread = order_prepare_setup.writer_setup(config, {"lane": "api", "size": "s"}, root)
    assert "CURRENT NOTES" in writer.notes and "STALE" not in writer.notes
    assert writer.notes.replace(str(tree), "") == preread.notes.replace(str(root), "")
    # The copied FIFO is part of the baseline, so an untouched tree reports nothing.
    assert order_scratch.changes(git, tree, manifest, "tests/acceptance/") == ({}, [], [])


def test_a_proposed_lane_copies_library_skills_whole_and_its_agent(git_history, tmp_path, monkeypatch):
    root, git, base = checkout(git_history, {"resources/x.txt": "main's own file\n"})
    library = tmp_path / "library/web-skill"
    make_skill(library, "web-skill")
    monkeypatch.setattr(order_prepare_setup, "SkillManager",
                        lambda config: SimpleNamespace(check_install=lambda identity, root: SimpleNamespace(path=library)))
    monkeypatch.setattr(order_prepare_setup, "with_vendored_library", lambda config, cwd, root: config)
    plan = {"new_lanes": [{"name": "web", "paths": ["web/**"], "skills": ["lib:web-skill"]}]}
    config, context = order_prepare_setup.lane_context(plan, {"lane": "web"}, Config(), root)
    assert config.lanes["web"] == {"paths": ["web/**"], "agent": "fa-web", "skills": ["web-skill"]}
    tree = tmp_path / "attempt/tree"
    order_scratch.build(git, base, tree, context)
    assert context.refused == []
    assert_faithful(tree, library, ".claude/skills/web-skill")
    assert (tree / "resources/x.txt").read_text() == "main's own file\n"
    assert b"web lane (web/**)" in (tree / ".claude/agents/fa-web.md").read_bytes()
    start = order_prepare_setup.writer_setup(replace(config, executor="codex"), {"lane": "web", "size": "s"}, tree)
    assert start.missing_skills == [] and ".claude/skills/web-skill/SKILL.md" in start.notes


def test_links_that_find_other_content_in_the_scratch_are_refused_by_path(git_history, tmp_path):
    import socket

    from foragent import lane_memory

    config = Config(lanes={"api": {"paths": ["api/**"], "agent": "fa-api", "skills": ["tdd"]}})
    folder = lane_memory.lane_directory(config, "api")
    root, git, base = checkout(git_history, {"shared/same.md": "same\n", "shared/changed.md": "main\n"})
    (root / "shared/changed.md").write_text("checkout\n")
    (root / "shared/untracked.md").write_text("only in the checkout\n")
    (tmp_path / "outside.md").write_text("outside\n")
    (root / ".claude/agents").mkdir(parents=True)
    (root / ".claude/agents/fa-api.md").symlink_to("../../shared/same.md")         # same content: kept
    (root / ".claude/agents/cycle.md").symlink_to("cycle.md")                        # a cycle
    (root / folder).mkdir(parents=True)
    (root / folder / "MEMORY.md").symlink_to(tmp_path / "outside.md")               # leads outside
    (root / ".claude/skills/tdd").mkdir(parents=True)
    (root / ".claude/skills/tdd/SKILL.md").write_text("---\nname: tdd\ndescription: d\n---\n")
    (root / ".claude/skills/tdd/changed.md").symlink_to("../../../shared/changed.md")   # main differs
    (root / ".claude/skills/tdd/untracked.md").symlink_to("../../../shared/untracked.md")  # not in scratch
    listener = socket.socket(socket.AF_UNIX)
    cwd = __import__("os").getcwd()
    try:
        __import__("os").chdir(root / ".claude/skills/tdd")
        listener.bind("s.sock")                                                      # cannot be recreated
        _config, context = order_prepare_setup.lane_context({}, {"lane": "api"}, config, root)
        order_scratch.build(git, base, tmp_path / "attempt/tree", context)
    finally:
        __import__("os").chdir(cwd)
        listener.close()
    assert context.refused == sorted([".claude/agents/cycle.md", f"{folder}/MEMORY.md", ".claude/skills/tdd/changed.md",
                                      ".claude/skills/tdd/s.sock", ".claude/skills/tdd/untracked.md"])


def test_a_linked_claude_folder_leading_outside_refuses_every_context_place(git_history, tmp_path):
    from foragent import lane_memory

    root, outside = git_history.clone, tmp_path / "outside"
    config = Config(lanes={"api": {"paths": ["api/**"], "agent": "fa-api", "skills": ["tdd"]}})
    folder = lane_memory.lane_directory(config, "api")
    for name in ("agents/fa-api.md", f"{folder.removeprefix('.claude/')}/MEMORY.md", "skills/tdd/SKILL.md"):
        (outside / name).parent.mkdir(parents=True, exist_ok=True)
        (outside / name).write_text("OUTSIDE\n")
    (root / ".claude").symlink_to(outside)
    _config, context = order_prepare_setup.lane_context({}, {"lane": "api"}, config, root)
    assert sorted(context.refused) == sorted([".claude/agents", folder, ".claude/skills/tdd"])
    assert all(source is None for source, _name in context.sources)


def test_applying_context_never_reaches_through_a_tracked_symlink(git_history, tmp_path):
    outside = tmp_path / "checkout-claude"
    (outside / "agents").mkdir(parents=True)
    (outside / "agents/fa-api.md").write_text("ORIGINAL AGENT\n")
    target = str(outside).encode()
    stream = (f"commit refs/heads/main\ncommitter T <t@example.com> 1792483200 +0000\ndata 4\nlink\n"
              f"from {git_history.tip}\nM 120000 inline .claude\ndata {len(target)}\n").encode() + target + b"\n\ndone\n"
    subprocess.run(["git", "fast-import", "--quiet"], cwd=git_history.origin, input=stream, check=True)
    git_command(git_history.clone, "fetch", "origin", "main")
    git = GitRepo(git_history.clone)
    source = tmp_path / "source-agents"
    source.mkdir()
    (source / "fa-api.md").write_text("NEW AGENT\n")
    context = order_scratch_context.Context(tmp_path, sources=[(source, ".claude/agents")])
    tree = tmp_path / "attempt/tree"
    order_scratch.build(git, git.ref_sha("refs/remotes/origin/main"), tree, context)
    assert (outside / "agents/fa-api.md").read_text() == "ORIGINAL AGENT\n"
    assert not (tree / ".claude").is_symlink() and (tree / ".claude/agents/fa-api.md").read_text() == "NEW AGENT\n"


def test_refused_lane_context_asks_before_any_writer_starts(tmp_path, monkeypatch):
    plan = {"id": "o", "title": "T", "summary": "", "spec_markdown": "", "issues": [
        {"key": "a", "title": "A", "goal": "g", "statements": ["s"], "out_of_scope": "x",
         "done_when": ["true"], "lane": "main", "size": "s", "blocked_by": []}]}
    store = SimpleNamespace(root=tmp_path / "state")
    context = order_scratch_context.Context(tmp_path, refused=[".claude/agents/fa-api.md"])
    monkeypatch.setattr("foragent.order.validate_plan", lambda plan, root, config: {})
    monkeypatch.setattr(order_scratch, "base_revision", lambda git, branch: "abc")
    monkeypatch.setattr(order_scratch, "protected", lambda git, root, state: ())
    monkeypatch.setattr(order_scratch, "build", lambda git, base, tree, context: {})
    monkeypatch.setattr(order_prepare_setup, "lane_context", lambda plan, item, config, root: (config, context))
    monkeypatch.setattr(order_prepare, "executor_for", lambda name: (_ for _ in ()).throw(AssertionError))
    row = order_prepare.prepare(plan, tmp_path, Config(language="en"), None, store)[0]
    assert row["status"] == "question" and ".claude/agents/fa-api.md" in row["questions"][0]


def test_a_new_issue_and_an_edit_with_the_same_number_get_their_own_trees(tmp_path, monkeypatch):
    plan = {"id": "o", "title": "T", "summary": "", "spec_markdown": "", "issues": [
        {"key": "7", "title": "A", "goal": "g", "statements": ["s"], "out_of_scope": "x",
         "done_when": ["true"], "lane": "main", "size": "s", "blocked_by": []}],
        "edits": [{"number": 7, "goal": "g", "statements": ["s"], "out_of_scope": "x", "done_when": ["true"]}]}
    store = SimpleNamespace(root=tmp_path / "state")
    trees = []
    monkeypatch.setattr("foragent.order.validate_plan", lambda plan, root, config: {})
    monkeypatch.setattr("foragent.order.edit_view", lambda item, issue, config: {
        **item, "key": f"#{item['number']}", "title": "E", "lane": "main", "size": "s"})
    monkeypatch.setattr(order_scratch, "base_revision", lambda git, branch: "abc")
    monkeypatch.setattr(order_scratch, "protected", lambda git, root, state: ())
    monkeypatch.setattr(order_prepare_setup, "lane_context",
                        lambda plan, item, config, root: (config, order_scratch_context.Context(root)))

    def build(git, base, tree, context):
        assert not tree.exists(), "each issue gets a tree of its own"
        tree.mkdir(parents=True)
        trees.append(tree)
        raise OSError("stop before the writer")

    monkeypatch.setattr(order_scratch, "build", build)
    rows = order_prepare.prepare(plan, tmp_path, Config(language="en"), None, store,
                                 lambda: SimpleNamespace(get_issue=lambda n: None))
    assert [row["key"] for row in rows] == ["7", "#7"] and len(set(trees)) == 2
    assert order_prepare._record_path(store, {"id": "a/b"}) != order_prepare._record_path(store, {"id": "a-b"})



def test_the_copy_keeps_every_mode_whatever_the_umask(tmp_path):
    import os
    import stat

    source = tmp_path / "source/skill"
    make_skill(source, "skill")
    (source / "shared").mkdir()
    (source / "shared/data.txt").write_text("d\n")
    (source / "shared").chmod(0o777)
    for mask in (0o022, 0o077, 0o777):
        dest = tmp_path / f"copy-{mask:o}"
        refused, links = [], []
        dest.mkdir()
        copy = dest / "skill"
        previous = os.umask(mask)
        try:
            order_scratch_context.copy_tree(source, copy, "skill", refused, links)
        finally:
            os.umask(previous)
        assert refused == [] and [name for name, _source in links] == ["skill/alias"]
        assert order_scratch_context.signature(copy) == order_scratch_context.signature(source), oct(mask)
        assert stat.S_IMODE(os.lstat(copy / "pipe").st_mode) == 0o666
        assert stat.S_IMODE(os.lstat(copy / "shared").st_mode) == 0o777


def test_a_writer_changing_a_fifo_mode_is_a_question(git_history, tmp_path):
    import os

    source = tmp_path / "agents"
    source.mkdir()
    os.mkfifo(source / "pipe")
    os.chmod(source / "pipe", 0o600)
    git_history.update({"app.py": "x = 1\n"})
    git = GitRepo(git_history.clone)
    git_command(git_history.clone, "fetch", "origin", "main")
    context = order_scratch_context.Context(git_history.clone, sources=[(source, ".claude/agents")])
    tree = tmp_path / "attempt/tree"
    manifest = order_scratch.build(git, git.ref_sha("refs/remotes/origin/main"), tree, context)
    assert order_scratch.changes(git, tree, manifest, "tests/acceptance/")[1] == []
    os.chmod(tree / ".claude/agents/pipe", 0o666)
    assert order_scratch.changes(git, tree, manifest, "tests/acceptance/")[1] == [".claude/agents/pipe"]



def test_the_scratch_keeps_no_settings_that_run_commands_outside_the_sandbox(git_history, tmp_path):
    hook = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "touch /tmp/owned"}]}]},
            "statusLine": {"type": "command", "command": "touch /tmp/owned"}, "apiKeyHelper": "touch /tmp/owned"}
    root, git, base = checkout(git_history, {
        ".claude/settings.json": json.dumps(hook), ".claude/settings.local.json": json.dumps(hook),
        ".mcp.json": json.dumps({"mcpServers": {"x": {"command": "touch"}}}),
        ".codex/config.toml": 'notify = ["touch", "/tmp/owned"]\n', ".codex/prompts/keep.md": "kept\n"})
    _config, context = order_prepare_setup.lane_context({}, {"lane": "main"}, Config(), root)
    tree = tmp_path / "attempt/tree"
    manifest = order_scratch.build(git, base, tree, context)
    assert context.refused == []
    for name in order_prepare_setup.EXECUTOR_SETTINGS:
        assert not (tree / name).exists(), name
    assert (tree / ".codex/prompts/keep.md").read_text() == "kept\n"
    # Removed before the baseline is read, so the removal itself is no question.
    assert order_scratch.changes(git, tree, manifest, "tests/acceptance/") == ({}, [], [])


def _review_outcome(tmp_path, reply, *, session="reviewer-1", commands=("check",), changed=()):
    from foragent import order_prepare_review

    (tmp_path / "test_x.py").write_text("assert False\n")
    result = SimpleNamespace(error_kind="normal", timed_out=False, exit_code=0, session_id=session,
                             log_path=tmp_path / "reviewer.log")
    item = {"statements": ["One delivery is reported."]}
    entry = {"written": {"test_x.py": "assert False\n"}, "done_when": list(commands)}
    changed = list(changed)
    return order_prepare_review._outcome(result, reply, item, entry, changed, {"writer-1"}, Config())


RUN = {"command": "check", "collected": 1, "failed": 1, "exit_code": 1, "output": "AssertionError: 0 != 1",
       "failure_kind": "assertion"}
PASS = {"verdict": "pass", "reason": "ok", "questions": [],
        "coverage": [{"statement": "One delivery is reported.", "tests": ["test_x.py::test_once"],
                      "break_it": ["wrong value", "skipped effect", "doubled effect"]}],
        "red": [RUN]}


def test_a_pass_needs_every_command_red_mapped_statements_and_its_own_session(tmp_path):
    assert _review_outcome(tmp_path, PASS)["status"] == "reviewed"
    # A command reported passing on current main, or not reported at all, contradicts the pass.
    # Reported facts that contradict a red run on current main make the pass a quality hold.
    for wrong in ({"exit_code": 0}, {"collected": 0}, {"failed": 0}):
        outcome = _review_outcome(tmp_path, {**PASS, "red": [{**RUN, **wrong}]})
        assert outcome["status"] == outcome["verdict"] == "hold", wrong
    # Missing evidence or a reused writer session approves nothing, and is no quality hold.
    unmapped = {**PASS, "coverage": [{"statement": "Something else."}]}
    assert _review_outcome(tmp_path, unmapped)["status"] == "interrupted"
    assert _review_outcome(tmp_path, {**PASS, "red": []})["status"] == "interrupted"
    assert _review_outcome(tmp_path, None)["status"] == "interrupted"
    assert _review_outcome(tmp_path, PASS, session="writer-1")["status"] == "interrupted"


def test_a_pass_without_the_agreed_evidence_promotes_nothing(tmp_path):
    cover = PASS["coverage"][0]
    incomplete = [
        # The reported case: a statement without tests or break-it, a red run without counts or output.
        {**PASS, "coverage": [{"statement": cover["statement"]}], "red": [{"command": "check", "exit_code": 2}]},
        {**PASS, "coverage": [{**cover, "tests": []}]},
        {**PASS, "coverage": [{**cover, "tests": ["test_other.py"]}]},
        {**PASS, "coverage": [{**cover, "break_it": []}]},
        {**PASS, "coverage": [{**cover, "break_it": [" "]}]},
        {**PASS, "red": [RUN, RUN]},
        {**PASS, "red": [{**RUN, "command": "other"}]},
        {**PASS, "reason": " "},
        {"verdict": "hold", "reason": "", "questions": []},
        {"verdict": "question", "reason": "", "questions": []},
    ] + [{**PASS, "red": [{key: value for key, value in RUN.items() if key != name}]} for name in RUN] + [
        {**PASS, "red": [{**RUN, "collected": True}]}, {**PASS, "red": [{**RUN, "output": ""}]}]
    for reply in incomplete:
        assert _review_outcome(tmp_path, reply)["status"] == "interrupted", reply
    assert _review_outcome(tmp_path, PASS, commands=("check", "second"))["status"] == "interrupted"
    assert _review_outcome(tmp_path, PASS, commands=())["status"] == "interrupted"


def test_reviewer_runs_checks_in_the_writer_sandbox_without_edit_tools(tmp_path):
    from foragent import order_prepare_review

    tree, state = tmp_path / "state/attempt/tree", tmp_path / "state"
    tree.mkdir(parents=True)
    claude = order_prepare_review._options("claude", tree, (tmp_path / "checkout", state), Config(),
                                           tmp_path / "review")
    settings = json.loads(claude["settings_path"].read_text())
    assert claude["read_only"] is True and claude["permission_mode"] == "dontAsk"
    assert {"Edit", "Write"} <= set(settings["permissions"]["deny"])
    assert not any(rule.startswith(("Edit(", "Write(")) for rule in settings["permissions"]["allow"])
    assert settings["sandbox"]["enabled"] and settings["sandbox"]["allowUnsandboxedCommands"] is False
    assert settings["sandbox"]["filesystem"]["allowWrite"] == [str(tree)]
    assert settings["sandbox"]["network"]["allowedDomains"] == [] and settings["disableAllHooks"] is True
    assert order_prepare_review._options("codex", tree, (), Config(), tmp_path / "r2") == {"isolated": True}


def test_promotion_needs_the_reviewed_requirements_and_base(tmp_path):
    from foragent import order_prepare_cycle

    item = {"key": "a", "title": "A", "goal": "g", "statements": ["s"], "out_of_scope": "o", "lane": "api",
            "size": "s", "done_when": ["true"], "blocked_by": []}
    plan = {"id": "p", "title": "P", "summary": "x", "spec_markdown": "spec", "issues": [item]}
    config = Config(lanes={"api": {"paths": ["src/**"]}})
    path = tmp_path / "PLAN.json"
    entry = {"cycle": order_prepare_cycle.identity(plan, item, config, tmp_path), "base_revision": "b1",
             "written": {"tests/acceptance/test_a.py": "assert False\n"},
             "done_when": ["pytest tests/acceptance/test_a.py"]}
    for changed in ({"goal": "other goal"}, {"out_of_scope": "less"}, {"statements": ["t"]}):
        path.write_text(json.dumps({**plan, "issues": [{**item, **changed}]}))
        assert not order_prepare_cycle.promote(path, item, entry, tmp_path, config, "b1"), changed
    path.write_text(json.dumps({**plan, "spec_markdown": "new spec"}))
    assert not order_prepare_cycle.promote(path, item, entry, tmp_path, config, "b1")
    path.write_text(json.dumps(plan))
    assert not order_prepare_cycle.promote(path, item, entry, tmp_path, config, "b2")
    other = Config(lanes={"api": {"paths": ["src/**"]}}, reviewer="codex", executor="claude")
    assert not order_prepare_cycle.promote(path, item, entry, tmp_path, other, "b1")
    assert "acceptance_files" not in json.loads(path.read_text())["issues"][0]
    (tmp_path / "tests/acceptance").mkdir(parents=True)
    assert order_prepare_cycle.promote(path, item, entry, tmp_path, config, "b1")
    promoted = json.loads(path.read_text())["issues"][0]
    assert promoted["acceptance_files"] == entry["written"] and promoted["done_when"] == entry["done_when"]


def test_holds_are_counted_in_the_current_cycle_only():
    from foragent.order_prepare_cycle import holds

    attempts = [{"role": "reviewer", "verdict": "hold", "status": "hold", "cycle": "old", "reason": "a"},
                {"role": "writer", "cycle": "new"},
                {"role": "reviewer", "verdict": "hold", "status": "hold", "cycle": "new", "reason": "b"},
                {"role": "reviewer", "verdict": "question", "status": "question", "cycle": "new", "reason": "c"},
                # A malformed or failed review is no quality decision, whatever verdict it carried.
                {"role": "reviewer", "verdict": "hold", "status": "interrupted", "cycle": "new", "reason": "d"},
                {"role": "reviewer", "status": "waiting", "cycle": "new", "reason": "e"}]
    assert holds({"cycle": "new", "attempts": attempts}) == ["b"]
    assert holds({"cycle": "old", "attempts": attempts}) == ["a"]
    assert holds(None) == []


def _lane_case_module():
    """The order's shared offline fixtures (local Git, faked executors, no GitHub)."""
    import importlib
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent / "acceptance"))
    return importlib.import_module("lane_tests_case")


def test_results_made_on_an_older_main_are_written_and_reviewed_again(request, git_history, capsys):
    cases = _cases
    case = request.getfixturevalue("lane_case")
    first = cases.prepare(case, capsys)
    assert first["status"] == "reviewed"
    git_history.update({"later.py": "x = 1\n"})
    second = cases.prepare(case, capsys)
    assert second["status"] == "reviewed" and second["base_revision"] == git_history.tip != first["base_revision"]
    assert [call["role"] for call in case.calls] == ["writer", "reviewer"] * 2
    # A review left pending on the older main is not resumed on its old scratch copy either.
    case.reviewer_exit = 1
    git_history.update({"later.py": "x = 2\n"})
    assert cases.prepare(case, capsys)["status"] == "waiting"
    case.reviewer_exit = 0
    git_history.update({"later.py": "x = 3\n"})
    third = cases.prepare(case, capsys)
    assert third["status"] == "reviewed" and third["base_revision"] == git_history.tip
    assert [call["role"] for call in case.calls][-2:] == ["writer", "reviewer"]
    assert [call["role"] for call in case.calls].count("writer") == 4


_cases = _lane_case_module()
lane_case, offline_publication = _cases.lane_case, _cases.offline_publication


def test_promotion_never_loses_a_planning_edit_made_meanwhile(tmp_path, monkeypatch):
    from foragent import order, order_prepare_cycle

    item = {"key": "a", "title": "A", "goal": "g", "statements": ["s"], "out_of_scope": "o", "lane": "api",
            "size": "s", "done_when": ["true"], "blocked_by": []}
    plan = {"id": "p", "title": "P", "summary": "x", "spec_markdown": "spec", "issues": [item]}
    config = Config(lanes={"api": {"paths": ["src/**"]}})
    (tmp_path / "tests/acceptance").mkdir(parents=True)
    path = tmp_path / "PLAN.json"
    entry = {"cycle": order_prepare_cycle.identity(plan, item, config, tmp_path), "base_revision": "b1",
             "written": {"tests/acceptance/test_a.py": "assert False\n"}, "done_when": ["check"]}
    edited = json.dumps({**plan, "issues": [{**item, "goal": "planning's newer goal"}]})
    validate = order.validate_plan

    def planning_edits_during_validation(*args, **kwargs):
        path.write_text(edited)  # in place, as an editor does
        return validate(*args, **kwargs)

    monkeypatch.setattr(order, "validate_plan", planning_edits_during_validation)
    path.write_text(json.dumps(plan))
    assert not order_prepare_cycle.promote(path, item, entry, tmp_path, config, "b1")
    assert path.read_text() == edited
    assert sorted(p.name for p in tmp_path.iterdir()) == ["PLAN.json", "tests"]
    monkeypatch.setattr(order, "validate_plan", validate)
    path.write_text(json.dumps(plan))
    assert order_prepare_cycle.promote(path, item, entry, tmp_path, config, "b1")
    assert json.loads(path.read_text())["issues"][0]["done_when"] == ["check"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["PLAN.json", "tests"]


def test_compare_and_swap_keeps_writes_that_race_each_step(tmp_path, monkeypatch):
    from foragent import order_prepare_cycle

    path = tmp_path / "PLAN.json"
    path.write_bytes(b"old")
    assert order_prepare_cycle.replace_unchanged(path, b"old", b"ours") and path.read_bytes() == b"ours"
    assert not order_prepare_cycle.replace_unchanged(path, b"old", b"again") and path.read_bytes() == b"ours"
    real_rename, real_link = order_prepare_cycle.os.rename, order_prepare_cycle.os.link

    def recreate_after_move(source, target):  # planning saves a new plan right after the move
        real_rename(source, target)
        if Path(source) == path:
            path.write_bytes(b"planning")

    path.write_bytes(b"old")
    monkeypatch.setattr(order_prepare_cycle.os, "rename", recreate_after_move)
    assert not order_prepare_cycle.replace_unchanged(path, b"old", b"ours") and path.read_bytes() == b"planning"
    monkeypatch.setattr(order_prepare_cycle.os, "rename", real_rename)

    late = []

    def write_into_moved_file(source, target):  # an editor with the old plan open writes once after the move
        real_link(source, target)
        if Path(target) == path and not late:
            late.append(next(tmp_path.glob(".PLAN.json.prepare-*.old")).write_bytes(b"late edit"))

    path.write_bytes(b"old")
    monkeypatch.setattr(order_prepare_cycle.os, "link", write_into_moved_file)
    assert not order_prepare_cycle.replace_unchanged(path, b"old", b"ours") and path.read_bytes() == b"late edit"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["PLAN.json"]


def test_a_plan_changed_before_the_move_is_never_replaced_even_briefly(tmp_path, monkeypatch):
    from foragent import order_prepare_cycle

    path, linked = tmp_path / "PLAN.json", []
    real_link = order_prepare_cycle.os.link
    monkeypatch.setattr(order_prepare_cycle.os, "link",
                        lambda source, target: (linked.append((Path(source).read_bytes(), Path(target))),
                                                real_link(source, target)))
    path.write_bytes(b"planning's newer plan")
    assert not order_prepare_cycle.replace_unchanged(path, b"old", b"ours")
    assert path.read_bytes() == b"planning's newer plan"
    assert (b"ours", path) not in linked


def _editor_writes_at_removal(monkeypatch, cycle, editor, data, *, after_removal):
    """An editor holding the old plan open flushes through its descriptor around the name's removal."""
    real_unlink, done = cycle.os.unlink, []

    def unlink(name, *args, **kwargs):
        moved = ".prepare-" in str(name) and str(name).endswith(".old") and not done
        if moved and not after_removal:
            done.append((editor.write(data), editor.flush()))
        real_unlink(name, *args, **kwargs)
        if moved and after_removal:
            done.append((editor.write(data), editor.flush()))

    monkeypatch.setattr(cycle.os, "unlink", unlink)
    return done


def test_a_write_through_an_open_descriptor_during_removal_is_never_deleted(tmp_path, monkeypatch):
    from foragent import order_prepare_cycle as cycle

    for after_removal in (False, True):
        path = tmp_path / f"PLAN-{after_removal}.json"
        path.write_bytes(b"old")
        with open(path, "r+b") as editor:  # the editor opened the plan before promotion
            done = _editor_writes_at_removal(monkeypatch, cycle, editor, b"new", after_removal=after_removal)
            assert not cycle.replace_unchanged(path, b"old", b"ours")
        assert done and path.read_bytes() == b"new", after_removal
        assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith((path.name, f".{path.name}"))) == [
            path.name], after_removal


def test_a_late_write_is_kept_beside_a_plan_that_planning_replaced_meanwhile(tmp_path, monkeypatch):
    from foragent import order_prepare_cycle as cycle

    path = tmp_path / "PLAN.json"
    path.write_bytes(b"old")
    real_rename = cycle.os.rename

    def planning_saves_after_the_move(source, target):
        real_rename(source, target)
        if Path(source) == path:
            path.write_bytes(b"planning")

    monkeypatch.setattr(cycle.os, "rename", planning_saves_after_the_move)
    with open(path, "r+b") as editor:
        done = _editor_writes_at_removal(monkeypatch, cycle, editor, b"late", after_removal=True)
        assert not cycle.replace_unchanged(path, b"old", b"ours")
    assert done and path.read_bytes() == b"planning"
    kept = [p.read_bytes() for p in tmp_path.glob("PLAN.json.conflict-*")]
    assert kept == [b"late"] and not list(tmp_path.glob(".PLAN.json.prepare-*"))


def test_moved_bytes_that_differ_are_renamed_never_deleted_when_the_name_is_taken(tmp_path, monkeypatch):
    from foragent import order_prepare_cycle as cycle

    path = tmp_path / "PLAN.json"
    path.write_bytes(b"edited in place")
    real_rename = cycle.os.rename

    def planning_saves_after_the_move(source, target):
        real_rename(source, target)
        if Path(source) == path:
            path.write_bytes(b"planning")

    monkeypatch.setattr(cycle.os, "rename", planning_saves_after_the_move)
    assert not cycle.replace_unchanged(path, b"old", b"ours")
    assert path.read_bytes() == b"planning"
    assert [p.read_bytes() for p in tmp_path.glob("PLAN.json.conflict-*")] == [b"edited in place"]


def test_identity_binds_every_other_issue_and_the_risk_guidance(tmp_path):
    from foragent import order_prepare_cycle

    item = {"key": "a", "title": "A", "goal": "g", "statements": ["s"], "out_of_scope": "o", "lane": "api",
            "size": "s", "done_when": ["true"], "blocked_by": []}
    other = {**item, "key": "b", "title": "B", "statements": ["the other contract"]}
    legacy = {key: value for key, value in item.items() if key != "statements"} | {
        "key": "c", "acceptance_files": {"tests/acceptance/test_c.py": "assert False\n"}}
    plan = {"id": "p", "title": "P", "summary": "x", "spec_markdown": "spec", "issues": [item, other, legacy],
            "edits": [{"number": 7, "goal": "edit goal", "out_of_scope": "o", "statements": ["e"]}]}
    config = Config(lanes={"api": {"paths": ["notice.py"]}})
    first = order_prepare_cycle.identity(plan, item, config, tmp_path)
    changes = [
        {**plan, "issues": [item, {**other, "statements": ["a changed contract"]}, legacy]},
        {**plan, "issues": [item, {**other, "goal": "another goal"}, legacy]},
        {**plan, "issues": [item, other, {**legacy, "acceptance_files": {"tests/acceptance/test_c.py": "x\n"}}]},
        {**plan, "issues": [item, other, {**legacy, "done_when": ["other"]}]},
        {**plan, "edits": [{**plan["edits"][0], "statements": ["f"]}]},
    ]
    for changed in changes:
        assert order_prepare_cycle.identity(changed, item, config, tmp_path) != first, changed
    # Another statement issue's prepared tests are that issue's own output, bound by its own cycle.
    promoted = {**plan, "issues": [item, {**other, "acceptance_files": {"tests/acceptance/test_b.py": "y\n"},
                                          "done_when": ["pytest tests/acceptance/test_b.py"]}, legacy]}
    assert order_prepare_cycle.identity(promoted, item, config, tmp_path) == first
    (tmp_path / ".foragent-risks.yml").write_text("- paths: [notice.py]\n  guidance: fake process tables only\n")
    assert order_prepare_cycle.identity(plan, item, config, tmp_path) != first


def test_invalid_review_outcomes_carry_no_verdict(tmp_path):
    for reply in ({"verdict": "hold", "reason": " ", "questions": []},
                  {"verdict": "question", "reason": "", "questions": [" "]},
                  {**PASS, "coverage": []}):
        outcome = _review_outcome(tmp_path, reply)
        assert outcome["status"] == "interrupted" and "verdict" not in outcome, reply
    changed = _review_outcome(tmp_path, {"verdict": "hold", "reason": "weak", "questions": []}, changed=["notice.py"])
    assert changed["status"] == "interrupted" and "verdict" not in changed


def test_a_reviewer_changing_main_never_reaches_a_pass_or_the_next_review(request, capsys):
    cases = _cases
    case = request.getfixturevalue("lane_case")
    seen = []

    def corrupt(worktree):
        seen.append((worktree / "notice.py").read_text())
        (worktree / "notice.py").write_text(cases.NOTICE.replace('"deliveries": 0', '"deliveries": 1'))

    case.reviewer_hook = corrupt
    row = cases.prepare(case, capsys)
    assert row["status"] == "interrupted" and "notice.py" in row["reason"] and row["holds"] == 0
    assert "acceptance_files" not in cases.disk_plan(case)["issues"][0]
    case.reviewer_exit = 1  # an interrupted reviewer that changed main: nothing it left may reach the retry
    assert cases.prepare(case, capsys)["status"] == "waiting"
    case.reviewer_hook = lambda worktree: seen.append((worktree / "notice.py").read_text())
    case.reviewer_exit = 0
    assert cases.prepare(case, capsys)["status"] == "reviewed"
    assert seen == [cases.NOTICE] * 3
    assert [call["role"] for call in case.calls] == ["writer", "reviewer", "reviewer", "reviewer"]


def test_writer_and_reviewer_read_every_other_issues_contract(tmp_path):
    from foragent import order_prepare_cycle, order_prepare_prompt, order_prepare_review

    item = {"key": "a", "title": "A", "goal": "g", "statements": ["s"], "out_of_scope": "o", "lane": "api",
            "size": "s", "done_when": ["true"], "blocked_by": ["b"], "files": ["notice.py"],
            "checklist": ["A person reads the receipt aloud."]}
    other = {**item, "key": "b", "title": "Receipts", "goal": "Receipts are JSON lines named receipt.jsonl.",
             "statements": ["Every send appends one line to receipt.jsonl."], "out_of_scope": "Rotation.",
             "blocked_by": [], "files": ["receipts.py"], "checklist": [],
             "acceptance_files": {"tests/acceptance/test_b.py": "PREPARED BY ITS OWN CYCLE\n"}}
    legacy = {key: value for key, value in item.items() if key not in {"statements", "checklist"}} | {
        "key": "c", "title": "Legacy", "goal": "legacy goal", "blocked_by": [],
        "acceptance_files": {"tests/acceptance/test_c.py": "LEGACY LOCKED TEST\n"}}
    plan = {"id": "p", "title": "P", "summary": "x", "spec_markdown": "spec", "issues": [item, other, legacy],
            "edits": [{"number": 7, "goal": "The CLI exit code stays 0.", "out_of_scope": "Flags.",
                       "statements": ["Sending exits with status 0."]}]}
    config = Config(lanes={"api": {"paths": ["notice.py"]}})
    entry = {"written": {"tests/acceptance/test_a.py": "assert False\n"}, "done_when": ["check"],
             "base_revision": "b1"}
    prompts = {"writer": order_prepare_prompt.writer_prompt(plan, item, config, tmp_path, tmp_path, "b1"),
               "reviewer": order_prepare_review.review_prompt(plan, item, config, tmp_path, entry)}
    for role, prompt in prompts.items():
        for fact in ("Receipts are JSON lines named receipt.jsonl.", "Every send appends one line to receipt.jsonl.",
                     "Rotation.", "receipts.py", "Issue b: Receipts", "legacy goal", "LEGACY LOCKED TEST",
                     "Edit of existing issue #7", "The CLI exit code stays 0.", "Sending exits with status 0.",
                     "A person reads the receipt aloud."):
            assert fact in prompt, (role, fact)
        # Another statement issue's prepared tests are its own cycle's output, never this one's context.
        assert "PREPARED BY ITS OWN CYCLE" not in prompt, role
    # What both read is what the cycle binds: changing the other issue's contract starts a new cycle.
    changed = {**plan, "issues": [item, {**other, "goal": "Receipts are CSV."}, legacy]}
    assert order_prepare_cycle.identity(changed, item, config, tmp_path) != order_prepare_cycle.identity(
        plan, item, config, tmp_path)


def _record(case):
    return order_prepare._record_path(case.store, case.plan)


def _stopped_owner():
    """A process lifetime known to have ended: a child that already exited."""
    import sys

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return {"pid": child.pid, "start_time": 1.0, "taken_at": "2026-10-31T00:00:00+00:00"}


def _left_running(case, phase, **changes):
    """The record as an owner leaves it when it stops during ``phase``: no finish, no outcome."""
    path = _record(case)
    record = json.loads(path.read_text())
    entry = record["issues"]["notice"]
    entry.update(status=phase, phase=phase, **changes)
    for name in ("finished_at", "status", "reason"):
        entry["attempts"][-1].pop(name, None)
    record["owner"] = _stopped_owner()
    path.write_text(json.dumps(record))
    return path


def test_a_stopped_review_shows_interrupted_and_resumes_from_the_written_bytes(request, capsys):
    case = request.getfixturevalue("lane_case")
    case.reviewer_exit = 1
    assert _cases.prepare(case, capsys)["status"] == "waiting"
    path = _left_running(case, "reviewing_tests")
    before, calls = path.read_bytes(), len(case.calls)
    row = _cases.prepare(case, capsys, status=True)
    # Status is a projection: the stopped owner's step is interrupted, the record is not rewritten.
    assert row["status"] == "interrupted" and row["phase"] == "reviewing_tests" and row["holds"] == 0
    assert "stopped" in row["reason"] and path.read_bytes() == before and len(case.calls) == calls
    case.reviewer_exit = 0
    assert _cases.prepare(case, capsys)["status"] == "reviewed"
    assert [call["role"] for call in case.calls] == ["writer", "reviewer", "reviewer"]
    assert case.calls[-1]["candidate"] == case.candidate
    attempts = json.loads(path.read_text())["issues"]["notice"]["attempts"]
    # The successor closed the stopped attempt as an interruption; it is no hold.
    assert attempts[1]["status"] == "interrupted" and attempts[1]["finished_at"]
    assert attempts[2]["status"] == "reviewed"


def test_a_stopped_writer_never_promotes_its_partial_files(request, capsys):
    case = request.getfixturevalue("lane_case")
    case.writer_exit = 1
    _cases.prepare(case, capsys)
    path = _left_running(case, "writing_tests", written={_cases.TEST_PATH: "PARTIAL\n"})
    row = _cases.prepare(case, capsys, status=True)
    assert row["status"] == "interrupted" and row["phase"] == "writing_tests"
    case.writer_exit = 0
    assert _cases.prepare(case, capsys)["status"] == "reviewed"
    assert [call["role"] for call in case.calls] == ["writer", "writer", "reviewer"]
    assert _cases.disk_plan(case)["issues"][0]["acceptance_files"] == {_cases.TEST_PATH: case.candidate}
    attempts = json.loads(path.read_text())["issues"]["notice"]["attempts"]
    assert attempts[0]["status"] == "interrupted" and attempts[1]["status"] == "written"


def test_a_running_recorded_owner_keeps_its_preparation(request, capsys, monkeypatch):
    from foragent import processes

    case = request.getfixturevalue("lane_case")
    case.reviewer_exit = 1
    _cases.prepare(case, capsys)
    path = _left_running(case, "reviewing_tests")
    record = json.loads(path.read_text())
    # A lifetime the (fake) process table still lists: e.g. the lock file was replaced under it.
    monkeypatch.setattr(processes, "list_processes", lambda: (processes.ProcessIdentity(424242, 1, 424242, 424242, 5.0),))
    record["owner"] = {"pid": 424242, "start_time": 5.0}
    path.write_text(json.dumps(record))
    before, calls = path.read_bytes(), len(case.calls)
    assert _cases.prepare(case, capsys, status=True)["status"] == "reviewing_tests"
    row = _cases.prepare(case, capsys)
    assert row["status"] == "waiting" and "running" in row["reason"]
    assert path.read_bytes() == before and len(case.calls) == calls


def test_holds_and_a_question_survive_an_unrelated_issue_edit(request, capsys):
    case = request.getfixturevalue("lane_case")
    legacy_path = "tests/acceptance/test_existing_notice.py"
    legacy = {"key": "legacy", "title": "Legacy notice", "goal": "Keep the old contract.",
              "out_of_scope": "New delivery.", "done_when": [f"python {legacy_path}"],
              "lane": "api", "size": "s", "acceptance_files": {legacy_path: "assert True\n"}}
    case.plan["issues"].append(legacy)
    case.save()
    case.verdicts = ["hold", "hold"]
    first = _cases.prepare(case, capsys)
    assert first["status"] == "hold" and first["holds"] == 1
    # Another issue's edit is new context: new evidence is written, but the hold is not erased.
    case.plan["issues"][1]["goal"] = "Keep the old contract, documented."
    case.save()
    second = _cases.prepare(case, capsys)
    assert second["status"] == "question" and second["holds"] == 2
    calls = len(case.calls)
    case.plan["issues"][1]["goal"] = "Keep the old contract, documented twice."
    case.save()
    third = _cases.prepare(case, capsys)
    assert third["status"] == "question" and len(case.calls) == calls
    # Planning's clarification of this issue starts a new cycle; the earlier attempts stay inspectable.
    # (The offline reviewer maps the fixture's one statement, so the goal carries the clarification.)
    case.plan["issues"][0]["goal"] = _cases.STATEMENT + " The receipt holds exactly one line."
    case.save()
    fourth = _cases.prepare(case, capsys)
    assert fourth["status"] == "reviewed" and fourth["holds"] == 0 and fourth["attempts"] == 6
