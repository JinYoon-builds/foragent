"""Restart and status use persisted preparation facts, without starting agents."""

import json
import os
import subprocess
import sys

from lane_tests_case import (assert_no_publication, disk_plan, lane_case,
                             offline_publication, prepare)


def test_repeating_prepare_reuses_the_exact_reviewed_result(lane_case, capsys):
    case = lane_case
    first = prepare(case, capsys)
    before, contents = len(case.calls), case.path.read_bytes()
    second = prepare(case, capsys)
    assert first["status"] == second["status"] == "reviewed"
    assert second["base_revision"] == first["base_revision"]
    assert len(case.calls) == before and case.path.read_bytes() == contents
    assert_no_publication(case)


def test_reviewer_unavailability_resumes_review_without_rewriting_tests(lane_case, capsys):
    case = lane_case
    case.reviewer_exit = 1
    row = prepare(case, capsys)
    assert row["status"] == "waiting" and row["phase"] == "reviewing_tests"
    assert row["holds"] == 0
    case.reviewer_exit = 0
    assert prepare(case, capsys)["status"] == "reviewed"
    assert [c["role"] for c in case.calls].count("writer") == 1
    assert [c["role"] for c in case.calls].count("reviewer") == 2
    assert_no_publication(case)


def test_interrupted_writer_does_not_promote_partial_files(lane_case, capsys):
    case = lane_case
    case.writer_exit = 1
    row = prepare(case, capsys)
    assert row["status"] in {"waiting", "interrupted"}
    assert row["phase"] == "writing_tests" and row["holds"] == 0
    assert "acceptance_files" not in disk_plan(case)["issues"][0]
    assert [c["role"] for c in case.calls] == ["writer"]
    case.writer_exit = 0
    assert prepare(case, capsys)["status"] == "reviewed"
    assert_no_publication(case)


def test_status_survives_fresh_cli_invocations_and_starts_no_agent(lane_case, capsys):
    case = lane_case
    case.reviewer_exit = 1
    prepare(case, capsys)
    before = len(case.calls)
    # Child processes cannot reuse the preparation process's in-memory state.
    # Their PATH stubs prevent accidental service/model calls even on a defect.
    bindir = case.root / "offline-bin"
    bindir.mkdir()
    for name in ("gh", "codex", "claude"):
        path = bindir / name
        path.write_text("#!/bin/sh\necho 'unexpected external call' >&2\nexit 99\n")
        path.chmod(0o755)
    env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-c", "from foragent.cli import main; raise SystemExit(main())",
             "order", "prepare", str(case.path), "--status", "--json", "--repo-root", str(case.root)],
            env=env, cwd=case.root, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        row = json.loads(result.stdout)["issues"][0]
        assert row["key"] == "notice" and row["status"] == "waiting"
        assert row["phase"] == "reviewing_tests" and row["holds"] == 0
        assert row["reason"] and row["updated_at"]
    assert len(case.calls) == before
    assert_no_publication(case)


def test_status_reports_both_active_preparation_phases(lane_case, capsys):
    case = lane_case
    phases = []
    case.writer_hook = lambda worktree: phases.append(prepare(case, capsys, status=True)["phase"])
    case.reviewer_hook = lambda worktree: phases.append(prepare(case, capsys, status=True)["phase"])
    assert prepare(case, capsys)["status"] == "reviewed"
    assert phases == ["writing_tests", "reviewing_tests"]
    assert [call["role"] for call in case.calls] == ["writer", "reviewer"]
