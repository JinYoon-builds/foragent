"""Acceptance test for listing-sanity. Locked by Foragent; do not edit."""

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from test_local_truth import local_rows, record_gh, snapshot_case
from test_order import plan, publishing
from test_publish_out_of_scope import publish_cli
from test_skills_autosync import cli_exit, isolated_home
from test_tracker import issue_json


def scripted_listings(monkeypatch, state, *responses):
    """Script only issue-list GETs at the real gh subprocess boundary."""
    remaining = list(responses)

    def reply(argv):
        assert argv[0] == "api" and "--paginate" in argv, argv
        for flag in ("-X", "--method"):
            assert flag not in argv or argv[argv.index(flag) + 1] == "GET", argv
        endpoint = urlsplit(next(arg for arg in argv if arg.startswith("repos/owner/repo/")))
        assert endpoint.path == "repos/owner/repo/issues", argv
        assert parse_qs(endpoint.query)["state"] == [state], argv
        assert remaining, f"Unexpected extra listing attempt: {argv}"
        return json.dumps(remaining.pop(0))

    return record_gh(monkeypatch, reply=reply)


def test_listing_sanity_both_listing_methods_retry_once_and_return_recovered_issues(monkeypatch):
    from foragent.tracker import GhTracker, Issue

    observed = []
    expected = [Issue(150, "Issue 150", "body", frozenset({"approved"}), "open")]
    for method, state in (("list_issues", "all"), ("list_open_issues", "open")):
        with monkeypatch.context() as patch:
            calls = scripted_listings(patch, state, [], [issue_json(150)])
            issues = getattr(GhTracker("owner/repo"), method)()
            observed.append((method, issues, len(calls)))
    assert observed == [("list_issues", expected, 2), ("list_open_issues", expected, 2)]


def test_listing_sanity_repeated_empty_raises_from_last_nonempty_listing(monkeypatch):
    from foragent.tracker import GhTracker, TrackerError

    calls = scripted_listings(monkeypatch, "all", [issue_json(150)], [], [])
    tracker = GhTracker("owner/repo")
    assert [issue.number for issue in tracker.list_issues()] == [150]
    assert len(calls) == 1, "A nonempty listing needs no retry"
    with pytest.raises(TrackerError, match="empty listing"):
        tracker.list_issues()
    assert len(calls) == 3, "Retry an anomalous empty listing exactly once"


def test_listing_sanity_fresh_tracker_uses_persisted_local_evidence(
    git_history, tmp_path, monkeypatch,
):
    from foragent.state import StateStore
    from foragent.tracker import GhTracker, TrackerError

    record_gh(monkeypatch)
    case = snapshot_case(git_history, tmp_path, monkeypatch)

    def for_repo(cls, repo, **kwargs):
        assert repo == "owner/repo"
        return cls(case.store.root, **kwargs)

    monkeypatch.setattr(StateStore, "for_repo", classmethod(for_repo))
    calls = scripted_listings(monkeypatch, "open", [], [])
    # This new tracker has never seen a nonempty listing. Only the gate's
    # persisted records can establish that this repository has open issues.
    with pytest.raises(TrackerError, match="empty listing"):
        GhTracker("owner/repo").list_open_issues()
    assert len(calls) == 2
    assert local_rows(StateStore(case.store.root))[1]["state"] == "open"


def test_listing_sanity_first_empty_repo_without_evidence_returns_empty_after_retry(monkeypatch):
    from foragent.tracker import GhTracker

    observed = []
    for method, state in (("list_issues", "all"), ("list_open_issues", "open")):
        with monkeypatch.context() as patch:
            calls = scripted_listings(patch, state, [], [])
            tracker = GhTracker("owner/repo")
            observed.append((method, getattr(tracker, method)(), len(calls)))
    assert observed == [("list_issues", [], 2), ("list_open_issues", [], 2)], (
        "A first empty listing without local evidence is valid, but still needs one retry"
    )


def test_listing_sanity_publish_reports_empty_listing_and_creates_no_duplicate_parent(
    publishing, plan, monkeypatch, capsys,
):
    from foragent.tracker import GhTracker

    root, config, tracker, git, events = publishing
    marker = f"<!-- foragent:plan id={plan['id']} map={{}} acceptance={{}} -->"
    parent = tracker.add_issue(150, plan["title"], marker)
    calls = scripted_listings(monkeypatch, "all", [issue_json(
        parent.number, title=parent.title, body=parent.body, labels=[],
    )], [], [])
    transport = GhTracker("owner/repo")
    assert [issue.number for issue in transport.list_issues()] == [parent.number]
    # Keep publication's offline Git and mutation fakes, but exercise the real
    # listing adapter instead of making a fake raise the error under test.
    monkeypatch.setattr(tracker, "list_issues", transport.list_issues)
    main = publish_cli((root, config, tracker, git, events), monkeypatch)
    path = root / "PLAN.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    code = cli_exit(main, ["order", "publish", str(path), "--repo-root", str(root)])
    captured = capsys.readouterr()
    parents = [issue.number for issue in tracker.issues.values()
               if f"<!-- foragent:plan id={plan['id']} " in issue.body]
    assert code != 0, f"Publish continued after an untrusted listing; parent issues: {parents}"
    assert len(captured.err.splitlines()) == 1 and "empty listing" in captured.err
    assert "Traceback" not in captured.err and captured.out == ""
    assert parents == [parent.number], "An untrusted listing must not create a duplicate parent"
    assert events == [], "Publication must stop before Git writes or issue creation"
    assert len(calls) == 3
