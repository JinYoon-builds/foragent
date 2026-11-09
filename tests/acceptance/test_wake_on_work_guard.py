"""Regression control moved out of test_wake_on_work.py (11-09, #2340 re-approval).

This statement already passes on main (the owner publishes every pending order in one pass and
kicks the next pass), so the publication check returns it from Done when; PR CI keeps guarding it.
"""

from test_skills_autosync import isolated_home  # noqa: F401 (fixture)
from test_tick_failure_alert import tick_case  # noqa: F401 (fixture)
from test_order import offline_publication  # noqa: F401 (fixture)
from test_order_publications import case  # noqa: F401 (fixture)
from test_wake_on_work import drain_owner, owner_kicks


def test_owner_publishing_a_queued_order_starts_the_next_pass_for_the_next_publication(case, monkeypatch):
    from foragent import order, order_publications

    (case.root / ".foragent.yml").write_text("repo: owner/repo\n", encoding="utf-8")
    order.publish(case.plan, case.root, case.config, case.tracker, repo=case.config.repo, git=case.git, approve=True)
    case.plan.update(id="second", title="Second", issues=[])
    order.publish(case.plan, case.root, case.config, case.tracker, repo=case.config.repo, git=case.git, approve=True)
    assert [row["status"] for row in order_publications.requests(case.store)] == ["pending", "pending"]
    # Also restores the publication kick the shared fixture silences.
    kicks = owner_kicks(monkeypatch, case.store)
    # Since main e3fd04ae a separate publication-check process certifies pending orders before the
    # owner may publish them; run one check pass so the owner sees what it would see on the machine.
    from datetime import UTC, datetime
    from foragent import runner
    from foragent.scheduler import TickReport
    checker = runner.RunContext(case.config.repo, case.root, case.tracker, case.store, case.config, case.git,
                                lambda: datetime.now(UTC))
    order_publications.check_pending(checker, TickReport())

    per_pass = drain_owner(monkeypatch, case.store, case.config, case.root, case.tracker, case.git, kicks)

    statuses = [row["status"] for row in order_publications.requests(case.store)]
    assert statuses == ["published", "published"], (
        f"After publishing one queued order the owner must start the next pass without the timer: "
        f"{statuses}, kicks per owner pass: {per_pass}"
    )
    assert all(repo == "owner/repo" for kicked in per_pass for repo, _ in kicked), per_pass
    # The owner applies every pending publication in one pass (main a15d8bf3): one publishing pass, one idle pass.
    assert len(per_pass) == 2, f"One publishing pass and one idle pass, not {per_pass}"
    assert per_pass[-1] == [], "An owner pass that published nothing must not ask for another pass"
