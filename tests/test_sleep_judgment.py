"""Gate judgments use the ledger's own moment: a suspend after observing it is not run time (#415)."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))

import pytest  # noqa: E402

from foragent import sleep_ledger  # noqa: E402
from test_machine_sleep import NIGHT, assert_running, sleep_case, tick_case  # noqa: E402


def suspend_after_observing(case, monkeypatch):
    observe = sleep_ledger.observe

    def observed_then_suspended(ctx, report):
        ledger = observe(ctx, report)
        case.facts.advance(NIGHT, asleep=True)  # The machine sleeps right after the ledger read.
        return ledger

    monkeypatch.setattr(sleep_ledger, "observe", observed_then_suspended)


@pytest.mark.parametrize("limit", ["runtime", "stall"])
def test_a_suspend_after_ledger_observation_is_not_charged(git_history, tmp_path, monkeypatch, limit):
    config = {"stall_minutes": 2000} if limit == "runtime" else {"max_runtime_minutes": 2000}
    case = sleep_case(git_history, tmp_path, monkeypatch, **config)
    case.facts.advance(30 * 60 if limit == "runtime" else 60)
    case.store.update_run(1, last_progress_at=case.clock[0].isoformat())
    suspend_after_observing(case, monkeypatch)
    report = tick_case(case)
    assert sum("slept" in action for action in report.actions) == 1, "The judgment observes the suspend once"
    assert_running(case, f"30 min work plus a suspend after observation must not trip the {limit} limit")
