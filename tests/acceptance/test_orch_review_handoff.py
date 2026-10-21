"""Acceptance test for orch-review-handoff. Locked by Foragent; do not edit."""


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


def bind_cli(case, monkeypatch):
    """Inject public CLI dependencies; no account, launchd or real executor."""
    from datetime import datetime
    import yaml
    from foragent import cli
    from foragent.state import StateStore

    (case.root / ".foragent.yml").write_text(yaml.safe_dump({"repo": "owner/repo"}))
    monkeypatch.chdir(case.root)
    monkeypatch.setattr(cli, "load_config", lambda root: case.ctx.config)
    monkeypatch.setattr(cli, "GhTracker", lambda repo: case.tracker)
    monkeypatch.setattr(cli, "GitRepo", lambda root: case.git)
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kw: case.store))
    from foragent.launchd import LaunchdStatus
    monkeypatch.setattr(cli.launchd, "status", lambda *a, **kw: LaunchdStatus("offline", False, False))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return case.clock[0] if tz else case.clock[0].replace(tzinfo=None)

    monkeypatch.setattr(cli, "datetime", Clock)
    return cli.main


def tick_context(case, **overrides):
    from foragent.scheduler import TickContext

    return TickContext(repo="owner/repo", repo_root=case.root, tracker=case.tracker,
                       store=case.store, config=case.ctx.config, git=case.git,
                       now=lambda: case.clock[0], launcher=lambda n: 7000 + n,
                       runner_process_exists=lambda n: True, kill_runner=lambda pid: None,
                       **overrides)


def test_unusable_review_goes_to_next_implementer_without_a_hold(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue

    case = make_case(git_history, tmp_path, monkeypatch)
    raw = "The retry path loses the user's selected locale.\nSee feature.txt:1."
    case.reviewer.steps = [raw, "VERDICT: PASS"]
    case.implementer.steps = [case.implement, case.implement]
    assert run_issue(1, case.ctx) == "merged"
    assert len(case.implementer.calls) == 2
    prompt = case.implementer.calls[1]["prompt"]
    assert "A reviewer said (verbatim):\n" + raw in prompt
    assert "For each reviewer point either fix it or write `.foragent/REBUTTAL.md`" in prompt
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human"}
    assert case.store.get_run(1).attempts == 2


def test_rebuttal_reaches_pr_and_next_review_without_entering_git(git_history, tmp_path, monkeypatch):
    import json

    from foragent.runner import run_issue

    case = make_case(git_history, tmp_path, monkeypatch, executor="codex", reviewer="claude")
    rebuttal = "- feature.txt:1 add payment support: payments are explicitly out of scope\n"
    seen = []

    def respond(kwargs):
        case.implement(kwargs)
        path = kwargs["worktree"] / ".foragent/REBUTTAL.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(rebuttal)

    def review(kwargs):
        seen.append((kwargs["worktree"] / "CLAUDE.local.md").read_text())
        return json.dumps({"result": "(none)"})

    case.implementer.steps = [case.implement, respond]
    case.reviewer.steps = [json.dumps({"result": "feature.txt:1 — add payment support"}), review]
    assert run_issue(1, case.ctx) == "merged"
    assert "Implementer rebuttals (judge them):" in seen[0]
    assert rebuttal.strip() in seen[0]
    assert "## Rebuttals" in case.tracker.created_prs[0].body
    assert rebuttal.strip() in case.tracker.created_prs[0].body
    assert case.snapshots
    assert all(".foragent/REBUTTAL.md" not in files for files in case.snapshots)


def test_repeated_normalized_finding_after_rebuttal_blocks_dispute(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue

    case = make_case(git_history, tmp_path, monkeypatch)

    def rebut(kwargs):
        case.implement(kwargs)
        path = kwargs["worktree"] / ".foragent/REBUTTAL.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text("- feature.txt:1 payment support: outside the approved goal\n")

    case.implementer.steps = [case.implement, rebut, rebut, rebut]
    case.reviewer.steps = ["VERDICT: FAIL\nfeature.txt:1 payment support",
                           "VERDICT: FAIL\nfeature.txt:1   payment support\n",
                           "VERDICT: FAIL\nfeature.txt:1 payment support"]
    run_issue(1, case.ctx)
    assert "blocked" in case.tracker.get_issue(1).labels
    assert "needs-human" not in case.tracker.get_issue(1).labels
    assert any(c.body.startswith("<!-- foragent:blocked reason=review-dispute -->")
               for c in case.tracker.list_comments(1))
    assert case.tracker.merged_prs == []
