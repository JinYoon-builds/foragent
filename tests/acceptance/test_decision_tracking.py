"""Order acceptance: decision links are published facts, never inferred implementation."""

import json
from pathlib import Path

import pytest

from foragent import order
from foragent.config import Config
from foragent.tracker import FakeTracker, Issue
from test_order import PublishingGit
from test_order_progress import offline_local_cli
from test_skills_autosync import cli_exit


def small_plan():
    return {"id": "decision-fixture", "title": "Fixture", "summary": "Fixture order",
            "spec_markdown": "# Fixture\nD79\n", "issues": [{
                "key": "principles", "title": "Principles", "lane": "main", "size": "s",
                "goal": "Expose approved principles.", "out_of_scope": "Procedures.",
                "done_when": ["true"], "decisions": ["D79", "D79"],
                "acceptance_files": {}}], "edits": []}


def test_publication_retains_decision_marker_and_republication_replaces_it(tmp_path):
    config, tracker, git = Config(skill_libraries=[]), FakeTracker(), PublishingGit(tmp_path, [])
    plan = small_plan()
    for decisions in (["D79", "D79"], ["D71"]):
        plan["issues"][0]["decisions"] = decisions
        order.publish(plan, tmp_path, config, tracker, repo="offline/decisions", git=git)
        children = [row for row in tracker.list_issues() if "foragent:plan-child" in row.body]
        assert len(children) == 1, "Republication updates the same linked issue"
        markers = [line for line in children[0].body.splitlines()
                   if line.startswith("<!-- foragent:decisions ")]
        assert markers == ["<!-- foragent:decisions " + json.dumps(sorted(set(decisions))) + " -->"]
    number = children[0].number
    del plan["issues"][0]["decisions"]
    order.publish(plan, tmp_path, config, tracker, repo="offline/decisions", git=git)
    assert '<!-- foragent:decisions ["D71"] -->' in tracker.get_issue(number).body
    edit = {key: plan["issues"][0][key] for key in ("goal", "out_of_scope", "done_when")}
    edit.update(number=number, decisions=["D82"])
    plan.update(id="decision-edit-fixture", issues=[], edits=[edit])
    order.publish(plan, tmp_path, config, tracker, repo="offline/decisions", git=git)
    body = tracker.get_issue(number).body
    assert '<!-- foragent:decisions ["D82"] -->' in body
    assert '<!-- foragent:decisions ["D71"] -->' not in body
    del edit["decisions"]
    order.publish(plan, tmp_path, config, tracker, repo="offline/decisions", git=git)
    assert '<!-- foragent:decisions ["D82"] -->' in tracker.get_issue(number).body
    edit["decisions"] = []
    order.publish(plan, tmp_path, config, tracker, repo="offline/decisions", git=git)
    assert '<!-- foragent:decisions [] -->' in tracker.get_issue(number).body
    assert '<!-- foragent:decisions ["D82"] -->' not in tracker.get_issue(number).body


@pytest.mark.parametrize("invalid", ["D79", [79], ["D790x"], ["D79-1"]])
def test_invalid_decision_ids_are_rejected_before_publication(tmp_path, invalid):
    plan = small_plan()
    plan["issues"][0]["decisions"] = invalid
    tracker, git = FakeTracker(), PublishingGit(tmp_path, [])
    with pytest.raises(order.PlanError):
        order.publish(plan, tmp_path, Config(skill_libraries=[]), tracker,
                      repo="offline/decisions", git=git)
    assert not tracker.list_issues() and not git.events


def test_decisions_command_combines_markers_and_published_legacy_specs_and_tracks_state(
    tmp_path, monkeypatch, capsys,
):
    case = offline_local_cli(tmp_path, monkeypatch)
    docs = case.root / "docs"
    (docs / "specs").mkdir(parents=True)
    (docs / "decisions.md").write_text("# Decisions\n" + "".join(
        f"### D{n}. Decision {n} — 미구현\n" for n in (7, 71, 74, 79, 82)))
    (docs / "specs/legacy.md").write_text("# Published legacy order\nD71-2 and D74; D740 is a different ID.\n")
    (docs / "specs/unpublished.md").write_text("# Unpublished draft\nD82\n")
    marker = '<!-- foragent:decisions ["D79"] -->'
    rows = [
        Issue(11, "First", marker, frozenset(), "closed", state_reason="completed"),
        Issue(12, "Second", marker, frozenset(), "open"),
        Issue(13, "Legacy", "<!-- foragent:plan-child id=legacy key=work -->", frozenset(),
              "closed", state_reason="completed"),
        Issue(14, "Cancelled", '<!-- foragent:decisions ["D74"] -->', frozenset(),
              "closed", state_reason="not_planned"),
        Issue(15, "Unrelated D7 in a title", "D7 in ordinary prose", frozenset(), "open"),
        Issue(16, "Explicitly unlinked legacy child",
              '<!-- foragent:plan-child id=legacy key=other -->\n<!-- foragent:decisions [] -->',
              frozenset(), "open"),
    ]
    observed = case.store._now()
    case.store.refresh_local_issues(rows, [], {}, observed)

    def read():
        before = {p: p.read_bytes() for p in case.store.root.rglob("*") if p.is_file()}
        capsys.readouterr()
        code = cli_exit(case.main, ["decisions", "--json", "--repo-root", str(case.root)])
        output = capsys.readouterr()
        assert code == 0, output.err
        assert {p: p.read_bytes() for p in case.store.root.rglob("*") if p.is_file()} == before
        assert case.gh_calls == [], "Decision status reads local recorded facts"
        return {row["id"]: row for row in json.loads(output.out)}

    result = read()
    assert set(result) == {"D7", "D71", "D74", "D79", "D82"}
    assert result["D79"]["issues"] == [11, 12] and result["D79"]["state"] == "in progress"
    assert result["D71"]["issues"] == [13] and result["D71"]["state"] == "done"
    assert result["D71"]["observed_at"] == observed.isoformat()
    assert read()["D71"]["observed_at"] == observed.isoformat(), "Reads do not renew observation time"
    assert result["D74"]["issues"] == [13, 14] and result["D74"]["state"] == "in progress"
    for identity in ("D7", "D82"):
        assert result[identity]["issues"] == []
        assert result[identity]["state"] == "decided, nothing ordered"
    case.store.update_local_issue(12, state="closed", state_reason="completed")
    assert read()["D79"]["state"] == "done", "Every linked issue must finish"
    case.store.update_local_issue(12, state="closed", state_reason=None, merged=True)
    assert read()["D79"]["state"] == "done", "An observed merge also proves completion"
    case.store.update_local_issue(12, state="open", state_reason=None, merged=True)
    assert read()["D79"]["state"] == "in progress", "Reopening outranks an old merged PR"
    case.store.update_local_issue(12, state="closed", state_reason="completed", merged=True)
    case.store.update_local_issue(11, state="open", state_reason=None)
    assert read()["D79"]["state"] == "in progress", "Reopening changes the derived state"
    case.store.update_local_issue(11, state="closed", state_reason=None)
    assert read()["D79"]["state"] == "in progress", "Unknown closure reason does not prove completion"


def test_missing_ledger_is_distinguishable_from_a_readable_empty_ledger(tmp_path, monkeypatch, capsys):
    case = offline_local_cli(tmp_path, monkeypatch)
    capsys.readouterr()
    missing = cli_exit(case.main, ["decisions", "--json", "--repo-root", str(case.root)])
    missing_output = capsys.readouterr()
    (case.root / "docs").mkdir()
    (case.root / "docs/decisions.md").write_text("# No decisions yet\n")
    empty = cli_exit(case.main, ["decisions", "--json", "--repo-root", str(case.root)])
    empty_output = capsys.readouterr()
    assert empty == 0 and json.loads(empty_output.out) == []
    assert missing != 0 or missing_output.err.strip(), "Unavailable input cannot look like an empty ledger"


def test_repository_decision_headings_have_no_hand_written_unimplemented_status():
    root = Path(order.__file__).resolve().parents[2]
    headings = [line for line in (root / "docs/decisions.md").read_text().splitlines()
                if line.startswith("### D")]
    assert {line.split(".", 1)[0].removeprefix("### ") for line in headings} >= {
        f"D{n}" for n in range(1, 98)
    }, "Keep the 97 decisions present in this snapshot; D98 is supplied with this order"
    assert not any("미구현" in line for line in headings), "Remove stale suffixes with extra notes too"
