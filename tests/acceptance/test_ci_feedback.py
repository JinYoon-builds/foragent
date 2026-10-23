"""Acceptance test for ci-feedback (D50). Locked by Foragent; do not edit."""

import re


def make_case(git_history, tmp_path, monkeypatch, **config_values):
    """Offline runner/CLI dependencies, shared by these acceptance files.

    Import objects through a Git runner instead of invoking commit or push.
    All subprocess Git operations are confined to the temporary history fixture.
    """
    from datetime import timedelta
    from pathlib import Path
    from types import SimpleNamespace

    from conftest import ACCEPTANCE, BASE_TIME, git_command
    from foragent.config import Config
    from foragent.executors import ExecResult
    from foragent.git import GitRepo
    from foragent.issue import body_hash
    from foragent.runner import RunContext
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker

    body = (
        "<!-- foragent:v1 -->\n## Goal\nImplement the feature.\n"
        "## Out of scope\nOther features.\n## Done when\n"
        "```sh\ntest -f feature.txt\n```\n"
        f"```sh\ntest -f {ACCEPTANCE}\n```\n"
    )
    clock = [BASE_TIME + timedelta(hours=1)]
    tracker = FakeTracker(now=lambda: clock[0], strict=True)
    tracker.add_issue(1, "Implement feature", body, labels=["approved", "running"])
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    snapshots = []

    def git_run(args):
        root = git_history.clone
        if args[:1] == ["-C"]:
            root, args = Path(args[1]), args[2:]
        if "commit" in args:
            # Fast-import accepts the staged tree without running git commit.
            parent = git_command(root, "rev-parse", "HEAD")
            branch = git_command(root, "symbolic-ref", "HEAD")
            tree = git_command(root, "ls-files", "--stage", "-z")
            import json

            stream = [f"commit {branch}\n", "committer Tests <test@example.com> 1792483200 +0000\n",
                      "data 4\nTest\n", f"from {parent}\n", "deleteall\n"]
            for entry in filter(None, tree.split("\0")):
                metadata, name = entry.split("\t", 1)
                mode, sha, stage = metadata.split()
                assert stage == "0"
                stream.append(f"M {mode} {sha} {json.dumps(name)}\n")
            git_command(root, "fast-import", "--quiet", input="".join(stream) + "\ndone\n")
            snapshots.append(git_command(root, "ls-tree", "-r", "--name-only", "HEAD").splitlines())
            return ""
        if args[:1] == ["push"]:
            branch = args[-1]
            if ":" in branch:
                source, destination = branch.split(":", 1)
            else:
                source = destination = f"refs/heads/{branch}"
            git_command(git_history.origin, "fetch", str(root), f"+{source}:{destination}")
            if ":" not in branch:
                pr = tracker.find_pr(branch) or max(tracker.issues) + 1
                tracker.set_pr_head_sha(pr, git_command(root, "rev-parse", source))
            return ""
        assert not {"reset", "checkout", "stash"}.intersection(args), args
        return git_command(root, *args)

    class Executor:
        def __init__(self, steps):
            self.steps, self.calls = list(steps), []

        def run(self, **kwargs):
            self.calls.append(kwargs)
            assert self.steps, "Unexpected executor invocation"
            step = self.steps.pop(0)
            message = step(kwargs) if callable(step) else step
            return ExecResult(0, False, message or "", kwargs["log_path"])

    def implement(kwargs):
        (kwargs["worktree"] / "feature.txt").write_text("done\n")
        return "Implemented"

    implementer = Executor([implement])
    reviewer = Executor(["VERDICT: PASS"])
    config = Config(**{"review": "fast", "reviewer": "codex", "executor": "claude", "approvers": ["human"],
                       "max_retries": 3, "review_max": 3, "repo": "owner/repo", **config_values})
    git = GitRepo(git_history.clone, git_run)
    store.save_approval(1, body=body, body_sha=body_hash(body),
                        acceptance_blobs=git.blob_shas("origin/main", [ACCEPTANCE]),
                        approved_by="human", approved_at=clock[0].isoformat())
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    ctx = RunContext("owner/repo", git_history.clone, tracker, store, config, git,
                     now=lambda: clock[0],
                     executor_factory=lambda name: implementer if name == config.executor else reviewer,
                     sleep=lambda seconds: clock.__setitem__(0, clock[0] + timedelta(seconds=seconds)),
                     monotonic=lambda: clock[0].timestamp())
    return SimpleNamespace(ctx=ctx, root=git_history.clone, history=git_history,
                           tracker=tracker, store=store, clock=clock, config=config, git=git,
                           implementer=implementer, reviewer=reviewer, implement=implement,
                           snapshots=snapshots)


LOG = "FAILED tests/test_cli.py::test_init_kwargs - AssertionError: assert 'lane' == 'lanes'"


def test_prompt_names_the_full_test_command():
    from foragent.issue import parse_body
    from foragent.runner import build_prompt

    body = ("<!-- foragent:v1 -->\n## Goal\nDo it.\n## Out of scope\nNothing.\n## Done when\n"
            "```sh\ntrue\n```\n")
    prompt = build_prompt(1, parse_body(body), "tests/acceptance/", test_command="uv run pytest -q")
    # #272: CI runs the full suite; the worker is told so, not invited to run it.
    flat = " ".join(prompt.split())
    assert "`uv run pytest -q`" in flat
    assert re.search(r"\bCI\b[^.]{0,120}`uv run pytest -q`|`uv run pytest -q`[^.]{0,120}\bCI\b", flat), (
        "Name the full test command as what CI runs")
    assert "Before you finish, also run the full test suite" not in flat
    without = " ".join(build_prompt(1, parse_body(body), "tests/acceptance/").split())
    assert "Before you finish, also run the full test suite" not in without


def test_runner_passes_configured_test_command(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue

    case = make_case(git_history, tmp_path, monkeypatch, test_command="uv run pytest -q")
    assert run_issue(1, case.ctx) == "merged"
    assert "`uv run pytest -q`" in case.implementer.calls[0]["prompt"]


def test_ci_failure_goes_back_to_the_implementer_with_the_log(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue

    case = make_case(git_history, tmp_path, monkeypatch)
    case.tracker.set_pr_checks(2, "fail")
    case.tracker.set_pr_failed_logs(2, "setup ok\n" + LOG + "\n")

    def fix(kwargs):
        (kwargs["worktree"] / "feature.txt").write_text("done, CI fixed\n")
        case.tracker.set_pr_checks(2, "pass")
        return "Fixed CI"

    case.implementer.steps = [case.implement, fix]
    case.reviewer.steps = ["VERDICT: PASS", "VERDICT: PASS"]
    assert run_issue(1, case.ctx) == "merged"
    assert len(case.implementer.calls) == 2
    assert LOG in case.implementer.calls[1]["prompt"]
    assert case.store.get_run(1).attempts == 2
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human"}


def test_ci_failure_blocks_only_after_the_budget(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue

    case = make_case(git_history, tmp_path, monkeypatch, max_retries=1)
    case.tracker.set_pr_checks(2, "fail")
    case.tracker.set_pr_failed_logs(2, LOG + "\n")
    case.implementer.steps = [case.implement, case.implement]
    case.reviewer.steps = ["VERDICT: PASS", "VERDICT: PASS"]
    assert run_issue(1, case.ctx) == "ci-failed"
    assert len(case.implementer.calls) == 2
    labels = case.tracker.get_issue(1).labels
    assert "blocked" in labels and "needs-human" not in labels
    assert any(LOG in comment.body for comment in case.tracker.comments[1])
