"""Shared runner test setup, including run_case lifecycle teardown assertions."""

from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import shlex
import sys
from types import SimpleNamespace

import pytest

from foragent.config import Config
from foragent.executors import ExecResult
from foragent.git import GitRepo
from foragent.issue import body_hash
from foragent.runner import RunContext
from foragent.state import StateStore
from foragent.tracker import FakeTracker

from conftest import ACCEPTANCE, BASE_TIME, git_command


COMMAND = 'test "$(cat feature.txt)" = done || { echo "feature not ready"; exit 1; }'


BODY = f"""<!-- foragent:v1 -->
## Goal
Implement the approved feature.
## Out of scope
Other features.
## Done when
```sh
{COMMAND}
```
```sh
test -f {ACCEPTANCE}
```
- [x] Looks right to a human
"""


class ScriptedExecutor:
    def __init__(self):
        self.steps = [{"feature.txt": "done"}]
        self.calls = []
        self.timed_out = False
        self.exit_code = 0

    def run(self, **kwargs):
        self.calls.append(kwargs)
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        for path, content in step.items():
            target = kwargs["worktree"] / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return ExecResult(self.exit_code, self.timed_out, "untrusted model message", kwargs["log_path"])


def import_index(worktree, title):
    """Fake a commit via fast-import, without executing git commit."""
    parent = git_command(worktree, "rev-parse", "HEAD")
    branch = git_command(worktree, "symbolic-ref", "HEAD")
    stream = [
        f"commit {branch}\n", "committer foragent <foragent@localhost> 1792483200 +0000\n",
        f"data {len(title.encode())}\n{title}\n", f"from {parent}\n",
    ]
    merge_head = worktree / git_command(worktree, "rev-parse", "--git-path", "MERGE_HEAD")
    merging = merge_head.exists()
    if merging:
        stream.extend(f"merge {head}\n" for head in merge_head.read_text().splitlines())
    stream.append("deleteall\n")
    index = git_command(worktree, "ls-files", "--stage", "-z")
    for entry in index.split("\0"):
        if entry:
            metadata, path = entry.split("\t", 1)
            mode, sha, stage = metadata.split()
            assert stage == "0"
            stream.append(f"M {mode} {sha} {json.dumps(path)}\n")
    stream.append("\ndone\n")
    git_command(worktree, "fast-import", "--quiet", input="".join(stream))
    if merging:
        git_command(worktree, "merge", "--quit")


@pytest.fixture
def run_case(git_history, tmp_path, monkeypatch):
    now = [BASE_TIME + timedelta(hours=1)]
    tracker = FakeTracker(now=lambda: now[0], strict=True)
    tracker.add_issue(1, "Implement feature", BODY, labels=["approved", "running"])
    executor = ScriptedExecutor()
    store = StateStore(tmp_path / "state", now=lambda: now[0])
    store.save_approval(
        1, body=BODY, body_sha=body_hash(BODY),
        acceptance_blobs=GitRepo(git_history.clone).blob_shas("origin/main", [ACCEPTANCE]),
        approved_by="human", approved_at=now[0].isoformat(),
    )
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    operations = []

    def git_run(args):
        root = git_history.clone
        if args[:1] == ["-C"]:
            root = Path(args[1])
            args = args[2:]
        operations.append((root, args))
        if args[:1] == ["-c"] and "commit" in args:
            import_index(root, args[-1])
            return ""
        if args[:1] == ["push"]:
            # The fake push transfers objects by fetching into the temporary bare origin.
            branch = args[-1]
            if ":" in branch:
                source, destination = branch.split(":", 1)
                sha = git_command(root, "rev-parse", source)
                git_command(git_history.origin, "fetch", str(root), f"{sha}:{destination}")
            else:
                git_command(git_history.origin, "fetch", str(root), f"+refs/heads/{branch}:refs/heads/{branch}")
                pr = tracker.find_pr(branch) or max(tracker.issues) + 1
                tracker.set_pr_head_sha(pr, git_command(root, "rev-parse", "HEAD"))
            return ""
        return subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True, errors="replace",
        ).stdout

    context = RunContext(
        repo="owner/repo", repo_root=git_history.clone, tracker=tracker, store=store,
        config=Config(review="fast", max_retries=1, reviewer="none"), git=GitRepo(git_history.clone, git_run),
        now=lambda: now[0], executor_factory=lambda name: executor,
        sleep=lambda seconds: now.__setitem__(0, now[0] + timedelta(seconds=seconds)),
        monotonic=lambda: (now[0] - BASE_TIME).total_seconds(),
    )
    tracker.set_pr_files(2, ["feature.txt"])
    finished = []
    finish = store.finish_run

    def finish_once(*args, **kwargs):
        finished.append((args, kwargs))
        return finish(*args, **kwargs)

    monkeypatch.setattr(store, "finish_run", finish_once)
    case = SimpleNamespace(
        ctx=context, executor=executor, tracker=tracker, store=store, operations=operations,
        history=git_history, finished=finished, now=now,
    )
    yield case
    history = store.read_history()
    assert len(finished) == len(history) >= 1
    for run in history:
        assert not Path(run.worktree).exists()
        assert run.worktree not in git_command(git_history.clone, "worktree", "list")
        assert store.get_run(run.number).status == "finished"
        current = store.get_run(run.number)
        # GitHub waits defer remote label cleanup until the reset; a first
        # stall leaves it to the next tick, which restarts the issue.
        assert "running" not in tracker.get_issue(run.number).labels or (
            current.outcome == "limit-wait" and current.waiting_until is not None
            and store.github_wait_until(now[0]) is not None
        ) or current.outcome == "stalled-restart"


def set_done_when(case, commands):
    body = "## Done when\n" + "\n".join(f"```sh\n{command}\n```" for command in commands)
    approval = replace(case.store.load_approval(1), body=body, body_sha=body_hash(body))
    case.store.save_approval(**approval.__dict__)


def enable_reviewer(case, messages, edit=None):
    calls = []
    messages = iter(messages)

    def review(**kwargs):
        calls.append(kwargs)
        if edit:
            edit(kwargs["worktree"])
        message = next(messages)
        if isinstance(message, ExecResult):
            return replace(message, log_path=kwargs["log_path"])
        return ExecResult(0, False, message, kwargs["log_path"])

    reviewer = SimpleNamespace(run=review)
    case.ctx.config = replace(case.ctx.config, executor="claude", reviewer="codex", reviewer_model="review-model")
    case.ctx.executor_factory = lambda name: case.executor if name == "claude" else reviewer
    return calls


def seed_batch(case, files):
    branch = "foragent/batch-99"
    stream = [
        f"commit refs/heads/{branch}\n",
        "committer Offline Tests <tests@example.com> 1792483200 +0000\n",
        f"data 7\nSibling\nfrom {case.history.tip}\n",
    ]
    for path, content in files.items():
        stream.append(f"M 100644 inline {path}\ndata {len(content.encode())}\n{content}\n")
    git_command(case.history.origin, "fast-import", "--quiet", input="".join(stream) + "\ndone\n")


def batch_setup(case):
    case.ctx.config = replace(case.ctx.config, review="batch")
    case.tracker.add_issue(99, "The whole feature")
    case.tracker.set_parent(1, 99)
    case.tracker.set_pr_files(100, ["feature.txt"])
    case.tracker.set_pr_checks(100, "pass")


REPORTED_JSON_FINDINGS = [
    {"file": "src/foragent/runner.py", "line": 121, "summary": "JSON 배열 지적이 판정 불가로 처리됨",
     "failure_scenario": '리뷰가 [{"file": "src/file.py"}] 형식이면 수정 대신 보류됨'},
    {"file": "src/foragent/cli.py", "line": 300, "summary": "결과가 누락됨",
     "failure_scenario": 'A quoted "value", a closing bracket ], and a path [name] are retained.'},
]


def enable_native_reviewer(case, messages, edit=None):
    calls = []
    iterator = iter(messages)

    def review(**kwargs):
        calls.append(kwargs)
        assert "feature.txt" in git_command(kwargs["worktree"], "diff", "--cached", "--name-only")
        if edit:
            edit(kwargs["worktree"])
        message = next(iterator)
        if isinstance(message, ExecResult):
            return message
        return ExecResult(0, False, message, kwargs["log_path"])

    case.ctx.config = replace(case.ctx.config, reviewer="claude", review_level="xhigh")
    case.ctx.executor_factory = lambda name: case.executor if name == "codex" else SimpleNamespace(run=review)
    return calls


@pytest.fixture
def review_context(git_history, tmp_path):
    now = BASE_TIME + timedelta(hours=1)
    git = GitRepo(git_history.clone)
    (git.root / "feature.txt").write_text("implementation")
    git.stage_changes()
    store = StateStore(tmp_path / "review-state", now=lambda: now)
    store.save_approval(
        1, body=BODY, body_sha=body_hash(BODY), acceptance_blobs={},
        approved_by="human", approved_at=now.isoformat(),
    )
    store.start_run(1, branch="foragent/issue-1", worktree=git.root)
    return RunContext(
        repo="owner/repo", repo_root=git.root, tracker=FakeTracker(), store=store,
        config=Config(), git=git, now=lambda: now,
        poll_hook=lambda execute, poll: execute(),
    )


def expected_review_rules(acceptance_dir):
    return (
        "Block only on: the Goal is not met, something listed in Out of scope was changed, or a check was weakened.\n"
        f"Tests under {acceptance_dir} that belong to other issues fail on purpose until those issues land; do not report them.\n"
        "Improvements beyond this issue's Goal are not defects.\n"
        f"Tests under {acceptance_dir} that this issue lists are locked acceptance tests. If a finding would "
        "require behaviour a locked acceptance test forbids, or forbid behaviour it requires, it is a spec "
        "conflict, not a defect: report it as one line "
        "`SPEC-CONFLICT: <locked test path:line> <what the test requires>; <what the finding wants>` "
        "instead of a finding.\n"
        "For each finding, say whether it is one instance of a broader defect class "
        "and where else to look in the change."
    )


@pytest.fixture
def skill_case(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    ctx = SimpleNamespace(
        store=SimpleNamespace(get_run=lambda number: SimpleNamespace(lane="ui")),
        config=Config(lanes={"ui": {"skills": ["testing"]}}),
    )
    return ctx, worktree


@pytest.fixture
def conflicting_lanes(run_case, monkeypatch):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_retries=2)
    c.history.update({"cli.py": "commands = ['help']\n"})
    c.executor.steps = [{"cli.py": "commands = ['help', 'issue']\n"}]
    c.tracker.set_pr_files(2, ["cli.py"])
    script = (
        "from cli import commands; from pathlib import Path; "
        "assert 'issue' in commands; "
        "assert not Path('base-feature.txt').exists() or 'base' in commands"
    )
    set_done_when(c, [f"{shlex.quote(sys.executable)} -B -c {shlex.quote(script)}"])
    c.ci_heads = []
    c.target_heads = []

    def move_target():
        c.history.update({"cli.py": "commands = ['help', 'base']\n", "base-feature.txt": "new base feature\n"})
        c.target_heads.append(c.history.tip)

    def ci(pr):
        c.ci_heads.append(c.tracker.pr_head_sha(pr))
        if not c.target_heads:
            move_target()
        head = c.ci_heads[-1]
        return "pending" if head != c.ci_heads[0] and c.ci_heads.count(head) == 1 else "pass"

    c.move_target = move_target
    monkeypatch.setattr(c.tracker, "pr_checks", ci)
    return c


def scripted_results(case, monkeypatch, messages, *, log_only=False):
    execute = case.executor.run
    messages = iter(messages)

    def run(**kwargs):
        result = execute(**kwargs)
        message, error_kind = next(messages)
        result = replace(result, exit_code=1 if error_kind != "normal" else result.exit_code, error_kind=error_kind)
        if log_only:
            kwargs["log_path"].parent.mkdir(parents=True, exist_ok=True)
            with kwargs["log_path"].open("a") as stream:
                stream.write(message + "\n")
            return replace(result, last_message="no result")
        return replace(result, last_message=message)

    monkeypatch.setattr(case.executor, "run", run)
