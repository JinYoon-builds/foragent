"""Acceptance test for premerge-unmerged-locks. Locked by Foragent; do not edit."""

from types import SimpleNamespace
import shlex
import sys

import pytest

from conftest import BASE_TIME, git_command
from foragent import orch
from foragent.config import Config
from foragent.git import GitRepo
from foragent.issue import body_hash
from foragent.runner import RunContext
from foragent.state import StateStore
from foragent.tracker import FakeTracker
from test_local_truth import record_gh


PENDING_FILE = "tests/acceptance/test_pending_main_feature.py"
PENDING_ID = f"{PENDING_FILE}::test_pending_feature"
PROTECTED_FILE = "tests/acceptance/test_supported_version.py"
PROTECTED_ID = f"{PROTECTED_FILE}::test_required_version"


@pytest.fixture(params=["ci", "local"])
def merge_scenario(request, git_history, tmp_path, monkeypatch):
    """Real local Git and pytest; fake only GitHub and machine/application services."""
    calls = record_gh(monkeypatch)

    def build(*, current_main=False, regression=False):
        history = git_history
        history.update({
            PROTECTED_FILE: (
                '"""Acceptance test for supported-version. Locked by Foragent; do not edit."""\n'
                "from pathlib import Path\n\n"
                "def test_required_version():\n"
                "    required = Path('required-version.txt').read_text().strip()\n"
                "    assert required in Path('supported-versions.txt').read_text().splitlines()\n"
            ),
            "required-version.txt": "v1\n",
            "supported-versions.txt": "v1\nv2\n",
        })
        common = history.tip
        # The PR target advances independently, before the other order reaches main.
        history.update({"required-version.txt": "v2\n"})
        target = history.tip
        git_command(history.origin, "update-ref", "refs/heads/foragent/batch-1", target)
        git_command(history.origin, "update-ref", "refs/heads/main", common)
        history.tip = common
        history.update({
            PENDING_FILE: (
                '"""Acceptance test for pending-main-feature. Locked by Foragent; do not edit."""\n'
                "from pathlib import Path\n\n"
                "def test_pending_feature():\n"
                "    assert Path('not-implemented-yet.txt').is_file(), 'approved work is still pending'\n"
            ),
        })
        published_main = history.tip
        history.update({
            "feature.txt": "unrelated PR work\n",
            "supported-versions.txt": "v1\n" if regression else "v1\nv2\n",
        })
        head = history.tip
        git_command(history.origin, "update-ref", "refs/heads/foragent/issue-1", head)
        git_command(history.origin, "update-ref", "refs/heads/main", published_main)
        history.tip = published_main

        tracker = FakeTracker(now=lambda: BASE_TIME)
        tracker.add_issue(1, "Unrelated implementation", labels=["approved"])
        body = (
            "<!-- foragent:v1 -->\n## Goal\nImplement the pending main feature.\n"
            "## Out of scope\nOther features.\n## Done when\n"
            f"```sh\nuv run pytest -q {PENDING_FILE}\n```\n"
        )
        tracker.add_issue(99, "Pending main feature", body, labels=["approved"])
        base_branch = "main" if current_main else "foragent/batch-1"
        pr = tracker.create_pr(head="foragent/issue-1", base=base_branch,
                               title="Unrelated implementation", body="Closes #1")
        tracker.set_pr_head_sha(pr, head)
        tracker.set_pr_checks(pr, "pass")
        git = GitRepo(history.clone)
        git.fetch("origin", "main")
        store = StateStore(tmp_path / "state", now=lambda: BASE_TIME)
        store.save_approval(
            99, body=body, body_sha=body_hash(body),
            acceptance_blobs={PENDING_FILE: git.blob_sha(published_main, PENDING_FILE)},
            approved_by="human", approved_at=BASE_TIME.isoformat(),
        )
        config = Config(
            repo="owner/repo", checks="ci" if request.param == "ci" else "local",
            runner_unit_tests=request.param,
            test_command=f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider",
        )
        ctx = RunContext("owner/repo", history.clone, tracker, store, config, git, now=lambda: BASE_TIME)
        return SimpleNamespace(ctx=ctx, tracker=tracker, pr=pr, head=head)

    yield build
    assert calls == [], "The public merge checks must use only the fake GitHub service"


@pytest.mark.parametrize("current_main", [False, True], ids=["older-target", "current-main"])
def test_pending_main_lock_does_not_refuse_human_merge(merge_scenario, current_main):
    case = merge_scenario(current_main=current_main)

    report = orch.premerge(case.ctx, case.pr)
    refusal = None
    try:
        orch.merge(case.ctx, case.pr, approved_by="jin")
    except orch.OrchError as exc:
        refusal = str(exc)

    assert PENDING_ID not in report["broken"], f"{report}; human merge: {refusal}"
    assert report["broken"] == [] and report["tests"] == "pass" and report["ready"] is True, report
    assert refusal is None, refusal
    assert case.tracker.merged_prs == [case.pr], "The approved PR must merge exactly once"
    assert case.tracker.pr_head_sha(case.pr) == case.head
    assert case.tracker.get_issue(99).state == "open"
    assert case.tracker.find_pr("foragent/issue-99") is None


def test_protected_test_broken_only_by_merge_still_refuses(merge_scenario):
    case = merge_scenario(regression=True)
    # The target supports v2; the head only needs v1. Their merge alone needs unsupported v2.
    report = orch.premerge(case.ctx, case.pr)

    assert report["tests"] == "fail" and report["ready"] is False, report
    assert PROTECTED_ID in report["broken"], report
    assert PROTECTED_ID not in report["failing_on_base"], report
    with pytest.raises(orch.OrchError, match="not ready"):
        orch.merge(case.ctx, case.pr, approved_by="jin")
    assert case.tracker.merged_prs == []
    assert not any("Approved by" in comment.body for comment in case.tracker.list_comments(case.pr))
