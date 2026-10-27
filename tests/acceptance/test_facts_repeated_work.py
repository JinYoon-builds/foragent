"""Acceptance test for facts-repeated-work. Locked by Foragent; do not edit."""

from datetime import timedelta
import os
import re

from test_orch_facts import (
    NOW,
    completed,
    facts,
    facts_case,
    isolated_home,
    no_facts_processes,
    recorded_phase_run,
)


# Wire contract: one indented `class: <slug>` line immediately under each
# finding. The reviewer chooses the slug; facts never infers it from prose.
# runs[].review_rounds counts review artifacts belonging to that execution.
# review_rounds = {median, top_runs: [{issue, review_rounds}, ...]} over recent
# merged executions, including zero-artifact legacy runs (empty median: null).
# runs[].finding_classes maps slugs to the number of distinct rounds containing
# them. finding_classes[slug] exposes rounds, runs, repeated_rounds, repeated_runs,
# and sorted distinct issues. Repeated rounds exclude the first appearance in
# each execution; repeated runs exclude the first execution containing the slug.
# An unlabelled finding belongs to `unclassified`; PASS without findings adds
# no class. Multiple findings of one class in one round count once.


PASS = "VERDICT: PASS\n"


def review(*classes):
    """Use identical finding prose with independent, reviewer-supplied classes."""
    return "VERDICT: FAIL\n" + "".join(
        f"- src/component_{index}.py:10 — The retry loses context.\n"
        + (f"  class: {slug}\n" if slug is not None else "")
        for index, slug in enumerate(classes, 1)
    )


def reviewed_run(case, number, reviews, *, started, first_round=1,
                 outcome="merged", active_phase=None):
    """Persist real records and review artifacts without executing a worker.

    Review filenames continue across executions of an issue, as the producer
    does. Artifact mtimes lie inside the recorded execution, allowing a reader
    to distinguish retries without charging the same files to both runs.
    A single reviewing phase deliberately contains several review artifacts.
    """
    recorded_phase_run(case, number, started=started, outcome=outcome,
                       active_phase=active_phase, phases=(
                           ("implementing", 10, {}),
                           ("reviewing", 10 * len(reviews), {}),
                       ))
    directory = case.store.root / "runs" / f"issue-{number}"
    directory.mkdir(parents=True, exist_ok=True)
    for offset, text in enumerate(reviews):
        path = directory / f"review-{first_round + offset}.txt"
        path.write_text(text, encoding="utf-8")
        at = (started + timedelta(seconds=20 + 10 * offset)).timestamp()
        os.utime(path, (at, at))


def test_facts_repeated_work_counts_review_files_per_merged_execution(tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)
    reviewed_run(case, 386, [review("retry-context"), PASS], started=NOW - timedelta(hours=3))
    # Sparse suffixes must not turn three observed rounds into twelve rounds.
    reviewed_run(case, 386, [review("retry-context"), review("retry-context"), PASS],
                 started=NOW - timedelta(hours=2), first_round=10)
    directory = case.store.root / "runs/issue-386"
    (directory / "review-notes.txt").write_text("Not a numbered review.\n", encoding="utf-8")
    (directory / "review-99.txt.bak").write_text(PASS, encoding="utf-8")
    orphan = case.store.root / "runs/issue-999"
    orphan.mkdir()
    (orphan / "review-1.txt").write_text(PASS, encoding="utf-8")

    result = facts(case, capsys)
    rows = result["runs"]
    assert len(rows) == 2 and all(row["issue"] == 386 for row in rows), (
        "Keep both executions, deduplicate snapshot/history copies, and ignore artifacts without a run")
    assert all(type(row["review_rounds"]) is int for row in rows)
    assert sorted(row["review_rounds"] for row in rows) == [2, 3], (
        "Count actual review-K.txt files within each execution, including PASS; do not use suffix maxima or phase counts")


def test_facts_repeated_work_median_and_top_five_use_recent_merged_runs(tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)
    for number, count in ((101, 1), (102, 2), (103, 3), (104, 4), (105, 5), (106, 7)):
        reviewed_run(case, number, [review("retry-context")] * (count - 1) + [PASS],
                     started=NOW - timedelta(hours=2))
    reviewed_run(case, 90, [review("excluded")] * 8, started=NOW - timedelta(hours=3),
                 outcome="review-failed")
    reviewed_run(case, 91, [review("excluded")] * 9, started=NOW - timedelta(days=40))
    reviewed_run(case, 92, [review("excluded")] * 10, started=NOW + timedelta(days=2))
    reviewed_run(case, 93, [review("excluded")] * 11, started=NOW - timedelta(hours=1),
                 active_phase="implementing")

    summary = facts(case, capsys)["review_rounds"]
    assert summary["median"] == 3.5, (
        "Take the median of recent merged execution counts, excluding failed, active, old and future runs")
    assert [(row["issue"], row["review_rounds"]) for row in summary["top_runs"]] == [
        (106, 7), (105, 5), (104, 4), (103, 3), (102, 2),
    ], "Name exactly the five merged runs with the most rounds, sorted descending"


def test_facts_repeated_work_classes_count_distinct_rounds_without_interpreting_prose(
    tmp_path, monkeypatch, capsys,
):
    case = facts_case(tmp_path, monkeypatch)
    reviewed_run(case, 386, [
        review("retry-context", "retry-context", "stale-base"),
        "VERDICT: FAIL\n- tests/check.py:87 — Another member of the defect family remains.\n"
        "  class: retry-context\n",
        review("scope-gap"),  # Same prose as round one, but a different explicit class.
        PASS,
    ], started=NOW - timedelta(hours=1))

    result = facts(case, capsys)
    row, = result["runs"]
    assert row["finding_classes"] == {"retry-context": 2, "stale-base": 1, "scope-gap": 1}, (
        "A class recurs only in a later round; do not count same-round duplicates or infer classes from wording")
    classes = result["finding_classes"]
    repeated = classes["retry-context"]
    assert (repeated["rounds"], repeated["runs"], repeated["repeated_rounds"],
            repeated["repeated_runs"], repeated["issues"]) == (2, 1, 1, 0, [386])
    assert classes["stale-base"]["repeated_rounds"] == classes["scope-gap"]["repeated_rounds"] == 0


def test_facts_repeated_work_classes_span_executions_and_list_distinct_issues(tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)
    reviewed_run(case, 386, [review("retry-context", "scope-gap"), review("retry-context"), PASS],
                 started=NOW - timedelta(hours=3))
    reviewed_run(case, 386, [review("retry-context"), PASS],
                 started=NOW - timedelta(hours=2), first_round=4)
    reviewed_run(case, 376, [review("retry-context", "scope-gap"), PASS],
                 started=NOW - timedelta(hours=1))

    result = facts(case, capsys)
    classes = result["finding_classes"]
    repeated = classes["retry-context"]
    assert (repeated["rounds"], repeated["runs"], repeated["repeated_rounds"],
            repeated["repeated_runs"], repeated["issues"]) == (4, 3, 1, 2, [376, 386]), (
        "Count distinct executions, including retries of one issue; list each issue once and never duplicate record copies")
    cross_run = classes["scope-gap"]
    assert (cross_run["rounds"], cross_run["runs"], cross_run["repeated_rounds"],
            cross_run["repeated_runs"], cross_run["issues"]) == (2, 2, 0, 1, [376, 386]), (
        "Expose recurrence across runs even when the class never repeats within a run")
    assert sorted(row["finding_classes"]["retry-context"] for row in result["runs"]) == [1, 1, 2]


def test_facts_repeated_work_legacy_findings_are_unclassified_beside_named_classes(
    tmp_path, monkeypatch, capsys,
):
    case = facts_case(tmp_path, monkeypatch)
    reviewed_run(case, 376, [review(None, "retry-context"), review(None, None), PASS],
                 started=NOW - timedelta(hours=1))

    result = facts(case, capsys)
    row, = result["runs"]
    assert row["review_rounds"] == 3
    assert row["finding_classes"] == {"unclassified": 2, "retry-context": 1}, (
        "Retain unlabelled findings, including mixed reviews, without losing named classes or classifying clean PASS rounds")
    legacy = result["finding_classes"]["unclassified"]
    assert (legacy["rounds"], legacy["runs"], legacy["repeated_rounds"],
            legacy["repeated_runs"], legacy["issues"]) == (2, 1, 1, 0, [376])
    assert result["finding_classes"]["retry-context"]["issues"] == [376]


def test_facts_repeated_work_empty_history_and_runs_without_reviews_are_read_only(
    tmp_path, monkeypatch, capsys,
):
    case = facts_case(tmp_path, monkeypatch)
    empty = facts(case, capsys)
    assert empty["review_rounds"]["median"] is None and empty["review_rounds"]["top_runs"] == []
    assert empty["finding_classes"] == {}
    completed(case, 7, started=NOW - timedelta(hours=2))  # Legacy run, no review artifacts.
    reviewed_run(case, 8, [PASS], started=NOW - timedelta(hours=1))

    result = facts(case, capsys)
    assert {row["issue"]: row["review_rounds"] for row in result["runs"]} == {7: 0, 8: 1}
    assert all(row["finding_classes"] == {} for row in result["runs"])
    assert result["finding_classes"] == {}, "Missing reviews and clean PASS rounds must not invent findings"
    assert result["review_rounds"]["median"] == 0.5
    assert facts(case, capsys) == result, "Repeated observation must preserve both records and facts"


def test_facts_repeated_work_human_line_names_run_with_most_review_rounds(tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)
    reviewed_run(case, 386, [review("retry-context"), review("retry-context"), PASS],
                 started=NOW - timedelta(hours=2))
    reviewed_run(case, 376, [PASS], started=NOW - timedelta(hours=1))

    output = facts(case, capsys, human=True)
    summary = [line for line in output.splitlines()
               if re.search(r"\b386\b", line) and re.search(r"review.*round|round.*review", line, re.I)]
    assert len(summary) == 1 and len(summary[0]) <= 160, (
        f"Name the run with the most review rounds in one short line: {output}")
    assert re.search(r"\b3\b", summary[0]), f"Show its observed three review rounds: {summary[0]}"


def test_facts_repeated_work_human_line_names_most_repeated_class(tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)
    reviewed_run(case, 386, [
        review("retry-context", *("many-instances" for _ in range(6))),
        review("retry-context"), review("retry-context"), PASS,
    ], started=NOW - timedelta(hours=2))
    reviewed_run(case, 376, [review("retry-context"), review("retry-context"), PASS],
                 started=NOW - timedelta(hours=1))

    output = facts(case, capsys, human=True)
    summary = [line for line in output.splitlines() if "retry-context" in line]
    assert len(summary) == 1 and len(summary[0]) <= 160, (
        f"Name the most repeated class in one short line; six instances in one round are not six recurrences: {output}")
