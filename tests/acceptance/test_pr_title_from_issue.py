"""Acceptance test for pr-title-from-issue. Locked by Foragent; do not edit."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile

import pytest

from conftest import git_command
from foragent import orch, runner
from foragent.tracker import GhTracker
from test_local_truth import record_gh
from test_orch_review_handoff import make_case


BRANCH = "foragent/issue-1"
ISSUE_TITLE = "사람용 events: resume & conflict (#108)"
TITLE = f"{ISSUE_TITLE} (#1)"
WIP = "WIP: Previous event wording (#1) [timeout]\n\nKeep this checkpoint for resume."


def _import_tree(root, ref, parent, tree, message):
    """Import the real tree and message without commit/push/reset/checkout/stash."""
    stream = [
        f"commit {ref}\n",
        "committer Offline Tests <tests@example.com> 1792483200 +0000\n",
        f"data {len(message.encode('utf-8'))}\n{message}\n",
        f"from {parent}\n",
        "deleteall\n",
    ]
    for entry in filter(None, git_command(root, "ls-tree", "-r", "-z", tree).split("\0")):
        metadata, name = entry.split("\t", 1)
        mode, kind, sha = metadata.split()
        assert kind == "blob"
        stream.append(f"M {mode} {sha} {json.dumps(name)}\n")
    git_command(root, "fast-import", "--quiet", input="".join(stream) + "\ndone\n")
    return git_command(root, "rev-parse", ref)


class RecordedPRs:
    """Fake remote state, recording writes through the real GitHub merge adapter.

    The squash subject must be explicit at the gh boundary: repository defaults
    and single-commit PRs can otherwise select a branch commit's subject.
    No GitHub subprocess is started, and no proposed tracker API is assumed.
    """

    def __init__(self, case, monkeypatch):
        self.case = case
        self.calls = []
        self.commands = []
        github = GhTracker(case.ctx.repo, self.command)
        create, edit, merge = case.tracker.create_pr, case.tracker.update_pr, case.tracker.merge_pr

        def create_pr(**kwargs):
            pr = create(**kwargs)
            self.calls.append(("create", pr, kwargs.copy()))
            return pr

        def update_pr(pr, **kwargs):
            self.calls.append(("edit", pr, kwargs.copy()))
            return edit(pr, **kwargs)

        def merge_pr(pr, **kwargs):
            self.calls.append(("merge", pr, {**kwargs, "title": case.tracker.get_issue(pr).title}))
            github.merge_pr(pr, **kwargs)
            return merge(pr, **kwargs)

        monkeypatch.setattr(case.tracker, "create_pr", create_pr)
        monkeypatch.setattr(case.tracker, "update_pr", update_pr)
        monkeypatch.setattr(case.tracker, "merge_pr", merge_pr)

    def command(self, args, input=None):
        self.commands.append((list(args), input))
        if args[:2] == ["pr", "merge"]:
            return ""
        # A fix may read the live PR title in GhTracker before merging it.
        if args[:2] == ["pr", "view"] or args[:1] == ["api"]:
            pr = self.case.tracker.created_prs[-1]
            return json.dumps({
                "number": pr.number, "title": pr.title, "body": pr.body,
                "state": "open", "labels": [],
                "head": {"ref": pr.head, "sha": self.case.tracker.pr_head_sha(pr.number)},
                "base": {"ref": pr.base},
            })
        pytest.fail(f"Unexpected offline GitHub command: {args!r}")

    def assert_title_before_merge(self, pr):
        writes = [call for call in self.calls if call[1] == pr]
        assert writes[-1][0] == "merge", writes
        assert writes[-1][2]["title"] == TITLE, (
            f"PR #{pr} reached merge with a branch/WIP title: {writes!r}"
        )
        assert any(action in {"create", "edit"} and data["title"] == TITLE
                   for action, _, data in writes[:-1]), (
            f"Create or retitle PR #{pr} from the issue before merging: {writes!r}"
        )

    def assert_squash_subject(self, head):
        commands = [args for args, _ in self.commands if args[:2] == ["pr", "merge"]]
        assert len(commands) == 1, self.commands
        command = commands[0]
        assert "--squash" in command, command
        assert command[command.index("--match-head-commit") + 1] == head
        subject = next((word.split("=", 1)[1] for word in command if word.startswith("--subject=")), None)
        if "--subject" in command:
            subject = command[command.index("--subject") + 1]
        assert subject == TITLE, (
            f"Squash must explicitly use the issue-derived PR title {TITLE!r}; "
            f"GitHub adds its own PR-number suffix. Requested subject={subject!r}, command={command!r}"
        )


def _case(git_history, tmp_path, monkeypatch):
    record_gh(monkeypatch)
    case = make_case(git_history, tmp_path, monkeypatch,
                     reviewer="none", test_command="true", skill_libraries=[], conflict_retries=1)
    case.tracker.issues[1] = replace(case.tracker.get_issue(1), title=ISSUE_TITLE)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    def no_model(*args, **kwargs):
        pytest.fail("PR title acceptance tests must not start model turns")

    from foragent.executors import ClaudeExecutor, CodexExecutor

    monkeypatch.setattr(ClaudeExecutor, "run", no_model)
    monkeypatch.setattr(CodexExecutor, "run", no_model)
    git_run = case.git._run

    def preserve_messages(args):
        root, command = case.root, list(args)
        while command[:1] == ["-C"]:
            root, command = Path(command[1]), command[2:]
        if "commit" not in command:
            return git_run(args)
        message = command[command.index("-m") + 1]
        _import_tree(root, git_command(root, "symbolic-ref", "HEAD"),
                     git_command(root, "rev-parse", "HEAD"), git_command(root, "write-tree"), message)
        return ""

    # make_case normally substitutes "Test"; retain requested WIP messages so
    # the regression cannot disappear inside the fixture's Git transport.
    monkeypatch.setattr(case.git, "_run", preserve_messages)
    # The scripted producer supplies the new summary value on every turn, including rebuilds.
    execute = case.implementer.run
    def with_summary(**kwargs):
        result = execute(**kwargs)
        producer = kwargs["worktree"] / ".foragent/PR.json"
        producer.parent.mkdir(exist_ok=True)
        producer.write_text(json.dumps({"summary": "The selected event wording is now applied."}))
        return result
    monkeypatch.setattr(case.implementer, "run", with_summary)
    case.prs = RecordedPRs(case, monkeypatch)
    return case


def _seed_wip(case):
    parent = git_command(case.history.origin, "rev-parse", "main")
    # Add a real partial implementation to the imported branch tree.
    case.history.update({"feature.txt": "unfinished\n"})
    wip = _import_tree(case.history.origin, f"refs/heads/{BRANCH}", parent, case.history.tip, WIP)
    git_command(case.history.origin, "update-ref", "refs/heads/main", parent)
    case.history.tip = parent
    case.git.fetch()
    git_command(case.root, "update-ref", f"refs/heads/{BRANCH}", wip)
    return wip


def _assert_wip_preserved(case, sha, message):
    assert git_command(case.root, "log", "-1", "--format=%B", sha) == message
    assert case.git.is_ancestor(sha, git_command(case.root, "rev-parse", BRANCH)), (
        "Retitling a PR must retain the branch's original WIP checkpoint"
    )
    assert git_command(case.history.origin, "log", "-1", "--format=%B", sha) == message


@pytest.mark.parametrize("history", ["wip-first", "existing-pr", "resumed-run", "conflict-rebuild"])
def test_issue_title_controls_pr_and_squash_across_branch_histories(
    git_history, tmp_path, monkeypatch, history,
):
    case = _case(git_history, tmp_path, monkeypatch)
    wip = None
    if history in {"wip-first", "existing-pr"}:
        wip, message = _seed_wip(case), WIP
        if history == "existing-pr":
            case.tracker.create_pr(head=BRANCH, base="main", title=WIP.splitlines()[0], body="Closes #1")
    elif history == "resumed-run":
        def stop(kwargs):
            (kwargs["worktree"] / "feature.txt").write_text("unfinished\n")
            question = kwargs["worktree"] / ".foragent/QUESTION.md"
            question.parent.mkdir(exist_ok=True)
            question.write_text("Which wording?\nA: Short (recommended)\nB: Long\n")

        case.implementer.steps = [stop]
        assert runner.run_issue(1, case.ctx) == "spec-question"
        wip = git_command(case.root, "rev-parse", BRANCH)
        message = git_command(case.root, "log", "-1", "--format=%B", wip)
        assert message.startswith("WIP:") and "[spec-question]" in message
        assert not case.tracker.created_prs
        orch.retry(case.ctx, 1, hint="Continue the saved checkpoint.")
        case.store.start_run(1, branch=BRANCH, worktree=case.store.worktree_path(1))
        case.implementer.steps = [case.implement]
    else:
        rebuilt = []

        def first(kwargs):
            (kwargs["worktree"] / "feature.txt").write_text("mine\n")
            case.history.update({"feature.txt": "theirs\n"})

        def unresolved(kwargs):
            assert "<<<<<<<" in (kwargs["worktree"] / "feature.txt").read_text()

        def rebuild(kwargs):
            text = (kwargs["worktree"] / "feature.txt").read_text()
            rebuilt.append(text)
            (kwargs["worktree"] / "feature.txt").write_text(text + "mine\n")

        case.implementer.steps = [first, unresolved, rebuild]

    case.tracker.set_pr_checks(2, "pass")
    assert runner.run_issue(1, case.ctx) == "merged"
    assert case.tracker.merged_prs == [2] and len(case.tracker.created_prs) == 1
    if wip is not None:
        _assert_wip_preserved(case, wip, message)
    if history == "conflict-rebuild":
        assert rebuilt == ["theirs\n"] and len(case.implementer.calls) == 3
    case.prs.assert_title_before_merge(2)
    case.prs.assert_squash_subject(case.tracker.pr_head_sha(2))


@pytest.mark.parametrize("entrypoint", ["review-only", "orch-merge"])
def test_existing_wip_pr_is_retitled_before_merge_without_changing_checkpoint(
    git_history, tmp_path, monkeypatch, entrypoint,
):
    case = _case(git_history, tmp_path, monkeypatch)
    head = _seed_wip(case)
    pr = case.tracker.create_pr(head=BRANCH, base="main", title=WIP.splitlines()[0], body="Closes #1")
    case.tracker.set_pr_head_sha(pr, head)
    case.tracker.set_pr_checks(pr, "pass")
    case.store.finish_run(1, outcome="pr-open")
    case.tracker.remove_labels(1, ["running"])

    # This fixture represents an already published gate PR: preserve its composition values,
    # not the old rendered body, so the title test still requires zero implementer turns.
    case.store._write_json(case.store.root / "pr-content/1.json", {
        "summary": "The selected event wording is now applied.", "checks": [], "reviews": [],
        "attempt": 1, "try_text": "Existing instructions.",
    })
    if entrypoint == "review-only":
        assert runner.review_only(1, case.ctx, verdict_head=head, repair_ci=False) == "merged"
    else:
        assert "merged" in orch.merge(case.ctx, pr, approved_by="human")

    assert case.tracker.merged_prs == [pr]
    assert not case.implementer.calls and not case.reviewer.calls
    _assert_wip_preserved(case, head, WIP)
    case.prs.assert_title_before_merge(pr)
    case.prs.assert_squash_subject(head)
