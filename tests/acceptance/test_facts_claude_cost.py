"""Acceptance test for n22-claude-cost-breakdown: recorded Claude cost by merged work and review rounds.

Wire contract (`fa orch facts --json`, key ``claude_cost``):

    claude_cost = {
      "scope": {"provider": "claude", "evidence": <nonempty text>, "window_days": 7},
      "total_usd": <float>,                        # observed issue-attributed Claude cost
      "issues": [{"issue": N, "usd": <float|None>, "merged": <bool|None>,
                  "review_rounds": <int|None>}, ...],
      "merged":   {"usd": <float>, "share": <float|None>, "issues": [N, ...]},
      "unmerged": {"usd": <float>, "share": <float|None>, "issues": [N, ...]},
      "review_heavy": {"min_rounds": 10, "usd": <float>, "share": <float|None>, "issues": [N, ...]},
      "missing_cost": {"issues": [N, ...]},      # recent issue executions without a reported cost
      "unknown_merge": {"usd": <float>, "issues": [N, ...]},
    }

Evidence is what the runner already retains: Claude's ``total_cost_usd`` in the result event
of ``logs/issue-N.stdout`` and in each panel reviewer's JSON document
``logs/issue-N-review-R-K.stdout``. Role and dream sessions (``logs/architect-*.stdout``,
``logs/dream-*.stdout``, ``logs/docs-*.stdout``) are not issue work. Shares are fractions of
``total_usd``; merged and unmerged are disjoint, review-heavy is an overlapping subset.
"""

from datetime import timedelta
import json
import os
import re

from test_facts_repeated_work import PASS, review, reviewed_run
from test_orch_facts import (
    NOW,
    completed,
    facts,
    facts_case,
    isolated_home,
    no_facts_processes,
    recorded_phase_run,
)


def _logs(case):
    directory = case.store.root / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _stamp(path, at):
    os.utime(path, (at.timestamp(), at.timestamp()))


def stream_result(case, name, cost, *, at, extra=None):
    """A Claude stream-json session log whose last event is the result (as `claude -p` writes it)."""
    path = _logs(case) / name
    result = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3,
              "result": "Done.", "session_id": f"session-{name}", **(extra or {})}
    if cost is not None:
        result["total_cost_usd"] = cost
    lines = [{"type": "system", "subtype": "init", "session_id": f"session-{name}"},
             {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}},
             result]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    _stamp(path, at)
    return path


def review_document(case, number, round_number, k, cost, *, at):
    """A panel reviewer's single JSON document (`claude -p /code-review --output-format json`)."""
    path = _logs(case) / f"issue-{number}-review-{round_number}-{k}.stdout"
    path.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                "result": "VERDICT: PASS", "total_cost_usd": cost}), encoding="utf-8")
    _stamp(path, at)
    return path


def rounds(count):
    return [review("retry-context")] * (count - 1) + [PASS]


def cost(result):
    assert "claude_cost" in result, "fa orch facts --json must carry a claude_cost section"
    return result["claude_cost"]


def close(value, expected):
    return value is not None and abs(value - expected) < 1e-6


def cost_lines(out):
    """Human lines about Claude dollar cost (not the executor health or quota lines)."""
    return [line for line in out.splitlines()
            if "claude" in line.lower() and ("$" in line or "cost" in line.lower())]


def by_issue(section):
    return {entry["issue"]: entry for entry in section["issues"]}


def money(amount):
    """$10, $10.0 or $10.00, never $100 or $10.5."""
    return re.compile(r"\$" + re.escape(str(amount)) + r"(?:\.0+)?(?![\d.]*[1-9])(?!\d)")


def percent(value):
    return re.compile(r"(?<![\d.])" + str(value) + r"(?:\.0+)?\s?%")


def seeded(case):
    """$6 merged (#301 $3 with ten rounds, #302 $3 with nine) and $4 unmerged (#303 with ten, after a retry)."""
    start = NOW - timedelta(hours=6)
    # #301: merged; implementation stream $2 plus two panel reviewers $0.5 each.
    reviewed_run(case, 301, rounds(10), started=start)
    stream_result(case, "issue-301.stdout", 2.0, at=start + timedelta(seconds=15))
    review_document(case, 301, 1, 1, 0.5, at=start + timedelta(seconds=25))
    review_document(case, 301, 1, 2, 0.5, at=start + timedelta(seconds=26))
    # #302: merged with nine usable rounds; one implementation result $3.
    start = NOW - timedelta(hours=5)
    reviewed_run(case, 302, rounds(9), started=start)
    stream_result(case, "issue-302.stdout", 3.0, at=start + timedelta(seconds=15))
    # #303: a failed first execution whose reviewer cost ($1.5) is retained, then an unmerged
    # second execution with ten rounds and a $2.5 implementation result.
    first = NOW - timedelta(hours=4)
    recorded_phase_run(case, 303, started=first, outcome="review-failed",
                       phases=(("implementing", 10, {}), ("reviewing", 10, {})))
    review_document(case, 303, 1, 1, 1.5, at=first + timedelta(seconds=15))
    second = NOW - timedelta(hours=3)
    reviewed_run(case, 303, rounds(10), started=second, outcome="retries-exceeded")
    stream_result(case, "issue-303.stdout", 2.5, at=second + timedelta(seconds=15))
    # Not issue work: role, docs and dream sessions with large costs.
    stream_result(case, "architect-20261024T090000000000Z.stdout", 50.0, at=NOW - timedelta(hours=2))
    stream_result(case, "docs-20261024T091000000000Z.stdout", 9.0, at=NOW - timedelta(hours=2))
    stream_result(case, "dream-roles.stdout", 20.0, at=NOW - timedelta(hours=2))
    # Outside the recent window: a merged issue nine days ago with a $100 result.
    old = NOW - timedelta(days=9)
    completed(case, 290, started=old, phases=(("implementing", 30),))
    stream_result(case, "issue-290.stdout", 100.0, at=old + timedelta(seconds=10))


def test_claude_cost_totals_per_issue_and_merged_shares(tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)
    seeded(case)

    section = cost(facts(case, capsys))
    assert close(section["total_usd"], 10.0), (
        f"Observed total is the recent issue-attributed Claude cost, each reported once: {section['total_usd']}")
    issues = by_issue(section)
    assert {n: issues[n]["usd"] for n in (301, 302, 303) if n in issues} == {301: 3.0, 302: 3.0, 303: 4.0}, (
        "Per-issue cost sums every retained result of the issue (implementation, panel reviewers, earlier "
        f"attempts) exactly once: {section['issues']}")
    assert issues[301]["merged"] is True and issues[302]["merged"] is True and issues[303]["merged"] is False
    assert close(section["merged"]["usd"], 6.0) and close(section["merged"]["share"], 0.6), section["merged"]
    assert close(section["unmerged"]["usd"], 4.0) and close(section["unmerged"]["share"], 0.4), section["unmerged"]
    assert sorted(section["merged"]["issues"]) == [301, 302]
    assert sorted(section["unmerged"]["issues"]) == [303], "Merged and unmerged groups are disjoint"


def test_claude_cost_review_heavy_subset_overlaps_the_disjoint_groups(tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)
    seeded(case)

    section = cost(facts(case, capsys))
    heavy = section["review_heavy"]
    assert heavy["min_rounds"] == 10
    assert sorted(heavy["issues"]) == [301, 303], (
        f"Issues with at least ten recorded usable reviewer rounds, merged or not; nine rounds stay out: {heavy}")
    assert close(heavy["usd"], 7.0) and close(heavy["share"], 0.7), heavy
    issues = by_issue(section)
    assert issues[301]["review_rounds"] == 10 and issues[302]["review_rounds"] == 9
    assert issues[303]["review_rounds"] == 10
    # The subset does not move the disjoint groups.
    assert close(section["merged"]["usd"] + section["unmerged"]["usd"], section["total_usd"])


def test_claude_cost_scope_excludes_role_sessions_and_old_results(tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)
    seeded(case)

    section = cost(facts(case, capsys))
    scope = section["scope"]
    assert scope["provider"] == "claude" and scope["window_days"] == 7, scope
    assert isinstance(scope["evidence"], str) and scope["evidence"].strip(), "Name the retained-evidence scope"
    assert 290 not in by_issue(section), "A result outside the recent window is excluded"
    assert all(type(entry["issue"]) is int for entry in section["issues"]), "Role and dream sessions are not issues"
    assert close(section["total_usd"], 10.0)


def test_claude_cost_human_report_names_total_issue_costs_shares_and_scope(tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)
    seeded(case)

    out = facts(case, capsys, human=True)
    lines = cost_lines(out)
    assert lines, f"The human facts name the Claude cost breakdown:\n{out}"
    text = "\n".join(lines)
    assert money(10).search(text), f"Show the $10 observed total:\n{text}"
    for number, amount in ((301, 3), (302, 3), (303, 4)):
        assert re.search(rf"#{number}\b[^#\n]*?" + money(amount).pattern, text), (
            f"Show #{number}'s ${amount} cost:\n{text}")
    for value in (60, 40, 70):
        assert percent(value).search(text), f"Show the {value}% share:\n{text}"
    assert money(7).search(text), f"Show the $7 review-heavy subset:\n{text}"
    assert re.search(r"\b7\b", text) and "retained" in text.lower(), (
        f"Name the 7-day window and the retained-log scope:\n{text}")
    assert not money(100).search(out) and not money(50).search(out), "Old and role costs stay out"


def test_claude_cost_discloses_missing_cost_and_unknown_merge_evidence(tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)
    seeded(case)
    # #304: a recent unmerged execution with no retained result at all.
    recorded_phase_run(case, 304, started=NOW - timedelta(hours=2), outcome="review-failed",
                       phases=(("implementing", 10, {}),))
    # #305: a recent execution whose result reports no cost.
    started = NOW - timedelta(hours=1)
    recorded_phase_run(case, 305, started=started, outcome="merged", phases=(("implementing", 10, {}),))
    stream_result(case, "issue-305.stdout", None, at=started + timedelta(seconds=5))
    # #306: a recent issue result with a cost but no recorded run, so no merge evidence.
    stream_result(case, "issue-306.stdout", 5.0, at=NOW - timedelta(minutes=30))

    section = cost(facts(case, capsys))
    assert sorted(set(section["missing_cost"]["issues"]) & {304, 305}) == [304, 305], (
        f"Executions without a reported cost are disclosed: {section['missing_cost']}")
    issues = by_issue(section)
    for number in (304, 305):
        assert number not in issues or issues[number]["usd"] is None, "Absent cost is never a measured $0"
        assert number not in section["merged"]["issues"] and number not in section["unmerged"]["issues"]
    assert 306 in section["unknown_merge"]["issues"], f"Cost without merge evidence is disclosed: {section}"
    assert close(section["unknown_merge"]["usd"], 5.0)
    assert 306 not in section["merged"]["issues"] and 306 not in section["unmerged"]["issues"], (
        "Unknown merge state is not assumed unmerged")
    assert close(section["merged"]["usd"], 6.0) and close(section["unmerged"]["usd"], 4.0)


def test_claude_cost_zero_total_has_unknown_shares(tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)
    start = NOW - timedelta(hours=2)
    reviewed_run(case, 401, rounds(10), started=start)
    stream_result(case, "issue-401.stdout", 0.0, at=start + timedelta(seconds=15))

    section = cost(facts(case, capsys))
    assert section["total_usd"] == 0
    for group in ("merged", "unmerged", "review_heavy"):
        assert section[group]["share"] is None, f"A zero total gives an unknown {group} share: {section[group]}"
    out = facts(case, capsys, human=True)
    text = "\n".join(cost_lines(out))
    assert text, f"The human facts still show the Claude cost line:\n{out}"
    assert not re.search(r"\d\s?%", text) and "nan" not in text.lower(), (
        f"No invented percentage for a zero total:\n{text}")


def test_claude_cost_empty_history_is_zero_with_unknown_shares(tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)

    section = cost(facts(case, capsys))
    assert section["total_usd"] == 0 and section["issues"] == []
    assert section["merged"]["share"] is None and section["unmerged"]["share"] is None
    assert section["review_heavy"]["share"] is None
