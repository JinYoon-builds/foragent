"""Locked acceptance for pr-body-limit; exercise real run publication offline."""

from dataclasses import replace
from datetime import timedelta
import re

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
    """Accept a retained file/run log or linked tracker comments, without fixing a filename."""
    assert re.search(r"\bfull\b", body, re.I), "Shortening must visibly identify the full text"
    sources = []
    for path in case.store.root.rglob("*"):
        if path.is_file():
            relative = path.relative_to(case.store.root).as_posix()
            if str(path.resolve()) in body or ("/" in relative and relative in body):
                sources.append(path.read_text(encoding="utf-8", errors="replace"))
    for issue in case.tracker.issues.values():
        for comment in case.tracker.list_comments(issue.number):
            if f"#issuecomment-{comment.id}" in body:
                sources.append(comment.body)
    # Comment-based storage may split large text across several linked comments.
    retained = "\n".join(sources)
    for payload in payloads:
        assert payload in body or payload in retained, (
            "The notice must resolve to retained, complete text after run cleanup: "
            + payload[:60]
        )


def assert_required_run_body(case, body):
    assert body.splitlines().count("Closes #1") == 1
    assert SUMMARY in body, "Keep the short change summary whole"
    assert "## Review" in body and "VERDICT: PASS" in body
    assert "Opened by Foragent gate (attempt 1)" in body
    assert f"<code>test -f {ACCEPTANCE}</code> | pass |" in body
    assert case.store.load_approval(1).body == case.tracker.get_issue(1).body


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
    assert len(c.tracker.created_prs) == 1, "Do not replace or duplicate the issue PR"
    pr = c.tracker.created_prs[0]
    assert existing is None or pr.number == existing
    assert c.tracker.merged_prs == [pr.number]
    assert any(operation == ("create_pr" if existing is None else "update_pr") for operation, _ in calls)
    assert_required_run_body(c, pr.body)
    assert_full_text_is_reachable(c, pr.body, list(payloads.values()))


# A literal public-body baseline: never derive this expected value from the renderer.
NORMAL_BODY = '''Closes #1

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
    empty_size = len(NORMAL_BODY.replace("{evidence}", ""))
    if size == "exact-limit":
        evidence = "증" * (LIMIT - empty_size)
    expected = NORMAL_BODY.replace("{evidence}", evidence)
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
    enable_reviewer(c, ["VERDICT: PASS"] * 4)
    c.ctx.config = replace(c.ctx.config, review_scope="pr-body")
    payload = "evidence-BEGIN\n" + "e" * 70000 + "\nevidence-END"
    if entry == "run":
        c.executor.steps[0].update({".foragent/SEARCHED.md": payload, ".foragent/TRY.md": CARD})
    else:
        # A legal, nearly full existing PR becomes oversized when re-review adds
        # the current review scope, risk notes and review checklist.
        c.store.finish_run(1, outcome="held")
        c.now[0] += timedelta(seconds=1)
        c.tracker.remove_labels(1, ["running"])
        base = c.history.tip
        c.history.update({"feature.txt": "done"})
        head = c.history.tip
        git_command(c.history.origin, "update-ref", "refs/heads/foragent/issue-1", head)
        git_command(c.history.origin, "update-ref", "refs/heads/main", base)
        prefix = f"Closes #1\n\n## Summary\n\n{SUMMARY}\n\n"
        payload = "evidence-BEGIN\n" + "e" * (LIMIT - len(prefix) - 40) + "\nevidence-END"
        body = prefix + payload
        assert len(body) <= LIMIT
        pr = c.tracker.create_pr(head="foragent/issue-1", base="main", title="Existing PR", body=body)
        c.tracker.set_pr_head_sha(pr, head)
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
    assert "Preserve the feature output." in body
    assert "## Review scope" in body
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
