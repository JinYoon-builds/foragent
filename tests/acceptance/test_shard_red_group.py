"""Locked shard-red group acceptance; do not edit during implementation.

Reject converting usable red combined-run evidence into void infrastructure.
The public runner/FIFO/group path uses existing real Git and external CI fakes;
only the completed measurement run's external conclusion changes to failure.
"""

import pytest

import test_ci_group_evidence as groups
from test_merge_group import (  # noqa: F401
    contend, group_case, isolated_home, offline_acceptance, publish_case,
)


@pytest.mark.parametrize("result", ["failed", "void"], ids=["red-lost-is-failed", "red-unusable-is-void"])
def test_red_combined_run_is_judged_from_its_artifacts(group_case, monkeypatch, result):
    original = groups.GitHubCI.reports

    def red_reports(transport, row):
        values = original(transport, row)
        if row.group:
            for value in values:
                value["run"]["conclusion"] = "failure"
        return values

    monkeypatch.setattr(groups.GitHubCI, "reports", red_reports)
    case = group_case
    ci = groups.configure(case, monkeypatch, result=result)
    contend(case)
    joint, = [row for row in ci.rounds if row.group]
    record, = [row for row in groups.group_records(case)
               if [member["n"] for member in row["members"]] == list(groups.MEMBERS)]
    assert record["verdict"] == ("failed" if result == "failed" else "void"), record
    if result == "failed":
        lost = sorted({node for item in joint.reports
                       for node in set(item["report"]["base"]["passing_ids"])
                       - set(item["report"]["head"]["passing_ids"])})
        assert lost and all(node in str(record) for node in lost), record
        assert record["run_ids"] == [joint.run_id] and record["reports_sha256"]
    groups.assert_finished(case)
    assert all(merge.head != joint.head for merge in case.merges)
    for merge in case.merges:
        assert ci.by_head[merge.head].base == merge.base
        assert any(actor == merge.issue and head == merge.head for actor, head, _ in ci.reads)
