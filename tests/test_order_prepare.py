"""Unit checks for `fa order prepare`'s scratch tree comparison and status projection."""

from dataclasses import replace
import json
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
    return git, tree, order_scratch.build(git, base, tree, {".claude/agents/fa-api.md": ("file", 0o644, b"agent\n")})


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
    config, files, refused = order_prepare_setup.lane_context(plan, {"lane": "web"}, Config(), tmp_path)
    assert refused == []
    assert seen == ["lib:skills/tdd"]
    assert config.lanes["web"] == {"paths": ["web/**"], "agent": "fa-web", "skills": ["tdd"]}
    assert files[".claude/skills/tdd/SKILL.md"][2].endswith(b"Write the test first.\n")
    assert b"web lane (web/**)" in files[".claude/agents/fa-web.md"][2]
    tree = tmp_path / "tree"
    tree.mkdir()
    assert files[".claude/skills/tdd"] is None, "main's older copy of the skill is removed first"
    for name, data in files.items():
        if data is not None:
            (tree / name).parent.mkdir(parents=True, exist_ok=True)
            (tree / name).write_bytes(data[2])
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
    config, _files, _refused = order_prepare_setup.lane_context(plan, item, Config(), tmp_path)
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


def test_a_lane_without_an_agent_still_brings_its_checkout_memory(tmp_path):
    from foragent import lane_memory

    config = Config(lanes={"api": {"paths": ["api/**"]}})
    memory = tmp_path / lane_memory.lane_directory(config, "api") / "MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("- lesson\n")
    _config, files, _refused = order_prepare_setup.lane_context({}, {"lane": "api"}, config, tmp_path)
    folder = memory.parent.relative_to(tmp_path).as_posix()
    assert files == {folder: None, memory.relative_to(tmp_path).as_posix(): ("file", 0o644, b"- lesson\n")}


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
    _config, overlay, _refused = order_prepare_setup.lane_context({}, {"lane": "api"}, config, root)
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


def test_symlinked_context_reaches_the_writer_as_preread_reads_it(git_history, tmp_path):
    from foragent import lane_memory

    git_history.update({"app.py": "x = 1\n"})
    root = git_history.clone
    git_command(root, "fetch", "origin", "main")
    git = GitRepo(root)
    base = git.ref_sha("refs/remotes/origin/main")
    config = Config(executor="codex", lanes={"api": {"paths": ["api/**"], "agent": "fa-api",
                                                       "skills": ["tdd", "linked"]}})
    shared = root / "shared"
    (shared / "linked-skill").mkdir(parents=True)
    (shared / "agent.md").write_text("---\nname: fa-api\n---\nLINKED AGENT NOTES\n")
    (shared / "memory.md").write_text("- LINKED LESSON\n")
    (shared / "SKILL.md").write_text("---\nname: tdd\ndescription: linked skill file\n---\nbody\n")
    (shared / "linked-skill/SKILL.md").write_text("---\nname: linked\ndescription: linked folder\n---\nbody\n")
    (root / ".claude/agents").mkdir(parents=True)
    (root / ".claude/agents/fa-api.md").symlink_to(shared / "agent.md")
    memory = root / lane_memory.lane_directory(config, "api")
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").symlink_to(shared / "memory.md")
    (root / ".claude/skills/tdd").mkdir(parents=True)
    (root / ".claude/skills/tdd/SKILL.md").symlink_to(shared / "SKILL.md")
    (root / ".claude/skills/linked").symlink_to(shared / "linked-skill")
    _config, overlay, refused = order_prepare_setup.lane_context({}, {"lane": "api"}, config, root)
    assert refused == []
    tree = tmp_path / "attempt/tree"
    order_scratch.build(git, base, tree, overlay)
    # Inside links stay links, now pointing inside the scratch tree, never at the checkout.
    for name in (".claude/agents/fa-api.md", ".claude/skills/tdd/SKILL.md", ".claude/skills/linked",
                 f"{lane_memory.lane_directory(config, 'api')}/MEMORY.md"):
        assert (tree / name).is_symlink() and (tree / name).resolve().is_relative_to(tree.resolve()), name
    writer = order_prepare_setup.writer_setup(config, {"lane": "api", "size": "s"}, tree)
    preread = order_prepare_setup.writer_setup(config, {"lane": "api", "size": "s"}, root)
    assert "LINKED AGENT NOTES" in writer.notes and "LINKED LESSON" in writer.notes
    assert writer.missing_skills == [] and writer.notes == preread.notes



def test_overlay_never_reaches_through_a_tracked_symlink_into_the_checkout(git_history, tmp_path):
    from foragent import lane_memory

    config = Config(executor="codex", lanes={"api": {"paths": ["api/**"], "agent": "fa-api"}})
    folder = lane_memory.lane_directory(config, "api")
    outside = tmp_path / "checkout-claude"
    (outside / folder.removeprefix(".claude/")).mkdir(parents=True)
    (outside / folder.removeprefix(".claude/") / "MEMORY.md").write_text("- original lesson\n")
    (outside / "agents").mkdir()
    (outside / "agents/fa-api.md").write_text("ORIGINAL AGENT\n")
    # main tracks `.claude` as a symlink that leaves the scratch tree.
    target = str(outside).encode()
    stream = (f"commit refs/heads/main\ncommitter T <t@example.com> 1792483200 +0000\ndata 4\nlink\n"
              f"from {git_history.tip}\nM 120000 inline .claude\ndata {len(target)}\n").encode() + target + b"\n\ndone\n"
    subprocess.run(["git", "fast-import", "--quiet"], cwd=git_history.origin, input=stream, check=True)
    root = git_history.clone
    git_command(root, "fetch", "origin", "main")
    git = GitRepo(root)
    overlay = {folder: None, f"{folder}/MEMORY.md": ("file", 0o644, b"- lesson\n"), ".claude/agents/fa-api.md": None,
               ".claude/skills/tdd": None, ".claude/skills/tdd/SKILL.md": ("file", 0o644, b"skill\n")}
    tree = tmp_path / "attempt/tree"
    order_scratch.build(git, git.ref_sha("refs/remotes/origin/main"), tree, overlay)
    assert (outside / folder.removeprefix(".claude/") / "MEMORY.md").read_text() == "- original lesson\n"
    assert (outside / "agents/fa-api.md").read_text() == "ORIGINAL AGENT\n"
    assert not (outside / "skills").exists()
    assert not (tree / ".claude").is_symlink()
    assert (tree / folder / "MEMORY.md").read_text() == "- lesson\n"
    assert (tree / ".claude/skills/tdd/SKILL.md").read_text() == "skill\n"


def test_a_directory_alias_inside_a_skill_stays_a_link_to_its_folder(tmp_path):
    skill = tmp_path / ".claude/skills/tdd"
    (skill / "resources").mkdir(parents=True)
    (skill / "resources/example.txt").write_text("example\n")
    (skill / "linked").symlink_to("resources")
    (skill / "loop").symlink_to(".")
    entries, refused = {}, []
    order_scratch_context.place(tmp_path, ".claude/skills/tdd", entries, refused)
    mode = (skill / "resources/example.txt").stat().st_mode & 0o7777
    assert refused == []
    assert entries == {".claude/skills/tdd": None, ".claude/skills/tdd/linked": ("link", "resources"),
                       ".claude/skills/tdd/resources": None,
                       ".claude/skills/tdd/loop": ("link", "."),
                       ".claude/skills/tdd/resources/example.txt": ("file", mode, b"example\n")}


def test_outside_links_and_cycles_are_refused_by_path_and_never_crash(git_history, tmp_path):
    from foragent import lane_memory
    from foragent.executors import ExecResult

    git_history.update({"app.py": "x = 1\n", ".gitignore": ".venv/\n"})
    root = git_history.clone
    git_command(root, "fetch", "origin", "main")
    git = GitRepo(root)
    config = Config(executor="codex", lanes={"api": {"paths": ["api/**"], "agent": "fa-api",
                                                       "skills": ["outside"]}})
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "skill").mkdir(parents=True)
    (elsewhere / "agent.md").write_text("---\nname: fa-api\n---\nOUTSIDE AGENT NOTES\n")
    (elsewhere / "memory.md").write_text("- OUTSIDE LESSON\n")
    (elsewhere / "skill/SKILL.md").write_text("---\nname: outside\ndescription: d\n---\nbody\n")
    (root / ".claude/agents").mkdir(parents=True)
    (root / ".claude/agents/fa-api.md").symlink_to(elsewhere / "agent.md")
    memory = root / lane_memory.lane_directory(config, "api")
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").symlink_to(elsewhere / "memory.md")
    (memory / "cycle.md").symlink_to(memory / "cycle.md")
    (root / ".claude/skills").mkdir(parents=True)
    (root / ".claude/skills/outside").symlink_to(elsewhere / "skill")
    _config, overlay, refused = order_prepare_setup.lane_context({}, {"lane": "api"}, config, root)
    folder = lane_memory.lane_directory(config, "api")
    # Escaping links and cycles are refused by path, never copied and never dropped silently.
    assert refused == sorted([".claude/agents/fa-api.md", f"{folder}/MEMORY.md", f"{folder}/cycle.md",
                              ".claude/skills/outside"])
    assert not any(isinstance(entry, tuple) and b"OUTSIDE" in entry[-1] for entry in overlay.values()
                   if entry and entry[0] == "file")
    tree = tmp_path / "attempt/tree"
    manifest = order_scratch.build(git, git.ref_sha("refs/remotes/origin/main"), tree, overlay)
    # A cyclic link the writer leaves behind becomes a path question, not a crash.
    (tree / "tests/acceptance").mkdir(parents=True, exist_ok=True)
    (tree / "tests/acceptance/loop.py").symlink_to(tree / "tests/acceptance/loop.py")
    (tree / ".venv").mkdir()
    (tree / ".venv/loop").symlink_to(tree / ".venv/loop")
    result = ExecResult(0, False, "{}", tmp_path / "log")
    outcome = order_prepare._judge(result, None, tree, git, manifest, Config(language="en"), ())
    assert outcome["status"] == "question"
    assert any("tests/acceptance/loop.py" in question for question in outcome["questions"])
    assert any(".venv/loop" in question for question in outcome["questions"])


def test_an_executable_skill_script_stays_runnable_in_the_scratch(git_history, tmp_path, monkeypatch):
    git_history.update({"app.py": "x = 1\n"})
    root = git_history.clone
    git_command(root, "fetch", "origin", "main")
    git = GitRepo(root)
    script = "#!/bin/sh\necho skill-ran\n"
    # A checkout skill and a proposed lane's library skill, each with a 0o755 script.
    (root / ".claude/skills/tdd").mkdir(parents=True)
    (root / ".claude/skills/tdd/SKILL.md").write_text("---\nname: tdd\ndescription: d\n---\n")
    (root / ".claude/skills/tdd/run.sh").write_text(script)
    (root / ".claude/skills/tdd/run.sh").chmod(0o755)
    library = tmp_path / "library/web-skill"
    library.mkdir(parents=True)
    (library / "SKILL.md").write_text("---\nname: web-skill\ndescription: d\n---\n")
    (library / "go.sh").write_text(script)
    (library / "go.sh").chmod(0o755)

    class Manager:
        def __init__(self, config):
            pass

        def check_install(self, identity, root):
            return SimpleNamespace(path=library)

    monkeypatch.setattr(order_prepare_setup, "SkillManager", Manager)
    monkeypatch.setattr(order_prepare_setup, "with_vendored_library", lambda config, cwd, root: config)
    base = git.ref_sha("refs/remotes/origin/main")
    checkout = Config(lanes={"api": {"paths": ["api/**"], "skills": ["tdd"]}})
    plan = {"new_lanes": [{"name": "web", "paths": ["web/**"], "skills": ["lib:web-skill"]}]}
    for number, (item, config, name) in enumerate([
            ({"lane": "api"}, checkout, ".claude/skills/tdd/run.sh"),
            ({"lane": "web"}, Config(), ".claude/skills/web-skill/go.sh")]):
        _config, overlay, refused = order_prepare_setup.lane_context(plan, item, config, root)
        tree = tmp_path / f"attempt-{number}/tree"
        manifest = order_scratch.build(git, base, tree, overlay)
        assert refused == [] and manifest[name][0] == "100755"
        ran = subprocess.run([str(tree / name)], capture_output=True, text=True, check=True)
        assert ran.stdout == "skill-ran\n"


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


def test_refused_lane_context_asks_before_any_writer_starts(tmp_path, monkeypatch):
    plan = {"id": "o", "title": "T", "summary": "", "spec_markdown": "", "issues": [
        {"key": "a", "title": "A", "goal": "g", "statements": ["s"], "out_of_scope": "x",
         "done_when": ["true"], "lane": "main", "size": "s", "blocked_by": []}]}
    store = SimpleNamespace(root=tmp_path / "state")
    monkeypatch.setattr("foragent.order.validate_plan", lambda plan, root, config: {})
    monkeypatch.setattr(order_scratch, "base_revision", lambda git, branch: "abc")
    monkeypatch.setattr(order_prepare_setup, "lane_context",
                        lambda plan, item, config, root: (config, {}, [".claude/agents/fa-api.md"]))
    monkeypatch.setattr(order_prepare, "executor_for", lambda name: (_ for _ in ()).throw(AssertionError))
    row = order_prepare.prepare(plan, tmp_path, Config(language="en"), None, store)[0]
    assert row["status"] == "question" and ".claude/agents/fa-api.md" in row["questions"][0]


def test_library_skill_links_land_inside_the_installed_skill_folder(git_history, tmp_path, monkeypatch):
    git_history.update({"resources/example.txt": "main's own file\n"})
    root = git_history.clone
    git_command(root, "fetch", "origin", "main")
    git = GitRepo(root)
    library = tmp_path / "library/web-skill"
    (library / "resources").mkdir(parents=True)
    (library / "SKILL.md").write_text("---\nname: web-skill\ndescription: d\n---\n")
    (library / "resources/example.txt").write_text("skill resource\n")
    (library / "alias").symlink_to("resources")
    monkeypatch.setattr(order_prepare_setup, "SkillManager",
                        lambda config: SimpleNamespace(check_install=lambda identity, root: SimpleNamespace(path=library)))
    monkeypatch.setattr(order_prepare_setup, "with_vendored_library", lambda config, cwd, root: config)
    plan = {"new_lanes": [{"name": "web", "paths": ["web/**"], "skills": ["lib:web-skill"]}]}
    _config, overlay, refused = order_prepare_setup.lane_context(plan, {"lane": "web"}, Config(), root)
    assert refused == [] and overlay[".claude/skills/web-skill/alias"] == ("link", "resources")
    tree = tmp_path / "attempt/tree"
    order_scratch.build(git, git.ref_sha("refs/remotes/origin/main"), tree, overlay)
    assert (tree / ".claude/skills/web-skill/alias/example.txt").read_text() == "skill resource\n"
    assert (tree / "resources/example.txt").read_text() == "main's own file\n"


def test_a_linked_claude_folder_leading_outside_refuses_every_context_place(tmp_path):
    from foragent import lane_memory

    root, outside = tmp_path / "repo", tmp_path / "outside"
    config = Config(lanes={"api": {"paths": ["api/**"], "agent": "fa-api", "skills": ["tdd"]}})
    folder = lane_memory.lane_directory(config, "api")
    for name in ("agents/fa-api.md", f"{folder.removeprefix('.claude/')}/MEMORY.md", "skills/tdd/SKILL.md"):
        (outside / name).parent.mkdir(parents=True, exist_ok=True)
        (outside / name).write_text("OUTSIDE\n")
    root.mkdir()
    (root / ".claude").symlink_to(outside)
    _config, overlay, refused = order_prepare_setup.lane_context({}, {"lane": "api"}, config, root)
    assert refused == sorted([".claude/agents/fa-api.md", folder, ".claude/skills/tdd"])
    assert all(entry is None for entry in overlay.values()), overlay


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



def test_a_linked_memory_folder_drops_what_the_checkout_deleted_at_its_target(git_history, tmp_path):
    from foragent import lane_memory

    config = Config(executor="codex", lanes={"api": {"paths": ["api/**"], "agent": "fa-api"}})
    folder = lane_memory.lane_directory(config, "api")
    # main has the shared memory with a pending file and a link target the checkout no longer has.
    git_history.update({"shared/memory/MEMORY.md": "- old\n", "shared/memory/MEMORY.new.md": "- STALE PENDING\n",
                        "shared/gone.md": "STALE TARGET\n"})
    root = git_history.clone
    git_command(root, "fetch", "origin", "main")
    git_command(root, "merge", "--ff-only", "origin/main")
    git = GitRepo(root)
    (root / "shared/memory/MEMORY.new.md").unlink()
    (root / "shared/memory/MEMORY.md").write_text("- current lesson\n")
    (root / "shared/gone.md").unlink()
    (root / folder).parent.mkdir(parents=True)
    (root / folder).symlink_to(root / "shared/memory")
    (root / ".claude/agents").mkdir(parents=True)
    (root / ".claude/agents/fa-api.md").symlink_to(root / "shared/gone.md")  # dangling in the checkout
    _config, overlay, refused = order_prepare_setup.lane_context({}, {"lane": "api"}, config, root)
    tree = tmp_path / "attempt/tree"
    order_scratch.build(git, git.ref_sha("refs/remotes/origin/main"), tree, overlay)
    assert refused == []
    assert sorted(path.name for path in (tree / folder).iterdir()) == ["MEMORY.md"]
    assert not (tree / ".claude/agents/fa-api.md").exists(), "dangling as in the checkout"
    writer = order_prepare_setup.writer_setup(config, {"lane": "api", "size": "s"}, tree)
    preread = order_prepare_setup.writer_setup(config, {"lane": "api", "size": "s"}, root)
    assert "STALE" not in writer.notes and "current lesson" in writer.notes
    assert writer.notes.replace(str(tree), "") == preread.notes.replace(str(root), "")


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
    monkeypatch.setattr(order_prepare_setup, "lane_context", lambda plan, item, config, root: (config, {}, []))
    monkeypatch.setattr(order_scratch, "protected", lambda git, root, state: ())

    def build(git, base, tree, overlay):
        assert not tree.exists(), "each issue gets a tree of its own"
        tree.mkdir(parents=True)
        trees.append(tree)
        raise OSError("stop before the writer")

    monkeypatch.setattr(order_scratch, "build", build)
    rows = order_prepare.prepare(plan, tmp_path, Config(language="en"), None, store,
                                 lambda: SimpleNamespace(get_issue=lambda n: None))
    assert [row["key"] for row in rows] == ["7", "#7"] and len(set(trees)) == 2
    assert order_prepare._record_path(store, {"id": "a/b"}) != order_prepare._record_path(store, {"id": "a-b"})
