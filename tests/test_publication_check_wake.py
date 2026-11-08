"""A finished red check wakes the gate, and the owner pass it starts publishes every certified order."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent / "acceptance"))

from test_order import offline_publication  # noqa: E402,F401 (fixture)
from test_order_publications import case  # noqa: E402,F401 (fixture)
from test_wake_on_work import drain_owner, owner_kicks  # noqa: E402


def test_certified_publications_wake_the_owner_and_an_idle_check_does_not(case, monkeypatch):  # noqa: F811
    from foragent import merge_owner, order, order_publications
    from foragent.scheduler import TickReport

    (case.root / ".foragent.yml").write_text("repo: owner/repo\n", encoding="utf-8")
    order.publish(case.plan, case.root, case.config, case.tracker, repo=case.config.repo, git=case.git, approve=True)
    case.plan.update(id="second", title="Second", issues=[])
    order.publish(case.plan, case.root, case.config, case.tracker, repo=case.config.repo, git=case.git, approve=True)
    kicks = owner_kicks(monkeypatch, case.store)

    with merge_owner.waking(case.root):  # The red-check process's entry wraps its pass the same way.
        order_publications.check_pending(case.ctx, TickReport())
    assert kicks == [("owner/repo", True)], "Fresh certification must start the owner's pass now, once"
    with merge_owner.waking(case.root):
        order_publications.check_pending(case.ctx, TickReport())
    assert len(kicks) == 1, "A check pass that certified nothing new must not ask for another pass"

    del kicks[:]
    per_pass = drain_owner(monkeypatch, case.store, case.config, case.root, case.tracker, case.git, kicks)
    assert [row["status"] for row in order_publications.requests(case.store)] == ["published", "published"]
    assert len(per_pass) == 2 and per_pass[-1] == [], per_pass
