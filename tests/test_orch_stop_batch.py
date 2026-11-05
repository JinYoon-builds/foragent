"""A project stop keeps a published merge train for the release (#1491)."""

import pytest

from foragent import merge_owner, merge_owner_batch as batch, orch
from test_merge_owner_batch import batched, queue_workers, refs  # noqa: F401
from test_merge_owner_proto import gate, proto, world  # noqa: F401


@pytest.mark.parametrize("boundary", ["member-check", "before-push"])
def test_stop_keeps_the_train_and_release_lands_it(batched, monkeypatch, boundary):
    case = batched
    queue_workers(case, [5, 1])
    before = case.history.tip
    stops = []

    def stop_once():
        if not stops:
            stops.append(orch.stop_all(case.ctx, "Keep the unfinished train"))

    with monkeypatch.context() as patch:
        if boundary == "member-check":
            ready = merge_owner._ready

            def stopped_ready(*args, **kwargs):
                stop_once()
                return ready(*args, **kwargs)

            patch.setattr(merge_owner, "_ready", stopped_ready)
        else:
            validate = batch._validate_trees

            def stopped_validate(*args, **kwargs):
                result = validate(*args, **kwargs)
                stop_once()
                return result

            patch.setattr(batch, "_validate_trees", stopped_validate)
        gate(case)

    assert stops, "Exercise a stop during the final train publication"
    assert case.history.tip == before and case.target_attempts == []
    assert refs(case), "The published train remains a recoverable Git root"
    assert len(merge_owner.admissions(case.store)) == 2
    assert not batch.retirements(case.ctx), "A stop defers the train; it does not discard or spend it"
    assert not case.store.owner_merge_facts()
    orch.release(case.ctx)
    gate(case)
    assert len(case.target_pushes) == 1 and len(case.store.owner_merge_facts()) == 2
    assert not refs(case) and not merge_owner.admissions(case.store)
