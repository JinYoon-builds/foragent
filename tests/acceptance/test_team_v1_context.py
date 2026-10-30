"""The interview receives overlap facts and leaves the decision to the person."""
import json
import pytest

from foragent import cli
from foragent.config import Config
from foragent.tracker import FakeTracker
from team_v1_support import legacy, person


@pytest.mark.parametrize("focus", ["overlap", "questions"])
def test_order_context_lists_foreign_files_without_deciding_overlap(tmp_path, monkeypatch, capsys, focus):
    tracker = person(FakeTracker(), monkeypatch, "alice")
    config = legacy(Config(repo="owner/repo", skill_libraries=[]))
    body = "<!-- foragent:v1 -->\n## Goal\nChange things.\n## Files\n- src/shared.py\n- ui/**\n## Out of scope\nRest.\n## Done when\n```sh\ntrue\n```\n"
    tracker.add_issue(1, "Bob's change", body, labels=["by:bob"])
    tracker.add_issue(2, "My change", body, labels=["by:alice"])
    tracker.add_issue(3, "Old work", body)
    tracker.add_issue(4, "Closed work", body, labels=["by:bob"], state="closed")
    from foragent.reasons import Reason, format_comment
    tracker.add_issue(5, "Bob's question", body, labels=["by:bob", "needs-human"])
    tracker.add_issue(6, "Alice's question", body, labels=["by:alice", "needs-human"])
    for number, login in ((5, "bob"), (6, "alice")):
        tracker.comment(number, format_comment(Reason.SPEC_QUESTION, "Choose behaviour", "@" + login + " choose an option"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda root: config)
    monkeypatch.setattr(cli, "GhTracker", lambda repo: tracker)
    assert cli.main(["order", "context", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    if focus == "overlap":
        assert payload.get("other_orders") == [
            {"number": 1, "title": "Bob's change", "orderer": "bob", "files": ["src/shared.py", "ui/**"]},
            {"number": 5, "title": "Bob's question", "orderer": "bob", "files": ["src/shared.py", "ui/**"]}
        ], "The planning agent needs foreign Files as facts, with no wait/go decision"
    assert [d["number"] for d in payload["decisions"]] == [6], "Ask only this factory's orderer"
    assert "@alice" in payload["decisions"][0]["question"]
