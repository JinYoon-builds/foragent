"""A reviewed follower asks the same group-reuse question at every base-move point."""

from dataclasses import replace
from datetime import timedelta

from foragent.runner import run_issue
from runner_support import run_case  # noqa: F401


def test_reviewed_follower_reuses_before_and_during_lock_acquisition(run_case, monkeypatch):
    from foragent import run_group_evidence, run_publish, runner

    c = run_case
    c.ctx.config = replace(c.ctx.config, review="careful")
    assert run_issue(1, c.ctx) == "pr-open"
    c.ctx.config = replace(c.ctx.config, review="fast")
    c.now[0] += timedelta(seconds=1)
    head = c.tracker.pr_head_sha(2)
    calls, moved, merged = [], [], []

    def note(n, ctx, head_sha, target, neutral=None):
        calls.append((n, head_sha, target, neutral))
        return None  # the joint pass verifies this head on every target

    final = run_publish._final_merge_inputs

    def inputs(*args, **kwargs):
        result = final(*args, **kwargs)
        if not moved:  # main moved while this run waited for the lock
            moved.append(result.base_sha)
            return replace(result, base_sha="f" * 40)
        return result

    merge = c.tracker.merge_pr

    def record(pr, **kwargs):
        merged.append(kwargs.get("expected_head_sha"))
        return merge(pr, **kwargs)

    monkeypatch.setattr(run_group_evidence, "note", note)
    monkeypatch.setattr(run_publish, "_final_merge_inputs", inputs)
    monkeypatch.setattr(c.tracker, "merge_pr", record)
    assert runner.review_only(1, c.ctx, verdict_head=head) == "merged"
    assert merged == [head] and c.tracker.pr_head_sha(2) == head, "Merge the exact checked head, unpushed"
    targets = [target for _, _, target, _ in calls]
    assert targets[0] == "f" * 40, "The in-lock move must ask the reuse question, not refresh"
    assert len(calls) == 2 and all(n == 1 and h == head and callable(rule) for n, h, _, rule in calls), (
        "Every reuse decision carries the run_base_move neutral rule"
    )
    assert run_publish.REVERIFY not in c.store.log_path(1).read_text()
