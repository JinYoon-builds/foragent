"""Reject review-only ancestry refusal, stale verification and broadened pass verdicts.

Locked by Foragent. Real CLI, Git merges and Done when commands; existing
offline tracker/model/transport fakes supply the external boundaries.
"""

from collections import Counter
from dataclasses import replace
import json
import shlex
import sys
from types import SimpleNamespace

import pytest

from conftest import git_command
from foragent import cli
from foragent.issue import body_hash
from foragent.reasons import Reason
from runner_support import enable_reviewer, run_case  # noqa: F401
from test_orch import review_case  # noqa: F401
from test_orch_review_handoff import bind_cli


BRANCH = "foragent/issue-1"
BASE_FILE = "src/base_gate.py"
BASE_CONTENT = "GATE_VERSION = 2\n"


@pytest.fixture
def waiting(review_case, tmp_path, monkeypatch):
    """Reuse the existing review-only lifecycle and detached-head push fake."""
    c = review_case
    c.ctx.config = replace(c.ctx.config, merge_group_max=1)
    c.checked = tmp_path / "checked-heads.jsonl"
    c.reviewed = []
    c.review_calls = enable_reviewer(
        c, ["VERDICT: PASS"],
        edit=lambda root: c.reviewed.append(git_command(root, "rev-parse", "HEAD")),
    )
    adapter = SimpleNamespace(root=c.history.clone, git=c.ctx.git, ctx=c.ctx,
                              tracker=c.tracker, store=c.store, clock=c.now)
    c.main = bind_cli(adapter, monkeypatch)
    monkeypatch.setattr(cli, "RunContext", lambda **kwargs: c.ctx)
    c.check_body = c.store.load_approval(1).body
    approve_checks(c)
    return c


def approve_checks(c, effect="pass"):
    """A real approved command records the HEAD and tree it actually measures."""
    script = (
        "import json, subprocess; from pathlib import Path; "
        "head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(); "
        f"base = Path({BASE_FILE!r}); "
        "row = {'head': head, 'base': base.read_text() if base.exists() else None, "
        "'feature': Path('feature.txt').read_text()}; "
        f"log = Path({str(c.checked)!r}); "
        "stream = log.open('a'); stream.write(json.dumps(row) + '\\n'); stream.close(); "
        "assert row['feature'] == 'done'; "
    )
    if effect == "fail":
        script += "assert not base.exists(), 'new base exposes a failing check'; "
    elif effect == "mutate":
        script += "base.exists() and Path('feature.txt').write_text('unreviewed content'); "
    command = f"{shlex.quote(sys.executable)} -B -c {shlex.quote(script)}"
    approval = c.store.load_approval(1)
    body = c.check_body + f"\n```sh\n{command}\n```\n"
    c.tracker.edit_body(1, body)
    c.store.save_approval(**{**approval.__dict__, "body": body, "body_sha": body_hash(body)})


def move_base(c, *, conflict=False):
    """Advance main from its own tip, independently of the waiting PR head."""
    c.history.tip = git_command(c.history.origin, "rev-parse", "refs/heads/main")
    files = {BASE_FILE: BASE_CONTENT}
    if conflict:
        files["feature.txt"] = "incompatible base feature\n"
    c.history.update(files)
    c.base = c.history.tip
    c.ctx.git.fetch()
    assert not c.ctx.git.is_ancestor(c.base, c.head)
    assert not c.ctx.git.is_ancestor(c.head, c.base), "This must exercise a divergent base merge"
    if not conflict:
        c.expected_tree = git_command(c.history.clone, "merge-tree", "--write-tree", c.head, c.base)


def invoke(c, entry):
    args = ["orch", entry, "1"]
    if entry == "resolve":
        args += ["--verdict", "pass", "--note", "Reviewed this exact waiting head"]
    assert c.main(args) == 0


def measurements(c):
    return [json.loads(line) for line in c.checked.read_text().splitlines()] if c.checked.exists() else []


def assert_review(c, entry):
    assert c.executor.calls == [], "Base refresh must not launch an implementation round"
    assert c.reviewed == ([] if entry == "resolve" else [c.head]), (
        "resolve judges its recorded head; rerun-review reviews that head once, "
        "and a clean base merge needs no further reviewer round"
    )
    assert len(c.review_calls) == len(c.reviewed)
    if entry == "resolve":
        verdicts = [comment.body for comment in c.tracker.list_comments(1)
                    if comment.body.startswith("<!-- foragent:orch-verdict")]
        assert len(verdicts) == 1 and f"head={c.head} verdict=pass" in verdicts[0]


def assert_merged(c):
    assert c.store.get_run(1).outcome == "merged", (
        "A clean, behind PR must reach its merge turn without implementation retry; "
        f"got {c.store.get_run(1).outcome}"
    )
    assert c.tracker.merged_prs == [c.pr]
    assert c.tracker.get_issue(1).state == "closed"
    assert not c.tracker.get_issue(1).labels & {"blocked", "ready", "running", "needs-human"}
    assert c.store.load_flag(1) is None
    assert not any("reason=risk-high" in comment.body for comment in c.tracker.list_comments(1))
    assert len(c.tracker.created_prs) == 1
    turns = [json.loads(line) for line in (c.store.root / "merge-queue.jsonl").read_text().splitlines()]
    assert any(row.get("event") == "turn" and row.get("result") == "merged"
               and row.get("base_sha") == c.base for row in turns), "Merge must take the ordinary FIFO turn"


@pytest.mark.parametrize("entry", ["resolve", "rerun-review"])
def test_behind_head_clean_merge_is_rechecked_once_without_an_extra_review(waiting, entry):
    """Reject deleting the refusal while merging an old head, skipping checks or reviewing twice."""
    c = waiting
    move_base(c)
    invoke(c, entry)
    assert_merged(c)
    updated = c.tracker.pr_head_sha(c.pr)
    assert updated != c.head
    git_command(c.history.origin, "merge-base", "--is-ancestor", c.head, updated)
    git_command(c.history.origin, "merge-base", "--is-ancestor", c.base, updated)
    assert git_command(c.history.origin, "rev-parse", f"{updated}^{{tree}}") == c.expected_tree, (
        "The verdict may carry over only to the clean Git merge tree, with no extra edits"
    )
    assert git_command(c.history.origin, "rev-parse", f"refs/heads/{BRANCH}") == updated
    rows = measurements(c)
    assert Counter(row["head"] for row in rows)[updated] == 1, "Verify the updated head exactly once"
    assert all(row["head"] in {c.head, updated} for row in rows)
    assert Counter(row["head"] for row in rows)[c.head] <= 1
    assert [row for row in rows if row["head"] == updated] == [
        {"head": updated, "base": BASE_CONTENT, "feature": "done"}
    ]
    assert_review(c, entry)


@pytest.mark.parametrize("entry", ["resolve", "rerun-review"])
def test_real_conflict_uses_existing_merge_turn_conflict_outcome(waiting, entry):
    """Reject treating every behind head as a conflict or silently resolving a real conflict."""
    c = waiting
    move_base(c, conflict=True)
    invoke(c, entry)
    assert any("merge" in args and args[-1] == c.base for _, args in c.operations), (
        "Try the actual base merge before declaring a conflict; ancestry alone is not a conflict"
    )
    assert any(args == ["merge", "--quit"] for _, args in c.operations)
    # _publish_reviewed_in_turn's existing conflict contract: quit, flag, preserve PR.
    assert c.store.get_run(1).outcome == "base-changed"
    assert c.store.load_flag(1).reason == Reason.RISK_HIGH
    assert c.tracker.merged_prs == []
    assert c.tracker.get_issue(c.pr).state == "open"
    assert c.tracker.pr_head_sha(c.pr) == c.head
    assert git_command(c.history.origin, "rev-parse", f"refs/heads/{BRANCH}") == c.head
    assert_review(c, entry)


@pytest.mark.parametrize("entry", ["resolve", "rerun-review"])
def test_head_already_containing_base_keeps_current_behavior(waiting, entry):
    """Reject refreshing every review head, adding a push, a check or a reviewer round to the control."""
    c = waiting
    c.base = git_command(c.history.origin, "rev-parse", "refs/heads/main")
    assert c.ctx.git.is_ancestor(c.base, c.head)
    invoke(c, entry)
    assert_merged(c)
    assert c.tracker.pr_head_sha(c.pr) == c.head
    assert measurements(c) == [{"head": c.head, "base": None, "feature": "done"}]
    assert not any("merge" in args or args[:1] == ["push"] for _, args in c.operations)
    assert_review(c, entry)


@pytest.mark.parametrize("effect", ["fail", "mutate"])
def test_resolve_pass_cannot_waive_updated_head_checks_or_extra_content(waiting, effect):
    """Reject carrying a pass across failed checks or content edits beyond the clean base merge."""
    c = waiting
    approve_checks(c, effect)
    move_base(c)
    invoke(c, "resolve")
    updated_rows = [row for row in measurements(c) if row["base"] == BASE_CONTENT]
    assert len(updated_rows) == 1, "The actual merged head must be checked, even on resolve pass"
    assert updated_rows[0]["head"] != c.head
    assert c.store.get_run(1).outcome == "checks-failed"
    assert c.tracker.merged_prs == []
    assert c.tracker.pr_head_sha(c.pr) == c.head, "A failed refresh must not replace the remote head"
    assert git_command(c.history.origin, "rev-parse", f"refs/heads/{BRANCH}") == c.head
    assert c.tracker.get_issue(1).labels & {"ready", "blocked", "running"} == {"ready"}
    assert "checks-failed" in (c.store.root / "hints/1.txt").read_text()
    assert_review(c, "resolve")
