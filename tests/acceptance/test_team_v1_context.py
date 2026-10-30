"""The interview receives overlap facts and leaves the decision to the person."""
import json

from foragent import cli
from foragent.config import Config
from foragent.tracker import FakeTracker
from team_v1_support import legacy, person


def test_order_context_lists_foreign_files_without_deciding_overlap(tmp_path, monkeypatch, capsys):
    tracker = person(FakeTracker(), monkeypatch, "alice")
    config = legacy(Config(repo="owner/repo", skill_libraries=[]))
    body = "<!-- foragent:v1 -->\n## Goal\nChange things.\n## Files\n- src/shared.py\n- ui/**\n## Out of scope\nRest.\n## Done when\n```sh\ntrue\n```\n"
    tracker.add_issue(1, "Bob's change", body, labels=["by:bob"])
    tracker.add_issue(2, "My change", body, labels=["by:alice"])
    tracker.add_issue(3, "Old work", body)
    tracker.add_issue(4, "Closed work", body, labels=["by:bob"], state="closed")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda root: config)
    monkeypatch.setattr(cli, "GhTracker", lambda repo: tracker)
    assert cli.main(["order", "context", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload.get("other_orders") == [
        {"number": 1, "title": "Bob's change", "orderer": "bob", "files": ["src/shared.py", "ui/**"]}
    ], "The planning agent needs foreign Files as facts, with no wait/go decision"
