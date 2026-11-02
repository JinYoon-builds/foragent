# PR bodies always fit GitHub

On 2026-11-01, issue #1105 finished its code change but publication was blocked by:

> Runner failed: pull request create failed: GraphQL: Body is too long (maximum is 65536 characters) (createPullRequest)

## Outcome

Every pull request body Foragent sends, on creation and on every update, stays within GitHub's 65,536-character limit, so a completed run never loses its PR to a body-length error. This includes implementation publication, refreshes of existing PRs, review-only updates and batch publication.

When the complete content does not fit, keep the required parts whole: Closes #N on issue PRs, the short change summary, review and approval lines where present, and the gate's machine-readable markers (including complete risk-notes and lane-decisions marker pairs where present). Shorten expandable text with a visible note that identifies where its complete text is retained and can still be retrieved after the run finishes. No attempted create or update sends an oversized body.

A body that already fits, including one exactly 65,536 characters long, is unchanged. Batch PRs keep their parent reference and existing completion semantics.

## Out of scope

Changing review verdicts, approval requirements, agent judgment, lane scheduling, merge behavior, or GitHub's limit. Unrelated output limits and issue/comment formatting changes. The implementer owns the design and any new helper names or representation.

## Acceptance

The locked acceptance test drives the public `run_issue` and `review_only` entry points against a fake tracker that rejects every create or update over the limit. It covers independently large rebuttals, evidence, review text and Try it text, their combined size, existing-PR updates, review-scope growth, batch bodies, complete required lines and markers, retrievable full text, and unchanged short and exact-limit bodies.

```sh
uv run pytest -q tests/acceptance/test_pr_body_limit.py
```

## Planning assumptions

The GitHub character limit is inclusive and is supplied by the incident evidence; it is not a UTF-8 byte limit. The short required summary and protocol metadata fit within the limit. This order belongs to the implicit `main` lane: `.foragent.yml` assigns no named lane to the faulty `src/foragent/run_pr_body.py` renderer or its publication helpers.
