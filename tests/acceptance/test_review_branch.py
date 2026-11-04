"""`fa review <branch|worktree> [--base main]`: the #1651 review engine on any branch, read-only.

Work built outside the lanes never reaches the run's review. This command runs the same
engine on any branch or worktree: every configured perspective (fatal-path among them), then
one scoring session per FAIL finding, and prints the verdict and each grounded finding.
It needs no issue, no PR and no run state, and it changes nothing: no Git refs, worktrees
or files, no state store files, no GitHub call.

Exit code: 0 for PASS, 1 for FAIL, anything else for an error (an error is not a verdict).

Agent judgments are scripted data at the public executor boundary (ClaudeExecutor.run and
CodexExecutor.run), as the other review locks do. A scoring session is recognised by its
public request (#1651): the prompt starts with one JSON object that carries "finding".

Break-it: a review that skips perspectives or runs one twice, sends main's own later changes
as the branch's diff, reviews main's tree instead of the branch's, skips scoring or scores
twice, ignores the fatal-path lens, writes refs, worktrees, state or GitHub, or prints a
traceback for an unknown branch fails here.
"""

from importlib import import_module
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

from foragent import cli
from foragent.executors import ClaudeExecutor, CodexExecutor, ExecResult

BRANCH_LINE = "BRANCH-ONLY: retry the upload after a timeout"
MAIN_LINE = "MAIN-ONLY: a later base change the branch never saw"
FINDING = ("src/upload.py:3: A timeout retries the upload without a limit, so one dead server "
           "stalls the caller forever.")
FAIL = f"VERDICT: FAIL\n{FINDING}"
PROJECT = {"name": "observability", "focus": "FOCUS-observability: lost diagnostic context"}

UNGROUNDED = {"score": 90, "critical_path": False, "exists": False, "goal_line": None,
              "locked_test": None, "breaking_input": None, "spec_question": None}
GROUNDED = {**UNGROUNDED, "breaking_input": "An upload to a server that never answers"}

FAKE_GH = r'''
import json, os, sys
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
sys.exit(1)
'''


def configured():
    """Every default perspective overridden with a known focus, plus one project addition."""
    defaults = import_module("foragent.review_perspectives").DEFAULT_PERSPECTIVES
    items = [{"name": item["name"], "focus": f"FOCUS-{item['name']}: project focus"} for item in defaults]
    return [*items, PROJECT]


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


class Script:
    """Every reviewer session answers `review`; every scoring session answers `score`."""

    def __init__(self, review=FAIL, score=GROUNDED):
        self.review, self.score = review, score
        self.reviews, self.scorings = [], []

    def run(self, **kwargs):
        prompt = kwargs["prompt"]
        try:
            request = json.JSONDecoder().raw_decode(prompt)[0]
        except ValueError:
            request = None
        if isinstance(request, dict) and isinstance(request.get("finding"), str):
            self.scorings.append(request)
            text = json.dumps(self.score)
        else:
            worktree = Path(kwargs["worktree"])
            seen = (worktree / "src" / "upload.py").read_text() if (worktree / "src" / "upload.py").exists() else ""
            self.reviews.append({"prompt": prompt, "tree": seen})
            text = self.review
        if kwargs.get("review_level") is not None:
            text = json.dumps({"result": text})
        return ExecResult(0, False, text, kwargs["log_path"], text)


def commit(origin, ref, files, *, parent=None, message="Update"):
    """Import one commit onto ``ref`` in the bare origin (the test guard forbids git commit)."""
    stream = [f"commit {ref}\n", "committer Test Human <human@example.com> 1792483200 +0000\n",
              f"data {len(message.encode())}\n{message}\n"]
    if parent:
        stream.append(f"from {parent}\n")
    for path, content in files.items():
        stream.append(f"M 100644 inline {path}\ndata {len(content.encode())}\n{content}\n")
    stream.append("\ndone\n")
    subprocess.run(["git", "fast-import", "--quiet", "--force"], cwd=origin, input="".join(stream),
                   check=True, capture_output=True, text=True)
    return git(origin, "rev-parse", ref).strip()


def build(tmp_path, monkeypatch, reviewer="codex"):
    """A clone on main with a feature branch; main moved on after the branch forked."""
    for key, value in (("GIT_AUTHOR_NAME", "Test Human"), ("GIT_AUTHOR_EMAIL", "human@example.com"),
                       ("GIT_COMMITTER_NAME", "Test Human"), ("GIT_COMMITTER_EMAIL", "human@example.com"),
                       ("GIT_TERMINAL_PROMPT", "0"), ("FORAGENT_UPDATE_CHECK", "off"),
                       ("FORAGENT_ORCH_SPAWN", "off")):
        monkeypatch.setenv(key, value)
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "FORAGENT_ROLE"):
        monkeypatch.delenv(name, raising=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(shutil.which("git"))
    gh = bin_dir / "gh"
    gh.write_text(f"#!{sys.executable}\n{FAKE_GH}", encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh-calls.jsonl"))
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + "/usr/bin" + os.pathsep + "/bin")

    origin, repo = tmp_path / "origin.git", tmp_path / "project"
    git(tmp_path, "init", "-q", "--bare", "--initial-branch=main", str(origin))
    config = yaml.safe_dump({"executor": "claude" if reviewer == "codex" else "codex", "reviewer": reviewer,
                             "language": "en", "review_perspectives": configured()})
    base = commit(origin, "refs/heads/main", {
        "src/upload.py": "def upload(data):\n    return send(data)\n", "README.md": "# Demo\n",
        ".foragent.yml": config}, message="Base")
    commit(origin, "refs/heads/feature", {
        "src/upload.py": f"def upload(data):\n    # {BRANCH_LINE}\n    while True:\n        return send(data)\n"},
        parent=base, message="Retry uploads")
    commit(origin, "refs/heads/main", {"README.md": f"# Demo\n\n{MAIN_LINE}\n"}, parent=base,
           message="Main moves on")
    git(tmp_path, "clone", "-q", str(origin), str(repo))
    git(repo, "branch", "-q", "feature", "origin/feature")
    monkeypatch.chdir(repo)
    return repo


@pytest.fixture
def project(tmp_path, monkeypatch):
    return build(tmp_path, monkeypatch)


def use(monkeypatch, script):
    monkeypatch.setattr(CodexExecutor, "run", script.run)
    monkeypatch.setattr(ClaudeExecutor, "run", script.run)


def fa(argv, capsys):
    capsys.readouterr()
    try:
        code = cli.main(argv)
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def snapshot(repo):
    """Everything a read-only review must leave as it was."""
    files = {}
    for root in (repo, Path.home()):
        for path in sorted(root.rglob("*")):
            if ".git" in path.relative_to(root).parts or not path.is_file() or path.is_symlink():
                continue
            data = path.read_bytes()
            if root == Path.home() and not data:
                continue  # an empty lock file holds no state
            files[str(path)] = data
    return {
        "refs": git(repo, "for-each-ref", "--format=%(refname) %(objectname)"),  # stash included
        "head": git(repo, "symbolic-ref", "-q", "HEAD"),
        "status": git(repo, "status", "--porcelain=v1", "--ignored", "--untracked-files=all"),
        "worktrees": git(repo, "worktree", "list", "--porcelain"),
        "config": git(repo, "config", "--local", "--list"),
        "files": files,
    }


def gh_calls(tmp_path):
    log = tmp_path / "gh-calls.jsonl"
    return log.read_text().splitlines() if log.is_file() else []


def single_focus_prompts(reviews, items):
    """Per perspective, the reviewer prompts that carry its focus and no other configured focus."""
    focuses = [item["focus"] for item in items]
    return {item["name"]: [r for r in reviews
                           if item["focus"] in r["prompt"]
                           and not any(other in r["prompt"] for other in focuses if other != item["focus"])]
            for item in items}


@pytest.mark.parametrize("reviewer", ["codex", "claude"])
@pytest.mark.parametrize("target", ["branch", "worktree"])
def test_review_runs_every_perspective_and_scoring_on_the_branch_and_prints_a_grounded_fail(
        tmp_path, monkeypatch, capsys, reviewer, target):
    project = build(tmp_path, monkeypatch, reviewer)
    where = "feature"
    if target == "worktree":
        where = str(tmp_path / "feature-wt")
        git(project, "worktree", "add", "-q", where, "feature")
    script = Script()
    use(monkeypatch, script)
    before = snapshot(project)

    code, out, err = fa(["review", where, "--base", "main"], capsys)

    assert "Traceback" not in out + err
    assert code == 1, f"A grounded FAIL exits 1; got {code}\n{out}\n{err}"
    assert "FAIL" in out and FINDING in out, "The verdict and the grounded finding are printed"
    items = configured()
    per = single_focus_prompts(script.reviews, items)
    for name, prompts in per.items():
        assert len(prompts) == 1, f"Perspective {name} must get exactly one reviewer session, got {len(prompts)}"
    assert any("fatal-path" == item["name"] for item in items)
    for review in script.reviews:
        assert BRANCH_LINE in review["prompt"], "Every reviewer sees the branch's diff"
        assert MAIN_LINE not in review["prompt"], "Main's own later change is not the branch's diff"
        assert BRANCH_LINE in review["tree"], "The reviewer works on the branch's files, not main's"
    assert len(script.scorings) == 1, "One scoring session per FAIL finding"
    assert FINDING in script.scorings[0]["finding"]
    assert snapshot(project) == before, "fa review changes no ref, worktree, file or state"
    assert gh_calls(tmp_path) == [], "fa review needs no issue and no PR"


@pytest.mark.parametrize("reply,code", [
    (UNGROUNDED, 0),  # a taste finding without a ground is a NOTE: PASS
    (GROUNDED, 1),
    ({**UNGROUNDED, "score": 10, "critical_path": True, "exists": True,
      "breaking_input": "Stop the server mid-upload"}, 1),  # a fatal path blocks however rare
    ({**GROUNDED, "score": 100, "critical_path": True, "exists": False}, 0),  # a fatal path that is not there
])
def test_review_verdict_comes_from_the_scoring_step(project, tmp_path, monkeypatch, capsys, reply, code):
    script = Script(score=reply)
    use(monkeypatch, script)
    before = snapshot(project)

    got, out, err = fa(["review", "feature", "--base", "main"], capsys)

    assert "Traceback" not in out + err
    assert got == code, f"expected exit {code}, got {got}\n{out}\n{err}"
    assert ("PASS" if code == 0 else "FAIL") in out
    if code == 1:
        assert FINDING in out
    assert len(script.scorings) == 1, "The FAIL finding is scored exactly once"
    assert len(script.reviews) >= len(configured())
    assert snapshot(project) == before
    assert gh_calls(tmp_path) == []


def test_review_of_an_unknown_branch_is_one_plain_error_line(project, tmp_path, monkeypatch, capsys):
    script = Script(review="VERDICT: PASS")
    use(monkeypatch, script)
    # Control: the same command on a real branch reaches the reviewers and passes.
    code, out, err = fa(["review", "feature", "--base", "main"], capsys)
    assert code == 0 and "PASS" in out and script.reviews, f"{code}\n{out}\n{err}"
    script.reviews.clear()
    before = snapshot(project)

    code, out, err = fa(["review", "no-such-branch", "--base", "main"], capsys)

    assert code not in (0, 1, None), "An error is neither PASS nor FAIL"
    lines = [line for line in (out + err).splitlines() if line.strip()]
    assert len(lines) == 1, f"One plain error line, got:\n{out}{err}"
    assert "no-such-branch" in lines[0] and "Traceback" not in lines[0]
    assert script.reviews == [] and script.scorings == [], "No reviewer starts for a missing branch"
    assert snapshot(project) == before
    assert gh_calls(tmp_path) == []
