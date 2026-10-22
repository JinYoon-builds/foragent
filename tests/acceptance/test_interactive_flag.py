"""Acceptance test for interactive-flag. Locked by Foragent; do not edit."""

import json
from pathlib import Path

from test_local_truth import record_gh
from test_order import plan, publishing
from test_publish_out_of_scope import publish_cli
from test_skills_autosync import isolated_home


def interactive_plan(plan):
    """Cover true, explicit false and omitted independently in one order."""
    plan["issues"][0]["interactive"] = True
    plan["issues"][1]["interactive"] = False
    plan["issues"].append({**plan["issues"][1], "key": "default", "title": "Default background issue"})
    del plan["issues"][-1]["interactive"]
    return plan


def test_interactive_flag_publish_labels_only_the_opted_in_issue(publishing, plan, monkeypatch):
    root, _, tracker, _, _ = publishing
    main = publish_cli(publishing, monkeypatch)
    gh = record_gh(monkeypatch)
    path = root / "PLAN.json"
    path.write_text(json.dumps(interactive_plan(plan)), encoding="utf-8")
    assert main(["order", "publish", str(path), "--repo-root", str(root)]) == 0
    issues = {issue.title: issue for issue in tracker.list_issues()}
    assert "interactive" in issues["Login API"].labels
    assert "interactive" not in issues["Login client"].labels
    assert "interactive" not in issues["Default background issue"].labels
    assert gh == []


def test_interactive_flag_dry_run_shows_the_choice_without_publishing(
    publishing, plan, monkeypatch, capsys,
):
    root, _, tracker, _, events = publishing
    main = publish_cli(publishing, monkeypatch)
    gh = record_gh(monkeypatch)
    path = root / "PLAN.json"
    path.write_text(json.dumps(interactive_plan(plan)), encoding="utf-8")
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert main(["order", "publish", str(path), "--repo-root", str(root), "--dry-run"]) == 0
    output = capsys.readouterr().out
    rows = [line for line in output.splitlines() if line.startswith("|")]
    header = next((row for row in rows if "interactive" in row.lower() or "대화형" in row), None)
    assert header is not None, output
    columns = [cell.strip() for cell in header.strip("|").split("|")]
    index = next(i for i, cell in enumerate(columns) if "interactive" in cell.lower() or "대화형" in cell)
    values = {}
    for title in ("Login API", "Login client", "Default background issue"):
        row = next(row for row in rows if title in row)
        values[title] = row.strip("|").split("|")[index].strip()
    assert values["Login API"] and values["Login API"] != values["Login client"], output
    assert values["Login client"] == values["Default background issue"], output
    assert tracker.issues == {} and events == [] and gh == []
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_interactive_flag_order_skill_assigns_back_and_forth_to_interactive_and_suggests_design():
    path = Path(__file__).resolve().parents[2] / "skills/fa-order/SKILL.md"
    text = " ".join(path.read_text(encoding="utf-8").lower().split())
    assert "interactive" in text and "d72" in text, "Name the per-issue D72 interactive flag"
    assert any(word in text for word in ("back-and-forth", "back and forth", "주고받", "대화가 많"))
    assert "fa design open" in text or "fa-design-session" in text
    assert any(word in text for word in ("suggest", "offer", "propose", "제안"))
