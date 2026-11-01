from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from foragent.executors import ExecResult
from foragent.issue import parse_body
from foragent.reasons import Reason
from foragent.runner import REVIEW_PROMPT, _review, parse_code_review, run_issue

from conftest import ACCEPTANCE, git_command
from runner_support import (
    BODY, COMMAND, REPORTED_JSON_FINDINGS, batch_setup, enable_native_reviewer, enable_reviewer,
    expected_review_rules, import_index, review_context, run_case,
)


@pytest.mark.parametrize("message,note", [
    ("VERDICT: PASS", ""),
    ("preamble\nVERDICT: PASS\nfeature.txt:1: harmless observation", ""),
])
def test_review_pass_opens_pr_and_records_review(run_case, message, note):
    c = run_case
    calls = enable_reviewer(c, [message])
    c.store.log_path(1).parent.mkdir(parents=True, exist_ok=True)
    c.store.log_path(1).write_text("PRIVATE IMPLEMENTER LOG\n")
    assert run_issue(1, c.ctx) == "merged"
    assert len(calls) == 1 and len(c.executor.calls) == 1
    call = calls[0]
    assert call["model"] == "review-model"
    assert call["worktree"] == c.executor.calls[0]["worktree"]
    assert call["prompt"].startswith(REVIEW_PROMPT)
    assert "Implement the approved feature." in call["prompt"]
    assert "Other features." in call["prompt"] and COMMAND in call["prompt"]
    assert "diff --git a/feature.txt b/feature.txt" in call["prompt"]
    assert "PRIVATE IMPLEMENTER LOG" not in call["prompt"]
    assert "untrusted model message" not in call["prompt"]
    assert "Looks right to a human" not in call["prompt"]
    body = c.tracker.created_prs[0].body
    assert "## Review" in body and "Round 1: VERDICT: PASS" in body
    if note:
        assert note in body


@pytest.mark.parametrize("merge_state", ["before", "pending", "committed"])
def test_review_prompt_excludes_main_only_changes(review_context, git_history, merge_state):
    ctx = review_context
    ctx.config = replace(ctx.config, reviewer="codex")
    import_index(ctx.git.root, "Run change")
    git_history.update({"main-only.txt": "another run's change\n"})
    ctx.git.fetch()
    if merge_state != "before":
        ctx.git.merge_base("origin/main", commit=False)
        assert ctx.git.merge_in_progress()
        if merge_state == "committed":
            import_index(ctx.git.root, "Integrate main")
    git_history.update({"later-main-only.txt": "later target change\n"})
    ctx.git.fetch()
    calls = []

    def review(**kwargs):
        calls.append(kwargs)
        return ExecResult(0, False, "VERDICT: PASS", kwargs["log_path"])

    ctx.executor_factory = lambda name: SimpleNamespace(run=review)
    assert _review(1, ctx, ctx.git, parse_body(BODY), "origin/main").verdict == "PASS"
    assert len(calls) == 1
    prompt = calls[0]["prompt"]
    assert "diff --git a/feature.txt b/feature.txt" in prompt
    assert "main-only.txt" not in prompt


def test_review_failure_retries_implementation_and_done_when(run_case):
    c = run_case
    c.executor.steps.append({})
    finding = "feature.txt:1: special-casing this input misses the actual goal"
    calls = enable_reviewer(c, [f"VERDICT: FAIL\n{finding}", "VERDICT: PASS"])
    assert run_issue(1, c.ctx) == "merged"
    assert c.store.get_run(1).attempts == 2
    assert finding in c.executor.calls[1]["prompt"]
    assert len(calls) == 2
    assert c.store.log_path(1).read_text().count("Done when command:") == 4
    body = c.tracker.created_prs[0].body
    assert "Round 1: VERDICT: FAIL" in body and finding in body
    assert "Round 2: VERDICT: PASS" in body
    assert finding in calls[1]["prompt"]
    assert "Limit this review to confirming" in calls[1]["prompt"]
    directory = c.store.root / "runs/issue-1"
    assert (directory / "review-1.txt").read_text() == f"VERDICT: FAIL\n{finding}"
    assert (directory / "review-2.txt").read_text() == "VERDICT: PASS"


@pytest.mark.parametrize("review_max,retries,rounds", [(1, 3, 2), (2, 2, 3)])
def test_exceeding_review_max_opens_pr_with_risk_high_hold(run_case, review_max, retries, rounds):
    c = run_case
    c.ctx.config = replace(c.ctx.config, review_max=review_max, max_retries=retries)
    c.executor.steps += [{}] * (rounds - 1)
    finding = "feature.txt:1: the stated goal is still broken"
    enable_reviewer(c, [f"VERDICT: FAIL\n{finding}"] * rounds)
    assert run_issue(1, c.ctx) == "held"
    assert len(c.tracker.created_prs) == 1 and not c.tracker.merged_prs
    assert c.store.get_run(1).attempts == rounds
    assert c.store.load_flag(1).reason == Reason.RISK_HIGH
    comment = c.tracker.list_comments(1)[-1].body
    assert f"Reviewer still found defects after {rounds} rounds" in comment
    assert finding in comment and "https://github.com/owner/repo/pull/2" in comment
    assert sum(f"Round {k}: VERDICT: FAIL" in c.tracker.created_prs[0].body for k in range(1, rounds + 1)) == rounds
    directory = c.store.root / "runs/issue-1"
    assert len(list(directory.glob("review-*.txt"))) == rounds
    assert all((directory / f"review-{number}.txt").read_text() == f"VERDICT: FAIL\n{finding}"
               for number in range(1, rounds + 1))


def test_review_failure_uses_shared_retry_budget(run_case):
    c = run_case
    c.executor.steps = [{"feature.txt": "wrong"}, {"feature.txt": "done"}]
    calls = enable_reviewer(c, ["VERDICT: FAIL\nfeature.txt:1: goal still broken"])
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert len(calls) == 1 and len(c.executor.calls) == 2
    assert c.tracker.created_prs == []
    assert "goal still broken" in c.tracker.list_comments(1)[-1].body


@pytest.mark.parametrize("findings,rebuttal,expected,attempts", [
    (["feature.txt:1 Payment support", "- feature.txt:1   PAYMENT support", "feature.txt:1 payment support"],
     True, "held", 3),
    (["feature.txt:1 payment support", "feature.txt:1 payment support", None], True, "merged", 3),
    (["feature.txt:1 payment support", "feature.txt:1 locale", "feature.txt:1 payment support", None],
     True, "merged", 4),
    (["feature.txt:1 payment support"] * 3 + [None], False, "merged", 4),
])
def test_review_dispute_requires_consecutive_findings_after_rebuttal(run_case, findings, rebuttal, expected, attempts):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_retries=3, review_max=4)
    c.executor.steps += ([{".foragent/REBUTTAL.md": "- payment support: outside the approved goal\n"}]
                         if rebuttal else [{}]) + [{}] * 2
    calls = enable_reviewer(c, [f"VERDICT: FAIL\n{point}" if point else "VERDICT: PASS" for point in findings])
    assert run_issue(1, c.ctx) == expected
    assert c.store.get_run(1).attempts == attempts
    assert len(calls) == len(c.executor.calls) == attempts
    if expected == "held":
        assert c.store.load_flag(1).reason == Reason.REVIEW_DISPUTE
        assert c.tracker.get_issue(1).labels & {"blocked", "needs-human"} == {"blocked"}
        assert "## Rebuttals" in c.tracker.created_prs[0].body
        assert not c.tracker.merged_prs
    else:
        assert c.store.load_flag(1) is None


def test_unstructured_reviews_with_shared_preamble_do_not_create_a_dispute(run_case):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_retries=3, review_max=4)
    c.executor.steps += [{".foragent/REBUTTAL.md": "- points: outside the approved goal\n"}, {}, {}]
    calls = enable_reviewer(c, [
        "Please inspect:\npayment support", "Please inspect:\nlocale support",
        "Please inspect:\nemail support", "VERDICT: PASS",
    ])
    assert run_issue(1, c.ctx) == "merged"
    assert len(calls) == 4
    assert c.store.load_flag(1) is None


def test_repeated_unstructured_finding_normalizes_wrapping_after_rebuttal(run_case):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_retries=3, review_max=0)
    c.executor.steps += [{".foragent/REBUTTAL.md": "- payment support: outside the approved goal\n"}, {}, {}]
    calls = enable_reviewer(c, ["Needs payment support.", "Needs\npayment support.",
                                "  NEEDS payment   support.\n", "VERDICT: PASS"])
    assert run_issue(1, c.ctx) == "merged"
    assert len(calls) == len(c.executor.calls) == 4
    assert c.store.load_flag(1) is None


def test_rebuttal_history_reaches_pr_and_codex_review_even_when_executor_stages_it(run_case, monkeypatch):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_retries=2, review_max=3)
    first = "- payment support: outside the approved goal\n"
    second = "- locale support: outside the approved goal\n"
    c.executor.steps += [{".foragent/REBUTTAL.md": first}, {".foragent/REBUTTAL.md": second}]
    implement = c.executor.run

    def stage_rebuttal(**kwargs):
        result = implement(**kwargs)
        if (kwargs["worktree"] / ".foragent/REBUTTAL.md").is_file():
            git_command(kwargs["worktree"], "add", "-f", ".foragent/REBUTTAL.md")
        return result

    monkeypatch.setattr(c.executor, "run", stage_rebuttal)
    calls = enable_reviewer(c, ["VERDICT: FAIL\nfeature.txt:1 payment support",
                                "VERDICT: FAIL\nfeature.txt:1 locale support", "VERDICT: PASS"])
    assert run_issue(1, c.ctx) == "merged"
    assert "Implementer rebuttals (judge them):\n" + first in calls[1]["prompt"]
    assert "Implementer rebuttals (judge them):\n" + second in calls[2]["prompt"]
    body = c.tracker.created_prs[0].body
    assert "## Rebuttals" in body and first in body and second in body
    assert ".foragent/REBUTTAL.md" not in git_command(c.history.origin, "ls-tree", "-r", "--name-only", "foragent/issue-1")


def test_reviewer_edits_are_discarded_before_commit(run_case):
    c = run_case

    def edit(worktree):
        (worktree / "feature.txt").write_text("broken")
        (worktree / ACCEPTANCE).unlink()
        (worktree / "rogue.txt").write_text("must not reach PR")
        git_command(worktree, "add", "-A")
        (worktree / "feature.txt").write_text("unstaged reviewer edit")

    enable_reviewer(c, ["VERDICT: PASS"], edit)
    assert run_issue(1, c.ctx) == "merged"
    branch = "foragent/issue-1"
    assert git_command(c.history.origin, "show", f"{branch}:feature.txt") == "done"
    assert git_command(c.history.origin, "rev-parse", f"{branch}:{ACCEPTANCE}") == c.store.load_approval(1).acceptance_blobs[ACCEPTANCE]
    assert "rogue.txt" not in git_command(c.history.origin, "ls-tree", "-r", "--name-only", branch)
    assert "Reviewer changes discarded; staged index restored." in c.tracker.created_prs[0].body


def test_disabled_review_does_not_create_a_reviewer(run_case):
    c = run_case
    requested = []
    c.ctx.executor_factory = lambda name: requested.append(name) or c.executor
    assert run_issue(1, c.ctx) == "merged"
    assert requested == ["codex"]
    assert "Disabled (reviewer: none)" in c.tracker.created_prs[0].body


@pytest.mark.parametrize("invalid", [
    ExecResult(0, False, "No defects found.", Path("unused")),
    ExecResult(0, False, "feature.txt:1 — the retry loses the selected locale", Path("unused")),
    ExecResult(1, False, "VERDICT: PASS", Path("unused")),
    ExecResult(None, False, "VERDICT: PASS", Path("unused")),
    ExecResult(0, True, "VERDICT: PASS", Path("unused")),
    ExecResult(0, False, "VERDICT: PASS-ish", Path("unused")),
    ExecResult(0, False, "VERDICT: PASS with caveats", Path("unused")),
])
@pytest.mark.parametrize("retry", ["pass", "fail", "invalid"])
def test_unusable_review_retries_implementation_using_the_shared_budget(run_case, invalid, retry):
    c = run_case
    c.executor.steps.append({})
    second = {"pass": "VERDICT: PASS", "fail": "VERDICT: FAIL\nfeature.txt:1: broken", "invalid": invalid}[retry]
    calls = enable_reviewer(c, [invalid, second])
    assert run_issue(1, c.ctx) == ("merged" if retry == "pass" else "retries-exceeded")
    assert len(calls) == len(c.executor.calls) == 2
    assert c.store.get_run(1).attempts == 2
    raw = invalid.full_message if invalid.full_message is not None else invalid.last_message
    assert "A reviewer said (verbatim):\n" + raw in c.executor.calls[1]["prompt"]
    assert c.store.log_path(1).read_text().count("Done when command:") == 4
    if retry == "pass":
        assert c.tracker.merged_prs == [2] and c.store.load_flag(1) is None
        body = c.tracker.created_prs[0].body
        assert "Round 1: VERDICT: UNUSABLE" in body and "Round 2: VERDICT: PASS" in body
    else:
        assert not c.tracker.merged_prs and c.tracker.get_issue(1).state == "open"
        assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED


def test_full_claude_verdict_survives_output_tail_truncation(run_case, monkeypatch):
    import sys
    from foragent.executors import ClaudeExecutor

    c = run_case
    c.executor.steps.append({})
    finding = "- feature.txt:1 — " + "x" * 930
    messages = iter(["VERDICT: FAIL\n" + "\n".join([finding] * 5)])
    monkeypatch.setattr("foragent.executors.claude_review_command", lambda level, model, *, pr=None: [
        sys.executable, "-c", f"print({json.dumps({'result': next(messages)})!r})",
    ])
    monkeypatch.setattr("foragent.executors.claude_command", lambda *args, **kwargs: [
        sys.executable, "-c", f"print({json.dumps({'type': 'result', 'result': 'VERDICT: PASS'})!r})",
    ])
    c.ctx.config = replace(c.ctx.config, reviewer="claude")
    c.ctx.executor_factory = lambda name: c.executor if name == "codex" else ClaudeExecutor()
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 2
    assert finding in c.executor.calls[1]["prompt"] or "x" * 900 in c.executor.calls[1]["prompt"]
    assert "Round 1: VERDICT: FAIL" in c.tracker.created_prs[0].body
    assert "Round 2: VERDICT: PASS" in c.tracker.created_prs[0].body
    directory = c.store.root / "runs/issue-1"
    assert (directory / "review-1.txt").read_text() == "VERDICT: FAIL\n" + "\n".join([finding] * 5)
    assert (directory / "review-2.txt").read_text() == "VERDICT: PASS"


def test_reviewer_timeout_at_runtime_limit_does_not_launch_more_sessions(run_case):
    c = run_case

    def consume_runtime(worktree):
        c.now[0] += timedelta(minutes=c.ctx.config.max_runtime_minutes)

    timeout = ExecResult(0, True, "VERDICT: PASS", Path("unused"))
    calls = enable_reviewer(c, [timeout], consume_runtime)
    assert run_issue(1, c.ctx) == "timeout"
    assert len(calls) == len(c.executor.calls) == 1
    assert not c.tracker.created_prs and not c.tracker.merged_prs
    assert c.store.load_flag(1).reason == Reason.TIMEOUT


def test_reviewer_commit_is_detected_and_head_index_and_files_are_restored(run_case):
    c = run_case
    baseline = git_command(c.history.clone, "rev-parse", "origin/main")
    reviewer_heads = []

    def reviewer_commit(worktree):
        import_index(worktree, "Reviewer committed the implementation")
        reviewer_heads.append(git_command(worktree, "rev-parse", "HEAD"))

    enable_reviewer(c, ["VERDICT: PASS"], reviewer_commit)
    assert run_issue(1, c.ctx) == "merged"
    assert reviewer_heads[0] != baseline
    assert git_command(c.history.origin, "rev-parse", "foragent/issue-1^") == baseline
    assert git_command(c.history.origin, "show", "foragent/issue-1:feature.txt") == "done"
    assert "Reviewer changes discarded" in c.tracker.created_prs[0].body


@pytest.mark.parametrize("batch", [False, True])
def test_latin1_source_reaches_review_and_pr_without_corruption(run_case, monkeypatch, batch):
    c = run_case
    if batch:
        batch_setup(c)
        c.ctx.config = replace(c.ctx.config, review="fast", review_paths={"latin1.txt": "batch"})
    implement = c.executor.run

    def with_latin1(**kwargs):
        result = implement(**kwargs)
        (kwargs["worktree"] / "latin1.txt").write_bytes(b"caf\xe9\n")
        return result

    monkeypatch.setattr(c.executor, "run", with_latin1)
    calls = enable_reviewer(c, ["VERDICT: PASS"])
    assert run_issue(1, c.ctx) == "merged"
    assert "caf\ufffd" in calls[0]["prompt"]
    raw = subprocess.run(
        ["git", "show", "foragent/issue-1:latin1.txt"], cwd=c.history.origin, check=True, capture_output=True,
    ).stdout
    assert raw == b"caf\xe9\n"


@pytest.mark.parametrize("text,verdict", [
    ("\n(none)\n", "PASS"),
    ("(none)\nNo defects found.", "PASS"),
    ("- src/file.py:12 — broken feature", "FAIL"),
    ("- src/file.py:12 - broken feature", "FAIL"),
    ("preamble\n- src/file.py:12 -- broken feature", "FAIL"),
    ("- src/array[].py:12 — broken feature", "FAIL"),
    ("- src/file.py:12 — mishandles []", "FAIL"),
    ("- src/app/[123]/page.tsx:12 — broken feature", "FAIL"),
    ("(none)\n- file:1 — defect", "FAIL"),
    ("There are no changes to review.", "UNUSABLE"),
    ("No staged or unstaged changes.", "UNUSABLE"),
    ("No findings", "UNUSABLE"), ("", "UNUSABLE"),
    ("preamble\n(none)", "UNUSABLE"),
    ("- src/file.py:no-line — broken", "UNUSABLE"),
])
def test_code_review_result_parsing(text, verdict):
    message = f"VERDICT: {verdict}\n{text}" if verdict != "UNUSABLE" else text
    result = parse_code_review(json.dumps({"result": message}))
    assert result.verdict == verdict
    if verdict == "FAIL":
        assert result.findings == next(line for line in text.splitlines() if line.startswith("- "))


def test_code_review_plain_finding_reports_failure():
    text = "src/foragent/adopt.py:180 — I couldn't confirm this one, because ..."
    result = parse_code_review(json.dumps({"result": "VERDICT: FAIL\n" + text}))
    assert result.verdict == "FAIL"
    assert result.findings == text


def test_code_review_numbered_backtick_finding_reports_failure():
    text = '1. `src/foragent/design.py:50` — `path.read_text(encoding="utf-8")` has no error handling.'
    result = parse_code_review(json.dumps({"result": "VERDICT: FAIL\n" + text}))
    assert result.verdict == "FAIL"
    assert result.findings == text


@pytest.mark.parametrize("message", ["garbage", "[]", "null", "{}", '{"result":null}',
                                          '{"result":"(none)","is_error":true}'])
def test_code_review_invalid_json_or_error_is_unusable(message):
    assert parse_code_review(message).verdict == "UNUSABLE"


@pytest.mark.parametrize("wrapper", [
    "{}", " \n{}\n\t", "```json\n{}\n```", "```\n{}\n```",
    "```text\n{}\n```", "```python\n{}\n```",
    "[Review notes]\n```json\n{}\n```",
    "FAIL: foo([]) crashes.\n```json\n{}\n```\nEnd of review.",
    "FAIL: foo([]) crashes.\r\n```json\r\n{}\r\n```\r\nEnd of review.",
])
@pytest.mark.parametrize("items", [[], REPORTED_JSON_FINDINGS])
def test_code_review_accepts_reported_json_array_shape(wrapper, items):
    text = ("VERDICT: FAIL\n" if items else "VERDICT: PASS\n") + wrapper.format(json.dumps(items, ensure_ascii=False, indent=2))
    result = parse_code_review(json.dumps({"result": text}))
    assert result.verdict == ("FAIL" if items else "PASS")
    assert result.findings == "\n".join(f"{item['file']}:{item['line']} — {item['summary']}" for item in items)


@pytest.mark.parametrize("first,second", [([], REPORTED_JSON_FINDINGS), (REPORTED_JSON_FINDINGS, [])])
@pytest.mark.parametrize("wrapper", ["{}", "```json\n{}\n```"])
def test_code_review_multiple_json_arrays_are_unusable(first, second, wrapper):
    text = wrapper.format(json.dumps(first)) + "\n" + wrapper.format(json.dumps(second))
    from foragent.run_review import code_review_findings

    assert code_review_findings(text)[0] == "UNUSABLE"


@pytest.mark.parametrize("items", [[], REPORTED_JSON_FINDINGS])
@pytest.mark.parametrize("wrapper", [
    "Review findings: {}\nEnd of review.", "FAIL: foo([]) crashes.\n{}",
    "FAIL: foo([]) crashes.\n```\n{}\n```", "Review:\n```python\n{}\n```",
    "Example `[]`, then {}", "[Review notes]\n{}",
])
def test_code_review_arrays_outside_findings_payload_are_unusable(wrapper, items):
    from foragent.run_review import code_review_findings

    assert code_review_findings(wrapper.format(json.dumps(items)))[0] == "UNUSABLE"


@pytest.mark.parametrize("item", [
    {"file": "src/foo.py"}, {"line": 12}, {"summary": "Empty input crashes"},
    {"file": "src/foo.py", "line": 12}, {"summary": "mishandles []", "details": []},
])
def test_code_review_partial_findings_fail(item):
    result = parse_code_review(json.dumps({"result": "VERDICT: FAIL\n" + json.dumps([item])}))
    assert result.verdict == "FAIL" and result.findings


@pytest.mark.parametrize("text", [
    "[", "[{", '[{"file":"x.py","line":1,"summary":"broken","details":[],}]',
    '[{"file":"x.py","line":1,"summary":"broken","details":[]}',
    '[{}, {"file":"x.py","line":1,"summary":"broken"}]',
    '["unexpected finding"]', '[null]', '[123]', '[[]]', '[{"details": []}]',
    '[{"summary":"broken"}, "unexpected"]', '[{"summary":"broken"}, null]',
    '(none)\n```json\n["unexpected"]\n```',
    '(none)\n```json\n[]\n```\n```json\n[]\n```',
])
def test_code_review_bad_arrays_are_unusable_without_passing_on_nested_empty_arrays(text):
    from foragent.run_review import code_review_findings

    assert code_review_findings(text)[0] == "UNUSABLE"


def test_code_review_json_array_reports_every_item():
    items = [dict(REPORTED_JSON_FINDINGS[0], line=number) for number in range(1, 8)]
    result = parse_code_review(json.dumps({"result": "VERDICT: FAIL\n" + json.dumps(items)}))
    assert result.verdict == "FAIL"
    assert result.findings.splitlines() == [f"src/foragent/runner.py:{number} — JSON 배열 지적이 판정 불가로 처리됨"
                                          for number in range(1, 8)]


@pytest.mark.parametrize("last_result,outcome", [("VERDICT: PASS\n(none)", "merged"), ("No verdict", "retries-exceeded")])
@pytest.mark.parametrize("checks", ["local", "ci"])
def test_pr_body_reviews_published_head_and_keeps_findings_in_comments(run_case, last_result, outcome, checks):
    from foragent.ci_workflow import EXCLUSIONS_TRAILER

    c = run_case
    c.ctx.config = replace(c.ctx.config, reviewer="claude", review_scope="pr-body", checks=checks,
                           checks_fallback="local", test_command=f"{sys.executable} -m pytest")
    c.executor.steps.append({"fix.txt": "fixed"})
    finding = "feature.txt:1 — Fix the feature"
    messages = iter(["VERDICT: FAIL\n" + finding, last_result])
    heads = []

    def review(**kwargs):
        pr = c.tracker.created_prs[0]
        if not heads:
            assert kwargs["review_pr"] == pr.number
        else:
            assert "review_pr" not in kwargs and "review_level" not in kwargs
            assert finding in kwargs["prompt"] and "Limit this review to confirming" in kwargs["prompt"]
        assert not (kwargs["worktree"] / "CLAUDE.local.md").exists()
        assert "## Review scope" in pr.body and finding not in pr.body
        assert git_command(kwargs["worktree"], "diff", "--cached", "--name-only") == ""
        head = git_command(kwargs["worktree"], "rev-parse", "HEAD")
        assert head == git_command(c.history.origin, "rev-parse", "foragent/issue-1")
        assert head == c.tracker.pr_head_sha(pr.number)
        if checks == "ci":
            value = git_command(c.history.origin, "log", "-1", f"--format=%(trailers:key={EXCLUSIONS_TRAILER},valueonly)", head)
            assert json.loads(value) == [ACCEPTANCE]
        message = next(messages)
        raw = json.dumps({"result": message}) if not heads else (
            "VERDICT: PASS" if message == "(none)" else message)
        heads.append(head)
        return ExecResult(0, False, raw, kwargs["log_path"])

    c.ctx.executor_factory = lambda name: c.executor if name == "codex" else SimpleNamespace(run=review)
    assert run_issue(1, c.ctx) == outcome
    assert len(heads) == 2 and heads[0] != heads[1]
    assert finding in c.executor.calls[1]["prompt"]
    comments = c.tracker.list_comments(c.tracker.created_prs[0].number)
    assert any(finding in comment.body for comment in comments)
    if outcome != "merged":
        assert any(last_result in comment.body for comment in comments)


@pytest.mark.parametrize("mutates_file", [False, True], ids=["no-op", "changed-file"])
def test_review_of_legacy_head_preserves_tracked_exclusions(run_case, mutates_file):
    from foragent.ci_workflow import read_exclusions
    from foragent.git import GitRepo
    from foragent.runner import review_only
    from runner_support import set_done_when

    c = run_case
    legacy_file = ".foragent/acceptance-exclusions.json"
    command = f"printf '[\"changed\"]' > {legacy_file}" if mutates_file else "true"
    set_done_when(c, [command])
    c.tracker.edit_body(1, c.store.load_approval(1).body)
    c.store.finish_run(1, outcome="held")
    c.now[0] += timedelta(seconds=1)
    c.tracker.remove_labels(1, ["running"])
    base = c.history.tip
    c.history.update({"feature.txt": "done", legacy_file: "[]\n"})
    head = c.history.tip
    pr = c.tracker.create_pr(head="foragent/issue-1", base="main", title="Legacy PR", body="Review")
    c.tracker.set_pr_head_sha(pr, head)
    git_command(c.history.origin, "update-ref", "refs/heads/foragent/issue-1", head)
    git_command(c.history.origin, "update-ref", "refs/heads/main", base)
    assert read_exclusions(GitRepo(c.history.origin), base) is None

    def review(worktree):
        assert git_command(worktree, "ls-files", "--", legacy_file) == legacy_file
        assert git_command(worktree, "diff", "--cached", "HEAD") == ""

    calls = enable_reviewer(c, ["VERDICT: PASS"], review)
    c.operations.clear()
    assert review_only(1, c.ctx) == ("checks-failed" if mutates_file else "merged")
    assert len(calls) == (0 if mutates_file else 1)
    assert c.tracker.merged_prs == ([] if mutates_file else [pr])
    assert not c.executor.calls
    assert not any("commit" in args or "push" in args for _, args in c.operations)
    assert git_command(c.history.origin, "rev-parse", "foragent/issue-1") == head
    assert git_command(c.history.origin, "show", f"{head}:{legacy_file}") == "[]"


@pytest.mark.parametrize("original", [None, b"", b"# Personal instructions\r\nKeep exact bytes: \xff\n"],
                         ids=["absent", "empty", "existing-bytes"])
@pytest.mark.parametrize("outcome", ["pass", "fail", "unusable", "timeout", "nonzero", "exception", "force-stage"])
def test_claude_review_scope_restores_original_or_removes_temporary_file(review_context, original, outcome):
    ctx = review_context
    ctx.config = replace(ctx.config, acceptance_dir="locked checks/other/")
    path = ctx.git.root / "CLAUDE.local.md"
    if original is not None:
        path.write_bytes(original)
        path.chmod(0o640)
    tree = ctx.git.staged_tree_sha()
    head = ctx.git.head_sha()
    expected = (
        "# Review scope (written by Foragent for this review only)\n\n"
        + BODY + "\n\n" + expected_review_rules(ctx.config.acceptance_dir) + "\n\n"
    ).encode() + (original if original is not None else b"")
    calls = []

    def review(**kwargs):
        calls.append(kwargs)
        assert kwargs["prompt"] == "" and kwargs["review_level"] == "low"
        assert path.read_bytes() == expected
        assert git_command(ctx.git.root, "check-ignore", "CLAUDE.local.md") == "CLAUDE.local.md"
        assert "CLAUDE.local.md" not in ctx.git.changed_paths("HEAD")
        if outcome == "force-stage":
            git_command(ctx.git.root, "add", "-f", "--", "CLAUDE.local.md")
        path.write_bytes(b"reviewer changed the local instructions")
        if outcome == "exception":
            raise RuntimeError("reviewer crashed")
        raw = {"pass": "VERDICT: PASS\n[]", "fail": 'VERDICT: FAIL\n[{"summary":"Goal is not met"}]', "unusable": "unrecognized"}.get(outcome, "VERDICT: PASS\n[]")
        return ExecResult(1 if outcome == "nonzero" else 0, outcome == "timeout",
                          json.dumps({"result": raw}), kwargs["log_path"])

    ctx.executor_factory = lambda name: SimpleNamespace(run=review)
    if outcome == "exception":
        with pytest.raises(RuntimeError, match="reviewer crashed"):
            _review(1, ctx, ctx.git, parse_body("Different parsed body"), "HEAD")
    else:
        result = _review(1, ctx, ctx.git, parse_body("Different parsed body"), "HEAD")
        assert result.verdict == {"pass": "PASS", "fail": "FAIL", "force-stage": "PASS"}.get(outcome, "UNUSABLE")
        assert any("changes discarded" in note for note in result.notes) == (outcome == "force-stage")
    assert len(calls) == 1
    if original is None:
        assert not path.exists()
    else:
        assert path.read_bytes() == original and path.stat().st_mode & 0o777 == 0o640
    assert ctx.git.staged_tree_sha() == tree and ctx.git.head_sha() == head
    assert git_command(ctx.git.root, "ls-files", "--", "CLAUDE.local.md") == ""


def test_claude_review_scope_does_not_accumulate_between_reviews(review_context):
    ctx = review_context
    path = ctx.git.root / "CLAUDE.local.md"
    original = b"Existing instructions\r\n"
    path.write_bytes(original)
    observed = []

    def review(**kwargs):
        observed.append(path.read_bytes())
        return ExecResult(0, False, json.dumps({"result": "VERDICT: PASS\n[]"}), kwargs["log_path"])

    ctx.executor_factory = lambda name: SimpleNamespace(run=review)
    for round_number in (1, 2):
        assert _review(1, ctx, ctx.git, parse_body(BODY), "HEAD", round_number=round_number).verdict == "PASS"
        assert path.read_bytes() == original
    assert observed[0] == observed[1]
    assert observed[0].count(b"# Review scope (written by Foragent for this review only)") == 1
    exclude = ctx.git.root / ".git/info/exclude"
    assert exclude.read_bytes().splitlines().count(b"/CLAUDE.local.md") == 1


def test_claude_review_scope_preserves_a_local_instructions_symlink(review_context):
    ctx = review_context
    target = ctx.git.root.parent / "personal-instructions.md"
    target.write_bytes(b"Original target\r\n")
    path = ctx.git.root / "CLAUDE.local.md"
    path.symlink_to(target)

    def review(**kwargs):
        assert not path.is_symlink() and path.read_bytes().endswith(target.read_bytes())
        path.write_bytes(b"reviewer change")
        raise RuntimeError("reviewer crashed")

    ctx.executor_factory = lambda name: SimpleNamespace(run=review)
    with pytest.raises(RuntimeError, match="reviewer crashed"):
        _review(1, ctx, ctx.git, parse_body(BODY), "HEAD")
    assert path.is_symlink() and os.readlink(path) == str(target)
    assert target.read_bytes() == b"Original target\r\n"


def test_codex_review_gets_the_same_rules_without_a_temporary_scope_file(review_context):
    ctx = review_context
    ctx.config = replace(ctx.config, reviewer="codex", acceptance_dir="custom/locked-checks/")
    path = ctx.git.root / "CLAUDE.local.md"
    original = b"Personal instructions\r\n"
    path.write_bytes(original)

    def review(**kwargs):
        assert path.read_bytes() == original
        assert expected_review_rules(ctx.config.acceptance_dir) in kwargs["prompt"]
        assert "review_level" not in kwargs
        return ExecResult(0, False, "VERDICT: PASS", kwargs["log_path"])

    ctx.executor_factory = lambda name: SimpleNamespace(run=review)
    assert _review(1, ctx, ctx.git, parse_body(BODY), "HEAD").verdict == "PASS"
    assert path.read_bytes() == original


@pytest.mark.parametrize("fenced", [False, True])
def test_native_json_findings_trigger_prompt_confirmation_and_save_raw_results(run_case, fenced):
    c = run_case
    c.executor.steps.append({})
    raw = json.dumps(REPORTED_JSON_FINDINGS, ensure_ascii=False, indent=2)
    if fenced:
        raw = f"```json\n{raw}\n```"
    raw = "VERDICT: FAIL\n" + raw
    calls = enable_native_reviewer(c, [json.dumps({"result": raw}), "VERDICT: PASS"])
    assert run_issue(1, c.ctx) == "merged"
    assert len(calls) == len(c.executor.calls) == 2
    for item in REPORTED_JSON_FINDINGS:
        assert f"{item['file']}:{item['line']} — {item['summary']}" in c.executor.calls[1]["prompt"]
    assert "Round 1: VERDICT: FAIL" in c.tracker.created_prs[0].body
    assert "Round 2: VERDICT: PASS" in c.tracker.created_prs[0].body
    directory = c.store.root / "runs/issue-1"
    assert (directory / "review-1.txt").read_text() == raw
    assert (directory / "review-2.txt").read_text() == "VERDICT: PASS"
    assert raw in calls[1]["prompt"] and "review_level" not in calls[1]
    assert "CLAUDE.local.md" not in git_command(c.history.origin, "ls-tree", "-r", "--name-only", "foragent/issue-1")
    assert not c.store.worktree_path(1).exists()


@pytest.mark.parametrize("fenced", [False, True])
def test_native_prose_empty_array_cannot_skip_defects_and_merge(run_case, fenced):
    c = run_case
    finding = {"file": "src/foo.py", "line": 12, "summary": "Empty input crashes"}
    payload = json.dumps([finding])
    raw = "FAIL: foo([]) crashes.\n" + (f"```json\n{payload}\n```" if fenced else payload)
    if fenced:
        raw = "VERDICT: FAIL\n" + raw
        c.executor.steps.append({})
        messages = [json.dumps({"result": raw}), "VERDICT: PASS"]
    else:
        c.executor.steps.append({})
        messages = [json.dumps({"result": raw})] * 2
    calls = enable_native_reviewer(c, messages)
    assert run_issue(1, c.ctx) == ("merged" if fenced else "retries-exceeded")
    assert len(calls) == 2
    assert len(c.executor.calls) == 2
    if fenced:
        assert "src/foo.py:12 — Empty input crashes" in c.executor.calls[1]["prompt"]
        assert "Round 1: VERDICT: FAIL" in c.tracker.created_prs[0].body
    else:
        assert c.tracker.merged_prs == []
        assert "A reviewer said (verbatim):\n" + raw in c.executor.calls[1]["prompt"]
    assert (c.store.root / "runs/issue-1/review-1.txt").read_text() == raw


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("exit_code,timed_out", [(0, False), (1, False), (0, True)])
def test_unusable_review_saves_full_result_and_hands_off_full_raw_text(run_case, native, exit_code, timed_out):
    c = run_case
    c.executor.steps.append({})
    first = "First raw result\n" + "가" * 4000 + "\nFIRST END"
    second = "Latest raw result\n" + "나" * 4000 + "\nSECOND END"
    messages = [ExecResult(exit_code, timed_out, "only a tail", Path("unused"),
                           full_message=json.dumps({"result": raw}, ensure_ascii=False) if native else raw)
                for raw in (first, second)]
    (enable_native_reviewer if native else enable_reviewer)(c, messages)
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert "blocked" in c.tracker.get_issue(1).labels
    assert c.tracker.merged_prs == []
    directory = c.store.root / "runs/issue-1"
    assert (directory / "review-1.txt").read_text() == first
    assert (directory / "review-2.txt").read_text() == second
    assert "A reviewer said (verbatim):\n" + first in c.executor.calls[1]["prompt"]
    assert second in c.tracker.list_comments(1)[-1].body
    assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED


def test_unusable_review_followed_by_fail_keeps_both_raw_results(run_case):
    c = run_case
    c.executor.steps.append({})
    raw = "Unrecognized output\n" + "x" * 3500
    enable_native_reviewer(c, [json.dumps({"result": raw}), json.dumps({"result": "VERDICT: FAIL\n" + json.dumps(REPORTED_JSON_FINDINGS)})])
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert "A reviewer said (verbatim):\n" + raw in c.executor.calls[1]["prompt"]
    assert (c.store.root / "runs/issue-1/review-1.txt").read_text() == raw
    assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED


def test_invalid_native_envelope_is_saved_and_handed_off_unchanged(run_case):
    c = run_case
    c.executor.steps.append({})
    raw = "Not a JSON response\n" + "x" * 3500
    enable_native_reviewer(c, [raw, raw])
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert (c.store.root / "runs/issue-1/review-2.txt").read_text() == raw
    assert "A reviewer said (verbatim):\n" + raw in c.executor.calls[1]["prompt"]


@pytest.mark.parametrize("invalid", [
    "garbage", json.dumps({"result": "No changes to review."}),
    json.dumps({"result": "(none)", "is_error": True}),
    ExecResult(1, False, json.dumps({"result": "VERDICT: PASS\n(none)"}), Path("unused")),
])
@pytest.mark.parametrize("recover", [False, True])
def test_native_unusable_review_retries_implementation_then_passes_or_exhausts_budget(run_case, invalid, recover):
    c = run_case
    c.executor.steps.append({})
    calls = enable_native_reviewer(c, [invalid, json.dumps({"result": "VERDICT: PASS\n(none)"}) if recover else invalid])
    assert run_issue(1, c.ctx) == ("merged" if recover else "retries-exceeded")
    assert len(calls) == len(c.executor.calls) == 2
    assert all(call["prompt"] == "" and call["review_level"] == "xhigh" for call in calls)
    if not recover:
        assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    else:
        assert "reviewer: /code-review xhigh" in c.tracker.created_prs[0].body


def test_native_review_findings_repair_and_reviewer_commits_are_restored(run_case):
    c = run_case
    c.executor.steps.append({})
    finding = "- feature.txt:1 — feature is broken"

    def edit(worktree):
        (worktree / "rogue.txt").write_text("reviewer edit")
        git_command(worktree, "add", "rogue.txt")
        import_index(worktree, "Reviewer commit")
        (worktree / "feature.txt").write_text("unstaged edit")

    calls = enable_native_reviewer(c, [json.dumps({"result": "VERDICT: FAIL\n" + finding}), "VERDICT: PASS"], edit)
    assert run_issue(1, c.ctx) == "merged"
    assert len(calls) == len(c.executor.calls) == 2
    assert finding in c.executor.calls[1]["prompt"]
    assert git_command(c.history.origin, "show", "foragent/issue-1:feature.txt") == "done"
    assert "rogue.txt" not in git_command(c.history.origin, "ls-tree", "-r", "--name-only", "foragent/issue-1")
    assert "Reviewer changes discarded" in c.tracker.created_prs[0].body


@pytest.mark.parametrize("resolved", [False, True])
def test_review_returns_conflicted_pr_to_implementer_even_with_pass_verdict(run_case, resolved):
    from foragent.runner import review_only

    c = run_case
    c.store.finish_run(1, outcome="held")
    c.now[0] += timedelta(seconds=1)
    c.tracker.remove_labels(1, ["running"])
    base = c.history.tip
    c.history.update({"feature.txt": "done", "notes.txt": "<<<<<<< HEAD\n||||||| ancestor\n=======\n>>>>>>> target\n"})
    head = c.history.tip
    pr = c.tracker.create_pr(head="foragent/issue-1", base="main", title="Existing PR", body="Review")
    c.tracker.set_pr_head_sha(pr, head)
    git_command(c.history.origin, "update-ref", "refs/heads/foragent/issue-1", head)
    git_command(c.history.origin, "update-ref", "refs/heads/main", base)
    enable_reviewer(c, ["VERDICT: PASS"])
    c.executor.steps = [{"notes.txt": "resolved\n"}]
    assert review_only(1, c.ctx, verdict_head=head if resolved else None) == "merged"
    assert len(c.executor.calls) == 1
    assert "Conflict marker left: notes.txt:1" in c.executor.calls[0]["prompt"]
    assert c.tracker.merged_prs == [pr]
    assert git_command(c.history.origin, "show", "foragent/issue-1:notes.txt") == "resolved"
    journal = c.store.root / "orch-events.jsonl"
    reviews = [event for line in journal.read_text().splitlines()
               if (event := json.loads(line))["kind"] == "review"]
    # The orchestrator's verdict for the PR head is journaled as its own review source.
    assert ("orchestrator" in {event["source"] for event in reviews}) is resolved
    assert all(event["verdict"] == "PASS" and event["findings"] == 0 for event in reviews)


def test_review_text_about_limits_uses_normal_review_failure_budget(run_case):
    c = run_case
    c.executor.steps = [{"feature.txt": "done"}] * 2
    calls = enable_reviewer(c, ["VERDICT: FAIL\nfeature.py:503: expected quota=10"] * 2)
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert len(calls) == 2 and c.store.get_run(1).attempts == 2
    assert c.store.get_run(1).waited_seconds == 0


def test_pr_head_verification_reports_every_failing_command(run_case):
    """#420: PR-head checks run every Done-when command and keep each failure for the retry hint."""
    from foragent import cuts
    from foragent.runner import review_only
    from runner_support import set_done_when

    c = run_case
    set_done_when(c, ["echo first-broken; exit 1", "echo second-broken; exit 2"])
    c.tracker.edit_body(1, c.store.load_approval(1).body)
    c.store.finish_run(1, outcome="held")
    c.now[0] += timedelta(seconds=1)
    c.tracker.remove_labels(1, ["running"])
    base = c.history.tip
    c.history.update({"feature.txt": "done"})
    head = c.history.tip
    pr = c.tracker.create_pr(head="foragent/issue-1", base="main", title="PR", body="Review")
    c.tracker.set_pr_head_sha(pr, head)
    git_command(c.history.origin, "update-ref", "refs/heads/foragent/issue-1", head)
    git_command(c.history.origin, "update-ref", "refs/heads/main", base)
    enable_reviewer(c, ["VERDICT: PASS"])
    assert review_only(1, c.ctx) == "checks-failed"
    saved = cuts.failures_path(c.store.root, 1).read_text()
    assert "first-broken" in saved and "second-broken" in saved
    comment = c.tracker.list_comments(1)[-1].body
    assert "first-broken" in comment and "second-broken" in comment


def test_pr_head_timeout_saves_earlier_failures_and_the_timed_out_output(run_case, monkeypatch):
    """#420: a timeout exit keeps everything this verification gathered."""
    from foragent import cuts, runner
    from foragent.run_checks import CheckResult
    from foragent.runner import review_only
    from runner_support import set_done_when

    c = run_case
    set_done_when(c, ["check-one", "check-two", "check-three"])
    c.tracker.edit_body(1, c.store.load_approval(1).body)
    c.store.finish_run(1, outcome="held")
    c.now[0] += timedelta(seconds=1)
    c.tracker.remove_labels(1, ["running"])
    base = c.history.tip
    c.history.update({"feature.txt": "done"})
    head = c.history.tip
    pr = c.tracker.create_pr(head="foragent/issue-1", base="main", title="PR", body="Review")
    c.tracker.set_pr_head_sha(pr, head)
    git_command(c.history.origin, "update-ref", "refs/heads/foragent/issue-1", head)
    git_command(c.history.origin, "update-ref", "refs/heads/main", base)
    results = {"check-one": CheckResult("check-one", 1, False, "first output"),
               "check-two": CheckResult("check-two", None, True, "second output")}
    ran = []
    monkeypatch.setattr(runner, "_check", lambda command, *a, **k: ran.append(command) or results[command])
    enable_reviewer(c, ["VERDICT: PASS"])
    review_only(1, c.ctx)
    assert ran == ["check-one", "check-two"]
    saved = cuts.failures_path(c.store.root, 1).read_text()
    assert "first output" in saved and "second output" in saved and "Check timed out." in saved


def test_pr_head_exception_keeps_the_failures_gathered_before_it(run_case, monkeypatch):
    """#420: an exception on a later PR-head command still saves the earlier failure."""
    from foragent import cuts, runner
    from foragent.run_checks import CheckResult, _verify_review_head
    from foragent.issue import parse_body
    from foragent.git import GitRepo

    c = run_case

    def check(command, *args, **kwargs):
        if len(calls) == 1:
            raise OSError("cannot launch")
        calls.append(command)
        return CheckResult(command, 1, False, "first failed output")

    calls = []
    monkeypatch.setattr(runner, "_check", check)
    parsed = parse_body(c.store.load_approval(1).body)
    parsed = parsed.__class__(**{**parsed.__dict__, "done_when_commands": ["one", "two"]})
    with pytest.raises(OSError):
        _verify_review_head(1, c.ctx, GitRepo(c.ctx.repo_root), parsed, "head", "link")
    assert "first failed output" in cuts.failures_path(c.store.root, 1).read_text()
    c.store.finish_run(1, "error")
    c.tracker.remove_labels(1, ["running"])


def test_pr_head_launch_error_keeps_the_previously_saved_failures(run_case, monkeypatch):
    """#420: a new PR-head verification whose first command cannot launch deletes nothing."""
    from foragent import cuts, runner
    from foragent.git import GitRepo
    from foragent.issue import parse_body
    from foragent.run_checks import CheckResult, _verify_review_head, save_failures

    c = run_case
    save_failures(c.store.root, 1, [CheckResult("one", 1, False, "earlier failure")])
    monkeypatch.setattr(runner, "_check", lambda *a, **k: (_ for _ in ()).throw(OSError("cannot launch")))
    parsed = parse_body(c.store.load_approval(1).body)
    parsed = parsed.__class__(**{**parsed.__dict__, "done_when_commands": ["one", "two"]})
    with pytest.raises(OSError):
        _verify_review_head(1, c.ctx, GitRepo(c.ctx.repo_root), parsed, "head", "link")
    assert "earlier failure" in cuts.failures_path(c.store.root, 1).read_text()
    c.store.finish_run(1, "error")
    c.tracker.remove_labels(1, ["running"])


def test_lane_decisions_block_is_appended_replaced_and_dropped_once():
    from foragent.run_prompts import lane_decisions_section, refresh_lane_decisions

    old, new = lane_decisions_section(["Old choice."]), lane_decisions_section(["New choice."])
    body = refresh_lane_decisions("Closes #1", old)
    assert body.startswith("Closes #1\n\n") and body.count("- Old choice.") == 1
    replaced = refresh_lane_decisions(body + "\n\nTail.", new)
    assert "Old choice." not in replaced and replaced.count("- New choice.") == 1 and replaced.endswith("Tail.")
    assert refresh_lane_decisions(replaced, new) == replaced
    assert refresh_lane_decisions(body, "") == "Closes #1"


def _native_pr_reviewer(c, seen):
    def review(**kwargs):
        seen.append(c.tracker.get_issue(kwargs["review_pr"]).body)
        return ExecResult(0, False, json.dumps({"result": "VERDICT: PASS\n(none)"}), kwargs["log_path"])

    c.ctx.config = replace(c.ctx.config, reviewer="claude", review_scope="pr-body")
    c.ctx.executor_factory = lambda name: c.executor if name == "codex" else SimpleNamespace(run=review)


def test_pr_body_review_receives_the_lanes_decide_lines(run_case, monkeypatch):
    from foragent import run_review
    from foragent.run_prompts import lane_decisions_section

    c = run_case
    section = lane_decisions_section(["Choose a helper name while implementing."])
    monkeypatch.setattr(run_review, "_lane_decisions", lambda n, ctx, worktree: section)
    seen = []
    _native_pr_reviewer(c, seen)
    assert run_issue(1, c.ctx) == "merged"
    # Native PR review reads only the PR body, so the decisions must be there when it runs.
    assert len(seen) == 1 and seen[0].count("- Choose a helper name while implementing.") == 1


def test_pr_body_re_review_refreshes_the_lanes_decide_lines(run_case, monkeypatch):
    from foragent import run_review
    from foragent.run_prompts import lane_decisions_section, refresh_lane_decisions
    from foragent.runner import review_only

    c = run_case
    c.store.finish_run(1, outcome="held")
    c.now[0] += timedelta(seconds=1)
    c.tracker.remove_labels(1, ["running"])
    base = c.history.tip
    c.history.update({"feature.txt": "done"})
    head = c.history.tip
    old = refresh_lane_decisions("Review", lane_decisions_section(["Obsolete choice."]))
    pr = c.tracker.create_pr(head="foragent/issue-1", base="main", title="Existing PR", body=old)
    c.tracker.set_pr_head_sha(pr, head)
    git_command(c.history.origin, "update-ref", "refs/heads/foragent/issue-1", head)
    git_command(c.history.origin, "update-ref", "refs/heads/main", base)
    section = lane_decisions_section(["Use a local tuple."])
    monkeypatch.setattr(run_review, "_lane_decisions", lambda n, ctx, worktree: section)
    seen = []
    _native_pr_reviewer(c, seen)
    assert review_only(1, c.ctx) == "merged"
    assert len(seen) == 1 and seen[0].count("- Use a local tuple.") == 1
    assert "Obsolete choice." not in seen[0]


@pytest.mark.parametrize("text", [
    "VERDICT: PASS\nVERDICT: PASS", "VERDICT: PASS\nVERDICT: FAIL",
    "VERDICT: FAIL\nVERDICT: PASS", "VERDICT: PASS\nVERDICT: invalid",
])
@pytest.mark.parametrize("native", [False, True])
def test_review_requires_exactly_one_verdict(text, native):
    from foragent.run_review import parse_review

    result = parse_code_review(json.dumps({"result": text})) if native else parse_review(text)
    assert result.verdict == "UNUSABLE" and not result.no_findings


@pytest.mark.parametrize("text,empty", [
    ("", True), ("(none)", True), ("Review completed.\n\n(none)", True),
    ("NOTE: another valid structure", True), ("[]", False),
    ("The selected locale is lost.", False),
    ('[ {"summary":"selected locale is lost"} ]\n(none)', False),
    ("feature.txt:1 — selected locale is lost\n(none)", False),
    ('```json\n[{"summary":"selected locale is lost"}]\n```\n(none)', False),
])
@pytest.mark.parametrize("native", [False, True])
def test_missing_verdict_keeps_protocol_separate_from_empty_findings(text, empty, native):
    from foragent.run_review import parse_review

    result = parse_code_review(json.dumps({"result": text})) if native else parse_review(text)
    assert result.verdict == "UNUSABLE" and result.findings == ""
    assert result.no_findings is empty


@pytest.mark.parametrize("raw", ["(none)", ""])
@pytest.mark.parametrize("native", [False, True])
def test_empty_review_merges_without_rework_or_a_recorded_pass(run_case, native, raw):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_retries=0, review_max=0)
    messages = [raw]
    full_message = json.dumps({"result": raw}) if native else raw
    replies = [ExecResult(0, False, "VERDICT: PASS", Path("unused"), full_message=full_message)]
    calls = (enable_native_reviewer if native else enable_reviewer)(c, replies)
    assert run_issue(1, c.ctx) == "merged"
    assert len(calls) == len(c.executor.calls) == 1
    events = [json.loads(line) for line in (c.store.root / "orch-events.jsonl").read_text().splitlines()]
    reviews = [event for event in events if event["kind"] == "review"]
    assert [(event["round"], event["verdict"], event["findings"]) for event in reviews] == [
        (1, "UNUSABLE", 0),
    ]
    assert c.store.get_run(1).last_review_pass_at is None
    for number, raw in enumerate(messages, 1):
        assert (c.store.root / f"runs/issue-1/review-{number}.txt").read_text() == raw


@pytest.mark.parametrize("exit_code,timed_out", [(1, False), (0, True)])
def test_failed_review_retains_retry_ceiling_without_a_defect_hold(run_case, exit_code, timed_out):
    c = run_case
    c.ctx.config = replace(c.ctx.config, review_max=0, max_retries=1)
    calls = enable_reviewer(c, [ExecResult(exit_code, timed_out, "VERDICT: PASS", Path("unused"))])
    assert run_issue(1, c.ctx) == "review-failed"
    assert len(calls) == len(c.executor.calls) == 1
    assert not c.tracker.merged_prs
    assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    comments = "\n".join(comment.body for comment in c.tracker.list_comments(1))
    assert "Reviewer still found defects" not in comments


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("exit_code,timed_out", [(1, False), (0, True)])
def test_confirmation_failure_retains_retry_ceiling_without_a_defect_hold(run_case, native, exit_code, timed_out):
    c = run_case
    c.ctx.config = replace(c.ctx.config, review_max=1, max_retries=2)
    # Supply a third attempt so losing the execution ceiling would silently merge.
    c.executor.steps += [{}, {}]
    finding = "VERDICT: FAIL\nfeature.txt:1 — the selected locale is lost"
    calls = (enable_native_reviewer if native else enable_reviewer)(c, [
        json.dumps({"result": finding}) if native else finding,
        ExecResult(exit_code, timed_out, "VERDICT: PASS", Path("unused")),
        "VERDICT: PASS",
    ])
    assert run_issue(1, c.ctx) == "review-failed"
    assert len(calls) == len(c.executor.calls) == c.store.get_run(1).attempts == 2
    assert finding in calls[1]["prompt"] and "review_level" not in calls[1]
    assert not c.tracker.merged_prs
    assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    assert "Reviewer still found defects" not in "\n".join(comment.body for comment in c.tracker.list_comments(1))


@pytest.mark.parametrize("native", [False, True])
def test_break_it_confirmation_cannot_lose_the_execution_ceiling(run_case, monkeypatch, native):
    from foragent import run_review

    result_type = run_review.ReviewResult

    def lose_execution_failure(*args, **kwargs):
        return result_type(*args, **{**kwargs, "execution_failed": False})

    monkeypatch.setattr(run_review, "ReviewResult", lose_execution_failure)
    with pytest.raises(AssertionError, match="review-failed"):
        test_confirmation_failure_retains_retry_ceiling_without_a_defect_hold(run_case, native, 1, False)
    assert run_case.store.get_run(1).outcome == "merged"
    assert len(run_case.executor.calls) == 3


def test_break_it_rejects_charging_unusable_reviews_as_defects(run_case, monkeypatch):
    """The handoff regression check must reject the old defect-budget behavior."""
    import inspect

    from foragent import run_review, runner

    source = inspect.getsource(run_review._review_implementation)
    original = 'if review.verdict == "FAIL":'
    assert source.count(original) == 1
    broken = source.replace(original, 'if review.verdict != "PASS":', 1)
    namespace = vars(run_review).copy()
    exec(compile("from __future__ import annotations\n" + broken, "<wrong-review-budget>", "exec"), namespace)
    monkeypatch.setattr(runner, "_review_implementation", namespace["_review_implementation"])
    with pytest.raises(AssertionError, match="merged"):
        test_repeated_unstructured_finding_normalizes_wrapping_after_rebuttal(run_case)


def test_break_it_rejects_a_native_scope_without_the_verdict_contract(review_context, monkeypatch):
    """The actual context check must catch a contract omitted from Claude's scope."""
    from foragent import run_review
    from foragent.run_prompts import REVIEW_VERDICT_CONTRACT

    rules = run_review.review_scope_rules

    def without_contract(*args, **kwargs):
        return rules(*args, **kwargs).replace(REVIEW_VERDICT_CONTRACT, "", 1)

    monkeypatch.setattr(run_review, "review_scope_rules", without_contract)
    with pytest.raises(AssertionError):
        test_claude_review_scope_restores_original_or_removes_temporary_file(review_context, None, "pass")


def test_break_it_rejects_native_none_becoming_a_pass(run_case, monkeypatch):
    """The receipt check must catch the old bare-(none)-means-PASS shortcut."""
    from foragent import run_review

    parse = run_review.parse_code_review

    def none_passes(message):
        if json.loads(message).get("result") == "(none)":
            return run_review.ReviewResult("PASS", "")
        return parse(message)

    monkeypatch.setattr(run_review, "parse_code_review", none_passes)
    with pytest.raises(AssertionError, match="UNUSABLE"):
        test_empty_review_merges_without_rework_or_a_recorded_pass(run_case, True, "(none)")


@pytest.mark.parametrize("case", ["full-message", "json", "fenced-json", "fenced-prose", "restored-commit"])
def test_break_it_native_confirmation_rejects_a_false_first_pass(run_case, monkeypatch, case):
    from foragent import run_review

    monkeypatch.setattr(run_review, "parse_code_review", lambda message: run_review.ReviewResult("PASS", ""))
    with pytest.raises(AssertionError):
        if case == "full-message":
            test_full_claude_verdict_survives_output_tail_truncation(run_case, monkeypatch)
        elif case in {"json", "fenced-json"}:
            test_native_json_findings_trigger_prompt_confirmation_and_save_raw_results(run_case, case == "fenced-json")
        elif case == "fenced-prose":
            test_native_prose_empty_array_cannot_skip_defects_and_merge(run_case, True)
        else:
            test_native_review_findings_repair_and_reviewer_commits_are_restored(run_case)


def test_break_it_shared_retry_budget_rejects_an_extra_unusable_rework(run_case, monkeypatch):
    import inspect

    from foragent import runner

    source = inspect.getsource(runner._implement_and_publish)
    boundary = "if run.attempts < ctx.config.max_retries + 1:"
    assert source.count(boundary) == 1
    broken = source.replace(boundary, boundary.replace(" < ", " <= "), 1)
    namespace = vars(runner).copy()
    exec(compile("from __future__ import annotations\n" + broken, "<extra-unusable-rework>", "exec"), namespace)
    monkeypatch.setattr(runner, "_implement_and_publish", namespace["_implement_and_publish"])
    # Let the extra attempt complete, so the regression fails on the count, not fixture exhaustion.
    run_case.executor.steps.append({})
    enable = enable_reviewer

    def extra_reply(case, messages, edit=None):
        return enable(case, [*messages, messages[-1]], edit)

    monkeypatch.setattr(sys.modules[__name__], "enable_reviewer", extra_reply)
    finding = ExecResult(0, False, "feature.txt:1 — the retry loses the selected locale", Path("unused"))
    with pytest.raises(AssertionError):
        test_unusable_review_retries_implementation_using_the_shared_budget(run_case, finding, "invalid")
    assert len(run_case.executor.calls) == run_case.store.get_run(1).attempts == 3
    assert run_case.store.get_run(1).outcome == "retries-exceeded"
