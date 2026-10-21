"""Acceptance test for conflict-budget (D51). Locked by Foragent; do not edit."""


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


def test_conflict_retries_default():
    from foragent.config import Config

    assert Config().conflict_retries == 2


def test_conflict_uses_its_own_budget_after_review_rounds(git_history, tmp_path, monkeypatch):
    """#41: review rounds used max_retries, then the base moved and conflicted."""
    from foragent.runner import run_issue

    case = make_case(git_history, tmp_path, monkeypatch, max_retries=1)

    def first(kwargs):
        (kwargs["worktree"] / "feature.txt").write_text("mine\n")
        case.history.update({"feature.txt": "theirs\n"})
        return "Implemented"

    def again(kwargs):
        (kwargs["worktree"] / "feature.txt").write_text("mine\n")
        return "Fixed review"

    def resolve(kwargs):
        assert "<<<<<<<" in (kwargs["worktree"] / "feature.txt").read_text()
        (kwargs["worktree"] / "feature.txt").write_text("theirs\nmine\n")
        return "Resolved"

    case.implementer.steps = [first, again, resolve]
    case.reviewer.steps = ["VERDICT: FAIL\nfeature.txt:1 wording", "VERDICT: PASS", "VERDICT: PASS", "VERDICT: PASS"]
    assert run_issue(1, case.ctx) == "merged"
    assert len(case.implementer.calls) == 3
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human"}


def test_unresolved_conflict_rebuilds_on_the_latest_base(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue

    case = make_case(git_history, tmp_path, monkeypatch, conflict_retries=1)
    seen = []

    def first(kwargs):
        (kwargs["worktree"] / "feature.txt").write_text("mine\n")
        case.history.update({"feature.txt": "theirs\n"})
        return "Implemented"

    def give_up(kwargs):
        return "Could not resolve"

    def rebuild(kwargs):
        text = (kwargs["worktree"] / "feature.txt").read_text()
        seen.append(text)
        (kwargs["worktree"] / "feature.txt").write_text(text + "mine\n")
        return "Re-implemented on the latest base"

    case.implementer.steps = [first, give_up, rebuild]
    case.reviewer.steps = ["VERDICT: PASS", "VERDICT: PASS", "VERDICT: PASS"]
    assert run_issue(1, case.ctx) == "merged"
    assert seen == ["theirs\n"]
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human"}
