"""Publishing binds work to its orderer, independently of who approves it."""
from copy import deepcopy
import json
import pytest

from foragent import order
from foragent.config import Config
from foragent.tracker import FakeTracker, GhTracker, TrackerError
from test_order import PublishingGit
from team_v1_support import person


def small_plan():
    return {"id": "team-identity", "title": "An order", "summary": "Owned work",
            "spec_markdown": "# Owned work\n", "issues": [
                {"key": "one", "title": "One", "goal": "Create one file.",
                 "out_of_scope": "Other files.", "done_when": ["true"],
                 "lane": "main", "size": "s", "files": ["one.py"]}]}


@pytest.mark.parametrize("label_failure", [None, "ensure", "create"])
def test_publication_and_republication_keep_original_orderer(tmp_path, monkeypatch, label_failure):
    events = []
    class LabelTracker(FakeTracker):
        def ensure_labels(self, labels):
            labels = list(labels)
            if label_failure == "ensure" and any(label.name.startswith("by:") for label in labels):
                raise TrackerError("Owner label permission denied")
            return super().ensure_labels(labels)

        def create_issue(self, title, body, labels):
            labels = list(labels)
            if label_failure == "create" and "by:alice" in labels:
                raise TrackerError("Owner label permission denied")
            return super().create_issue(title, body, labels)

    tracker = person(LabelTracker(actor="reviewer"), monkeypatch, "alice")
    git = PublishingGit(tmp_path, events)
    config, plan = Config(skill_libraries=[]), small_plan()
    if label_failure:
        with pytest.raises((TrackerError, order.PlanError), match="(?i)(label|permission)"):
            order.publish(plan, tmp_path, config, tracker, repo="owner/repo", git=git)
        assert not tracker.list_issues(), "Failed owner labelling exposed unlabelled legacy work"
        return
    order.publish(plan, tmp_path, config, tracker, repo="owner/repo", git=git)
    child = next(i for i in tracker.list_issues() if "foragent:plan-child" in i.body)
    assert child.labels & {"by:alice", "by:reviewer"} == {"by:alice"}
    parent = next(i for i in tracker.list_issues() if "foragent:plan id=" in i.body)
    assert "by:alice" in parent.labels
    # A different publisher may retry the same order but cannot silently adopt it.
    person(tracker, monkeypatch, "bob")
    order.publish(deepcopy(plan), tmp_path, config, tracker, repo="owner/repo", git=git)
    assert len(tracker.list_issues()) == 2
    assert {x for x in tracker.get_issue(child.number).labels if x.startswith("by:")} == {"by:alice"}


@pytest.mark.parametrize("switch_account", [False, True])
def test_machine_identity_uses_one_fake_api_read_across_tracker_instances(tmp_path, monkeypatch, switch_account):
    # Environment credentials are a public gh input; no cache format is prescribed.
    monkeypatch.setenv("GH_TOKEN", "fake-alice-credential")
    principal = ["Alice"]
    calls = []
    def gh(args, input=None):
        calls.append(args)
        assert args == ["api", "user"], args
        return json.dumps({"login": principal[0]})
    # Existing GhTracker injection point; never starts gh or accesses the network.
    first, second = GhTracker("owner/repo", run=gh), GhTracker("owner/other", run=gh)
    lookup = getattr(first, "authenticated_login", None)
    assert callable(lookup), "The tracker needs a cached authenticated_login() fact"
    assert lookup() == "alice"
    assert second.authenticated_login() == "alice"
    assert calls == [["api", "user"]]
    if switch_account:
        principal[0] = "Bob"
        monkeypatch.setenv("GH_TOKEN", "fake-bob-credential")
        assert GhTracker("owner/repo", run=gh).authenticated_login() == "bob"
        assert len(calls) == 2, "Account changes must not reuse Alice's cached principal"
