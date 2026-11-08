

def test_issue_records_accepts_merge_batch_members_as_numbers():
    from foragent.orch_scorecard_four import issue_records
    rows = [{"issue": None, "data": {"members": [7, 8]}}, {"issue": 9, "data": {}},
            {"issue": None, "data": {"issues": [{"n": 7}]}}]
    assert issue_records(rows, 7) == [rows[0], rows[2]]
    assert issue_records(rows, 9) == [rows[1]]
