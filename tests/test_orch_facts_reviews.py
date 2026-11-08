"""Review rounds follow the runner's journaled verdict, not a re-parse of the raw text."""

from datetime import UTC, datetime, timedelta
import os
from types import SimpleNamespace

from foragent import orch_events
from foragent.orch_facts_reviews import attach, round_classes
from foragent.state import StateStore

START = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
FINDING = "VERDICT: FAIL\n- src/a.py:3 — The retry loses context.\n  class: retry-context\n"


def _store(tmp_path):
    return StateStore(tmp_path / "state", now=lambda: START)


def _review(store, number, round_number, raw, at, verdict=None, **fields):
    path = store.root / "runs" / f"issue-{number}" / f"review-{round_number}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    os.utime(path, (at.timestamp(), at.timestamp()))
    if verdict is not None:  # The runner journals right after it saves the file.
        orch_events.append_event(store, at + timedelta(milliseconds=5), number, "review", f"{verdict} 0 findings",
                                 verdict=verdict, findings=0, round=round_number, source="reviewer", **fields)


def _attach(store, number=7):
    execution = SimpleNamespace(run=SimpleNamespace(number=number), started=START)
    rows = [{"issue": number, "status": "finished", "outcome": "merged", "started_at": START.isoformat()}]
    return attach(store, [execution], rows), rows[0]


def test_unusable_journaled_round_with_a_verdict_line_counts_nothing(tmp_path):
    store = _store(tmp_path)
    # A timed-out reviewer, or a failed panel's "Reviewer 1:\nVERDICT: FAIL ...", still has a VERDICT line.
    _review(store, 7, 1, FINDING, START + timedelta(seconds=10), verdict="UNUSABLE")
    _review(store, 7, 2, "Reviewer 1:\n" + FINDING + "\nReviewer 2 of 2 gave no verdict\n",
            START + timedelta(seconds=20), verdict="UNUSABLE")
    _review(store, 7, 3, "VERDICT: PASS\n", START + timedelta(seconds=30), verdict="PASS")
    summary, row = _attach(store)
    assert row["review_rounds"] == 1 and row["finding_classes"] == {}
    assert summary["finding_classes"] == {}


def test_journaled_verdict_decides_over_the_text(tmp_path):
    store = _store(tmp_path)
    _review(store, 7, 1, "not a verdict at all\n", START + timedelta(seconds=10), verdict="FAIL")
    _review(store, 7, 2, FINDING, START + timedelta(seconds=20), verdict="FAIL")
    # Recorded PASS still counts its path:line finding: the verdict says only that the round counts.
    _review(store, 7, 3, "VERDICT: FAIL\n- src/b.py:1 — Gone.\n  class: scope-gap\n",
            START + timedelta(seconds=30), verdict="PASS")
    summary, row = _attach(store)
    assert row["review_rounds"] == 3
    assert row["finding_classes"] == {"unclassified": 1, "retry-context": 1, "scope-gap": 1}


CONFLICT = "SPEC-CONFLICT: tests/acceptance/test_x.py:1 — wants the opposite\n"


def test_conflict_round_counts_the_reviewers_own_verdict(tmp_path):
    store = _store(tmp_path)
    _review(store, 7, 1, FINDING + CONFLICT, START + timedelta(seconds=10),
            verdict="SPEC-CONFLICT", review_verdict="FAIL")
    _review(store, 7, 2, FINDING + CONFLICT, START + timedelta(seconds=20),
            verdict="SPEC-CONFLICT", review_verdict="UNUSABLE")
    _review(store, 7, 3, "VERDICT: PASS\n" + CONFLICT, START + timedelta(seconds=30),
            verdict="SPEC-CONFLICT", review_verdict="PASS")
    # Journaled before the reviewer's verdict was kept: judged from the text, conflict lines excluded.
    _review(store, 7, 4, "VERDICT: FAIL\n" + CONFLICT, START + timedelta(seconds=40), verdict="SPEC-CONFLICT")
    _, row = _attach(store)
    assert row["review_rounds"] == 3
    assert row["finding_classes"] == {"retry-context": 1, "unclassified": 1}


def test_earlier_orchestrator_verdict_of_the_same_round_number_does_not_hide_the_review(tmp_path):
    store = _store(tmp_path)
    for seconds in (5, 59.8):  # Long before, and within a second of, the reviewer's own file.
        orch_events.append_event(store, START + timedelta(seconds=seconds), 7, "review", "PASS 0 findings",
                                 verdict="PASS", findings=0, round=1, source="orchestrator")
    _review(store, 7, 1, FINDING, START + timedelta(seconds=60), verdict="UNUSABLE")
    _, row = _attach(store)
    assert row["review_rounds"] == 0


def test_unjournaled_legacy_review_is_judged_from_the_text(tmp_path):
    store = _store(tmp_path)
    _review(store, 7, 1, FINDING, START + timedelta(seconds=10))
    _review(store, 7, 2, "no verdict here\n", START + timedelta(seconds=20))
    _, row = _attach(store)
    assert row["review_rounds"] == 1 and row["finding_classes"] == {"retry-context": 1}


def test_json_findings_read_the_class_field():
    text = '```json\n[{"file": "a.py", "line": 3, "summary": "x", "class": "stale-base"},' \
           ' {"file": "b.py", "line": 1, "summary": "y"}]\n```'
    assert round_classes(text) == {"stale-base", "unclassified"}
    assert round_classes(text, "UNUSABLE") is None
    assert round_classes("(none)") == set()


def test_pass_round_with_path_line_findings_counts_their_classes():
    text = "VERDICT: PASS\n- src/a.py:3 — Minor, not blocking.\n  class: naming\n- src/b.py:4 — Also minor.\n"
    assert round_classes(text) == {"naming", "unclassified"}
    assert round_classes(text, "PASS") == {"naming", "unclassified"}
    assert round_classes("VERDICT: PASS\n", "PASS") == set()


def test_verdict_line_followed_by_claude_json_findings_keeps_class_fields():
    text = ('VERDICT: FAIL\n```json\n[{"file": "a.py", "line": 3, "summary": "x", "class": "stale-base"},'
            ' {"file": "b.py", "line": 1, "summary": "y", "class": "retry-context"}]\n```\n')
    assert round_classes(text) == {"stale-base", "retry-context"}
    assert round_classes(text, "FAIL") == {"stale-base", "retry-context"}


def test_note_lines_are_never_findings_like_the_runner_reads_them():
    # A NOTE written without a space after the colon still looks like path:line to the finding pattern.
    text = "VERDICT: FAIL\nNOTE:src/a.py:3 — the root cause is upstream\n  class: noted\n- src/b.py:1 — bug\n  class: real\n"
    assert round_classes(text) == {"real"}
    assert round_classes('```json\n[{"file": "b.py", "line": 1, "summary": "y", "class": "real"}]\n```\n'
                         "NOTE:src/a.py:3 — upstream\n") == {"real"}


def test_pr_lines_belong_to_their_execution_and_unknown_is_null(tmp_path):
    from foragent.orch_facts import _shown_runs
    from foragent.orch_facts_reviews import loop_text, pr_lines

    store = _store(tmp_path)
    old = SimpleNamespace(number=7, started_at="2026-10-24T10:00:00+00:00")
    new = SimpleNamespace(number=7, started_at="2026-10-24T11:00:00+00:00")
    store.record_pr_lines(7, new.started_at, {"additions": 900, "deletions": 100, "changed_files": 2,
                                              "binary_files": 0, "head": "a" * 40, "tree": "b" * 40,
                                              "measured_at": "2026-10-24T11:30:00+00:00"})
    assert pr_lines(store, new)["additions"] == 900 and pr_lines(store, new)["tree"] == "b" * 40
    assert set(pr_lines(store, old).values()) == {None}  # another execution's measurement is not this one's
    row = {"review_rounds": 6, "spend": {"size": "s"}, "pr_lines": pr_lines(store, new)}
    assert loop_text(row, "en") == "size s, 6 review rounds, PR lines +900/-100 (tree bbbbbbb)"
    assert loop_text({**row, "pr_lines": pr_lines(store, old)}, "en").endswith("PR lines ?")
    rows = [{"status": "running", "n": 0}] + [{"status": "finished", "n": n} for n in range(1, 9)]
    assert [row["n"] for row in _shown_runs(rows)] == [0, 4, 5, 6, 7, 8]
    assert [row["n"] for row in _shown_runs(rows[:3])] == [0, 1, 2]


def test_measured_lines_count_a_rename_as_github_shows_it(git_history):
    from conftest import git_command
    from foragent.git import GitRepo
    from foragent.run_review import measure_lines

    clone = git_history.clone
    git_history.update({"big.txt": "".join(f"line {index}\n" for index in range(900)), "photo.bin": "\0\1\2"})
    git_command(clone, "fetch", "origin")
    git_command(clone, "update-ref", "HEAD", "origin/main")
    git_command(clone, "read-tree", "-u", "--reset", "HEAD")
    git = GitRepo(clone)
    git_command(clone, "mv", "big.txt", "moved.txt")
    assert measure_lines(git, "origin/main") == {"additions": 0, "deletions": 0, "changed_files": 1, "binary_files": 0}
    lines = (clone / "moved.txt").read_text().splitlines(keepends=True)
    (clone / "moved.txt").write_text("".join(["edited 0\n", *lines[1:], "added\n"]))
    git_command(clone, "mv", "photo.bin", "photo2.bin")
    (clone / "new.txt").write_text("a\nb\n")
    git_command(clone, "add", "-A")
    assert measure_lines(git, "origin/main") == {"additions": 4, "deletions": 1, "changed_files": 3, "binary_files": 1}
