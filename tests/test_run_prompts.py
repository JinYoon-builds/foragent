from dataclasses import replace

import pytest

from foragent.issue import parse_body
from foragent.runner import build_prompt, build_review_prompt, goal_condition, parse_review, run_issue
from foragent.run_skill_catalog import catalog_section
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


def test_review_prompt_truncates_only_diff_and_parse_limits_findings(tmp_path):
    # The diff keeps its limit with the shared cut notice; findings are no longer limited (#420).
    prompt = build_review_prompt(parse_body(BODY), "x" * 60000 + "TRUNCATED", full_text_dir=tmp_path)
    assert prompt.endswith("x" * 60000 + f"[cut: 9 more chars; full text: {tmp_path / 'review-diff.patch'}]")
    assert (tmp_path / "review-diff.patch").read_text() == "x" * 60000 + "TRUNCATED"
    review = parse_review("VERDICT: FAIL\n" + "\n".join(f"file:{i}: defect" for i in range(8)))
    assert review.verdict == "FAIL" and len(review.findings.splitlines()) == 8


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
    # No skill library is present here, so the implementer gets the empty catalog section.
    original = build_prompt(1, parse_body(BODY), c.ctx.config.acceptance_dir, skill_catalog=catalog_section([]))
    assert call["prompt"] == (f"/goal {goal_condition(c.ctx.config.acceptance_dir)}\n\n" if enabled else "") + original
    assert set(call) == {"worktree", "prompt", "model", "timeout_s", "log_path", "effort"}
    assert call["effort"] == "high"
    assert not (c.store.root / "runs/issue-1").exists()


@pytest.mark.parametrize("label,bug", [
    ("bug", True), ("Bug", True), ("type: bug", True), ("kind:bug", True), (" BUG ", True),
    ("debug", False), ("bugfix", False), ("bug-report", False), ("approved", False),
])
def test_bug_label_forms(label, bug):
    from foragent.run_prompts import is_bug_label

    assert is_bug_label(label) is bug


def test_bug_fix_steps_only_for_bug_label_and_points_to_reference_when_present(tmp_path):
    from foragent.run_prompts import BUG_FIX_REFERENCE, bug_fix_steps

    assert bug_fix_steps({"approved", "running", "debug"}, tmp_path) is None
    steps = bug_fix_steps({"approved", "type: bug"}, tmp_path)
    assert steps is not None and "Reproduce" in steps and "failing regression test first" in steps
    assert BUG_FIX_REFERENCE not in steps
    reference = tmp_path / BUG_FIX_REFERENCE
    reference.parent.mkdir(parents=True)
    reference.write_text("# Bug fix\n", encoding="utf-8")
    assert BUG_FIX_REFERENCE in bug_fix_steps(["bug"], tmp_path)


def test_fresh_and_resume_prompts_carry_procedure_only_when_given():
    from foragent.runner import build_resume_prompt

    parsed = parse_body(BODY)
    plain = build_prompt(1, parsed, "tests/acceptance/")
    assert "This issue is a bug." not in plain
    assert "This issue is a bug." in build_prompt(1, parsed, "tests/acceptance/", procedure="This issue is a bug.\n")
    resume = build_resume_prompt(1, BODY, BODY, "check failed", procedure="This issue is a bug.\n")
    assert "This issue is a bug." in resume
    assert "This issue is a bug." not in build_resume_prompt(1, BODY, BODY, "check failed")


@pytest.mark.parametrize("label", ["type: bug", "enhancement"])
def test_bug_steps_reach_first_attempt_and_retry_for_bug_label_only(run_case, label):
    c = run_case
    c.tracker.add_labels(1, [label])
    c.executor.steps = [{"feature.txt": "not done"}, {"feature.txt": "done"}]
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 2
    for call in c.executor.calls:
        assert ("This issue is a bug. Follow these steps:" in call["prompt"]) is (label == "type: bug")


def test_implementation_prompt_carries_the_lanes_earlier_plan_only_when_given():
    from foragent.run_prompts import lane_plan_section

    parsed = parse_body(BODY)
    section = lane_plan_section(["Reuse the writer."], ["Name the helper while implementing."])
    prompt = build_prompt(1, parsed, "tests/acceptance/", lane_plan=section)
    assert "Your lane's earlier plan" in prompt
    assert "- Reuse the writer." in prompt and "- Name the helper while implementing." in prompt
    assert "Your lane's earlier plan" not in build_prompt(1, parsed, "tests/acceptance/")


def test_resume_prompt_restates_the_lanes_earlier_plan_only_when_given():
    from foragent.run_prompts import build_resume_prompt, lane_plan_section

    section = lane_plan_section(["Reuse the writer."], [])
    assert section in build_resume_prompt(1, BODY, BODY, lane_plan=section)
    assert "Your lane's earlier plan" not in build_resume_prompt(1, BODY, BODY)
