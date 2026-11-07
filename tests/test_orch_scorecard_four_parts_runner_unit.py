"""CI evidence of the worker publication paths in CI comparison mode (#2484).

The real runner runs through tests/acceptance/test_ci_mode.py's offline world; its
autouse fixtures live only in this module, so the other four-part tests keep theirs.
"""

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))
from test_state_split import offline_acceptance  # noqa: F401,E402
from test_skills_autosync import isolated_home  # noqa: F401,E402
from test_ci_mode import mode_case, run, ci_evidence_recording, no_external_execution  # noqa: F401,E402

from foragent import orch_scorecard_four as four  # noqa: E402
from foragent.orch_scorecard_records import _executions, _issues  # noqa: E402


def test_waiting_for_the_acceptance_reports_is_recorded_as_that_heads_ci(git_history, tmp_path, monkeypatch):
    from foragent.tracker import GhTracker

    case = mode_case(git_history, tmp_path, monkeypatch)
    reports, polls = GhTracker.pr_acceptance_reports, []

    def slow(self, head):
        polls.append(head)
        if len(polls) == 1:
            case.clock[0] += timedelta(minutes=3)  # The reports arrive 3 minutes after the checks.
        return reports(self, head)

    monkeypatch.setattr(GhTracker, "pr_acceptance_reports", slow)
    assert run(case) == "merged"
    head = case.tracker.pr_head_sha(2)
    runs = [row["data"] for row in case.store.flow_records()
            if row["kind"] == "activity" and row["data"].get("activity") == "ci"]
    assert [(row["head"], row.get("stage")) for row in runs] == [(head, "comparison")]
    now = case.clock[0] + timedelta(minutes=1)
    executions = [row for row in _executions(case.store, now) if row["number"] == 1]
    [day] = four.windows(case.store, now, 1, _issues(case.store, {}), executions)
    [entry] = day["issues"]
    assert entry["value_work"] >= 3, entry
