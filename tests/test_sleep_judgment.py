"""Gate judgments read the sleep ledger with their own now: a suspend mid-tick is not run time (#415)."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))

import pytest  # noqa: E402

from foragent import capacity, sleep_ledger  # noqa: E402
from test_machine_sleep import NIGHT, assert_running, sleep_case, tick_case  # noqa: E402


def suspend_after_first_reading(case, monkeypatch):
    read = sleep_ledger.read
    armed = [True]

    def read_then_suspend(*args, **kwargs):
        snapshot = read(*args, **kwargs)
        if armed:
            armed.clear()
            case.facts.advance(NIGHT, asleep=True)  # The machine sleeps right after the tick's first reading.
        return snapshot

    monkeypatch.setattr(sleep_ledger, "read", read_then_suspend)


@pytest.mark.parametrize("limit", ["runtime", "stall"])
def test_a_suspend_after_ledger_observation_is_not_charged(git_history, tmp_path, monkeypatch, limit):
    config = {"stall_minutes": 2000} if limit == "runtime" else {"max_runtime_minutes": 2000}
    case = sleep_case(git_history, tmp_path, monkeypatch, **config)
    case.facts.advance(30 * 60 if limit == "runtime" else 60)
    case.store.update_run(1, last_progress_at=case.clock[0].isoformat())
    suspend_after_first_reading(case, monkeypatch)
    report = tick_case(case)
    assert sum("slept" in action for action in report.actions) == 1, "The judgment sees the suspend once"
    assert_running(case, f"30 min work plus a suspend mid-tick must not trip the {limit} limit")


def test_a_pre_upgrade_run_record_gets_credit_for_30_min_work_and_8_h_sleep(git_history, tmp_path, monkeypatch):
    case = sleep_case(git_history, tmp_path, monkeypatch, stall_minutes=2000)
    # Upgrade: the run record exists, the ledger does not yet; the gate's next tick starts it.
    for name in (sleep_ledger.LEDGER_FILE, sleep_ledger.STATE_FILE):
        (capacity.machine_root() / name).unlink(missing_ok=True)
    tick_case(case)
    case.facts.advance(30 * 60)
    case.store.update_run(1, last_progress_at=case.clock[0].isoformat())
    case.facts.advance(NIGHT, asleep=True)
    tick_case(case)
    assert_running(case, "30 min work plus 8 h sleep fits a 90 min limit for any run record")
