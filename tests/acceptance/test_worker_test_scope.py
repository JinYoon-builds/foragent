"""Acceptance test for worker-test-scope. Locked by Foragent; do not edit."""

import re

from test_local_truth import record_gh
from test_orch_review_handoff import make_case


def implementer_prompt(git_history, tmp_path, monkeypatch, test_command):
    from foragent.runner import run_issue

    record_gh(monkeypatch)
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command=test_command)
    assert run_issue(1, case.ctx) == "merged"
    return " ".join(case.implementer.calls[0]["prompt"].split())


def test_worker_test_scope_prompt_no_longer_invites_the_full_suite(git_history, tmp_path, monkeypatch):
    prompt = implementer_prompt(git_history, tmp_path, monkeypatch, "uv run pytest -q")
    assert not re.search(r"(?:also )?run the full test suite", prompt, re.I), (
        "The worker must not be invited to run the full suite (the runner and CI do)")
    assert "Optional additional check" not in prompt


def test_worker_test_scope_prompt_explains_who_runs_what(git_history, tmp_path, monkeypatch):
    prompt = implementer_prompt(git_history, tmp_path, monkeypatch, "uv run pytest -q")
    assert re.search(r"runner.{0,200}(?:Done when|completion).{0,200}(?:baseline|already pass|previously pass)"
                     r".{0,200}related unit tests", prompt, re.I | re.S), (
        "Tell the worker the runner runs Done when, the passing acceptance tests and related unit tests")
    assert re.search(r"\bCI\b.{0,80}full", prompt, re.I), "Tell the worker CI runs the full suite"
    assert re.search(r"(?:you|yourself).{0,120}(?:Done when|completion).{0,160}(?:files? you (?:change|edit|touch))",
                     prompt, re.I | re.S), (
        "Tell the worker to run only the Done-when tests and the tests of the files it changed")
    assert re.search(r"parallel", prompt, re.I), "Say that no parallel test helper is needed"


def test_worker_test_scope_holds_without_a_test_command(git_history, tmp_path, monkeypatch):
    prompt = implementer_prompt(git_history, tmp_path, monkeypatch, None)
    assert not re.search(r"run the full test suite", prompt, re.I)
