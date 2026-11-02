"""Locked acceptance for pr-body-limit; exercise real run publication offline."""

from dataclasses import replace
from datetime import timedelta
import re
import json

import pytest

from foragent.runner import review_only, run_issue
from foragent.tracker import TrackerError

from conftest import ACCEPTANCE, git_command
from runner_support import batch_setup, enable_reviewer, run_case  # noqa: F401


LIMIT = 65536
SUMMARY = "The feature now writes done."
CARD = (
    f"## What changed\n{SUMMARY}\n## Try it\nkind: cli\n"
    "```sh\ncat feature.txt\n```\n## Expect\ndone\n"
)
RISK_START = "<!-- foragent:risk-notes -->"
RISK_END = "<!-- /foragent:risk-notes -->"


def reject_oversized_bodies(case, monkeypatch):
    """Observe the external tracker boundary, including rejected write attempts."""
    calls = []
    for operation in ("create_pr", "update_pr"):
        send = getattr(case.tracker, operation)

        def checked(*args, _send=send, _operation=operation, **kwargs):
            body = kwargs["body"]
            calls.append((_operation, body))
            if len(body) > LIMIT:
                raise TrackerError(
                    "GraphQL: Body is too long (maximum is 65536 characters) "
                    f"({_operation}); got {len(body)} characters"
                )
            return _send(*args, **kwargs)

        monkeypatch.setattr(case.tracker, operation, checked)
    return calls


def assert_writes_fit(calls):
    assert calls, "Publication must actually send a PR body"
    assert all(len(body) <= LIMIT for _, body in calls), (
        "Every attempted PR write must fit, including updates and retries: "
        + repr([(operation, len(body)) for operation, body in calls])
    )


def assert_full_text_is_reachable(case, body, payloads):
    """Full removed text must survive in this PR's comments, with a named notice."""
    assert "full" in body.lower() and "PR comment" in body
    comments = [comment.body for pr in case.tracker.created_prs
                for comment in case.tracker.list_comments(pr.number)]
    retained = "".join(comment.split("\n", 1)[1] for comment in comments
                       if comment.startswith("Foragent full "))
    for payload in payloads:
        assert payload in body or payload in retained, payload[:60]
    for comment in comments:
        if comment.startswith("Foragent full "):
            label = comment.split("\n", 1)[0].split(" [part", 1)[0]
            assert label in body, "Each removed block needs its own named PR-comment notice"



def assert_required_run_body(case, body):
    assert body.splitlines().count("Closes #1") == 1
    assert SUMMARY in body, "Keep the short change summary whole"
    assert "## Review" in body and "VERDICT: PASS" in body
    assert "Opened by Foragent gate (attempt 1)" in body
    assert f"<code>test -f {ACCEPTANCE}</code> | pass |" in body
    assert "- [ ] Looks right to a human" in body
    approval = case.store.load_approval(1)
    assert f"Approval: {approval.approved_by} at {approval.approved_at}" in body
    assert approval.body == case.tracker.get_issue(1).body


@pytest.mark.parametrize("publication", ["create", "update"])
@pytest.mark.parametrize("source", ["rebuttal", "evidence", "review", "try", "combined"])
def test_large_run_text_still_publishes_once(run_case, monkeypatch, publication, source):
    c = run_case
    existing = None
    if publication == "update":
        existing = c.tracker.create_pr(
            head="foragent/issue-1", base="main", title="Existing PR", body="Closes #1"
        )
    # Each individual source can overflow, as can their sum when each is smaller.
    width = 20000 if source == "combined" else 70000
    payloads = {
        name: f"{name}-BEGIN\n" + ("증거x " * (width // 4)) + f"\n{name}-END"
        for name in ("rebuttal", "evidence", "review", "try")
        if source in (name, "combined")
    }
    step = c.executor.steps[0]
    step[".foragent/TRY.md"] = CARD + payloads.get("try", "")
    if "rebuttal" in payloads:
        step[".foragent/REBUTTAL.md"] = payloads["rebuttal"]
    if "evidence" in payloads:
        step[".foragent/SEARCHED.md"] = payloads["evidence"]
    enable_reviewer(c, ["VERDICT: PASS\n" + payloads.get("review", "")])
    calls = reject_oversized_bodies(c, monkeypatch)

    outcome = run_issue(1, c.ctx)

    assert outcome == "merged", f"A finished change must publish successfully, got {outcome}"
    assert_writes_fit(calls)
    assert len(calls) == 1, "Fit on the first call; no length retry or follow-up body update"
    assert len(c.tracker.created_prs) == 1, "Do not replace or duplicate the issue PR"
    pr = c.tracker.created_prs[0]
    assert existing is None or pr.number == existing
    assert c.tracker.merged_prs == [pr.number]
    assert any(operation == ("create_pr" if existing is None else "update_pr") for operation, _ in calls)
    assert_required_run_body(c, pr.body)
    assert_full_text_is_reachable(c, pr.body, list(payloads.values()))


# A literal public-body baseline: never derive this expected value from the renderer.
NORMAL_BODY = '''Closes #1

## Summary

{summary}

{approval}

| Done when command | Result |
| --- | --- |
| <code>test "$(cat feature.txt)" = done &#124;&#124; { echo "feature not ready"; exit 1; }</code> | pass |
| <code>test -f tests/acceptance/test_feature.py</code> | pass |

## Review

Round 1: VERDICT: PASS


## Searched

{evidence}

## Needs a human check

- [ ] Looks right to a human

Skills used: not reported

Attempt 1 at tier m

Opened by Foragent gate (attempt 1)

## Try it

''' + CARD


@pytest.mark.parametrize("publication", ["create", "update"])
@pytest.mark.parametrize("size", ["short", "exact-limit"])
def test_body_that_already_fits_is_unchanged(run_case, monkeypatch, publication, size):
    c = run_case
    if publication == "update":
        c.tracker.create_pr(head="foragent/issue-1", base="main", title="Existing PR", body="Old body")
    # Multibyte characters also catch accidentally budgeting UTF-8 bytes as characters.
    evidence = "Looked in feature.txt; found the old output."
    approval = c.store.load_approval(1)
    baseline = NORMAL_BODY.replace("{summary}", SUMMARY).replace(
        "{approval}", f"Approval: {approval.approved_by} at {approval.approved_at}")
    empty_size = len(baseline.replace("{evidence}", ""))
    if size == "exact-limit":
        evidence = "증" * (LIMIT - empty_size)
    expected = baseline.replace("{evidence}", evidence)
    c.executor.steps[0].update({".foragent/SEARCHED.md": evidence, ".foragent/TRY.md": CARD})
    enable_reviewer(c, ["VERDICT: PASS"])
    calls = reject_oversized_bodies(c, monkeypatch)

    assert run_issue(1, c.ctx) == "merged"

    assert_writes_fit(calls)
    assert len(c.tracker.created_prs) == 1
    assert c.tracker.created_prs[0].body == expected
    assert all(body == expected for _, body in calls)
    assert len(expected) == LIMIT if size == "exact-limit" else len(expected) < LIMIT


@pytest.mark.parametrize("entry", ["run", "review-only"])
def test_native_review_markers_survive_oversized_creation_and_refresh(run_case, monkeypatch, entry):
    c = run_case
    c.history.update({".foragent-risks.yml": (
        "- paths: [feature.txt]\n  why: Preserve the feature output.\n"
        "  guidance: Check the resulting file.\n"
    )})
    # #1372 (2026-11-01, test owner): four perspective reviews plus one findings merge.
    enable_reviewer(c, ["VERDICT: PASS"] * 5)
    c.ctx.config = replace(c.ctx.config, review_scope="pr-body")
    payload = "evidence-BEGIN\n" + "e" * 70000 + "\nevidence-END"
    if entry == "run":
        c.executor.steps[0].update({".foragent/SEARCHED.md": payload, ".foragent/TRY.md": CARD})
    else:
        # Publish once to produce the record, then remove the worktree and edit the projection.
        # Re-review must recompose from persisted values; the old body has none of them.
        c.executor.steps[0].update({".foragent/SEARCHED.md": payload, ".foragent/TRY.md": CARD})
        c.ctx.config = replace(c.ctx.config, reviewer="none", review="careful")
        assert run_issue(1, c.ctx) == "pr-open"
        c.now[0] += timedelta(seconds=1)
        pr = c.tracker.created_prs[0]
        c.tracker.update_pr(pr.number, base=pr.base, title=pr.title, body="Person's unrelated edit")
        enable_reviewer(c, ["VERDICT: PASS"] * 5)
        c.ctx.config = replace(c.ctx.config, review="fast")
    calls = reject_oversized_bodies(c, monkeypatch)

    outcome = run_issue(1, c.ctx) if entry == "run" else review_only(1, c.ctx)

    assert outcome == "merged", f"Native review publication must succeed, got {outcome}"
    assert_writes_fit(calls)
    assert len(c.tracker.created_prs) == 1
    body = c.tracker.created_prs[0].body
    assert body.splitlines().count("Closes #1") == 1
    assert SUMMARY in body
    assert body.splitlines().count(RISK_START) == body.splitlines().count(RISK_END) == 1
    assert body.index(RISK_START) < body.index(RISK_END)
    assert "VERDICT: PASS" in body and "- [ ] Looks right to a human" in body
    approval = c.store.load_approval(1)
    assert f"Approval: {approval.approved_by} at {approval.approved_at}" in body
    assert "Preserve the feature output." in body
    assert "## Review scope" in body
    assert "Person's unrelated edit" not in body
    assert "Tests under tests/acceptance/ that this issue lists are locked acceptance tests." in body
    assert_full_text_is_reachable(c, body, [payload])


@pytest.mark.parametrize("publication", ["create", "update"])
def test_batch_publication_also_fits(run_case, monkeypatch, publication):
    c = run_case
    batch_setup(c)
    # Reserve the child's PR number before adding a large completed sibling set.
    c.tracker.create_pr(
        head="foragent/issue-1", base="foragent/batch-99", title="Existing child", body="Closes #1"
    )
    titles = []
    for number in range(200, 500):
        title = f"Completed child {number}: " + "x" * 240
        titles.append(title)
        c.tracker.add_issue(number, title=title, state="closed")
        c.tracker.set_parent(number, 99)
    if publication == "update":
        c.tracker.create_pr(head="foragent/batch-99", base="main", title="Existing batch", body="Old batch body")
    calls = reject_oversized_bodies(c, monkeypatch)

    assert run_issue(1, c.ctx) == "merged"

    assert_writes_fit(calls)
    batches = [pr for pr in c.tracker.created_prs if pr.head == "foragent/batch-99"]
    assert len(batches) == 1
    body = batches[0].body
    assert "#99" in body and "## Children" in body
    assert not re.search(r"\b(?:Closes|Fixes|Resolves)\s+#99\b", body, re.I)
    assert_full_text_is_reachable(c, body, titles)
    batch_calls = [(op, body) for op, body in calls if "## Children" in body]
    assert len(batch_calls) == 1
    assert batch_calls[0][0] == ("create_pr" if publication == "create" else "update_pr")


@pytest.fixture(autouse=True)
def summary_value(run_case):
    # Explicit producer protocol: summary is a value, TRY.md remains opaque optional text.
    run_case.executor.steps[0][".foragent/PR.json"] = json.dumps({"summary": SUMMARY})


@pytest.mark.parametrize("publication", ["create", "update"])
@pytest.mark.parametrize("incident", [1105, 993])
def test_incident_shape_fits_on_first_write(run_case, monkeypatch, publication, incident):
    c = run_case
    # Offline shapes: a repeated failure log/table and review/rebuttal blocks, not a flat pad.
    row = f"| issue-{incident} | tests/test_worker.py::test_capacity | FAIL |\r\n"
    log = "```text\r\n## Needs a human check ##\r\nCloses #99\r\n" + row * 1400 + "```\r\n"
    c.executor.steps[0].update({".foragent/SEARCHED.md": log, ".foragent/TRY.md": CARD})
    if publication == "update":
        c.tracker.create_pr(head="foragent/issue-1", base="main", title="Existing", body="Earlier human edit")
    enable_reviewer(c, ["VERDICT: PASS"])
    calls = reject_oversized_bodies(c, monkeypatch)
    assert run_issue(1, c.ctx) == "merged"
    assert len(calls) == 1 and calls[0][0] == publication + "_pr"
    assert_writes_fit(calls)
    assert_required_run_body(c, calls[0][1])
    assert_full_text_is_reachable(c, calls[0][1], [log])


@pytest.mark.parametrize("shape", ["crlf-summary", "human-heading", "fenced-heading"])
def test_rendered_markdown_cannot_choose_required_content(run_case, monkeypatch, shape):
    c = run_case
    summary = "A complete summary.\r\nSecond complete sentence." if shape == "crlf-summary" else SUMMARY
    c.executor.steps[0][".foragent/PR.json"] = json.dumps({"summary": summary})
    optional = ("## What changed\r\nAn optional, obsolete summary.\r\n## Needs a human check ##\r\n"
                "- [ ] Optional example only\r\n```md\r\n## What changed\r\nCloses #99\r\n```\r\n")
    payload = optional + "증거" * 40000
    c.executor.steps[0][".foragent/TRY.md"] = payload
    enable_reviewer(c, ["VERDICT: PASS"])
    calls = reject_oversized_bodies(c, monkeypatch)
    assert run_issue(1, c.ctx) == "merged"
    assert len(calls) == 1
    body = calls[0][1]
    assert summary in body and "Closes #1" in body and "VERDICT: PASS" in body
    assert "- [ ] Looks right to a human" in body
    assert_full_text_is_reachable(c, body, [payload])


@pytest.mark.parametrize("entry", ["run", "review-only"])
def test_required_summary_alone_over_limit_stops_before_write(run_case, monkeypatch, entry):
    c = run_case
    c.executor.steps[0][".foragent/PR.json"] = json.dumps({"summary": "큰 요약\r\n" * 15000})
    if entry == "review-only":
        # First retain a structured snapshot by a stopped publication, then retry review.
        assert run_issue(1, c.ctx) == "pr-body-too-long"
        c.now[0] += timedelta(seconds=1)
        pr = c.tracker.create_pr(head="foragent/issue-1", base="main", title="Existing", body="Short old body")
        head = git_command(c.history.origin, "rev-parse", "refs/heads/foragent/issue-1")
        c.tracker.set_pr_head_sha(pr, head)
        c.ctx.config = replace(c.ctx.config, review_scope="pr-body")
        enable_reviewer(c, ["VERDICT: PASS"] * 5)
    calls = reject_oversized_bodies(c, monkeypatch)
    outcome = run_issue(1, c.ctx) if entry == "run" else review_only(1, c.ctx)
    assert outcome == "pr-body-too-long"
    assert calls == [], "No write, including a doomed length probe"
    assert "required" in c.store.get_run(1).detail.lower() and "65536" in c.store.get_run(1).detail


class UnreadableBody(str):
    def denied(self, *args, **kwargs):
        raise AssertionError("A rendered PR body was read to compose an update")
    __str__ = __len__ = __contains__ = __eq__ = __add__ = denied
    split = splitlines = strip = replace = find = index = denied


def test_gate_update_overwrites_human_edits_without_reading_body(run_case, monkeypatch):
    c = run_case
    c.executor.steps[0][".foragent/TRY.md"] = CARD
    c.ctx.config = replace(c.ctx.config, review="careful")
    assert run_issue(1, c.ctx) == "pr-open"
    c.now[0] += timedelta(seconds=1)
    pr = c.tracker.created_prs[0]
    c.tracker.update_pr(pr.number, base=pr.base, title=pr.title, body="Human edit; do not preserve me")
    listing = c.tracker.list_prs
    monkeypatch.setattr(c.tracker, "list_prs", lambda *a, **kw: [
        replace(p, body=UnreadableBody("old body")) for p in listing(*a, **kw)])
    enable_reviewer(c, ["VERDICT: PASS"] * 5)
    c.ctx.config = replace(c.ctx.config, review="fast", review_scope="pr-body")
    calls = reject_oversized_bodies(c, monkeypatch)
    assert review_only(1, c.ctx) == "merged"
    assert_writes_fit(calls)
    for _, body in calls:
        assert SUMMARY in body and "Closes #1" in body
        assert "Human edit; do not preserve me" not in body
    # No requirement for an edit-overwrite notice: gate ownership is the contract.


def test_real_closing_reference_is_structured_not_the_first_closes_line(run_case, monkeypatch):
    c = run_case
    # An unrelated Closes line in a person's old projection must never choose the issue.
    c.ctx.config = replace(c.ctx.config, review="careful")
    assert run_issue(1, c.ctx) == "pr-open"
    c.now[0] += timedelta(seconds=1)
    original = c.tracker.get_issue(1)
    approval = c.store.load_approval(1)
    c.tracker.add_issue(7, original.title, original.body, labels=["approved"])
    c.store.save_approval(**{**approval.__dict__, "number": 7})
    # Move the saved values, without reading or classifying a Markdown body.
    record = c.store.root / "pr-content/1.json"
    destination = c.store.root / "pr-content/7.json"
    destination.write_bytes(record.read_bytes())
    pr = c.tracker.create_pr(head="foragent/issue-7", base="main", title="Existing (#7)",
                             body="Closes #99\n## Summary\nWrong old summary\nCloses #7")
    head = git_command(c.history.origin, "rev-parse", "refs/heads/foragent/issue-1")
    git_command(c.history.origin, "update-ref", "refs/heads/foragent/issue-7", head)
    c.tracker.set_pr_head_sha(pr, head)
    c.tracker.set_pr_files(pr, ["feature.txt"])
    enable_reviewer(c, ["VERDICT: PASS"] * 5)
    c.ctx.config = replace(c.ctx.config, review="fast", review_scope="pr-body")
    calls = reject_oversized_bodies(c, monkeypatch)
    assert review_only(7, c.ctx) == "merged"
    assert calls and all(body.splitlines().count("Closes #7") == 1 for _, body in calls)
    assert all("Closes #99" not in body and SUMMARY in body for _, body in calls)


@pytest.mark.parametrize("large", ["risk", "decisions", "both"])
def test_optional_marker_blocks_have_independent_shares(run_case, monkeypatch, large):
    from foragent import run_review, run_publish
    c = run_case
    risk = "risk-BEGIN\n" + ("r" * 70000 if large in ("risk", "both") else "Keep this risk whole.") + "\nrisk-END"
    decisions = "decisions-BEGIN\n" + ("d" * 70000 if large in ("decisions", "both") else "Keep this choice whole.") + "\ndecisions-END"
    monkeypatch.setattr(run_publish, "_risk_notes", lambda attempt: risk)
    monkeypatch.setattr(run_review, "_lane_decisions", lambda *a: decisions)
    c.executor.steps[0][".foragent/TRY.md"] = CARD
    enable_reviewer(c, ["VERDICT: PASS"] * 5)
    c.ctx.config = replace(c.ctx.config, review_scope="pr-body")
    calls = reject_oversized_bodies(c, monkeypatch)
    assert run_issue(1, c.ctx) == "merged"
    assert_writes_fit(calls)
    body = calls[-1][1]
    for start, end in ((RISK_START, RISK_END),
                       ("<!-- foragent:lane-decisions -->", "<!-- /foragent:lane-decisions -->")):
        assert body.splitlines().count(start) == body.splitlines().count(end) == 1
        assert body.index(start) < body.index(end)
    assert "Closes #1" in body and SUMMARY in body and "VERDICT: PASS" in body
    assert "- [ ] Looks right to a human" in body
    if large == "risk":
        assert decisions in body
    if large == "decisions":
        assert risk in body
    assert_full_text_is_reachable(c, body, [risk, decisions])
