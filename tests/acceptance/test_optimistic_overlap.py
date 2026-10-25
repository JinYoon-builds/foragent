"""Acceptance test for optimistic-overlap. Locked by Foragent; do not edit."""

from dataclasses import replace
from datetime import timedelta
import inspect
import json
import os
from pathlib import Path
import subprocess

import pytest

from conftest import ACCEPTANCE, git_command
from foragent import executors, runner, scheduler
from foragent.config import Config, load_config
from foragent.issue import body_hash, parse_body
from foragent.state import StateStore
from runner_support import import_index
from test_lane_width_by_overlap import _finish, _wide_lane
from test_orch_facts import NOW, completed
from test_orch_review_handoff import make_case, tick_context
from test_scheduler import BODY, approve, ctx  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


STATE = "src/foragent/state.py"
CLI = "src/foragent/cli.py"
OTHER = "src/foragent/other.py"

# Chosen wire contract, shared by producers and the consumer in test_overlap_facts.py:
# Config.overlap_per_file (positive int, default 2); plan issues/edits[].exclusive
# and ParsedIssue.exclusive (path lists). Body metadata: exclusive:\n  - path.
# Run.declared_files / changed_files / effective_files are lists of paths;
# changed/effective paths are concrete, including additions and deletions.
# Run.overlap_records contains one row per overlapping file, per execution:
# {file, issues: [peer issue numbers], conflicted: bool,
#  resolution_seconds: number, redo_count: int}.
# facts.runs[] adds declared_file_count / changed_file_count / overlap_records.
# facts.file_conflicts[path] = {overlapping_merges, conflicted_merges,
#                             conflict_rate, resolution_seconds, redo_count}.
# Rates count merged executions, not peer pairs or repeated merges of main.
# Legacy unknown file counts are null; they are not measured zeroes.


@pytest.fixture(autouse=True)
def offline_overlap(tmp_path, monkeypatch, isolated_home, offline_acceptance):
    """Only fixture-local Git may spawn; all model/check processes are fake."""
    popen = subprocess.Popen

    def local_git(argv, *args, **kwargs):
        assert isinstance(argv, (list, tuple)) and argv[0] == "git", argv
        assert Path(kwargs["cwd"]).resolve().is_relative_to(tmp_path.resolve()), kwargs
        return popen(argv, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("Optimistic-overlap tests must not signal processes or start model turns")

    monkeypatch.setattr(subprocess, "Popen", local_git)
    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(executors.ClaudeExecutor, "run", forbidden)
    monkeypatch.setattr(executors.CodexExecutor, "run", forbidden)
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.0))
    # Leave the real checks/slots/staging pipeline intact; fake its subprocess.
    monkeypatch.setattr(runner, "run_logged", lambda *a, **kw: (0, False, ""))


def _body(files, exclusive=()):
    metadata = "exclusive:\n" + "".join(f"  - {path}\n" for path in exclusive) if exclusive else ""
    return BODY.replace("<!-- foragent:v1 -->\n", "<!-- foragent:v1 -->\n" + metadata) + (
        "\n## Files\n" + "".join(f"- {path}\n" for path in files)
    )


def _issue(context, number, files, *, running=False, exclusive=(), lane="orch"):
    approve(context, number, body=_body(files, exclusive),
            labels=[f"lane:{lane}", "running" if running else "ready"])
    if running:
        context.store.start_run(number, branch=f"foragent/issue-{number}",
                                worktree=context.store.worktree_path(number), lane=lane,
                                pid=7000 + number)


def _tick(context):
    report = scheduler.tick(context)
    assert report.skipped is None, report
    return report


def _change(context, number, files, *, commit=True):
    path = context.store.worktree_path(number)
    if not path.exists():
        context.git.add_worktree(path, f"foragent/issue-{number}", "origin/main")
    for name, content in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    if commit:
        git = context.git.at(path)
        git.stage_changes()
        git.commit("Offline implementation")  # The shared fixture imports the index.
    return path


def test_optimistic_overlap_default_and_documented_conflict_evidence():
    assert getattr(Config(), "overlap_per_file", None) == 2, (
        "Start cautiously at two runs per file: one real conflict (#196) in 231 runs")
    source = inspect.getsource(Config).splitlines()
    line = next(i for i, text in enumerate(source) if "overlap_per_file:" in text)
    comments = "\n".join(text.split("#", 1)[1] for text in source[max(0, line - 5):line + 3] if "#" in text)
    assert all(term in comments.lower() for term in ("231", "196", "conflict", "orchestrator")), (
        "Explain the measured starting value and who adjusts it beside its default")
    root = Path(__file__).resolve().parents[2]
    documents = [root / "README.md", *(root / "docs").rglob("*.md")]
    assert any(all(term in path.read_text(encoding="utf-8")
                   for term in ("overlap_per_file", "231", "196", "fa orch facts", "orchestrator"))
               for path in documents), (
        "Document the evidence and that the orchestrator adjusts the setting using conflict facts")


@pytest.mark.parametrize("limit", [1, 3])
def test_optimistic_overlap_configured_limit_is_used_by_the_scheduler(ctx, limit):
    starts = _wide_lane(ctx, 5)
    (ctx.repo_root / ".foragent.yml").write_text(f"overlap_per_file: {limit}\n")
    configured = load_config(ctx.repo_root)
    ctx.config = replace(configured, approvers=["human"], max_parallel=5, skill_libraries=[],
                         lanes={"orch": {"paths": ["src/foragent/**"]}})
    for number in range(1, 5):
        _issue(ctx, number, [STATE])
    _issue(ctx, 5, [OTHER])

    report = _tick(ctx)

    assert starts == [*range(1, limit + 1), 5], report.actions
    assert {run.number for run in ctx.store.active_runs()} == set(starts)
    _finish(ctx, 1)
    report = _tick(ctx)
    assert starts == [*range(1, limit + 1), 5, limit + 1], report.actions


@pytest.mark.parametrize("first,second", [
    ([STATE], [STATE]),
    (["src/foragent/stat*.py"], [STATE]),
    ([STATE], ["src/foragent/stat*.py"]),
    ([STATE, "src/foragent/stat*.py", STATE], [STATE]),
], ids=["literal", "running-glob", "candidate-glob", "count-runs-not-patterns"])
def test_optimistic_overlap_starts_two_and_waits_the_third_without_wasting_a_slot(ctx, first, second):
    starts = _wide_lane(ctx, 5)
    _issue(ctx, 1, first)
    _issue(ctx, 2, second)
    _issue(ctx, 3, [STATE])
    _issue(ctx, 4, [OTHER])

    report = _tick(ctx)

    assert starts == [1, 2, 4], report.actions
    assert {run.number for run in ctx.store.active_runs()} == {1, 2, 4}
    assert "ready" in ctx.tracker.get_issue(3).labels
    assert "running" not in ctx.tracker.get_issue(3).labels
    _finish(ctx, 4)
    _tick(ctx)
    assert starts == [1, 2, 4], "An unrelated completion does not free a shared-file place"
    _finish(ctx, 1)
    report = _tick(ctx)
    assert starts == [1, 2, 4, 3], report.actions


def test_optimistic_overlap_counts_each_file_independently_and_honors_machine_capacity(ctx):
    starts = _wide_lane(ctx, 3)
    for number, paths in ((1, [STATE]), (2, [CLI]), (3, [STATE, CLI]), (4, [OTHER])):
        _issue(ctx, number, paths)

    report = _tick(ctx)

    assert starts == [1, 2, 3], report.actions
    assert len(ctx.store.active_runs()) == 3
    assert "ready" in ctx.tracker.get_issue(4).labels
    _finish(ctx, 2)
    report = _tick(ctx)
    assert starts == [1, 2, 3, 4], report.actions


def test_optimistic_overlap_per_file_limit_applies_across_lanes(ctx):
    starts = _wide_lane(ctx, 5)
    ctx.config = replace(ctx.config, lanes={
        **ctx.config.lanes, "api": {"paths": ["src/foragent/**"]},
        "view": {"paths": ["src/foragent/**"]},
    })
    for number, lane in ((1, "orch"), (2, "api"), (3, "view")):
        _issue(ctx, number, [STATE], lane=lane)
    _issue(ctx, 4, [OTHER], lane="view")

    report = _tick(ctx)

    assert set(starts) == {1, 2, 4}, report.actions
    assert "ready" in ctx.tracker.get_issue(3).labels
    _finish(ctx, 1)
    report = _tick(ctx)
    assert set(starts) == {1, 2, 3, 4}, report.actions


@pytest.mark.parametrize("declared", [[STATE, CLI], ["src/foragent/*.py"]], ids=["literal", "glob"])
def test_optimistic_overlap_uses_effective_files_only_after_first_commit_and_refreshes_them(ctx, declared):
    starts = _wide_lane(ctx, 5)
    _issue(ctx, 1, declared, running=True)
    _issue(ctx, 2, [STATE, CLI], running=True)
    _issue(ctx, 3, [STATE])
    _change(ctx, 1, {CLI: "VALUE = 1\n"}, commit=False)
    _tick(ctx)
    assert starts == [], "An empty committed diff before the first commit must retain declared Files"

    _change(ctx, 1, {CLI: "VALUE = 1\n"})
    report = _tick(ctx)
    assert starts == [3], report.actions

    # Later changes must join the effective set rather than freezing the first diff.
    _change(ctx, 1, {STATE: "VALUE = 2\n"})
    _finish(ctx, 2)
    _issue(ctx, 4, [STATE])
    _tick(ctx)
    assert starts == [3], "Run 1 now shares state.py with run 3, so run 4 must wait"
    _finish(ctx, 1)
    report = _tick(ctx)
    assert starts == [3, 4], report.actions


def test_optimistic_overlap_effective_files_exclude_changes_inherited_from_main(ctx, git_history):
    starts = _wide_lane(ctx, 5)
    _issue(ctx, 1, [STATE, CLI], running=True)
    _issue(ctx, 2, [STATE], running=True)
    _issue(ctx, 3, [STATE])
    path = _change(ctx, 1, {CLI: "VALUE = 1\n"})
    git_history.update({STATE: "BASE = 2\n"})
    ctx.git.fetch("origin", "main")
    git = ctx.git.at(path)
    git.merge_base("origin/main", commit=False)
    git.commit("Offline base refresh")
    assert git_command(path, "show", f"HEAD:{STATE}") == "BASE = 2"

    report = _tick(ctx)

    assert starts == [3], report.actions


@pytest.mark.parametrize("owner", ["running", "candidate"])
@pytest.mark.parametrize("lane", ["orch", "other"], ids=["same-lane", "cross-lane"])
def test_optimistic_overlap_exclusive_paths_lock_in_both_directions_and_release(ctx, owner, lane):
    starts = _wide_lane(ctx, 5)
    ctx.config = replace(ctx.config, lanes={**ctx.config.lanes, "other": {"paths": ["src/foragent/**"]}})
    # Exclusive paths may be broader than the implementation's declared Files.
    # This also distinguishes exclusivity from the old all-files mutex on main.
    _issue(ctx, 1, [CLI] if owner == "running" else [STATE], running=True,
           exclusive=["src/foragent/stat*.py"] if owner == "running" else [])
    _issue(ctx, 2, [STATE] if owner == "running" else [CLI], lane=lane,
           exclusive=["src/foragent/stat*.py"] if owner == "candidate" else [])
    _issue(ctx, 3, [OTHER], lane=lane)

    report = _tick(ctx)

    assert starts == [3], report.actions
    assert "ready" in ctx.tracker.get_issue(2).labels
    assert "running" not in ctx.tracker.get_issue(2).labels
    _finish(ctx, 1)
    report = _tick(ctx)
    assert starts == [3, 2], report.actions


def test_optimistic_overlap_exclusive_reservation_survives_effective_file_narrowing(ctx):
    starts = _wide_lane(ctx, 5)
    _issue(ctx, 1, [CLI], running=True, exclusive=[STATE])
    _change(ctx, 1, {CLI: "VALUE = 1\n"})
    _issue(ctx, 2, [STATE])
    _issue(ctx, 3, [OTHER])

    report = _tick(ctx)

    assert starts == [3], report.actions
    _finish(ctx, 1)
    report = _tick(ctx)
    assert starts == [3, 2], report.actions


def test_optimistic_overlap_order_plan_preserves_exclusive_paths_for_issues_and_edits(git_history):
    from foragent.order import issue_body, validate_plan

    item = {"key": "split", "title": "Split state", "lane": "main", "size": "m",
            "goal": "Split state without concurrent edits", "out_of_scope": "Other modules",
            "done_when": [f"pytest {ACCEPTANCE}"], "files": [CLI], "exclusive": [STATE, "src/foragent/state/**"]}
    edit = {key: item[key] for key in ("goal", "out_of_scope", "done_when", "files", "exclusive")}
    edit["number"] = 145
    plan = {"id": "optimistic-overlap", "title": "Split state", "summary": "Reserve split paths",
            "spec_markdown": "# Split state\n", "issues": [item], "edits": [edit]}

    validate_plan(plan, git_history.clone, Config(skill_libraries=[]))

    for entry in (item, edit):
        parsed = parse_body(issue_body(entry, "owner/repo", "main"))
        assert parsed.files == [CLI]
        assert getattr(parsed, "exclusive", None) == item["exclusive"]
    assert getattr(parse_body(_body([CLI], [STATE])), "exclusive", None) == [STATE]


def _runner_case(git_history, tmp_path, monkeypatch, files, **config):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true",
                     lanes={"orch": {"paths": ["**"]}}, **config)
    body = _body(files).replace(f"pytest {ACCEPTANCE}", "true")
    case.tracker.edit_body(1, body)
    case.tracker.add_labels(1, ["lane:orch"])
    case.store.save_approval(1, body=body, body_sha=body_hash(body),
                            acceptance_blobs=case.git.blob_shas("origin/main", [ACCEPTANCE]),
                            approved_by="human", approved_at=case.clock[0].isoformat())
    case.store.update_run(1, lane="orch", approval_body_sha=body_hash(body))
    original = case.git._run

    def git_run(args):
        root = Path(args[1]) if args[:1] == ["-C"] else case.root
        command = args[2:] if args[:1] == ["-C"] else args
        if "commit" in command or ("merge" in command and "--continue" in command):
            import_index(root, "Offline implementation or resolution")
            return ""
        if "merge" in command and "--no-edit" in command and "--no-commit" not in command:
            # Keep successful merge parents too, without git merge spawning commit.
            result = original([*args, "--no-commit"])
            if case.git.at(root).merge_in_progress():
                import_index(root, "Offline base refresh")
            return result
        return original(args)

    monkeypatch.setattr(case.git, "_run", git_run)
    return case


def _peer(case, files, *, exclusive=()):
    context = tick_context(case)
    approve(context, 2, body=_body(files, exclusive), labels=["lane:orch", "running"])
    body = case.tracker.get_issue(2).body
    case.store.save_approval(2, body=body, body_sha=body_hash(body), acceptance_blobs={},
                            approved_by="human", approved_at=case.clock[0].isoformat())
    case.store.start_run(2, branch="foragent/issue-2", worktree=case.store.worktree_path(2), lane="orch")


def _finish_peer(case):
    case.store.finish_run(2, outcome="merged")
    case.tracker.close_issue(2)
    case.tracker.remove_labels(2, ["ready", "running"])


@pytest.mark.parametrize("boundary", ["continue", "merge"])
def test_optimistic_overlap_late_undeclared_file_waits_before_continuing_or_merging(
    git_history, tmp_path, monkeypatch, boundary,
):
    case = _runner_case(git_history, tmp_path, monkeypatch, [CLI])
    _peer(case, ["feature.txt"], exclusive=["feature.txt"])
    waits = []

    def implement(kwargs):
        path = kwargs["worktree"] / CLI
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("VALUE = 1\n")
        git = case.git.at(kwargs["worktree"])
        git.stage_changes()
        git.commit("First implementation commit")
        # Effective Files expand after the first commit, outside the declaration.
        (kwargs["worktree"] / "feature.txt").write_text("done\n")

    def next_implementation(kwargs):
        assert waits and case.store.get_run(2).status == "finished", (
            "The next implementation turn ran while its new file was exclusively owned")
        (kwargs["worktree"] / "feature.txt").write_text("done, revised\n")

    def wait(seconds):
        assert len(case.implementer.calls) == 1
        assert case.tracker.merged_prs == []
        assert case.store.get_run(2).status == "running"
        waits.append(seconds)
        case.clock[0] += timedelta(seconds=seconds)
        _finish_peer(case)

    merge = case.tracker.merge_pr

    def merge_after_release(pr, **kwargs):
        assert waits and case.store.get_run(2).status == "finished", (
            "The merge crossed an exclusive reservation after its effective Files expanded")
        return merge(pr, **kwargs)

    case.ctx.sleep = wait
    case.implementer.steps = [implement] + ([next_implementation] if boundary == "continue" else [])
    case.reviewer.steps = (["VERDICT: FAIL\nfeature.txt:1 revise wording"] if boundary == "continue" else []) + ["VERDICT: PASS"]
    monkeypatch.setattr(case.tracker, "merge_pr", merge_after_release)

    outcome = runner.run_issue(1, case.ctx)

    assert outcome == "merged", case.store.log_path(1).read_text()
    assert waits and len(case.implementer.calls) == (2 if boundary == "continue" else 1)
    saved = json.loads((case.store.root / "runs/1.json").read_text())
    assert saved["declared_files"] == [CLI]
    assert set(saved["effective_files"]) == {CLI, "feature.txt"}


@pytest.mark.parametrize("conflict,peer_finishes", [
    ("none", False), ("none", True), ("resolved", False), ("rebuild", False),
], ids=["clean", "peer-finished-before-merge", "resolved", "rebuild"])
def test_optimistic_overlap_runner_persists_clean_conflict_and_redo_facts(
    git_history, tmp_path, monkeypatch, conflict, peer_finishes,
):
    declared = ["feature.txt", CLI, OTHER]
    case = _runner_case(git_history, tmp_path, monkeypatch, declared, conflict_retries=1)
    _peer(case, ["feature.txt"])

    def implement(kwargs):
        (kwargs["worktree"] / "feature.txt").write_text("mine\n")
        if conflict != "none":
            case.history.update({"feature.txt": "theirs\n"})

    def resolve(kwargs):
        path = kwargs["worktree"] / "feature.txt"
        assert "<<<<<<<" in path.read_text(), "Exercise an actual offline Git conflict"
        case.clock[0] += timedelta(seconds=13)
        if conflict == "resolved":
            path.write_text("theirs\nmine\n")
        return "Resolved" if conflict == "resolved" else "Could not resolve"

    def rebuild(kwargs):
        path = kwargs["worktree"] / "feature.txt"
        assert path.read_text() == "theirs\n"
        case.clock[0] += timedelta(seconds=7)  # Redo implementation is distinct from resolution.
        path.write_text("theirs\nmine\n")

    case.implementer.steps = [implement] + ([resolve] if conflict != "none" else []) + (
        [rebuild] if conflict == "rebuild" else [])

    def review(kwargs):
        if peer_finishes:
            _finish_peer(case)
        return "VERDICT: PASS"

    case.reviewer.steps = [review] * 4

    assert runner.run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
    assert len(case.implementer.calls) == {"none": 1, "resolved": 2, "rebuild": 3}[conflict]
    expected = {"file": "feature.txt", "issues": [2], "conflicted": conflict != "none",
                "resolution_seconds": 0 if conflict == "none" else 13,
                "redo_count": int(conflict == "rebuild")}
    saved = json.loads((case.store.root / "runs/1.json").read_text())
    required = {"declared_files", "changed_files", "effective_files", "overlap_records"}
    assert required <= saved.keys(), f"The runner did not persist overlap facts: {required - saved.keys()}"
    assert saved["declared_files"] == declared
    assert saved["changed_files"] == saved["effective_files"] == ["feature.txt"]
    assert saved["overlap_records"] == [expected]
    # The dashboard must be able to reopen both the snapshot and completion history.
    reopened = StateStore(case.store.root)
    for run in (reopened.get_run(1), next(row for row in reopened.read_history() if row.number == 1)):
        assert run.overlap_records == [expected]
        assert run.declared_files == declared and run.changed_files == ["feature.txt"]


def _recorded(case, number, *, hours, declared, changed, overlaps, outcome="merged"):
    """Seed only the facts consumer; producer tests above never seed new fields."""
    completed(case, number, started=NOW - timedelta(hours=hours), outcome=outcome)
    snapshot = case.store.root / "runs" / f"{number}.json"
    saved = json.loads(snapshot.read_text())
    fields = {"declared_files": declared, "changed_files": changed,
              "effective_files": changed, "overlap_records": overlaps}
    saved.update(fields)
    snapshot.write_text(json.dumps(saved) + "\n")
    history = case.store.root / "runs.jsonl"
    rows = [json.loads(line) for line in history.read_text().splitlines()]
    for row in rows:
        if (row["number"], row["started_at"]) == (number, saved["started_at"]):
            row.update(fields)
    history.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _overlap(path, *, conflict=False, seconds=0, redo=0, issues=(90,)):
    return {"file": path, "issues": list(issues), "conflicted": conflict,
            "resolution_seconds": seconds, "redo_count": redo}
