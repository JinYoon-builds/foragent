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
