"""Acceptance test for risk-map. Locked by Foragent; do not edit."""

from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess

import pytest
import yaml

from conftest import GitHistory, git_command
from foragent.executors import ExecResult
from foragent.runner import run_issue
from test_orch_review_handoff import bind_cli, make_case
from test_skills_autosync import cli_exit, isolated_home
from test_skills_review_scope import scope_run


# Public shapes: a root YAML sequence; `fa risk list --json` returns a JSON array.
# Paths use the existing repo-relative review-policy globs. Omitted always_human
# means false. Trace: nonempty added.by, timezone-aware ISO added.at, added.event
# equal to --event (null when omitted). No new Python API is required.
# With today's tiers, l means codex_effort for Codex and claude_model for Claude;
# Codex keeps reviewer_model. Unmatched reviews keep their existing settings.
# QA classification: additive review is missing agent context, not a new review
# decision algorithm. Candidate discovery is a static inventory tool; its fixed
# patterns report evidence, leaving risk judgment and map writes to the agent.
# Fix/guard metadata is trace data, not a reason to retire incident context.
# Candidates JSON: {candidates: [{path, line, pattern, covered}], proposals:
# [{paths: [uncovered_path]}]}; line is 1-based, pattern is a catalog label below,
# covered is boolean. Proposals may carry more context; they are never persisted.
# Optional metadata: fixed_by is an issue-reference string, guard_tests is a list
# of repo-relative test paths; repeated --guard-test preserves their order.
RISK_FILE = ".foragent-risks.yml"
BASELINE_GUARD = "tests/acceptance/test_acceptance_baseline.py"
PROCESS_GUARD = "tests/acceptance/test_stop_whole_tree.py"
ADDITIVE_REVIEW_SENTENCE = (
    "Review all changed files in full; unmapped code receives the same full review as always, "
    "and risk-map notes only add context."
)
# Freeze today's instructions so weakening the shared prompt builder cannot
# weaken the acceptance oracle at the same time.
FULL_REVIEW_INSTRUCTIONS = (
    "Review this change for defects that break the stated goal, violate Out of scope, "
    "or would make the Done when commands pass without the goal being met "
    "(weakened checks, special-casing test inputs). Ignore style and nice-to-haves. "
    "Do not edit files. Reply with exactly one line `VERDICT: PASS` or `VERDICT: FAIL` "
    "followed by every finding, each one line with file:line and the concrete failure."
)
FULL_SCOPE_RULES = (
    "Block only on: the Goal is not met, something listed in Out of scope was changed, or a check was weakened.\n"
    "Tests under tests/acceptance/ that belong to other issues fail on purpose until those issues land; do not report them.\n"
    "Improvements beyond this issue's Goal are not defects."
)
TIERS = {
    "s": {"codex_effort": "low", "claude_model": "haiku"},
    "m": {"codex_effort": "medium", "claude_model": "sonnet"},
    "l": {"codex_effort": "max", "claude_model": "opus"},
}


def entry(paths=None, **overrides):
    return {
        "paths": paths or ["feature*.txt"],
        "why": "RISK-MATCH: process ownership was trusted without evidence.",
        "incident": {"date": "2026-10-24", "summary": "INCIDENT-MATCH: a fake identity reached the signal sender."},
        "guidance": "GUIDANCE-MATCH: read only; use fake process tables and inspect weakened safety lines.",
        "added": {"by": "orchestrator", "at": "2026-10-24T12:00:00+00:00", "event": "incident:fixture"},
        **overrides,
    }


def notes(risk):
    return (risk["why"], str(risk["incident"]["date"]), risk["incident"]["summary"], risk["guidance"])


@pytest.fixture(autouse=True)
def offline(monkeypatch, isolated_home):
    from foragent import cli, executors

    def forbidden(*args, **kwargs):
        pytest.fail("Risk-map acceptance must not send signals, contact GitHub, or run a model")

    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(signal, "raise_signal", forbidden)
    monkeypatch.setattr(signal, "pthread_kill", forbidden)
    monkeypatch.setattr(executors, "run_logged", forbidden)
    monkeypatch.setattr(cli, "GhTracker", forbidden)


def risk_case(git_history, tmp_path, monkeypatch, *, entries=None, reviewer="codex", size="s",
              changed_paths=("feature.txt",), **settings):
    if entries is not None:
        text = yaml.safe_dump(entries, sort_keys=False)
        git_history.update({RISK_FILE: text})
        # Both the project root and the runner's separate worktree see the map.
        (git_history.clone / RISK_FILE).write_text(text, encoding="utf-8")
    case = make_case(
        git_history, tmp_path, monkeypatch,
        **{"executor": "claude" if reviewer == "codex" else "codex", "reviewer": reviewer,
           "reviewer_model": "ordinary-review-model", "tiers": TIERS, "skill_libraries": [], **settings},
    )
    case.tracker.add_labels(1, [f"size:{size}"])
    # The ordinary Done-when fixture always creates feature.txt as well.
    changed_paths = tuple(dict.fromkeys(("feature.txt", *changed_paths)))
    case.tracker.set_pr_files(2, list(changed_paths))

    def implement(kwargs):
        case.implement(kwargs)
        for name in changed_paths:
            path = kwargs["worktree"] / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("changed\n", encoding="utf-8")

    case.review_contexts = []
    case.review_diffs = []

    def review(kwargs):
        case.review_diffs.append(case.git.at(kwargs["worktree"]).staged_diff("origin/main"))
        context = kwargs["prompt"]
        if reviewer == "claude":
            # Native /code-review reads the local scope or the PR body rather
            # than stdin. Observe these at the fake executor boundary, before
            # temporary scope cleanup, as the existing scope tests do.
            local = kwargs["worktree"] / "CLAUDE.local.md"
            context += local.read_text(encoding="utf-8") if local.is_file() else ""
            context += "\n".join(pr.body for pr in case.tracker.created_prs)
        case.review_contexts.append(context)
        # Match the public executor's native-vs-prompt transport option.
        return json.dumps({"result": "(none)"}) if reviewer == "claude" and kwargs.get("review_level") is not None else "VERDICT: PASS"

    case.implementer.steps = [implement]
    # Expect one response per perspective and one merged response. The fake
    # transport repeats its unchanged passing judgment; it never schedules work.
    def review_call(**kwargs):
        case.reviewer.calls.append(kwargs)
        return ExecResult(0, False, review(kwargs), kwargs["log_path"])

    case.reviewer.run = review_call
    return case


def copy_history(history, destination):
    destination.mkdir()
    origin = shutil.copytree(history.origin, destination / "origin.git")
    clone = shutil.copytree(history.clone, destination / "clone")
    git_command(clone, "config", "remote.origin.url", str(origin))
    return GitHistory(origin, clone, history.tip)


@pytest.mark.parametrize("changed", ["src/runtime/stop.py", "src/runtime/nested/stop.py", "scripts/stop.py"])
def test_reviewer_prompt_contains_only_risks_matching_changed_paths(git_history, tmp_path, monkeypatch, changed):
    matched = entry(["src/runtime/**", "scripts/*.py"])
    other = entry(["docs/**"], why="UNMATCHED-WHY", guidance="UNMATCHED-GUIDANCE",
                  incident={"date": "2026-09-01", "summary": "UNMATCHED-INCIDENT"}, always_human=True)
    # This mapped file exists in the repository, but the PR does not change it.
    git_history.update({"docs/old-incident.md": "unchanged\n"})
    case = risk_case(git_history, tmp_path, monkeypatch, entries=[matched, other], changed_paths=(changed,))
    assert run_issue(1, case.ctx) == "merged", "An omitted always_human defaults to false; unrelated risks cannot hold a PR"
    assert len(case.reviewer.calls) == 5, "Four core perspective reviews and one merge are required"
    prompt = case.reviewer.calls[0]["prompt"]
    for note in notes(matched):
        assert note in prompt, f"Matched risk note missing from the reviewer executor's prompt: {note}"
    for note in notes(other):
        assert note not in prompt, f"Unchanged mapped paths leaked into the reviewer prompt: {note}"


@pytest.mark.parametrize("scope", ["claude-local", "pr-body"])
def test_native_claude_review_receives_only_matching_risk_context(git_history, tmp_path, monkeypatch, scope):
    matched = entry()
    other = entry(["docs/**"], why="UNMATCHED-WHY", guidance="UNMATCHED-GUIDANCE",
                  incident={"date": "2026-09-01", "summary": "UNMATCHED-INCIDENT"})
    case = risk_case(git_history, tmp_path, monkeypatch, entries=[matched, other],
                     reviewer="claude", review_scope=scope)
    assert run_issue(1, case.ctx) == "merged" and len(case.review_contexts) == 5, "Four core perspective reviews and one merge are required"
    for note in notes(matched):
        assert note in case.review_contexts[0], f"Native Claude {scope} review is missing risk context: {note}"
    for note in notes(other):
        assert note not in case.review_contexts[0], f"Native Claude {scope} review received unrelated risk context: {note}"


@pytest.mark.parametrize("reviewer,scope", [
    ("codex", "claude-local"), ("claude", "claude-local"), ("claude", "pr-body"),
], ids=["codex", "claude-local", "claude-pr-body"])
def test_risk_context_is_additive_and_keeps_full_review_of_unmapped_changes(
    git_history, tmp_path, monkeypatch, reviewer, scope,
):
    mapped, unmapped = "src/runtime/stop.py", "src/ordinary.py"
    risk = entry(["src/runtime/**"])
    case = risk_case(git_history, tmp_path, monkeypatch, entries=[risk], reviewer=reviewer,
                     review_scope=scope, changed_paths=(mapped, unmapped))
    assert run_issue(1, case.ctx) == "merged" and len(case.review_contexts) == 5, "Four core perspective reviews and one merge are required"
    context = case.review_contexts[0]
    assert FULL_SCOPE_RULES in context, "Risk context must retain all of today's ordinary review scope rules"
    if reviewer == "claude" and scope == "pr-body":
        # Today's native PR review follows Closes #1 to the approved issue;
        # the PR body contains scope rules rather than duplicating that body.
        assert "Closes #1" in context
        pr = case.tracker.created_prs[0]
        assert case.reviewer.calls[0]["prompt"] or case.reviewer.calls[0].get("review_pr") == pr.number
        assert {mapped, unmapped, "feature.txt"} <= set(case.tracker.pr_changed_files(pr.number))
    else:
        assert "Implement the feature." in context and "Other features." in context
    assert "test -f feature.txt" in context
    # Claude's native /code-review sees the complete repo diff in its worktree;
    # Codex receives that complete diff in its prompt. Both must include the
    # mapped file, the unmapped file, and the ordinary feature fixture.
    for name in (mapped, unmapped, "feature.txt"):
        assert f"diff --git a/{name} b/{name}" in case.review_diffs[0]
        if reviewer == "codex":
            assert f"diff --git a/{name} b/{name}" in context, f"Review scope excluded changed file {name}"
    if reviewer == "codex":
        assert FULL_REVIEW_INSTRUCTIONS in context, "Risk notes must not replace today's full review instructions"
    assert ADDITIVE_REVIEW_SENTENCE in context, "Reviewer must explicitly keep the same full review for unmapped code"
    for note in notes(risk):
        assert note in context


@pytest.mark.parametrize("reviewer", ["codex", "claude"])
@pytest.mark.parametrize("size", ["s", "m", "l"])
def test_matched_review_uses_l_tier_regardless_of_issue_size(git_history, tmp_path, monkeypatch, reviewer, size):
    case = risk_case(git_history, tmp_path, monkeypatch, entries=[entry()], reviewer=reviewer, size=size)
    assert run_issue(1, case.ctx) == "merged"
    assert len(case.reviewer.calls) == 5, "Four core perspective reviews and one merge are required"
    call = case.reviewer.calls[0]
    key, expected = ("effort", TIERS["l"]["codex_effort"]) if reviewer == "codex" else ("model", TIERS["l"]["claude_model"])
    assert call.get(key) == expected, (
        f"Mapped {size}-sized PR must use tiers.l for {reviewer}: expected {key}={expected}, got {call.get(key)!r}"
    )
    if reviewer == "codex":
        assert call["model"] == "ordinary-review-model"


def test_missing_map_keeps_existing_review_and_unmatched_map_keeps_strength(git_history, tmp_path, monkeypatch):
    # Exercise the existing Claude /code-review scenario unchanged, in an
    # independent history so its branch and state cannot contaminate the risk PR.
    control_history = copy_history(git_history, tmp_path / "control")
    assert not (control_history.clone / RISK_FILE).exists()
    control, calls, outcome = scope_run(control_history, tmp_path / "control", monkeypatch)
    assert outcome == "merged" and len(calls) == 1
    assert calls[0]["argv"][calls[0]["argv"].index("-p") + 1] == "/code-review low"
    assert calls[0]["prs"] == []
    assert "Goal" in calls[0]["scope"] and "tests/acceptance/" in calls[0]["scope"]
    assert control.tracker.merged_prs

    unrelated = entry(["docs/**"], always_human=True)
    unmatched_history = copy_history(git_history, tmp_path / "unmatched")
    unmatched = risk_case(unmatched_history, tmp_path / "unmatched", monkeypatch, entries=[unrelated], reviewer="claude")
    assert run_issue(1, unmatched.ctx) == "merged"
    ordinary = unmatched.reviewer.calls[0]
    assert ordinary["model"] == "ordinary-review-model" and ordinary["review_level"] == "low"
    assert all(note not in unmatched.review_contexts[0] for note in notes(unrelated))

    codex_history = copy_history(git_history, tmp_path / "unmatched-codex")
    codex = risk_case(codex_history, tmp_path / "unmatched-codex", monkeypatch, entries=[unrelated])
    assert run_issue(1, codex.ctx) == "merged" and len(codex.reviewer.calls) == 1
    assert codex.reviewer.calls[0]["model"] == "ordinary-review-model"
    assert codex.reviewer.calls[0].get("effort") is None
    assert all(note not in codex.reviewer.calls[0]["prompt"] for note in notes(unrelated))

    matched = risk_case(git_history, tmp_path, monkeypatch, entries=[entry()], reviewer="claude")
    assert run_issue(1, matched.ctx) == "merged"
    assert matched.reviewer.calls[0]["model"] == TIERS["l"]["claude_model"], (
        "A matching map must strengthen the existing review; missing and unrelated maps must leave it unchanged"
    )


def test_risk_always_human_holds_exactly_like_config_always_human(git_history, tmp_path, monkeypatch):
    from foragent.reasons import Reason, parse_reason

    control_history = copy_history(git_history, tmp_path / "control")
    control = risk_case(control_history, tmp_path / "control", monkeypatch, always_human=["feature*.txt"])
    assert run_issue(1, control.ctx) == "held"
    reference = control.store.load_flag(1)
    assert reference.reason == Reason.ALWAYS_HUMAN_PATH

    case = risk_case(git_history, tmp_path, monkeypatch, entries=[entry(always_human=True)])
    outcome = run_issue(1, case.ctx)
    assert outcome == "held", f"Mapped always_human must hold the PR like config.always_human; got {outcome!r}"
    assert case.tracker.merged_prs == [] and len(case.tracker.created_prs) == 1
    assert case.tracker.list_prs("open")[0].number == 2
    assert case.tracker.get_issue(1).labels == control.tracker.get_issue(1).labels
    assert case.store.load_flag(1).reason == reference.reason
    comment = case.tracker.list_comments(1)[-1].body
    assert parse_reason(comment) == Reason.ALWAYS_HUMAN_PATH
    assert "https://github.com/owner/repo/pull/2" in comment


def add_args(*, why="Ownership was not checked", guidance="Read only; use fake process tables", extra=()):
    return ["risk", "add", "--path", "src/runtime/**", "--path", "scripts/stop.py",
            "--why", why, "--incident-date", "2026-10-24", "--incident", "A review trusted fake identity 1",
            "--guidance", guidance, *extra]


def cli_ok(main, argv, capsys):
    code = cli_exit(main, argv)
    captured = capsys.readouterr()
    assert code == 0, f"fa {' '.join(argv[:2])} must succeed; exit={code}: {captured.err}"
    return captured.out


def assert_trace(risk, event):
    trace = risk["added"]
    assert isinstance(trace["by"], str) and trace["by"].strip(), "added.by must identify the author"
    assert isinstance(trace["at"], str), "added.at must be an ISO timestamp"
    assert datetime.fromisoformat(trace["at"].replace("Z", "+00:00")).utcoffset() is not None
    assert trace["event"] == event


def test_cli_creates_project_map_and_lists_json_with_trace(git_history, tmp_path, monkeypatch, capsys):
    case = risk_case(git_history, tmp_path, monkeypatch)
    main = bind_cli(case, monkeypatch)
    assert not (case.root / RISK_FILE).exists()
    assert json.loads(cli_ok(main, ["risk", "list", "--json"], capsys)) == []
    cli_ok(main, add_args(), capsys)
    stored = yaml.safe_load((case.root / RISK_FILE).read_bytes())
    listed = json.loads(cli_ok(main, ["risk", "list", "--json"], capsys))
    assert isinstance(stored, list) and isinstance(listed, list) and len(stored) == len(listed) == 1
    for risk in (stored[0], listed[0]):
        assert risk["paths"] == ["src/runtime/**", "scripts/stop.py"]
        assert risk["why"] == "Ownership was not checked"
        assert str(risk["incident"]["date"]) == "2026-10-24"
        assert risk["incident"]["summary"] == "A review trusted fake identity 1"
        assert risk["guidance"] == "Read only; use fake process tables"
        assert risk.get("always_human", False) is False
        assert_trace(risk, None)
    assert listed[0]["added"] == stored[0]["added"]


def test_cli_append_preserves_existing_entries_and_comments_byte_for_byte(git_history, tmp_path, monkeypatch, capsys):
    case = risk_case(git_history, tmp_path, monkeypatch)
    main = bind_cli(case, monkeypatch)
    before = (
        "# Keep the incident's original formatting.\r\n"
        "- paths: ['legacy/**']  # original path comment\r\n"
        "  why: 'Prior incident'\r\n"
        "  incident: {date: '2026-10-01', summary: 'Original summary'}\r\n"
        "  guidance: |\r\n    Read only.\r\n    Keep this line too.\r\n"
        "  added: {by: 'human', at: '2026-10-01T00:00:00Z', event: 'incident:old'}\r\n"
        "# Keep this final comment without a newline"
    ).encode("utf-8")
    path = case.root / RISK_FILE
    path.write_bytes(before)
    cli_ok(main, add_args(extra=("--always-human", "--event", "incident:kill-2026-10-24")), capsys)
    after = path.read_bytes()
    assert after.startswith(before), "Appending must retain all existing entry and comment bytes, including CRLF"
    stored = yaml.safe_load(after)
    assert len(stored) == 2 and stored[0] == yaml.safe_load(before)[0]
    assert stored[1]["always_human"] is True
    assert stored[1]["paths"] == ["src/runtime/**", "scripts/stop.py"]
    assert_trace(stored[1], "incident:kill-2026-10-24")
    listed = json.loads(cli_ok(main, ["risk", "list", "--json"], capsys))
    assert listed == stored, "JSON listing must include the original and appended entries in order"


@pytest.mark.parametrize("field", ["why", "guidance"])
@pytest.mark.parametrize("blank", ["", " \t "], ids=["empty", "whitespace"])
def test_cli_rejects_blank_context_without_changing_map(git_history, tmp_path, monkeypatch, capsys, field, blank):
    case = risk_case(git_history, tmp_path, monkeypatch)
    main = bind_cli(case, monkeypatch)
    # Prove the command works first: argparse rejecting an unknown `risk`
    # command must never count as validation of --why or --guidance.
    cli_ok(main, add_args(), capsys)
    path = case.root / RISK_FILE
    before = path.read_bytes()
    code = cli_exit(main, add_args(**{field: blank}))
    capsys.readouterr()
    assert code != 0, f"fa risk add must refuse blank --{field}"
    assert path.read_bytes() == before, f"Rejected --{field} must not alter the existing risk map"


# Literal operation labels and known guard modules define the fixed inventory.
# Shell and Python argv forms are both used by the repository. These are source
# strings only: no fixture code or command below may ever be executed.
CANDIDATE_EXAMPLES = [
    ("signals", "src/runtime/operations.py", "os.kill", "os.kill(424242, signal.SIGTERM)"),
    ("signals", "src/runtime/operations.py", "os.killpg", "os.killpg(424242, signal.SIGTERM)"),
    ("signals", "src/runtime/operations.py", "signal.raise_signal", "signal.raise_signal(signal.SIGTERM)"),
    ("signals", "src/runtime/operations.py", "signal.pthread_kill", "signal.pthread_kill(424242, signal.SIGTERM)"),
    ("deletion", "src/runtime/operations.py", "shutil.rmtree", "shutil.rmtree(scratch)"),
    ("deletion", "scripts/operations.sh", "rm -rf", 'rm -rf "$scratch"'),
    ("git-write", "scripts/operations.sh", "git push", "git push origin main"),
    ("git-write", "scripts/operations.sh", "git merge", "git merge --ff-only feature"),
    ("git-write", "src/transport.py", "git push", 'subprocess.run(["git", "push", "origin", "main"])'),
    ("git-write", "src/transport.py", "git merge", 'subprocess.run(["git", "merge", "--ff-only", "feature"])'),
    ("github-write", "scripts/operations.sh", "gh pr merge", "gh pr merge 7"),
    ("github-write", "scripts/operations.sh", "gh issue edit", "gh issue edit 7 --add-label blocked"),
    ("github-write", "scripts/operations.sh", "gh api -X POST", "gh api -X POST repos/owner/repo/issues"),
    ("github-write", "scripts/operations.sh", "gh api -X PATCH", "gh api -X PATCH repos/owner/repo/issues/7"),
    ("github-write", "scripts/operations.sh", "gh api -X DELETE", "gh api -X DELETE repos/owner/repo/issues/7"),
    ("github-write", "src/transport.py", "gh pr merge", 'subprocess.run(["gh", "pr", "merge", "7"])'),
    ("github-write", "src/transport.py", "gh issue edit", 'subprocess.run(["gh", "issue", "edit", "7"])'),
    ("github-write", "src/transport.py", "gh api -X POST", 'subprocess.run(["gh", "api", "-X", "POST", "repos/owner/repo/issues"])'),
    ("github-write", "src/transport.py", "gh api -X PATCH", 'subprocess.run(["gh", "api", "-X", "PATCH", "repos/owner/repo/issues/7"])'),
    ("github-write", "src/transport.py", "gh api -X DELETE", 'subprocess.run(["gh", "api", "-X", "DELETE", "repos/owner/repo/issues/7"])'),
    ("guard-functions", "src/foragent/policy.py", "merge-policy", "def merge_decision(paths):\n    return True"),
    ("guard-functions", "src/foragent/run_baseline.py", "baseline", "def _capture_acceptance_baseline(ctx):\n    return None"),
    ("guard-functions", "src/foragent/scheduler_approval.py", "approval", "def _check_integrity(snapshot):\n    return True"),
]


def candidate_case(git_history, tmp_path, monkeypatch, *, with_map=True):
    case = risk_case(git_history, tmp_path, monkeypatch,
                     entries=[entry(["src/runtime/**"])] if with_map else None)
    files, expected = {}, []
    python_header = (
        "from pathlib import Path\nimport os, signal, shutil, subprocess\n"
        "Path('candidate-executed').write_text('executed')\n"
        "raise RuntimeError('candidate sources must be scanned without execution')\n"
    )
    for family, path, pattern, snippet in CANDIDATE_EXAMPLES:
        text = files.setdefault(path, python_header if path.endswith(".py") else "#!/bin/sh\n")
        expected.append((family, path, text.count("\n") + 1, pattern))
        files[path] += snippet + "\n"
    files["src/read_only.py"] = (
        "import os, signal, shutil, subprocess\n"
        "def inspect_only(handler, source, destination):\n"
        "    signal.signal(signal.SIGTERM, handler)\n"
        "    shutil.copyfile(source, destination)\n"
        '    subprocess.run(["git", "status", "--short"])\n'
        '    subprocess.run(["gh", "pr", "view", "7"])\n'
        '    subprocess.run(["gh", "api", "-X", "GET", "repos/owner/repo"])\n'
    )
    for name, text in files.items():
        path = case.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git_command(case.root, "add", "--", *sorted(files))
    main = bind_cli(case, monkeypatch)
    if with_map:
        path = case.root / RISK_FILE
        path.write_bytes(b"# Preserve this project's incident history.\n" + path.read_bytes())
    return case, main, files, expected


def candidate_report(main, capsys, monkeypatch):
    popen = subprocess.Popen

    def read_only_process(argv, *args, **kwargs):
        assert isinstance(argv, (list, tuple)) and argv and argv[0] in {"git", "rg"}, (
            f"Candidate discovery must read source without executing it: {argv!r}"
        )
        if argv[0] == "git":
            assert not {"push", "merge", "commit", "reset", "checkout", "stash"}.intersection(argv)
        return popen(argv, *args, **kwargs)

    def no_execution(*args, **kwargs):
        pytest.fail("Candidate discovery must not execute source, delete files, or run shell commands")

    with monkeypatch.context() as static_only:
        static_only.setattr(subprocess, "Popen", read_only_process)
        static_only.setattr(os, "system", no_execution)
        static_only.setattr(shutil, "rmtree", no_execution)
        output = cli_ok(main, ["risk", "candidates", "--json"], capsys)
    report = json.loads(output)
    assert isinstance(report, dict) and isinstance(report["candidates"], list) and isinstance(report["proposals"], list)
    for item in report["candidates"]:
        assert isinstance(item["path"], str) and item["path"]
        assert type(item["line"]) is int and item["line"] >= 1, "Candidate line numbers must be 1-based"
        assert isinstance(item["pattern"], str) and item["pattern"]
        assert isinstance(item["covered"], bool)
    return report


@pytest.mark.parametrize("family", ["signals", "deletion", "git-write", "github-write", "guard-functions"])
def test_candidates_report_fixed_patterns_with_source_locations(git_history, tmp_path, monkeypatch, capsys, family):
    case, main, files, expected = candidate_case(git_history, tmp_path, monkeypatch)
    report = candidate_report(main, capsys, monkeypatch)
    observed = {(item["path"], item["line"], item["pattern"]) for item in report["candidates"]}
    required = {(path, line, pattern) for group, path, line, pattern in expected if group == family}
    assert required <= observed, f"Static risk inventory missed {family} locations: {sorted(required - observed)}"
    all_expected = {(path, line, pattern) for _, path, line, pattern in expected}
    assert observed <= all_expected, f"Candidate inventory reported operations outside its fixed catalog: {observed - all_expected}"
    assert all(item["path"] != "src/read_only.py" for item in report["candidates"]), (
        "Installing signal handlers, copying files, and read-only Git/GitHub calls are not the listed write operations"
    )
    for item in report["candidates"]:
        assert item["line"] <= len(files[item["path"]].splitlines())
    assert not (case.root / "candidate-executed").exists(), "Static discovery must not execute candidate sources"


@pytest.mark.parametrize("with_map", [True, False], ids=["existing-map", "missing-map"])
def test_candidates_mark_coverage_and_propose_uncovered_paths_without_writing_map(
    git_history, tmp_path, monkeypatch, capsys, with_map,
):
    from foragent.policy import touched

    case, main, files, expected = candidate_case(git_history, tmp_path, monkeypatch, with_map=with_map)
    path = case.root / RISK_FILE
    before = path.read_bytes() if path.exists() else None
    source_bytes = {name: (case.root / name).read_bytes() for name in files}
    report = candidate_report(main, capsys, monkeypatch)
    assert (path.read_bytes() if path.exists() else None) == before, "Candidate proposals must not create or rewrite the map"
    assert {name: (case.root / name).read_bytes() for name in files} == source_bytes
    assert not (case.root / "candidate-executed").exists()
    expected_paths = {name for _, name, _, _ in expected}
    assert {item["path"] for item in report["candidates"]} == expected_paths
    mapped = set(touched(sorted(expected_paths), ["src/runtime/**"])) if with_map else set()
    for item in report["candidates"]:
        assert item["covered"] is (item["path"] in mapped), f"Incorrect map coverage for {item['path']}"
    proposed = []
    for proposal in report["proposals"]:
        assert isinstance(proposal, dict) and isinstance(proposal["paths"], list) and proposal["paths"]
        proposed.extend(proposal["paths"])
    assert len(proposed) == len(set(proposed)), "Propose each uncovered path once"
    assert set(proposed) == expected_paths - mapped, "Proposals must cover exactly the unmapped candidate paths"


def test_cli_add_records_fix_and_guard_tests_without_replacing_incident(git_history, tmp_path, monkeypatch, capsys):
    case = risk_case(git_history, tmp_path, monkeypatch)
    for name in (PROCESS_GUARD, BASELINE_GUARD):
        path = case.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_guard():\n    assert True\n", encoding="utf-8")
    main = bind_cli(case, monkeypatch)
    cli_ok(main, add_args(extra=("--fixed-by", "owner/repo#261", "--guard-test", PROCESS_GUARD,
                                "--guard-test", BASELINE_GUARD)), capsys)
    path = case.root / RISK_FILE
    before_listing = path.read_bytes()
    stored = yaml.safe_load(before_listing)
    listed = json.loads(cli_ok(main, ["risk", "list", "--json"], capsys))
    assert len(stored) == len(listed) == 1
    for risk in (stored[0], listed[0]):
        assert risk["fixed_by"] == "owner/repo#261"
        assert risk["guard_tests"] == [PROCESS_GUARD, BASELINE_GUARD]
        assert str(risk["incident"]["date"]) == "2026-10-24"
        assert risk["incident"]["summary"] == "A review trusted fake identity 1", "A fix must preserve the incident"
        assert risk["why"] == "Ownership was not checked"
        assert risk["guidance"] == "Read only; use fake process tables"
    assert path.read_bytes() == before_listing, "Listing a fixed risk must not rewrite its history"


def test_fixed_risks_keep_incident_context_and_strength_in_review(git_history, tmp_path, monkeypatch):
    risk = entry(fixed_by="owner/repo#261", guard_tests=[PROCESS_GUARD])
    case = risk_case(git_history, tmp_path, monkeypatch, entries=[risk])
    assert run_issue(1, case.ctx) == "merged" and len(case.reviewer.calls) == 5, "Four core perspective reviews and one merge are required"
    for note in notes(risk):
        assert note in case.reviewer.calls[0]["prompt"], f"Fixed risks must retain incident context in review: {note}"
    assert case.reviewer.calls[0].get("effort") == TIERS["l"]["codex_effort"]


@pytest.mark.parametrize("paths,summary_fragments,guidance_fragments,guard_test", [
    (["src/foragent/processes.py", "src/foragent/scheduler_recovery.py", "src/foragent/inspect_runtime.py"],
     ("fake identity of 1", "user's apps"),
     ("never execute signal-sending code", "fake process tables only", "weakened kill-safety"), PROCESS_GUARD),
    (["src/foragent/run_baseline.py", "src/foragent/policy.py"],
     ("empty acceptance baseline", "unprotected"),
     ("missing or empty baseline", "passing"), BASELINE_GUARD),
], ids=["process-termination", "baseline-merge-policy"])
def test_repository_ships_its_two_incident_seeds(paths, summary_fragments, guidance_fragments, guard_test):
    from foragent.policy import touched

    path = Path(__file__).resolve().parents[2] / RISK_FILE
    assert path.is_file(), f"Repository risk map is missing: {RISK_FILE} (required seed: {paths[0]})"
    risks = yaml.safe_load(path.read_bytes())
    assert isinstance(risks, list) and len(risks) >= 2
    candidates = [risk for risk in risks if set(touched(paths, risk["paths"])) == set(paths)]
    assert candidates, f"Missing one seed entry covering all of {paths}"
    risk = next((risk for risk in candidates if str(risk["incident"]["date"]) == "2026-10-24"), None)
    assert risk is not None, f"Seed for {paths} must retain the 2026-10-24 incident date"
    assert isinstance(risk["why"], str) and risk["why"].strip()
    # Canonical English excerpts from the contract keep these assertions about
    # incident facts, without pinning a complete paragraph or heading format.
    for fragment in summary_fragments:
        assert fragment in risk["incident"]["summary"], f"Seed incident must record: {fragment}"
    for fragment in guidance_fragments:
        assert fragment in risk["guidance"], f"Seed guidance must record: {fragment}"
    assert isinstance(risk.get("always_human", False), bool)
    assert isinstance(risk.get("guard_tests"), list) and guard_test in risk["guard_tests"], (
        f"Seed must name the test guarding its incident: {guard_test}"
    )
    assert (path.parent / guard_test).is_file(), f"Seed guard test does not exist: {guard_test}"
    assert_trace(risk, risk["added"]["event"])
