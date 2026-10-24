from dataclasses import replace

import pytest

from foragent.issue import parse_body
from foragent.runner import build_prompt, build_review_prompt, goal_condition, parse_review, run_issue
from runner_support import BODY, COMMAND, run_case


@pytest.mark.parametrize("files,configured,expected", [
    ({}, None, None),
    ({"pyproject.toml": "[project]\n", "go.mod": "module example"}, None, "uv run pytest"),
    ({"package.json": '{"scripts":{"test":"vitest"}}'}, None, "npm test"),
    ({"pyproject.toml": "[project]\n"}, "make check", "make check"),
])
def test_implementation_prompt_full_suite_from_worktree(run_case, monkeypatch, files, configured, expected):
    c = run_case
    # Exercise prompt command detection without launching the placeholder projects' test tools.
    monkeypatch.setattr("foragent.run_implementation._capture_acceptance_baseline", lambda *args: None)
    c.history.update(files)
    c.ctx.config = replace(c.ctx.config, test_command=configured)
    assert run_issue(1, c.ctx) == "merged"
    prompt = c.executor.calls[0]["prompt"]
    assert "Optional additional check" not in prompt
    assert "run the full test suite" not in prompt
    assert "You run only the Done when tests and the tests for files you change" in prompt
    assert "no parallel test helper is needed" in prompt
    if expected is None:
        assert "The runner runs Done when." in prompt
        assert "previously passing acceptance tests" not in prompt
        assert "related unit tests" not in prompt
        assert "CI runs the full suite after publication" not in prompt
    else:
        assert "The runner runs Done when, the previously passing acceptance tests and related unit tests." in prompt
        assert f"CI runs the full suite after publication (`{expected}`)" in prompt


def test_snapshot_supplies_prompt_checks_and_checklist_despite_live_body_and_comments(run_case):
    c = run_case
    c.tracker.edit_body(1, "## Goal\nLIVE UNAPPROVED\n## Done when\n```sh\nexit 99\n```\n")
    c.tracker.comment(1, "UNAPPROVED COMMENT")
    assert run_issue(1, c.ctx) == "merged"
    prompt = c.executor.calls[0]["prompt"]
    assert "Implement the approved feature." in prompt and "Other features." in prompt
    assert "LIVE UNAPPROVED" not in prompt and "UNAPPROVED COMMENT" not in prompt
    assert "exit 99" not in prompt and COMMAND in prompt
    assert "Looks right to a human" in c.tracker.created_prs[0].body


def test_prompt_is_fixed_and_retry_tail_is_bounded():
    parsed = parse_body(BODY)
    prompt = build_prompt(7, parsed, "tests/acceptance/", "discard this" + "x" * 4000)
    assert prompt.startswith("You are implementing GitHub issue #7 in this repository.\n")
    assert "- Do not modify anything under tests/acceptance/. Those tests are fixed." in prompt
    assert "write .foragent/QUESTION.md" in prompt and "Do not commit, push or switch branches." in prompt
    assert "discard this" not in prompt and prompt.endswith("x" * 4000)


@pytest.mark.parametrize("language", ["ko", "en", "ja"])
def test_prompt_try_and_evidence_in_configured_language_on_every_attempt(run_case, language):
    c = run_case
    c.ctx.config = replace(c.ctx.config, language=language)
    c.executor.steps = [{"feature.txt": "not done"}, {"feature.txt": "done"}]
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 2
    for call in c.executor.calls:
        prompt = call["prompt"]
        assert f"Write it in {language}. Include an `## Evidence` section" in prompt
        assert "issue sentence; file:line and the code line" in prompt
        assert ".foragent/TRY.md" in prompt and f"Write the prose in {language}." in prompt
        assert all(heading in prompt for heading in ("## What changed", "## Try it", "## Expect", "## Web"))


def test_review_prompt_truncates_only_diff_and_parse_limits_findings():
    prompt = build_review_prompt(parse_body(BODY), "x" * 60000 + "TRUNCATED")
    assert prompt.endswith("x" * 60000) and "TRUNCATED" not in prompt
    review = parse_review("VERDICT: FAIL\n" + "\n".join(f"file:{i}: defect" for i in range(8)))
    assert review.verdict == "FAIL" and len(review.findings.splitlines()) == 5


def test_native_goal_condition_length_cap_preserves_instructions():
    condition = goal_condition("a" * 5000)
    assert len(condition) == 3500
    assert condition.startswith('Every command in the "Done when" list below exits 0')
    assert condition.endswith("no files outside this repository are changed. Or stop after 40 turns.")


@pytest.mark.parametrize("enabled", [False, True])
def test_codex_goal_opt_in_preserves_other_options_and_acceptance_rule(run_case, enabled):
    c = run_case
    c.ctx.config = replace(c.ctx.config, codex_goal=enabled)
    assert run_issue(1, c.ctx) == "merged"
    call = c.executor.calls[0]
    original = build_prompt(1, parse_body(BODY), c.ctx.config.acceptance_dir)
    assert call["prompt"] == (f"/goal {goal_condition(c.ctx.config.acceptance_dir)}\n\n" if enabled else "") + original
    assert set(call) == {"worktree", "prompt", "model", "timeout_s", "log_path", "effort"}
    assert call["effort"] == "high"
    assert not (c.store.root / "runs/issue-1").exists()
