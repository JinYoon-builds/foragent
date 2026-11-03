"""#1651: a review FAIL blocks only when it is grounded; otherwise it is a NOTE.

Public runner and executor boundary only. Agent judgments are scripted data.
A scoring session is recognised by its public request: the prompt starts with
one JSON object that carries the finding text under "finding". The reply is one
JSON object: score, critical_path, exists, goal_line, locked_test,
breaking_input and spec_question.

Review texts are verbatim reviewer output from real runs (2026-11-02), as the
architect classified them: TASTE (outside the Goal) and REAL (would have broken).
Break-it: skipping scoring, trusting a score without a ground, trusting an
invented Goal line or test file, scoring twice, letting an invalid reply pass,
sending a Goal question back as rework, or a later round that chains
"same kind still remains" without a ground all fail here.
"""

import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from conftest import ACCEPTANCE
from foragent.executors import ExecResult
from foragent.runner import run_issue
from test_orch_review_handoff import make_case
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import entry, offline, risk_case  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401

REPO = Path(__file__).resolve().parents[2]
GOAL_LINE = "Implement the feature."  # the fixture issue's Goal

TASTE = {
    "1644-r11": "VERDICT: FAIL\nsrc/foragent/tracker.py:186 — Omitted transport diagnostics remain: [`http: unexpected EOF reading trailer`](https://github.com/golang/go/blob/master/src/net/http/transfer.go#L946) leaves `waits=False`; both runner entry paths produce `error`/`retries-exceeded` without a pause. This broader class also affects issue, PR-head, and PR-list reads through `_call`.\nNOTE: Requiring EOF at line end misses contextual body-read errors; inspect bare net/http diagnostics across all `_call` callers.",
    "1679-r2": "VERDICT: FAIL\nsrc/foragent/runner.py:246: The provenance defect class remains: a branch-authored matching `Foragent-Approved-Lock` trailer excuses its own rollback or deletion. When main independently matches that edit and changes another lock, retry starts implementation instead of returning `hash-mismatch` (both reproduced). This affects every trailer-backed lock, including ABSENT_BLOB; also inspect `_approved_lock_records` and src/foragent/run_publish_preserve.py:88.\nNOTE: Commit messages are branch-controlled, so the implementer’s claimed sole writer of WIP provenance is unenforced; trailer equality does not establish inheritance.",
    "1679-r4": "VERDICT: FAIL\nsrc/foragent/runner.py:226: First-round class remains: when main independently matches the branch’s rollback or deletion and updates another lock, implementation starts (`spec-question`, one executor call) instead of rejecting the own edit. Also reproduced through the equality shortcut at :206 and source-conflict path at :220.\nsrc/foragent/runner.py:220: Another instance of the lock-origin classification class: a WIP carrying an earlier main lock update as an ordinary commit is rejected when main republishes that same lock. Reproduced `hash-mismatch` before retry execution despite no own lock edit; tests/test_runner_lock_provenance.py:112 covers only another-lock republication.\nNOTE: Merged-byte equality and conflict status do not establish ownership. The rebuttal’s replacement rule leaves the original bypass and misclassifies inherited WIP changes.",
    "1679-r5": "VERDICT: FAIL\nsrc/foragent/runner.py:225: After a quit-merge WIP, a subsequent own locked-file rollback or deletion conflicting with main is overwritten, and implementation starts instead of returning hash-mismatch. This remaining own-edit-as-inherited class affects every conflicted lock passed to _take_base_locks, including approved deletions.\nNOTE: merge_conflicts does not establish that subsequent lock edits were inherited. The rebuttal’s already-matching-bytes argument does not justify overwriting divergent edits before checking them.",
    "1641-r3": "VERDICT: FAIL\nsrc/foragent/ci_mode.py:146 — Serialization failure on a deeply nested JSON artifact sets fallback_reports=None, suppressing subsequent artifact reads and accepting local evidence despite replacement CI reports showing regressions. Remaining CI fallback freshness instance; affects run_publish_flow.py and run_publish_reviewed.py through run_publish_merge.py:118.\nNOTE: Failed report serialization shares the sentinel used to intentionally ignore untrusted workflow artifacts.",
    "1699-r3": "VERDICT: FAIL\nsrc/foragent/orch_role_memory.py:71 A valid memory filename containing `\\r` reaches commit_paths with `\\n`, causing repeated pathspec failures without a durable commit or publication. Remaining pathname-parsing class; also inspect _unpushed at line 58 and commit_paths’ pathname reader.\nNOTE: GitRepo’s text-mode transport normalizes carriage returns before NUL parsing; pathname reads require lossless decoding.",
    "1696-r3": "VERDICT: FAIL\nsrc/foragent/size_signal.py:105 A reference change committed on a side branch before approval, then merged into main during implementation, makes commit_before select that branch’s snapshot. A previously matched estimate becomes expected_size=null and disappears from wrong_share’s denominator. This remaining snapshot-class defect affects both run_publish_flow.py:368 and run_publish_reviewed.py:239.\nNOTE: The rebuttal’s immutable-approval claim fails because origin/main’s ancestry changes; retain the matched estimate or an immutable input snapshot.",
    "1696-r4": "VERDICT: FAIL\nsrc/foragent/size_signal.py:97 A later unpublished preread of changed references with identical body and acceptance blobs replaces the running issue’s estimate. Reproduced: omitting SIZE in that reread records expected_size=null and excludes the known estimate from wrong_share. This remaining snapshot-binding defect affects both run_publish_flow.py:368 and run_publish_reviewed.py:239.\nNOTE: The approval lacks reference identity, so reselecting from mutable preread history cannot preserve its estimate reliably; retain the matched preread when approval or implementation starts.",
    "1484-r1": "VERDICT: FAIL\nsrc/foragent/status_pm.py:103 After 21 subsequent takeovers, a still-live displaced session is forgotten, receives authority \"none\", and passes the publication/approval guard again. This also suppresses its displacement notice.\nNOTE: This is authority loss through history truncation; inspect all consumers of authority(), including CLI refusal and prompt-context notices. All 9 targeted tests pass despite this defect.",
}

REAL = {
    "1641-r2": "VERDICT: FAIL\nsrc/foragent/ci_mode.py:111 — After fallback for missing artifacts, newly usable CI reports are never reread at the final merge lock, allowing portable acceptance regressions to merge.\n  class: CI fallback evidence freshness; shared by implementation and review-only runs.\n  evidence: Reproduced both entrypoints returning merged with comparison source local and zero artifact rereads after valid CI reports showing two lost passing tests became available.\n  affected: src/foragent/ci_mode.py:111\n  affected: src/foragent/run_publish_merge.py:118\n  affected: src/foragent/run_publish_flow.py:263\n  affected: src/foragent/run_publish_reviewed.py:207\nNOTE: Local evidence checks revision and scope freshness but treats CI evidence availability as permanently unchanged after fallback.",
    "1652-r9": "VERDICT: FAIL\nsrc/foragent/skills.py:170: Complete MIT text plus `SPDX-License-Identifier: by LicenseRef-NonCommercial` passes and reaches sessions despite an unknown declaration. Broader declaration-bypass class: also test `SPDX License Identifier: GPL-3.0-only` through repository, imported and installed copies.\nNOTE: `_BY` overrides an unknown SPDX verdict; `_DECLARATION` also misses labels whose final word lacks “SPDX” or “License”.\nsrc/foragent/skills.py:183: LICENSE containing only `License: MIT` or `SPDX: MIT` installs and reaches sessions without complete terms, broadening the explicitly preserved SPDX-License-Identifier-only exception. Broader short-form acceptance regression affects every caller of redistributable_license.\nNOTE: Broad declaration recognition now also authorizes the legacy short form.\nNOTE: src/foragent/skills.py:157 still rejects complete MIT prefixed with `Example Project by Jane Doe`; this remains a nonblocking work-name limitation under the stated review standard.\nNOTE: The rebuttal is accepted for ordinary labelled notices, attribution lines and URLs, including the original BSD-NonCommercial attribution example.",
    "1679-r1": "VERDICT: FAIL\nsrc/foragent/runner.py:240: An issue’s deliberate rollback to historical locked-file bytes is treated as inherited. If main independently makes that rollback plus another change, the clean merge starts implementation instead of rejecting the issue’s own lock edit (reproduced: spec-question instead of hash-mismatch). This weakens the lock check; inspect all historical-blob matches and ABSENT_BLOB deletions in this helper for the same defect class.\nNOTE: Historical content equality does not establish that a branch inherited the change; the classifier loses the provenance needed to distinguish inherited updates from own edits.",
}

# The Goal of #1652 never defined which notices are allowed (architect kind 5).
UNDECIDED = ("VERDICT: FAIL\nsrc/foragent/skills.py:144: Complete MIT text prefixed with `Attribution: Jane Doe` or "
             "`Source: Example Project` returns `unknown`, rejecting notices allowed by the Goal. Broader class: valid "
             "notice formats are excluded; also inspect line 145, which rejects multiword work names.")

UNGROUNDED = {"score": 90, "critical_path": False, "exists": False, "goal_line": None,
              "locked_test": None, "breaking_input": None, "spec_question": None}
GROUNDS = {
    "breaking_input": {"breaking_input": "A run whose CI reports arrive after the local fallback"},
    "goal_line": {"goal_line": GOAL_LINE},
    "locked_test": {"locked_test": f"{ACCEPTANCE}::test_feature"},
}


def finding_count(text):
    lines = [line for line in text.splitlines()[1:] if line.strip() and not line.startswith("NOTE:")]
    return sum(1 for line in lines if not line[:1].isspace())


def scoring_request(prompt):
    try:
        request = json.JSONDecoder().raw_decode(prompt)[0]
    except ValueError:
        return None
    return request if isinstance(request, dict) and isinstance(request.get("finding"), str) else None


class Script:
    """Reviewer replies in order, and one scored reply per finding (by default the same for all)."""

    def __init__(self, case, reviews, score):
        self.case, self.reviews, self.score = case, list(reviews), score
        self.reviewer_calls, self.scorings = [], []

    def run(self, **kwargs):
        request = scoring_request(kwargs["prompt"])
        if request is not None:
            self.scorings.append((request, kwargs))
            reply = self.score(request["finding"]) if callable(self.score) else self.score
            text = reply if isinstance(reply, str) else json.dumps(reply)
        else:
            self.reviewer_calls.append(kwargs)
            assert self.reviews, "Unexpected reviewer call"
            text = self.reviews.pop(0)
        if kwargs.get("review_level") is not None:
            text = json.dumps({"result": text})
        return ExecResult(0, False, text, kwargs["log_path"])


def ordinary(git_history, tmp_path, monkeypatch, reviews, score, reviewer="codex", **settings):
    case = make_case(git_history, tmp_path, monkeypatch, reviewer=reviewer,
                     executor="claude" if reviewer == "codex" else "codex",
                     **{"skill_libraries": [], "test_command": "true", "language": "en",
                        "review_max": 0, "max_retries": 0, **settings})
    script = Script(case, reviews, score)
    install(case, script)
    return case, script


def install(case, script):
    reviewer = SimpleNamespace(run=script.run)
    case.ctx.executor_factory = lambda name: case.implementer if name == case.config.executor else reviewer


def records(case):
    """Everything the run kept on disk; the representation belongs to the lane."""
    return "\n".join(p.read_text(errors="replace") for p in case.store.root.rglob("*") if p.is_file())


def comments(case):
    return "\n".join(c.body for c in case.tracker.list_comments(1))


def assert_scored_once_each(script, text):
    findings = [request["finding"] for request, _ in script.scorings]
    assert len(findings) == finding_count(text), "Exactly one scoring session per FAIL finding"
    for request, call in script.scorings:
        assert not call.get("resume_session_id"), "Each finding gets a fresh session"
        assert GOAL_LINE in call["prompt"], "The scorer must see the approved Goal"
    first_lines = [line for line in text.splitlines()[1:]
                   if line.strip() and not line[:1].isspace() and not line.startswith("NOTE:")]
    for line in first_lines:
        assert sum(line in finding for finding in findings) == 1, "Each finding is scored in its own session"


@pytest.mark.parametrize("name", sorted(TASTE))
def test_taste_fail_without_a_ground_is_a_note_and_merges(git_history, tmp_path, monkeypatch, name):
    text = TASTE[name]
    case, script = ordinary(git_history, tmp_path, monkeypatch, [text], UNGROUNDED)
    assert run_issue(1, case.ctx) == "merged"
    assert len(case.tracker.merged_prs) == 1
    assert len(case.implementer.calls) == 1, "An ungrounded finding is not rework"
    assert len(script.reviewer_calls) == 1
    assert_scored_once_each(script, text)
    kept = records(case)
    first = text.splitlines()[1]
    assert first[:60] in kept, "The demoted finding stays findable as a record"
    assert re.search(r'"score":\s*90', kept), "The assessment and its score are kept"


@pytest.mark.parametrize("claim", [
    {"goal_line": "A displaced session keeps its authority after any number of takeovers."},
    {"locked_test": "tests/acceptance/test_status_pm_history.py::test_twenty_one_takeovers"},
    {"locked_test": "src/foragent/status_pm.py"},
])
def test_an_invented_goal_line_or_locked_test_is_not_a_ground(git_history, tmp_path, monkeypatch, claim):
    text = TASTE["1484-r1"]
    case, script = ordinary(git_history, tmp_path, monkeypatch, [text], {**UNGROUNDED, **claim})
    assert run_issue(1, case.ctx) == "merged"
    assert len(case.implementer.calls) == 1 and len(script.scorings) == 1


@pytest.mark.parametrize("ground", sorted(GROUNDS))
@pytest.mark.parametrize("name", sorted(REAL))
def test_real_defects_with_a_ground_still_fail(git_history, tmp_path, monkeypatch, name, ground):
    text = REAL[name]
    case, script = ordinary(git_history, tmp_path, monkeypatch, [text], {**UNGROUNDED, **GROUNDS[ground]})
    assert run_issue(1, case.ctx) == "held"
    assert not case.tracker.merged_prs and len(case.implementer.calls) == 1
    assert_scored_once_each(script, text)
    events = [json.loads(line) for line in (case.store.root / "orch-events.jsonl").read_text().splitlines()]
    reviews = [e for e in events if e.get("kind") == "review" and e.get("issue") == 1]
    assert [e["verdict"] for e in reviews] == ["FAIL"] and reviews[0]["findings"] >= finding_count(text)


@pytest.mark.parametrize("score,blocks", [(0, False), (79, False), (80, True), (100, True)])
def test_a_grounded_finding_blocks_only_from_score_80(git_history, tmp_path, monkeypatch, score, blocks):
    text = REAL["1679-r1"]
    reply = {**UNGROUNDED, **GROUNDS["breaking_input"], "score": score}
    case, script = ordinary(git_history, tmp_path, monkeypatch, [text], reply)
    assert run_issue(1, case.ctx) == ("held" if blocks else "merged")
    assert len(case.implementer.calls) == 1 and len(script.scorings) == 1
    assert re.search(rf'"score":\s*{score}\b', records(case))


@pytest.mark.parametrize("exists,score,ground,blocks", [
    (True, 5, "breaking_input", True), (False, 100, "breaking_input", False), (True, 100, None, False)])
def test_a_fatal_path_blocks_when_it_exists_whatever_its_rarity(git_history, tmp_path, monkeypatch,
                                                               exists, score, ground, blocks):
    text = REAL["1679-r1"]
    reply = {**UNGROUNDED, **(GROUNDS[ground] if ground else {}), "score": score,
             "critical_path": True, "exists": exists}
    case, script = ordinary(git_history, tmp_path, monkeypatch, [text], reply)
    assert run_issue(1, case.ctx) == ("held" if blocks else "merged")
    assert len(script.scorings) == 1


FATAL = {
    "data-loss": ("VERDICT: FAIL\nsrc/foragent/runner.py:300: When the run is stopped during cleanup, files the implementer wrote are deleted without being saved to the issue branch.",
                  "Stop a run after the implementer wrote a new file and before it commits"),
    "broken-main": ("VERDICT: FAIL\nsrc/foragent/merge.py:120: A merge-group failure still fast-forwards main to the failing head, so main no longer passes its tests.",
                    "A merge group whose CI fails after the local check passed"),
    "unapproved-merge": ("VERDICT: FAIL\nsrc/foragent/merge.py:88: An issue whose approved label was removed after publication is still merged.",
                         "Remove the approved label while the run is in CI"),
    "permission-handover": ("VERDICT: FAIL\nsrc/foragent/cli.py:40: A lane session can run fa orch merge --approved-by, a person-only command.",
                            "A lane run invokes fa orch merge --approved-by from its worktree"),
}


@pytest.mark.parametrize("kind", sorted(FATAL))
def test_named_fatal_paths_block_even_when_scored_rare(git_history, tmp_path, monkeypatch, kind):
    text, breaking = FATAL[kind]
    reply = {**UNGROUNDED, "breaking_input": breaking, "score": 10,
             "critical_path": True, "exists": True}
    case, script = ordinary(git_history, tmp_path, monkeypatch, [text], reply)
    assert run_issue(1, case.ctx) == "held", f"{kind} must block whatever its rarity"
    assert not case.tracker.merged_prs and len(script.scorings) == 1


@pytest.mark.parametrize("reply", [
    "VERDICT: PASS", "{}", {**UNGROUNDED, "score": 101}, {**UNGROUNDED, "score": "95"},
    {**UNGROUNDED, "score": True}, {**UNGROUNDED, "critical_path": "no"}])
def test_an_unusable_score_never_turns_a_fail_into_a_pass(git_history, tmp_path, monkeypatch, reply):
    text = TASTE["1641-r3"]
    case, script = ordinary(git_history, tmp_path, monkeypatch, [text], reply)
    assert run_issue(1, case.ctx) == "held"
    assert not case.tracker.merged_prs and len(script.scorings) == 1


@pytest.mark.parametrize("reviewer", ["codex", "claude"])
def test_a_point_the_goal_does_not_decide_becomes_a_spec_question_not_rework(
        git_history, tmp_path, monkeypatch, reviewer):
    question = "Which notice formats does the Goal allow beside the complete MIT text?"
    reply = {**UNGROUNDED, "score": 85, "spec_question": question}
    case, script = ordinary(git_history, tmp_path, monkeypatch, [UNDECIDED, "VERDICT: PASS"], reply,
                            reviewer=reviewer, max_retries=3, review_max=3)
    case.implementer.steps *= 2
    assert run_issue(1, case.ctx) == "spec-question"
    assert len(case.implementer.calls) == 1, "A Goal question must not send the work back"
    assert len(script.reviewer_calls) == 1 and len(script.scorings) == 1
    assert not case.tracker.merged_prs
    asked = [c.body for c in case.tracker.list_comments(1)
             if c.body.startswith("<!-- foragent:blocked reason=spec-question -->")]
    assert len(asked) == 1 and question in asked[0]
    assert "blocked" in case.tracker.get_issue(1).labels


def test_scoring_can_be_turned_off(git_history, tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".foragent.yml").write_text("review_scoring: false\n")
    from foragent.config import load_config
    assert load_config(project).review_scoring is False
    text = TASTE["1641-r3"]
    case, script = ordinary(git_history, tmp_path, monkeypatch, [text], UNGROUNDED, review_scoring=False)
    assert run_issue(1, case.ctx) == "held"
    assert script.scorings == [] and len(script.reviewer_calls) == 1


def later_round(git_history, tmp_path, monkeypatch, second_reply):
    first = REAL["1679-r1"]
    second = TASTE["1679-r2"]  # a real round-2 "the same class remains" finding

    def score(finding):
        if finding in first:
            return {**UNGROUNDED, **GROUNDS["breaking_input"]}
        return second_reply

    case, script = ordinary(git_history, tmp_path, monkeypatch, [first, second], score,
                            max_retries=1, review_max=1)
    case.implementer.steps *= 2
    return case, script, second


def test_round_two_same_kind_without_a_ground_is_a_note(git_history, tmp_path, monkeypatch):
    case, script, second = later_round(git_history, tmp_path, monkeypatch, UNGROUNDED)
    assert run_issue(1, case.ctx) == "merged"
    assert len(case.implementer.calls) == 2 and len(script.reviewer_calls) == 2
    assert len(script.scorings) == 2, "Round 2 findings are scored like round 1"
    assert second.splitlines()[1][:60] in records(case)
    # The round-2 reviewer is told the same standard, not "check every instance" alone.
    prompt = " ".join(script.reviewer_calls[1]["prompt"].split())
    sentences = re.split(r"(?<=[.])\s+", prompt)
    assert any(all(word in sentence.lower() for word in ("goal", "locked", "input", "note"))
               for sentence in sentences), "Round 2 must state the Goal / locked test / input standard"


def test_round_two_same_kind_with_a_breaking_input_still_fails(git_history, tmp_path, monkeypatch):
    reply = {**UNGROUNDED, "breaking_input": "A retry after main republishes the same lock"}
    case, script, _ = later_round(git_history, tmp_path, monkeypatch, reply)
    assert run_issue(1, case.ctx) == "held"
    assert not case.tracker.merged_prs
    assert len(case.implementer.calls) == 2 and len(script.scorings) == 2


@pytest.mark.parametrize("mapped,reviewers", [(True, 7), (False, 1)])
def test_the_panel_runs_only_where_it_runs_today(git_history, tmp_path, monkeypatch, mapped, reviewers):
    """First round with a risk note: six perspectives and one merge. Otherwise one reviewer."""
    case = risk_case(git_history, tmp_path, monkeypatch, entries=[entry(["feature.txt"] if mapped else ["docs/**"])],
                     **{"test_command": "true", "review_max": 0, "max_retries": 0})
    text = TASTE["1641-r3"]
    script = Script(case, [text] * reviewers, UNGROUNDED)
    install(case, script)
    assert run_issue(1, case.ctx) == "merged"
    assert len(script.reviewer_calls) == reviewers
    assert len(script.scorings) == 1
    if mapped:
        prompts = "\n".join(call["prompt"] for call in script.reviewer_calls)
        for name in ("rule-documents", "obvious-bugs", "git-history", "earlier-pr-comments",
                     "code-comments", "fatal-path"):
            assert name in prompts


def test_the_procedure_ships_as_a_reviewer_skill_with_its_apache_source_note():
    skill = REPO / "skills" / "review-procedure"
    text = (skill / "SKILL.md").read_text()
    for score in ("a. 0: Not confident at all.", "b. 25: Somewhat confident.", "c. 50: Moderately confident.",
                  "d. 75: Highly confident.", "e. 100: Absolutely certain."):
        assert score in text
    assert "Pedantic nitpicks that a senior engineer wouldn't call out" in text
    assert "Real issues, but on lines that the user did not modify in their pull request" in text
    assert "Apache License" in (skill / "LICENSE").read_text()
    notice = (skill / "NOTICE").read_text()
    assert "code-review" in notice and "Apache" in notice
    config = (REPO / ".foragent.yml").read_text()
    assert re.search(r"^\s*reviewers:\s*\[[^\]]*\breview-procedure\b", config, re.M)
    assert "skills/review-procedure/SKILL.md" in (REPO / "pyproject.toml").read_text()
