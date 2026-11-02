"""Locked D105 follow-up: a PASS scopes later reviews of the same issue body.

Only the public run/review paths are exercised. Fake reviewers supply judgments;
the harness supplies facts and must retain NOTE records. No new private API or
storage shape is required. Reject missing/wrong heads, unjustified full review
after PASS, dropped notes, suppressed new defects, stale body scope, missing risk
exemptions, a catch-all silently exempting everything, and a broken off switch.
"""

from dataclasses import replace
import json
import re

import pytest
import yaml

from conftest import git_command
from foragent.executors import ExecResult
from foragent.issue import body_hash
from foragent.runner import review_only, run_issue
from foragent.state import StateStore
from test_orch_review_handoff import make_case
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import entry, offline  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


OLD_FINDING = "feature.txt:1 — the previously accepted path loses a receipt"
NEW_FINDING = "feature.txt:2 — the changed retry path duplicates a receipt"
UPSTREAM_FINDING = "feature.txt:24 — main's new base path loses a receipt"
FIRST_FINDING = "feature.txt:2 — the first implementation needs repair"
PASS = "VERDICT: PASS\nNOTE: PASS-RECEIPT-47: reviewed the issue's receipt paths"


def content(accepted="reviewed", retry=1, base=0):
    # Far-apart lines let real Git separate an issue edit and an upstream edit
    # in the SAME file. Path-only filtering cannot satisfy this lock.
    return (f"accepted = {accepted!r}\nretry = {retry}\n"
            + "".join(f"padding_{i} = {i}\n" for i in range(21))
            + f"base = {base}\n")


def case_for(git_history, tmp_path, monkeypatch, *, reviewer="codex", **settings):
    git_history.update({"feature.txt": content("initial", 0)})
    return make_case(
        git_history, tmp_path, monkeypatch, reviewer=reviewer,
        executor="claude" if reviewer == "codex" else "codex",
        **{"skill_libraries": [], "test_command": "true", "language": "en",
           "max_retries": 1, "review_max": 0, **settings},
    )


def implement(text):
    def run(call):
        (call["worktree"] / "feature.txt").write_text(text)
        return "Implemented"
    return run


def capture_review(case, replies, *, calls=None):
    contexts = []
    replies = iter(replies)

    def run(**call):
        if calls is not None:
            calls.append(call)
        local = call["worktree"] / "CLAUDE.local.md"
        context = call["prompt"] + (local.read_text() if local.is_file() else "")
        if call.get("review_pr") is not None:
            context += case.tracker.get_issue(call["review_pr"]).body
        contexts.append(context)
        reply = next(replies)
        text = reply(call) if callable(reply) else reply
        if call.get("review_level") is not None:
            text = json.dumps({"result": text})
        return ExecResult(0, False, text, call["log_path"])

    case.reviewer.run = run
    return contexts


def scoped(context, passed_head, *, changed, main_changed=False):
    assert passed_head in context, "The reviewer must receive the actual PASS head"
    flat = " ".join(context.lower().split())
    assert "verdict: pass" in flat, "Keep the prior review's PASS record as evidence"
    assert "PASS-RECEIPT-47" in context, "Supply the actual prior review record, not only its verdict label"
    assert "issue body" in flat and re.search(r"same|unchanged", flat)
    assert re.search(r"(?:only|limit\w*).{0,180}chang", flat), "Explain the later review's blocking scope"
    assert re.search(r"unchanged.{0,180}note|note.{0,180}unchanged", flat), (
        "The reviewer must classify unchanged-code findings as nonblocking NOTE records"
    )
    assert re.search(r"main.{0,140}(?:count|includ|chang)", flat), (
        "Explain that code integrated from main after the PASS counts as later change"
    )
    assert "+accepted = 'reviewed'" not in context, "Do not present pre-PASS additions as new work"
    if main_changed:
        assert "-base = 0" in context and "+base = 1" in context, "Include main's same-file post-PASS edit"
    if changed:
        assert "-retry = 1" in context and "+retry = 2" in context, "Supply the actual post-PASS diff"
    elif not main_changed:
        assert "diff --git" not in context, "An unchanged issue has no post-PASS code diff"


def assert_result(case, outcome, *, blocks, note=None):
    assert outcome == ("held" if blocks else "merged"), case.store.log_path(1).read_text()
    assert len(case.tracker.merged_prs) == (0 if blocks else 1)
    returns = [c.body for c in case.tracker.list_comments(1)
               if c.body.startswith("<!-- foragent:blocked")]
    if blocks:
        assert len(returns) == 1 and NEW_FINDING in returns[0]
    else:
        assert returns == [], "A NOTE must not create a blocking return"
    if note:
        assert all(note not in text for text in returns), "A NOTE must never become repair work"
        # Reopen the durable records and observe the existing public run log
        # and PR. No new state filename or storage shape is prescribed.
        store = StateStore(case.store.root, now=case.ctx.now)
        assert "NOTE: " + note in store.log_path(1).read_text(), "The NOTE must survive in the run record"
        published = case.tracker.get_issue(2).body + "\n".join(c.body for c in case.tracker.list_comments(2))
        assert "NOTE: " + note in published, "The NOTE must remain visible on the PR"


@pytest.mark.parametrize("reviewer", ["codex", "claude"])
@pytest.mark.parametrize("finding", ["unchanged", "changed", "mixed"])
def test_ci_repair_reviews_only_post_pass_work_and_records_old_findings(
    git_history, tmp_path, monkeypatch, reviewer, finding,
):
    case = case_for(git_history, tmp_path, monkeypatch, reviewer=reviewer)
    case.tracker.set_pr_checks(2, "fail")
    case.tracker.set_pr_failed_logs(2, "offline CI requires retry repair")
    passed = []

    def repair(call):
        passed.append(case.tracker.pr_head_sha(2))
        case.tracker.set_pr_checks(2, "pass")
        return implement(content(retry=2))(call)

    case.implementer.steps = [implement(content()), repair]
    reply = ("VERDICT: PASS\nNOTE: " + OLD_FINDING if finding == "unchanged" else
             "VERDICT: FAIL\n" + NEW_FINDING
             + ("\nNOTE: " + OLD_FINDING if finding == "mixed" else ""))
    contexts = capture_review(case, [PASS, reply])
    outcome = run_issue(1, case.ctx)
    assert len(case.implementer.calls) == len(contexts) == 2, "CI repair must reach another review"
    assert len(passed) == 1
    assert git_command(case.history.origin, "show", f"{passed[0]}:feature.txt") == content().strip()
    scoped(contexts[1], passed[0], changed=True)
    assert_result(case, outcome, blocks=finding != "unchanged",
                  note=OLD_FINDING if finding != "changed" else None)


@pytest.mark.parametrize("finding", ["unchanged", "changed"])
def test_conflict_resolution_after_pass_receives_the_same_review_boundary(
    git_history, tmp_path, monkeypatch, finding,
):
    case = case_for(git_history, tmp_path, monkeypatch)
    passed = []

    def first_review(call):
        # Advance main after the reviewed contents exist, forcing a real conflict.
        case.history.update({"feature.txt": content("upstream", 0, 1)})
        return PASS

    def resolve(call):
        assert "<<<<<<<" in (call["worktree"] / "feature.txt").read_text()
        passed.append(git_command(call["worktree"], "rev-parse", "HEAD"))
        return implement(content(retry=2, base=1))(call)

    case.implementer.steps = [implement(content()), resolve]
    reply = ("VERDICT: PASS\nNOTE: " + OLD_FINDING if finding == "unchanged"
             else "VERDICT: FAIL\n" + NEW_FINDING)
    contexts = capture_review(case, [first_review, reply])
    outcome = run_issue(1, case.ctx)
    assert len(case.implementer.calls) == 2, "Exercise actual conflict resolution"
    assert len(contexts) == 2, "Conflict resolution must receive another review after the PASS"
    scoped(contexts[1], passed[0], changed=True, main_changed=True)
    assert_result(case, outcome, blocks=finding == "changed",
                  note=OLD_FINDING if finding == "unchanged" else None)


@pytest.mark.parametrize("body_edited", [False, True], ids=["same-body", "new-body"])
def test_restart_preserves_pass_but_body_edit_restores_a_full_review(
    git_history, tmp_path, monkeypatch, body_edited,
):
    case = case_for(git_history, tmp_path, monkeypatch, review="careful", review_max=1)
    case.implementer.steps = [implement(content()), implement(content())]
    contexts = capture_review(case, ["VERDICT: FAIL\n" + FIRST_FINDING, PASS])
    assert run_issue(1, case.ctx) == "pr-open"
    assert len(contexts) == 2 and len(case.implementer.calls) == 2
    assert "Limit this review to confirming" in contexts[1] and FIRST_FINDING in contexts[1], (
        "Before any PASS, retain today's first full review and saved-class confirmation"
    )
    head = case.tracker.pr_head_sha(2)
    # New objects and a new run lifecycle: no in-memory fact may carry the PASS.
    store = StateStore(case.store.root, now=case.ctx.now)
    case.ctx = replace(case.ctx, store=store, config=replace(case.config, review="fast", review_max=0))
    if body_edited:
        approval = store.load_approval(1)
        body = approval.body.replace("Implement the feature.", "Keep the old receipt path reliable too.")
        case.tracker.edit_body(1, body)
        store.save_approval(**{**approval.__dict__, "body": body, "body_sha": body_hash(body)})
    reply = ("VERDICT: FAIL\n" + OLD_FINDING if body_edited else "VERDICT: PASS\nNOTE: " + OLD_FINDING)
    later = capture_review(case, [reply])
    outcome = review_only(1, case.ctx)
    assert len(later) == 1 and len(case.implementer.calls) == 2, "Restart must review without another lane turn"
    if body_edited:
        assert "Keep the old receipt path reliable too." in later[0]
        assert "Limit this review to confirming" not in later[0], "A changed body invalidates old confirmation scope"
        assert "+accepted = 'reviewed'" in later[0], "Full review includes pre-PASS work again"
        assert outcome == "review-failed" and not case.tracker.merged_prs
        assert OLD_FINDING in store.log_path(1).read_text()
    else:
        scoped(later[0], head, changed=False)
        assert_result(case, outcome, blocks=False, note=OLD_FINDING)


@pytest.mark.parametrize("history", ["merge", "rebase"])
@pytest.mark.parametrize("finding", ["upstream", "changed"])
def test_main_integration_after_pass_counts_as_changed_code(
    git_history, tmp_path, monkeypatch, history, finding,
):
    case = case_for(git_history, tmp_path, monkeypatch, review="careful")
    case.implementer.steps = [implement(content())]
    capture_review(case, [PASS])
    assert run_issue(1, case.ctx) == "pr-open"
    head = case.tracker.pr_head_sha(2)
    case.history.update({"feature.txt": content("initial", 0, 1), "upstream.txt": "from main\n"})
    base = case.history.tip
    text = content(retry=2 if finding == "changed" else 1, base=1)
    # Import real merge/rebase histories in the offline remote. No commit,
    # push, checkout, reset or stash command is used, even in these fixtures.
    stream = ("commit refs/heads/foragent/issue-1\n"
              "committer Offline Tests <tests@example.com> 1792486800 +0000\n"
              "data 11\nIntegration\n"
              + (f"from {head}\nmerge {base}\n" if history == "merge" else f"from {base}\n")
              + f"M 100644 inline feature.txt\ndata {len(text.encode())}\n{text}\n"
              + "M 100644 inline upstream.txt\ndata 10\nfrom main\n\n\ndone\n")
    git_command(case.history.origin, "fast-import", "--quiet", "--force", input=stream)
    integrated = git_command(case.history.origin, "rev-parse", "refs/heads/foragent/issue-1")
    case.tracker.set_pr_head_sha(2, integrated)
    assert git_command(case.history.origin, "show", f"{integrated}:upstream.txt") == "from main"
    assert git_command(case.history.origin, "show", f"{integrated}:feature.txt") == text.strip()
    case.ctx = replace(case.ctx, store=StateStore(case.store.root, now=case.ctx.now),
                       config=replace(case.config, review="fast"))
    # PM follow-up replaces the old point (e). Keep these existing node IDs;
    # both upstream code and the issue's own later code can now block.
    defect = UPSTREAM_FINDING if finding == "upstream" else NEW_FINDING
    reply = "VERDICT: FAIL\n" + defect
    contexts = capture_review(case, [reply])
    outcome = review_only(1, case.ctx)
    assert len(contexts) == 1
    scoped(contexts[0], head, changed=finding == "changed", main_changed=True)
    assert outcome == "review-failed" and not case.tracker.merged_prs
    assert defect in case.ctx.store.log_path(1).read_text()


@pytest.mark.parametrize("mapping", ["marked-file", "unmarked-all", "marked-unrelated"])
def test_risk_exemption_requires_a_marked_entry_matching_a_changed_file(
    git_history, tmp_path, monkeypatch, mapping,
):
    marked = mapping == "marked-file"
    entries = []
    if mapping != "unmarked-all":
        entries.append(entry(["feature.txt" if marked else "untouched.txt"], pass_scope_exempt=True))
    # A later catch-all may add panel coverage but cannot erase a real marked
    # exception or silently grant one to every other path.
    entries.append(entry(["**"]))
    git_history.update({".foragent-risks.yml": yaml.safe_dump(entries)})
    case = case_for(git_history, tmp_path, monkeypatch)
    case.tracker.set_pr_checks(2, "fail")
    case.tracker.set_pr_failed_logs(2, "offline CI requires retry repair")
    passed = []

    def repair(call):
        passed.append(case.tracker.pr_head_sha(2))
        case.tracker.set_pr_checks(2, "pass")
        return implement(content(retry=2))(call)

    case.implementer.steps = [implement(content()), repair]
    contexts = []

    def review(**call):
        later = len(case.implementer.calls) > 1
        if later:
            contexts.append(call["prompt"])
        # Supply the reviewing agent's judgment to every perspective and its
        # merger, without choosing counts, seats or scheduling in this fake.
        reply = ("VERDICT: FAIL\n" + OLD_FINDING if marked else "VERDICT: PASS\nNOTE: " + OLD_FINDING)
        return ExecResult(0, False, reply if later else PASS, call["log_path"])

    case.reviewer.run = review
    outcome = run_issue(1, case.ctx)
    assert contexts and len(case.implementer.calls) == 2, "CI repair must reach a later review"
    if marked:
        assert outcome == "held" and not case.tracker.merged_prs
        returns = [c.body for c in case.tracker.list_comments(1) if c.body.startswith("<!-- foragent:blocked")]
        assert len(returns) == 1 and OLD_FINDING in returns[0]
        for context in contexts:
            assert passed[0] in context and "PASS-RECEIPT-47" in context
            flat = " ".join(context.lower().split())
            assert "exempt" in flat and re.search(r"unchanged.{0,140}block|block.{0,140}unchanged", flat)
            assert "+accepted = 'reviewed'" in context, "The marked entry preserves full review of accepted code"
    else:
        for context in contexts:
            scoped(context, passed[0], changed=True)
        assert_result(case, outcome, blocks=False, note=OLD_FINDING)


@pytest.mark.parametrize("reviewer", ["codex", "claude"])
def test_one_repository_setting_restores_legacy_review_despite_a_saved_pass(
    git_history, tmp_path, monkeypatch, reviewer,
):
    from foragent.config import load_config

    case = case_for(git_history, tmp_path, monkeypatch, reviewer=reviewer, review="careful")
    case.implementer.steps = [implement(content())]
    capture_review(case, [PASS])
    assert run_issue(1, case.ctx) == "pr-open"
    # Exercise the real public YAML setting, not a test-only Config substitute.
    # The saved PASS already exists when the person switches the behavior off.
    settings = {"repo": "owner/repo", "review": "fast", "reviewer": reviewer,
                "executor": "claude" if reviewer == "codex" else "codex", "approvers": ["human"],
                "review_max": 1, "max_retries": 1, "skill_libraries": [], "test_command": "true",
                "language": "en", "review_pass_scope": False}
    (case.root / ".foragent.yml").write_text(yaml.safe_dump(settings))
    case.ctx = replace(case.ctx, store=StateStore(case.store.root, now=case.ctx.now), config=load_config(case.root))
    calls = []
    contexts = capture_review(case, ["VERDICT: FAIL\n" + OLD_FINDING], calls=calls)
    outcome = review_only(1, case.ctx)
    assert len(contexts) == 1 and len(case.implementer.calls) == 1
    assert outcome == "review-failed" and not case.tracker.merged_prs, "Off restores blocking on unchanged code"
    assert OLD_FINDING in case.ctx.store.log_path(1).read_text()
    # Both today's native full review and a full prompt review are valid.
    assert "+accepted = 'reviewed'" in contexts[0] or calls[0].get("review_level") is not None
    assert not re.search(r"unchanged code.{0,100}(?:must be|as) nonblocking", contexts[0].lower())
