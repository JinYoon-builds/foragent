"""Acceptance test for night-publish-recovery. Locked by Foragent; do not edit."""

import pytest

from foragent.tracker import TrackerError
from test_order import plan, publishing
from test_order_preread import preread_case
from test_order_republish import publish, published_issues, republish_case
from test_skills_autosync import isolated_home


@pytest.mark.parametrize("interruption", ["create-response", "parent-map", "native-link"])
def test_partial_publish_recovers_exact_children_by_marker(republish_case, monkeypatch, capsys, interruption):
    case = republish_case
    fired = []
    create, update, link = case.tracker.create_issue, case.tracker.update_issue, case.tracker.add_sub_issue

    def create_once(title, body, labels):
        number = create(title, body, labels)
        if interruption == "create-response" and "foragent:plan-child" in body and not fired:
            fired.append(number)
            raise TrackerError("offline: child created, response lost")
        return number

    def update_once(number, **kwargs):
        children = [i for i in case.tracker.list_issues() if "foragent:plan-child" in i.body]
        if interruption == "parent-map" and children and not fired:
            fired.append(children[0].number)
            raise TrackerError("offline: parent map not recorded")
        return update(number, **kwargs)

    def link_once(parent, child):
        if interruption == "native-link" and not fired:
            fired.append(child)
            raise TrackerError("offline: native link not recorded")
        return link(parent, child)

    monkeypatch.setattr(case.tracker, "create_issue", create_once)
    monkeypatch.setattr(case.tracker, "update_issue", update_once)
    monkeypatch.setattr(case.tracker, "add_sub_issue", link_once)
    code, message = publish(case, capsys)
    assert code != 0 and fired, message
    survivors = {i.number for i in case.tracker.list_issues() if "foragent:plan-child" in i.body}
    assert survivors
    for _ in range(2):
        code, message = publish(case, capsys)
        assert code == 0, message
        parent, children = published_issues(case)
        assert survivors <= set(children.values())
        assert len(case.tracker.list_issues()) == 1 + len(case.plan["issues"])
        assert {i.number for i in case.tracker.sub_issues(parent)} == set(children.values())
        for item in case.plan["issues"]:
            marker = f"<!-- foragent:plan-child id={case.plan['id']} key={item['key']} -->"
            matches = [i for i in case.tracker.list_issues() if marker in i.body]
            assert [i.number for i in matches] == [children[item["key"]]]
            assert case.tracker.parent_of(matches[0].number) == parent
        assert [i.number for i in case.tracker.blocked_by(children["client"])] == [children["api"]]
