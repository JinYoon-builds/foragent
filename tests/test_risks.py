"""Risk map parsing, appending and the static candidate inventory."""

from datetime import UTC, datetime

import pytest
import yaml

from foragent import risks
from foragent.policy import merge_decision
from foragent.config import Config
from foragent.reasons import Reason


def make(**overrides):
    values = dict(paths=["src/x/**"], why="w", incident_date="2026-10-24", incident="i", guidance="g", by="me",
                  now=datetime(2026, 10, 25, tzinfo=UTC))
    return risks.entry(**{**values, **overrides})


def test_missing_or_comment_only_map_is_empty(tmp_path):
    assert risks.load(tmp_path) == []
    (tmp_path / risks.RISK_FILE).write_text("# only a comment\n")
    assert risks.load(tmp_path) == []


@pytest.mark.parametrize("text", ["paths: [a]\n", "- why: no paths\n", "- paths: []\n", "- paths: [a\n"])
def test_invalid_map_is_an_error(tmp_path, text):
    (tmp_path / risks.RISK_FILE).write_text(text)
    with pytest.raises(risks.RiskError):
        risks.load(tmp_path)


def test_append_keeps_bytes_and_adds_a_line_break_in_the_file_style(tmp_path):
    path = tmp_path / risks.RISK_FILE
    path.write_bytes(b"- paths: [a]\r\n# tail")
    risks.append(tmp_path, make())
    after = path.read_bytes()
    assert after.startswith(b"- paths: [a]\r\n# tail\r\n") and b"\n" not in after.replace(b"\r\n", b"")
    loaded = yaml.safe_load(after)
    assert loaded[1]["incident"]["date"] == "2026-10-24" and loaded[1]["added"]["event"] is None


def test_entry_rejects_bad_dates_and_missing_guard_tests(tmp_path):
    with pytest.raises(risks.RiskError):
        make(incident_date="yesterday")
    with pytest.raises(risks.RiskError):
        make(guard_tests=["tests/missing.py"], root=tmp_path)


def test_notes_only_for_matching_entries_and_tier_options():
    entries = [make(paths=["src/x/**"], why="X-WHY"), make(paths=["docs/**"], why="DOC-WHY")]
    matched = risks.matching(entries, ["src/x/a.py"])
    notes = risks.review_notes(matched, ["src/x/a.py"])
    assert "X-WHY" in notes and "DOC-WHY" not in notes and risks.ADDITIVE_REVIEW in notes
    assert risks.review_notes([], ["src/x/a.py"]) == ""
    config = Config()
    assert risks.review_options(config, "codex", "m") == {"model": "m", "effort": config.tiers["l"]["codex_effort"]}
    assert risks.review_options(config, "claude", "m") == {"model": config.tiers["l"]["claude_model"] or "m"}


def test_risk_always_human_holds_like_config():
    decision = merge_decision(changed_paths=["src/x/a.py"], issue_labels=[], config=Config(),
                              risk_always_human=["src/x/**"])
    assert decision.reason == Reason.ALWAYS_HUMAN_PATH
    assert merge_decision(changed_paths=["src/y.py"], issue_labels=[], config=Config(),
                          risk_always_human=["src/x/**"]).reason is None


def test_scan_skips_prose_and_lookalikes(tmp_path):
    (tmp_path / "a.py").write_text("os.killpg(1, 9)\nmy_os.kill(1)\nsubprocess.run(['git', 'status'])\n")
    (tmp_path / "b.md").write_text("os.kill(1, 9)\n")
    found = risks.scan(tmp_path, ["a.py", "b.md"])
    assert found == [{"path": "a.py", "line": 1, "pattern": "os.killpg"}]


def test_notes_carry_always_human_and_added_trace():
    risk = make(always_human=True, event="incident:x")
    notes = risks.review_notes([risk], ["src/x/a.py"])
    assert "Always human: yes" in notes
    assert "Added by me at 2026-10-25T00:00:00+00:00 (event: incident:x)" in notes
    assert "Always human: no" in risks.review_notes([make()], ["src/x/a.py"])


def test_refresh_notes_replaces_appends_and_drops_the_block():
    body = "## Review scope\n\nrules"
    first = risks.refresh_notes(body, "OLD")
    assert first == body + "\n\n" + risks.notes_block("OLD")
    second = risks.refresh_notes(first + "\n\n## Try it", "NEW")
    assert "OLD" not in second and second.count(risks.NOTES_START) == 1 and second.endswith("## Try it")
    assert risks.refresh_notes(first, "") == body
    assert risks.refresh_notes(body, "") == body


@pytest.mark.parametrize("before", [b"[]\n", b"# map\n[]", b"[{paths: [a], why: w}]\n"])
def test_append_to_flow_style_map_keeps_entries_and_comments(tmp_path, before):
    path = tmp_path / risks.RISK_FILE
    path.write_bytes(before)
    existing = yaml.safe_load(before)
    risks.append(tmp_path, make())
    after = path.read_bytes()
    loaded = yaml.safe_load(after)
    assert loaded[:-1] == existing and loaded[-1]["paths"] == ["src/x/**"]
    assert all(line in after for line in before.splitlines() if line.startswith(b"#"))


def test_errors_use_the_messages_registry():
    from foragent.i18n import text

    with pytest.raises(risks.RiskError) as caught:
        make(why=" ")
    assert caught.value.key == "risk_blank_options" and str(caught.value) == text(
        "risk_blank_options", "en", options="--why")
    assert "--why" in text(caught.value.key, "ko", **caught.value.values)
