"""Locked S2 acceptance through the public runner and real recorded CI artifacts."""

from dataclasses import replace

import pytest

from conftest import git_command
from foragent.config import load_config
from foragent.runner import run_issue
from test_ci_mode import (
    FUTURE, KEEP, OTHER, comparison_record, mode_case,
    no_external_execution,  # noqa: F401
)
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


@pytest.mark.parametrize("base", ["main", "delivery"])
@pytest.mark.parametrize("fallback", [None, "local"], ids=["ci-required", "local-fallback-allowed"])
def test_parented_ci_run_targets_configured_base_and_consumes_ci_evidence(
    git_history, tmp_path, monkeypatch, base, fallback,
):
    """Reject a default parented run targeting a batch ref or using local evidence despite usable CI."""
    case = mode_case(git_history, tmp_path, monkeypatch, fallback=fallback)
    (tmp_path / ".foragent.yml").write_text("{}\n", encoding="utf-8")
    default = load_config(tmp_path, tmp_path / "missing-global.yml")
    case.config = replace(case.config, review=default.review, base_branch=base)
    case.ctx.config = case.config
    case.tracker.add_issue(20, "Parent outcome")
    case.tracker.set_parent(1, 20)
    # An old remote ref must not select a run's destination; only base_branch does.
    git_command(case.history.origin, "update-ref", "refs/heads/foragent/batch-20", case.base_sha)
    git_command(case.history.origin, "update-ref", f"refs/heads/{base}", case.base_sha)

    outcome = run_issue(1, case.ctx)

    prs = case.tracker.created_prs
    assert prs and all(pr.base == base for pr in prs), [(pr.head, pr.base) for pr in prs]
    assert all(not pr.head.startswith("foragent/batch-") for pr in prs)
    assert outcome == "merged", case.store.log_path(1).read_text()
    assert len(prs) == 1 and case.tracker.merged_prs == [prs[0].number]
    assert case.ci.artifact_reads and case.ci.downloads, "Green check status alone is not CI evidence"
    comparison_record(case, "ci")
    assert not any(call.executed & {KEEP, OTHER, FUTURE} for call in case.processes.calls), (
        "Usable CI evidence must keep portable protected tests off the local machine"
    )


def test_explicit_fast_project_keeps_default_local_checks(git_history, tmp_path, monkeypatch):
    """Control: reject dropping local protection or changing the local product default."""
    (tmp_path / ".foragent.yml").write_text("{}\n", encoding="utf-8")
    defaults = load_config(tmp_path, tmp_path / "missing-global.yml")
    assert defaults.checks == "local"
    case = mode_case(git_history, tmp_path, monkeypatch, mode=defaults.checks)
    assert run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
    assert case.tracker.created_prs[0].base == "main"
    assert case.ci.artifact_reads == [] and case.ci.downloads == []
    assert any(KEEP in call.executed for call in case.processes.captures)
    assert KEEP in case.processes.head_ids
    comparison_record(case, "local")
