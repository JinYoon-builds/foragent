"""Boundary tests for reviewer NOTE lines and the break-it rule (#226) beyond the locked acceptance file."""

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))

from foragent import review_notes  # noqa: E402
from foragent.executors import ExecResult  # noqa: E402
from foragent.issue import parse_body  # noqa: E402
from foragent.run_previous import saved_review_section  # noqa: E402
from foragent.run_prompts import BREAK_IT, build_prompt, build_resume_prompt  # noqa: E402
from foragent.run_review import parse_code_review, parse_review  # noqa: E402
from foragent.runner import run_issue  # noqa: E402
from runner_support import BODY, enable_native_reviewer, enable_reviewer, run_case  # noqa: E402,F401
from test_review_parallel import PanelExecutor, install_panel  # noqa: E402
from test_review_perspectives import perspective_case, review_offline  # noqa: E402,F401
from test_risk_map import copy_history, offline  # noqa: E402,F401
from test_skills_autosync import isolated_home  # noqa: E402,F401

FINDING = "feature.txt:1: special-casing this input misses the actual goal"
NOTE = "The retry path parses the locale twice; extract one parser."


def notes(store, issue=1):
    path = store.root / "orch-events.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [event["summary"] for event in events if event["kind"] == "review-note" and event["issue"] == issue]


def flat(text):
    return " ".join(text.split())


def test_parse_review_keeps_notes_out_of_findings_and_an_all_note_fail_passes():
    review = parse_review(f"VERDICT: FAIL\n{FINDING}\nNOTE: {NOTE}")
    assert (review.verdict, review.findings, review.review_notes) == ("FAIL", FINDING, (NOTE,))
    only_notes = parse_review(f"VERDICT: FAIL\n- NOTE: {NOTE}\n")
    assert (only_notes.verdict, only_notes.findings) == ("PASS", "")
    assert parse_review("VERDICT: FAIL").verdict == "FAIL", "A FAIL without findings or notes still fails"


@pytest.mark.parametrize("text,verdict,findings", [
    (f"`feature.txt:1` — wrong value\nNOTE: {NOTE}", "FAIL", "`feature.txt:1` — wrong value"),
    (f"(none)\nNOTE: {NOTE}", "PASS", ""),
    (f"[]\nNOTE: {NOTE}", "PASS", ""),
])
def test_code_review_notes_are_plain_lines_beside_findings(text, verdict, findings):
    review = parse_code_review(json.dumps({"result": f"VERDICT: {verdict}\n{text}"}))
    assert (review.verdict, review.findings) == (verdict, findings)


def test_note_normalisation_dedups_whitespace_and_case():
    assert review_notes.extract(f"NOTE: {NOTE}", f"NOTE:   {NOTE.upper()}  ", "NOTE: other") == (NOTE, "other")
    assert review_notes.strip(f"a\nNOTE: {NOTE}\nb") == "a\nb"


def test_fail_with_note_retries_on_findings_only_and_records_note_once(run_case):
    c = run_case
    c.executor.steps.append({})
    calls = enable_reviewer(c, [f"VERDICT: FAIL\n{FINDING}\nNOTE: {NOTE}",
                                f"VERDICT: PASS\nNOTE:  {NOTE.lower()}"])
    assert run_issue(1, c.ctx) == "merged"
    assert len(calls) == 2
    retry = c.executor.calls[1]["prompt"]
    assert FINDING in retry and NOTE not in retry, "NOTE lines never reach the implementer"
    assert notes(c.store) == [NOTE], "The same normalised NOTE is recorded once per issue across rounds"
    body = c.tracker.created_prs[0].body
    assert f"NOTE: {NOTE}" in body, "The PR body keeps NOTE lines"
    assert NOTE in (c.store.root / "runs/issue-1/review-1.txt").read_text()
    assert NOTE in c.store.log_path(1).read_text(), "The run log keeps NOTE lines"


def test_unusable_review_still_records_its_notes_and_hands_off_without_them(run_case):
    c = run_case
    c.executor.steps.append({})
    raw = f"I could not decide.\nNOTE: {NOTE}"
    enable_reviewer(c, [raw, "VERDICT: PASS"])
    assert run_issue(1, c.ctx) == "merged"
    retry = c.executor.calls[1]["prompt"]
    assert "A reviewer said (verbatim):\nI could not decide." in retry and NOTE not in retry
    assert notes(c.store) == [NOTE]


def test_native_code_review_notes_are_recorded(run_case):
    c = run_case
    enable_native_reviewer(c, [ExecResult(0, False, "", Path("unused"),
                                          full_message=json.dumps({"result": f"VERDICT: PASS\n(none)\nNOTE: {NOTE}"}))])
    assert run_issue(1, c.ctx) == "merged"
    assert notes(c.store) == [NOTE]


def test_panel_records_every_members_notes_once(git_history, tmp_path, monkeypatch):
    case = perspective_case(git_history, tmp_path, monkeypatch, count=2)
    panel = PanelExecutor([f"VERDICT: PASS\nNOTE: {NOTE}", f"VERDICT: PASS\nNOTE: {NOTE.upper()}\nNOTE: second"],
                          merged=f"VERDICT: PASS\nNOTE: {NOTE}")
    install_panel(case, panel)
    assert run_issue(1, case.ctx) == "merged"
    recorded = notes(case.store)
    # Member order is concurrent, so compare normalised notes: each recorded exactly once.
    assert sorted(review_notes.normalize(note) for note in recorded) == sorted(
        review_notes.normalize(note) for note in (NOTE, "second"))


def test_saved_review_pasted_into_the_next_prompt_drops_notes(tmp_path):
    directory = tmp_path / "runs/issue-1"
    directory.mkdir(parents=True)
    (directory / "review-1.txt").write_text(f"VERDICT: FAIL\n{FINDING}\nNOTE: {NOTE}\n")
    section = saved_review_section(tmp_path, 1)
    assert FINDING in section and NOTE not in section


@pytest.mark.parametrize("options", [
    {}, {"failure": "boom"}, {"reviewer_text": "bad", "review_feedback": True},
    {"merge_conflicts": ["a.py"]}, {"retry_default": True},
])
def test_every_implementer_prompt_carries_the_break_it_rule(options):
    assert BREAK_IT in flat(build_prompt(1, parse_body(BODY), "tests/acceptance/", **options))
    assert BREAK_IT in flat(build_resume_prompt(1, BODY, BODY, **{
        key: value for key, value in options.items() if key != "failure"}))


class LimitPanel(PanelExecutor):
    """Reviewer 2 (or the findings merge) hits a usage limit after reviewer 1 wrote a NOTE."""

    def __init__(self, messages, *, at_merge):
        super().__init__(messages, merged="VERDICT: PASS")
        self.at_merge = at_merge

    def run(self, **kwargs):
        result = super().run(**kwargs)
        if (kwargs in self.merges) if self.at_merge else (kwargs in self.reviews[1:2]):
            return ExecResult(1, False, "usage limit reached", kwargs["log_path"], error_kind="limit")
        return result


@pytest.mark.parametrize("at_merge", [False, True], ids=["member-limit", "merge-limit"])
def test_panel_usage_limit_keeps_completed_members_notes(git_history, tmp_path, monkeypatch, at_merge):
    case = perspective_case(git_history, tmp_path, monkeypatch, count=2)
    install_panel(case, LimitPanel([f"VERDICT: PASS\nNOTE: {NOTE}", "VERDICT: PASS"], at_merge=at_merge))
    assert run_issue(1, case.ctx) == "limit-wait"
    assert notes(case.store) == [NOTE], "A limit that discards the round must not lose a member's NOTE"


def test_every_feedback_channel_drops_note_lines():
    hint = f"Fix the parser.\nNOTE: {NOTE}"
    fresh = build_prompt(1, parse_body(BODY), "tests/acceptance/", reviewer_text=f"bad\nNOTE: {NOTE}",
                         orchestrator_hint=hint, previous_attempt=f"Branch: x\nNOTE: {NOTE}")
    resumed = build_resume_prompt(1, BODY, BODY, reviewer_text=f"bad\nNOTE: {NOTE}", orchestrator_hint=hint)
    for prompt in (fresh, resumed):
        assert "Fix the parser." in prompt and "bad" in prompt and NOTE not in prompt


def limited(text, log_path=Path("unused")):
    return ExecResult(1, False, text, log_path, error_kind="limit")


def test_single_reviewer_limit_response_keeps_its_notes(run_case):
    c = run_case
    enable_reviewer(c, [limited(f"VERDICT: FAIL\nNOTE: {NOTE}\nusage limit reached")])
    assert run_issue(1, c.ctx) == "limit-wait"
    assert notes(c.store) == [NOTE], "The executor wrapper must not discard a limited response's NOTE"


class LimitWithNotePanel(PanelExecutor):
    """The limited call (reviewer 2, or the findings merge) itself wrote a NOTE before its limit."""

    def __init__(self, messages, *, at_merge):
        super().__init__(messages, merged="VERDICT: PASS")
        self.at_merge = at_merge

    def run(self, **kwargs):
        result = super().run(**kwargs)
        if (kwargs in self.merges) if self.at_merge else (kwargs in self.reviews[1:2]):
            return limited(f"NOTE: {NOTE}\nusage limit reached", kwargs["log_path"])
        return result


@pytest.mark.parametrize("at_merge", [False, True], ids=["member-limit", "merge-limit"])
def test_panel_limited_response_keeps_its_own_notes(git_history, tmp_path, monkeypatch, at_merge):
    case = perspective_case(git_history, tmp_path, monkeypatch, count=2)
    install_panel(case, LimitWithNotePanel(["VERDICT: PASS", "VERDICT: PASS"], at_merge=at_merge))
    assert run_issue(1, case.ctx) == "limit-wait"
    assert notes(case.store) == [NOTE]


def _write_events(store, *events):
    store.root.mkdir(parents=True, exist_ok=True)
    with (store.root / "orch-events.jsonl").open("a") as stream:
        for event in events:
            stream.write(json.dumps(event) + "\n")


def test_architect_gets_notes_since_its_last_clean_session_and_a_failed_session_uses_none(tmp_path):
    from foragent.state import StateStore
    store = StateStore(tmp_path / "state")
    assert review_notes.architect_section(store) == ""
    _write_events(
        store,
        {"at": "2026-10-30T10:00:00+00:00", "issue": 3, "kind": "review-note", "summary": "old", "note": "old",
         "round": 1},
        {"at": "2026-10-30T11:00:00+00:00", "issue": None, "kind": "architect-done", "exit_code": 0,
         "started_at": "2026-10-30T10:30:00+00:00"},
        {"at": "2026-10-30T10:45:00+00:00", "issue": 4, "kind": "review-note", "summary": "mid", "note": "mid",
         "round": 2},
        {"at": "2026-10-30T12:00:00+00:00", "issue": None, "kind": "architect-done", "exit_code": 1,
         "started_at": "2026-10-30T11:30:00+00:00"},
        {"at": "2026-10-30T13:00:00+00:00", "issue": 5, "kind": "review-note", "summary": "new", "note": "new"},
    )
    assert [note["note"] for note in review_notes.for_architect(store)] == ["mid", "new"]
    section = review_notes.architect_section(store)
    assert "- #5 (2026-10-30T13:00:00+00:00): new" in section
    assert "- #4 (round 2, 2026-10-30T10:45:00+00:00): mid" in section
    assert section.index("#5") < section.index("#4") and ": old" not in section
    assert str(store.root / "orch-events.jsonl") in section


def test_architect_section_caps_inline_notes_and_counts_the_rest(tmp_path):
    from foragent.state import StateStore
    store = StateStore(tmp_path / "state")
    _write_events(store, *({"at": f"2026-10-30T10:{i:02d}:00+00:00", "issue": i, "kind": "review-note",
                            "summary": f"n{i}", "note": f"n{i}"} for i in range(25)))
    section = review_notes.architect_section(store)
    assert section.count("\n- #") == review_notes.ARCHITECT_LIMIT
    assert "n24" in section and ": n4\n" not in section and "5 older notes are not shown." in section


def test_other_roles_keep_their_prompt_and_the_architect_appends_notes():
    from foragent import orch_role_prompt
    plain = orch_role_prompt.build_prompt("why")
    assert orch_role_prompt.build_prompt("why", review_notes="") == plain
    assert orch_role_prompt.build_prompt("why", review_notes="## Notes").endswith("## Notes\n")
