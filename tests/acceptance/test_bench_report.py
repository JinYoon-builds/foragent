"""Acceptance: `fa bench report` compares arms A/B/C from recorded rows."""

import json
from pathlib import Path

from foragent import cli


def row(arm: str, elapsed: int, passed: int, retries, tokens) -> dict:
    return {"scenario": "words", "arm": arm, "started": "2026-10-20T08:00:00+00:00",
            "finished": "2026-10-20T09:00:00+00:00", "elapsed_seconds": elapsed,
            "checks_passed": passed, "checks_total": 4, "runs": None if retries is None else 2,
            "merged": None if retries is None else 2, "retries": retries,
            "tokens": tokens, "interventions": 0}


def scenario(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "scenario"
    path.mkdir()
    (path / "scenario.yml").write_text("name: words\nbrief: x\nchecks: ['true']\n", encoding="utf-8")
    (path / "results.jsonl").write_text("".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8")
    return path


ROWS = [row("A", 100, 2, None, 50000), row("A", 300, 4, None, 70000), row("B", 240, 4, 1, 200000)]


def test_json_report_uses_medians_and_keeps_missing_arm(tmp_path, capsys):
    assert cli.main(["bench", "report", str(scenario(tmp_path, ROWS)), "--json"]) == 0
    arms = json.loads(capsys.readouterr().out)["arms"]
    assert list(arms) == ["A", "B", "C"]
    assert arms["A"]["records"] == 2
    assert arms["A"]["elapsed_seconds"] == 200
    assert arms["A"]["checks_passed"] == 3 and arms["A"]["checks_total"] == 4
    assert arms["A"]["retries"] is None and arms["A"]["tokens"] == 60000
    assert arms["B"]["retries"] == 1
    assert arms["C"] == {"records": 0, "elapsed_seconds": None, "checks_passed": None, "checks_total": None,
                         "retries": None, "tokens": None, "interventions": None}


def test_text_report_is_a_markdown_table(tmp_path, capsys):
    assert cli.main(["bench", "report", str(scenario(tmp_path, ROWS))]) == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.startswith("|")]
    assert len(lines) == 5  # header, separator, A, B, C
    assert lines[2].startswith("| A ") and lines[4].startswith("| C ")
    assert "—" in lines[4]


def test_no_results_yet(tmp_path, capsys):
    assert cli.main(["bench", "report", str(scenario(tmp_path, [])), "--json"]) == 0
    arms = json.loads(capsys.readouterr().out)["arms"]
    assert all(arms[arm]["records"] == 0 for arm in "ABC")
