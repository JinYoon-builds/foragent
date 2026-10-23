"""Acceptance test for keep-unfinished-work. Locked by Foragent; do not edit."""

from datetime import timedelta
import json
from pathlib import Path
import tempfile

import pytest

from conftest import ACCEPTANCE, git_command
from foragent import orch, runner
from test_orch_review_handoff import make_case


BRANCH = "foragent/issue-1"
CHANGES = {
    "existing.txt": "Partially implemented: 한국어\n\n",
    "removed.txt": None,
    "draft/new.txt": "New file, still unfinished.\n",
}


def _tree(root, ref):
    entries = git_command(root, "ls-tree", "-r", "-z", ref)
    result = {}
    for entry in filter(None, entries.split("\0")):
        metadata, name = entry.split("\t", 1)
        mode, kind, sha = metadata.split()
        assert kind == "blob"
        result[name] = (mode, sha)
    return result


def _import_commit(root, branch, parent, tree, message):
    """Offline Git transport: retain the real staged tree AND requested message.

    make_case's transport substitutes the message 'Test'. Import the same tree
    with the runner's message so the stop-reason assertion reads actual history.
    No git commit/push/reset/checkout/stash subprocess is invoked.
    """
    stream = [
        f"commit {branch}\n",
        "committer Offline Tests <tests@example.com> 1792483200 +0000\n",
        f"data {len(message.encode('utf-8'))}\n{message}\n",
        f"from {parent}\n",
        "deleteall\n",
    ]
    stream.extend(f"M {mode} {sha} {json.dumps(name)}\n"
                  for name, (mode, sha) in sorted(_tree(root, tree).items()))
    git_command(root, "fast-import", "--quiet", input="".join(stream) + "\ndone\n")
    return git_command(root, "rev-parse", branch)


def _case(git_history, tmp_path, monkeypatch, **config):
    git_history.update({
        "existing.txt": "Original implementation.\n",
        "removed.txt": "Obsolete implementation.\n",
        "untouched.txt": "Keep this exactly.\n",
    })
    case = make_case(git_history, tmp_path, monkeypatch, **{
        "reviewer": "none", "test_command": "true", "skill_libraries": [], "max_retries": 0, **config,
    })
    case.base = git_history.tip
    # All runner-created temporary files, including post-merge verification,
    # stay inside the test's temporary repository area.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    original_run = case.git._run

    def git_run(args):
        root, command = case.root, list(args)
        while command[:1] == ["-C"]:
            root, command = Path(command[1]), command[2:]
        if "commit" not in command:
            return original_run(args)
        message = command[command.index("-m") + 1]
        _import_commit(root, git_command(root, "symbolic-ref", "HEAD"),
                       git_command(root, "rev-parse", "HEAD"),
                       git_command(root, "write-tree"), message)
        case.snapshots.append(sorted(_tree(root, "HEAD")))
        return ""

    monkeypatch.setattr(case.git, "_run", git_run)
    return case


def _question(worktree):
    path = worktree / ".foragent/QUESTION.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("Which locale?\nA: English\nB: Korean (recommended)\n", encoding="utf-8")


def _write_changes(kwargs, *, question=False, stage=False):
    worktree = kwargs["worktree"]
    for name, content in CHANGES.items():
        path = worktree / name
        if content is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    artifacts = worktree / ".foragent"
    artifacts.mkdir(exist_ok=True)
    (artifacts / "session.log").write_text("Runner-only scratch data.\n", encoding="utf-8")
    if question:
        _question(worktree)
    if stage:
        # Executors can stage runner artifacts too; WIP must exclude them.
        git_command(worktree, "add", "-A")
    return "Partial implementation"


def _expected_tree(case, changes=CHANGES):
    expected = _tree(case.history.origin, case.base)
    for name, content in changes.items():
        if content is None:
            expected.pop(name, None)
        else:
            sha = git_command(case.root, "hash-object", "--stdin", input=content)
            expected[name] = ("100644", sha)
    return expected


def _assert_saved(case, reason):
    head = git_command(case.root, "rev-parse", BRANCH)
    assert head != case.base, f"{reason}: unfinished changes were discarded; no WIP commit on {BRANCH}"
    assert _tree(case.root, head) == _expected_tree(case), "WIP must contain exactly the implementer's changes"
    assert git_command(case.root, "rev-list", "--count", f"{case.base}..{head}") == "1"
    message = git_command(case.root, "log", "-1", "--format=%B", head)
    assert reason in message, f"WIP commit message must name the stop reason {reason!r}: {message!r}"
    assert git_command(case.history.origin, "rev-parse", f"refs/heads/{BRANCH}") == head, (
        "The normally-pushed issue branch must preserve the same WIP on origin"
    )
    assert git_command(case.history.origin, "rev-parse", "main") == case.base
    assert not case.tracker.created_prs and not case.tracker.merged_prs


def _retry(case):
    case.clock[0] += timedelta(minutes=1)
    orch.retry(case.ctx, 1, hint="Continue the unfinished implementation from the issue branch.")
    case.store.start_run(1, branch=BRANCH, worktree=case.store.worktree_path(1))


def test_spec_question_preserves_exact_wip_on_issue_branch_and_origin(git_history, tmp_path, monkeypatch):
    case = _case(git_history, tmp_path, monkeypatch)
    case.implementer.steps = [lambda kwargs: _write_changes(kwargs, question=True, stage=True)]

    assert runner.run_issue(1, case.ctx) == "spec-question"
    assert case.store.get_run(1).outcome == "spec-question"
    _assert_saved(case, "spec-question")


def test_check_timeout_preserves_exact_wip_with_stop_reason(git_history, tmp_path, monkeypatch):
    case = _case(git_history, tmp_path, monkeypatch, max_runtime_minutes=1, max_retries=1)
    case.implementer.steps = [_write_changes]
    real_run_logged = runner.run_logged
    timed_out = []

    def check_process(argv, **kwargs):
        if argv == ["/bin/sh", "-c", "test -f feature.txt"]:
            timed_out.append(argv)
            case.clock[0] += timedelta(seconds=kwargs["timeout_s"] + 1)
            with kwargs["log_path"].open("a", encoding="utf-8") as log:
                log.write("The check did not finish before its deadline.\n")
            return 124, True, "The check did not finish before its deadline."
        return real_run_logged(argv, **kwargs)

    # Inject only process I/O: the real check, timeout handling and cleanup run.
    monkeypatch.setattr(runner, "run_logged", check_process)
    assert runner.run_issue(1, case.ctx) == "timeout"
    assert timed_out and len(case.implementer.calls) == 1
    assert case.store.get_run(1).outcome == "timeout"
    _assert_saved(case, "timeout")


@pytest.mark.parametrize("stop_reason", ["spec-question", "retries-exceeded"])
def test_retry_restores_saved_files_before_implementer_and_squashes_on_merge(
    git_history, tmp_path, monkeypatch, stop_reason,
):
    case = _case(git_history, tmp_path, monkeypatch)
    case.implementer.steps = [lambda kwargs: _write_changes(kwargs, question=stop_reason == "spec-question")]
    assert runner.run_issue(1, case.ctx) == stop_reason
    # The retries-exceeded case already saves a real commit on today's code;
    # it independently exposes the next run resetting that existing branch.
    if stop_reason == "retries-exceeded":
        assert _tree(case.root, BRANCH) == _expected_tree(case)

    observed, merge_methods, merged_heads = [], [], []

    def finish(kwargs):
        worktree = kwargs["worktree"]
        observed.append({name: (worktree / name).read_text(encoding="utf-8")
                         if (worktree / name).exists() else None for name in CHANGES})
        assert not (worktree / ".foragent/QUESTION.md").exists()
        return case.implement(kwargs)

    original_merge = case.tracker.merge_pr

    def github_merge(pr, *, expected_head_sha, method="squash"):
        merge_methods.append(method)
        merged_heads.append(expected_head_sha)
        original_merge(pr, expected_head_sha=expected_head_sha, method=method)
        # FakeTracker records the request; model GitHub's squash in the local
        # bare origin so main's actual ancestry and tree can also be inspected.
        _import_commit(case.history.origin, "refs/heads/main", case.base,
                       expected_head_sha, case.tracker.get_issue(pr).title)

    monkeypatch.setattr(case.tracker, "merge_pr", github_merge)
    case.implementer.steps = [finish]
    _retry(case)
    assert runner.run_issue(1, case.ctx) == "merged"
    assert len(case.implementer.calls) == 2
    assert merge_methods == ["squash"]
    main = git_command(case.history.origin, "rev-parse", "main")
    assert git_command(case.history.origin, "rev-list", "--count", f"{case.base}..{main}") == "1"
    assert git_command(case.history.origin, "rev-parse", f"{main}^") == case.base
    assert git_command(case.root, "rev-parse", BRANCH) == merged_heads[0], (
        "Successful cleanup must not add another WIP commit"
    )
    assert observed == [CHANGES], "Retry discarded the prior edits before calling the implementer"
    assert _tree(case.history.origin, main) == _expected_tree(case, {**CHANGES, "feature.txt": "done\n"})


def test_wip_restores_locked_acceptance_even_when_implementer_staged_it(git_history, tmp_path, monkeypatch):
    case = _case(git_history, tmp_path, monkeypatch)
    approved_blob = case.store.load_approval(1).acceptance_blobs[ACCEPTANCE]

    def tamper(kwargs):
        _write_changes(kwargs)
        (kwargs["worktree"] / ACCEPTANCE).write_text("def test_feature():\n    pass\n", encoding="utf-8")
        git_command(kwargs["worktree"], "add", "-A")
        return "Partial implementation, including a modified locked check"

    case.implementer.steps = [tamper]
    assert runner.run_issue(1, case.ctx) == "retries-exceeded"
    assert git_command(case.root, "rev-parse", f"{BRANCH}:{ACCEPTANCE}") == approved_blob, (
        "WIP must restore the approved locked acceptance blob, including its staged contents"
    )
    _assert_saved(case, "retries-exceeded")


def test_empty_stop_skips_wip_but_a_changed_retry_is_saved(git_history, tmp_path, monkeypatch):
    case = _case(git_history, tmp_path, monkeypatch)
    case.implementer.steps = [lambda kwargs: _question(kwargs["worktree"])]
    assert runner.run_issue(1, case.ctx) == "spec-question"
    assert git_command(case.root, "rev-parse", BRANCH) == case.base, "Runner artifacts alone must not create WIP"
    assert case.snapshots == []

    case.implementer.steps = [lambda kwargs: _write_changes(kwargs, question=True)]
    _retry(case)
    assert runner.run_issue(1, case.ctx) == "spec-question"
    _assert_saved(case, "spec-question")
