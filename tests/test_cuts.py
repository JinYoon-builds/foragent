"""Shared cut notice, parent summary and moved-file detection."""

import subprocess

from foragent import cuts
from foragent.git import GitRepo
from foragent.run_parent import parent_summary
from foragent.run_prompts import build_resume_prompt


def test_head_tail_and_lines_keep_limits_and_link_the_full_text(tmp_path):
    text = "".join(f"line {n}\n" for n in range(10))
    head = cuts.head(text, 20, tmp_path, "head.txt")
    assert head.startswith("line 0\nline 1\n") and f"full text: {tmp_path / 'head.txt'}]" in head
    assert f"[cut: {len(text) - 14} more chars;" in head
    tail = cuts.tail(text, 7, tmp_path, "tail.txt")
    assert tail.endswith("line 9\n") and tail.startswith(f"[cut: {len(text) - 7} more chars;")
    lines = cuts.head_lines(text, 3, tmp_path, "lines.txt")
    assert lines.startswith("line 0\nline 1\nline 2\n[cut: 7 more lines;")
    assert all((tmp_path / name).read_text() == text for name in ("head.txt", "tail.txt", "lines.txt"))
    assert cuts.head(text, 1000, tmp_path, "x") == text and not (tmp_path / "x").exists()


def test_log_dir_maps_to_the_issue_state_folder(tmp_path):
    assert cuts.dir_for_log(tmp_path / "logs" / "issue-7.log") == tmp_path / "runs" / "issue-7"
    assert cuts.issue_dir(tmp_path, 7) == tmp_path / "runs" / "issue-7"


def test_parent_summary_needs_plan_marker_and_drops_checklist():
    body = "Outcome.\n\nConstraint A.\n\n- [ ] #2 Child\n- [x] #3 Other\n\n<!-- foragent:plan id=x map={} acceptance={} -->\n"
    assert parent_summary(body) == "Outcome.\n\nConstraint A."
    assert parent_summary("Batch parent without a marker") == ""


def test_resume_prompt_names_moved_files():
    prompt = build_resume_prompt(1, None, "body", moved_files=["a.py -> b.py"])
    assert "- a.py -> b.py" in prompt


def test_moved_by_names_branch_files_the_target_renamed_or_split(tmp_path):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}

    def git(*args, stdin=None):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True,
                              text=True, input=stdin, env=env).stdout.strip()

    def commit(files, parent=None):
        entries = "".join(f"100644 blob {git('hash-object', '-w', '--stdin', stdin=body)}\t{name}\n"
                          for name, body in files.items())
        tree = git("mktree", stdin=entries)
        return git("commit-tree", tree, "-m", "c", *(["-p", parent] if parent else []))

    git("init", "-q")
    content = "".join(f"def f{n}():\n    return {n}\n\n" for n in range(40))
    base = commit({"big.py": content, "other.py": "x = 1\n"})
    mine = commit({"big.py": content + "# mine\n", "other.py": "x = 1\n"}, base)
    main = commit({"moved.py": content, "other.py": "x = 1\n"}, base)
    git("update-ref", "refs/heads/main", main)
    git("update-ref", "--no-deref", "HEAD", mine)
    assert GitRepo(tmp_path).moved_by("main") == [("big.py", "moved.py")]


def test_resume_prompt_restates_parent_context():
    prompt = build_resume_prompt(1, "body", "body", parent_context="Use receipt_schema_v3.")
    assert "shared outcome and constraints:\nUse receipt_schema_v3." in prompt


def test_attempt_failure_keeps_every_check_whole():
    from foragent.run_attempt import AttemptFailure, WholeFailure

    whole = WholeFailure("Command: a\n" + "x" * 4000 + "\n\nCommand: b\nlast")
    assert isinstance(AttemptFailure(whole).failure, WholeFailure)
    assert type(AttemptFailure("plain").failure) is str


def test_fake_parent_issue_reads_the_parent_in_one_call():
    from foragent.tracker import FakeTracker

    tracker = FakeTracker()
    tracker.add_issue(1, "Parent", "Outcome.\n<!-- foragent:plan id=x map={} acceptance={} -->", [])
    tracker.add_issue(2, "Child", "body", [])
    tracker.set_parent(2, 1)
    assert tracker.parent_issue(2).body.startswith("Outcome.") and tracker.parent_issue(1) is None


def test_target_switch_names_files_the_new_target_moved(tmp_path):
    from types import SimpleNamespace
    from foragent.run_conflicts import _note_moved

    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}

    def git(*args, stdin=None):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True,
                              text=True, input=stdin, env=env).stdout.strip()

    def commit(files, parent):
        entries = "".join(f"100644 blob {git('hash-object', '-w', '--stdin', stdin=body)}\t{name}\n"
                          for name, body in files.items())
        return git("commit-tree", git("mktree", stdin=entries), "-m", "c", *(["-p", parent] if parent else []))

    git("init", "-q")
    content = "".join(f"def f{n}():\n    return {n}\n\n" for n in range(40))
    base = commit({"big.py": content}, None)
    # The batch branch renamed something else; main split big.py into a new module.
    batch = commit({"big.py": content, "batch_only.py": "y = 2\n"}, base)
    main = commit({"big.py": content[:200], "parts.py": content}, base)
    moved = GitRepo(tmp_path).moved_between(batch, main, ["big.py"])
    assert moved == [("big.py", "parts.py")]
    from foragent.state import StateStore

    store = StateStore(tmp_path / "state")
    store.start_run(1, branch="foragent/issue-1", worktree=tmp_path / "wt")
    attempt = SimpleNamespace(n=1, log=tmp_path / "logs" / "issue-1.log",
                              ctx=SimpleNamespace(store=store, config=SimpleNamespace(language="en")))
    _note_moved(attempt, moved)
    _note_moved(attempt, moved)
    assert store.get_run(1).moved_files == ["big.py -> parts.py"]
    assert attempt.log.read_text().count("big.py -> parts.py") == 1
    # A restart before the notice reached a prompt keeps it for the next run.
    store.finish_run(1, outcome="limit-wait")
    assert store.start_run(1, branch="foragent/issue-1", worktree=tmp_path / "wt").moved_files == ["big.py -> parts.py"]


def test_moved_between_names_a_copy_whose_original_main_left_unchanged(tmp_path):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}

    def git(*args, stdin=None):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True,
                              text=True, input=stdin, env=env).stdout.strip()

    def commit(files, parent):
        entries = "".join(f"100644 blob {git('hash-object', '-w', '--stdin', stdin=body)}\t{name}\n"
                          for name, body in files.items())
        return git("commit-tree", git("mktree", stdin=entries), "-m", "c", *(["-p", parent] if parent else []))

    git("init", "-q")
    content = "".join(f"def f{n}():\n    return {n}\n\n" for n in range(40))
    base = commit({"big.py": content}, None)
    # main copies big.py into a new module and leaves the original untouched.
    main = commit({"big.py": content, "copy.py": content}, base)
    assert GitRepo(tmp_path).moved_between(base, main, ["big.py"]) == [("big.py", "copy.py")]


def test_previous_attempt_records_name_what_their_limits_leave_out(tmp_path):
    from types import SimpleNamespace
    from foragent.run_previous import STOP_LIMIT, record_stop_point, stop_path

    text = "start\n" + "y" * (STOP_LIMIT + 10)
    record_stop_point(tmp_path, 3, SimpleNamespace(full_message=text, last_message=""), tmp_path / "log", 0)
    saved = cuts.issue_dir(tmp_path, 3) / "stop-full.txt"
    kept = stop_path(tmp_path, 3).read_text()
    assert kept.startswith(f"[cut: {len(text) - STOP_LIMIT} more chars; full text: {saved}]")
    assert saved.read_text() == text


def test_head_keeps_the_limit_when_no_line_end_is_near(tmp_path):
    text = "short\n" + "z" * 1000
    kept = cuts.head(text, 500, tmp_path, "long-line.txt")
    assert kept.startswith("short\n" + "z" * 494) and f"[cut: {len(text) - 500} more chars;" in kept


def test_parent_context_does_not_overwrite_an_approval_made_while_reading(tmp_path):
    from types import SimpleNamespace
    from foragent.run_parent import parent_context
    from foragent.state import StateStore

    store = StateStore(tmp_path / "state")
    store.save_approval(2, body="old", body_sha="a", acceptance_blobs={}, approved_by="h", approved_at="t1")
    first = store.load_approval(2)
    reads = []

    class Tracker:
        def parent_issue(self, n):
            reads.append(n)
            if len(reads) == 1:  # a re-approval lands while the parent is read
                store.save_approval(2, body="new", body_sha="b", acceptance_blobs={}, approved_by="h", approved_at="t2")
            return SimpleNamespace(body="Shared.\n<!-- foragent:plan id=x map={} acceptance={} -->")

    result = parent_context(SimpleNamespace(tracker=Tracker(), store=store), 2, first)
    assert (result.body, result.parent_context) == ("new", "Shared.")
    assert store.load_approval(2).body == "new" and len(reads) == 2


def test_saved_whole_failure_stays_whole_after_a_restart(tmp_path):
    from foragent.state import StateStore

    store = StateStore(tmp_path / "state")
    store.start_run(1, branch="foragent/issue-1", worktree=tmp_path / "wt")
    store.update_run(1, resume_reason="Command: a\n...", resume_whole=True)
    store.finish_run(1, outcome="limit-wait")
    assert store.start_run(1, branch="foragent/issue-1", worktree=tmp_path / "wt").resume_whole is True


def test_moved_between_names_every_part_of_a_three_way_split(tmp_path):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}

    def git(*args, stdin=None):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True,
                              text=True, input=stdin, env=env).stdout.strip()

    def commit(files, parent):
        entries = "".join(f"100644 blob {git('hash-object', '-w', '--stdin', stdin=body)}\t{name}\n"
                          for name, body in files.items())
        return git("commit-tree", git("mktree", stdin=entries), "-m", "c", *(["-p", parent] if parent else []))

    git("init", "-q")
    parts = ["".join(f"def f{n}():\n    return {n} * {n}\n\n" for n in range(k * 20, k * 20 + 20)) for k in range(3)]
    base = commit({"big.py": "".join(parts), "other.py": "x = 1\n"}, None)
    # main divides big.py into three equal modules and deletes it; Git's whole-file detection sees ~33% each.
    main = commit({"part_a.py": parts[0], "part_b.py": parts[1], "part_c.py": parts[2], "other.py": "x = 2\n"}, base)
    moved = GitRepo(tmp_path).moved_between(base, main, ["big.py", "other.py"])
    assert sorted(moved) == [("big.py", "part_a.py"), ("big.py", "part_b.py"), ("big.py", "part_c.py")]


def test_native_claude_gets_the_lane_memory_cut_notice(tmp_path):
    from foragent.config import Config
    from foragent.run_implementation import implementer_setup

    (tmp_path / ".claude/agents").mkdir(parents=True)
    (tmp_path / ".claude/agents/fa-x.md").write_text("---\nname: fa-x\n---\nNotes.\n")
    memory = tmp_path / ".claude/agent-memory/fa-x/MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("".join(f"line {n}\n" for n in range(230)))
    config = Config(executor="claude", lanes={"x": {"agent": "fa-x"}})
    setup = implementer_setup(config, "x", "m", tmp_path, tmp_path / "full")
    assert setup.options["agent"] == "fa-x"
    assert f"[cut: 30 more lines; full text: {tmp_path / 'full' / 'lane-memory.md'}]" in setup.memory_notice
    assert setup.notes == ""  # Claude reads the agent itself; the notice follows the task text
    assert (tmp_path / "full" / "lane-memory.md").read_text() == memory.read_text()


def test_partial_verification_never_deletes_failures_it_did_not_rerun(tmp_path):
    """#420: only a complete verification replaces the saved bundle; partial ones update what they ran."""
    from foragent.run_checks import CheckResult, save_failures

    failing = [CheckResult("a", 1, False, "a broke"), CheckResult("b", 1, False, "b broke")]
    save_failures(tmp_path, 1, failing)
    path = cuts.failures_path(tmp_path, 1)
    # A verification that could not launch anything leaves the bundle as it was.
    save_failures(tmp_path, 1, [], complete=False)
    assert "a broke" in path.read_text() and "b broke" in path.read_text()
    # A partial run where a now passes and b was never reached keeps b only.
    save_failures(tmp_path, 1, [CheckResult("a", 0, False, "")], complete=False)
    assert "a broke" not in path.read_text() and "b broke" in path.read_text()
    # A partial run that times out on b replaces b's entry with the new output.
    save_failures(tmp_path, 1, [CheckResult("b", None, True, "b slow")], complete=False)
    assert "b slow" in path.read_text() and "b broke" not in path.read_text()
    # A complete verification that passes clears everything.
    save_failures(tmp_path, 1, [CheckResult("a", 0, False, ""), CheckResult("b", 0, False, "")])
    assert not path.exists()


def test_duplicate_command_invocations_are_kept_apart(tmp_path):
    """#420: a passing duplicate never erases another invocation's failure."""
    from foragent.run_checks import CheckResult, save_failures

    checks = [CheckResult("make check", 1, False, "first run broke"), CheckResult("make check", 0, False, "")]
    bundle = save_failures(tmp_path, 1, checks)
    assert "first run broke" in bundle
    assert "first run broke" in cuts.failures_path(tmp_path, 1).read_text()
    # A partial update where only the first invocation runs again and passes clears it, nothing else.
    save_failures(tmp_path, 1, [CheckResult("other", 1, False, "other broke"),
                                CheckResult("make check", 1, False, "second run broke")])
    save_failures(tmp_path, 1, [CheckResult("other", 0, False, "")], complete=False)
    assert "second run broke" in cuts.failures_path(tmp_path, 1).read_text()


def test_save_full_replaces_atomically(tmp_path, monkeypatch):
    """#420: the old copy stays whole until the new one is fully written."""
    import os

    path = cuts.save_full(tmp_path, "bundle.txt", "old")

    def broken_replace(source, target):
        raise OSError("interrupted")

    monkeypatch.setattr(os, "replace", broken_replace)
    try:
        cuts.save_full(tmp_path, "bundle.txt", "new")
    except OSError:
        pass
    assert path.read_text() == "old"
