"""Acceptance test for lane-dream. Locked by Foragent; do not edit."""

from datetime import timedelta
import re

import pytest

from test_local_view import record_gh
from test_orch_review_handoff import bind_cli, make_case, tick_context
from test_skills_autosync import cli_exit, isolated_home
from test_squirrel_running import view_offline


@pytest.fixture(autouse=True)
def dream_offline(view_offline, monkeypatch):
    return record_gh(monkeypatch)


def dream_case(git_history, tmp_path, monkeypatch):
    case = make_case(
        git_history, tmp_path, monkeypatch, skill_libraries=[], language="ko",
        test_command="true", max_parallel=2,
        lanes={name: {"title": name, "paths": [f"{name}/**"]} for name in ("orch", "view")},
    )
    # The shared runner fixture starts issue #1; this scenario begins between jobs.
    case.store.finish_run(1, outcome="blocked")
    case.tracker.remove_labels(1, ["running"])
    case.tracker.close_issue(1)
    case.main = bind_cli(case, monkeypatch)
    return case


def dream_finish(case, number, lane="orch", *, outcome="merged"):
    case.clock[0] += timedelta(seconds=1)
    case.tracker.add_issue(number, f"Lane work {number}", labels=[f"lane:{lane}"], state="closed")
    case.store.start_run(number, branch=f"foragent/issue-{number}",
                         worktree=case.store.worktree_path(number), lane=lane)
    case.store.finish_run(number, outcome=outcome)


def dream_candidates(case):
    from foragent.scheduler import dream_due

    lanes = dream_due(case.store, case.config, case.tracker, case.clock[0])
    assert isinstance(lanes, list) and all(isinstance(lane, str) for lane in lanes)
    assert len(lanes) == len(set(lanes)), "A lane must be offered only once"
    return set(lanes)


def dreamed(case, lane):
    return cli_exit(case.main, ["lanes", "dreamed", lane, "--repo-root", str(case.root)])


def dream_launcher(starts, execute=None):
    """Use the issue launcher boundary, with kind/lane facts for maintenance runs."""
    def launch(number=None, *, kind="issue", lane=None):
        lane = lane if lane is not None else number if kind == "dream" else None
        starts.append({"kind": kind, "lane": lane})
        assert kind == "dream" and isinstance(lane, str), starts
        if execute is not None:
            execute(lane)
            return None
        return 88000 + len(starts)

    return launch


def test_lane_dream_empty_queue_completion_persists_per_lane_until_a_new_merge(
    git_history, tmp_path, monkeypatch, dream_offline,
):
    from foragent.state import StateStore
    from foragent.watch import recent_events

    case = dream_case(git_history, tmp_path, monkeypatch)
    dream_finish(case, 10, "orch")
    dream_finish(case, 20, "view")
    assert dream_candidates(case) == {"orch", "view"}
    case.clock[0] += timedelta(seconds=1)
    assert dreamed(case, "orch") == 0
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    assert dream_candidates(case) == {"view"}, "Finishing orch's dream must not reset view"
    assert any("꿈을 꿨어요" in event.get("say", "") and "orch" in event["say"]
               for event in recent_events(case.store))
    case.clock[0] += timedelta(days=30)
    dream_finish(case, 30, outcome="blocked")
    assert dream_candidates(case) == {"view"}, "Elapsed time and failed work are not new merges"
    dream_finish(case, 40, "orch")
    assert dream_candidates(case) == {"orch", "view"}
    assert dream_offline == []


def test_lane_dream_backlogged_lane_is_due_at_ten_merges_since_its_last_dream(
    git_history, tmp_path, monkeypatch, dream_offline,
):
    from foragent.scheduler import DREAM_EVERY_MERGES

    assert DREAM_EVERY_MERGES == 10
    case = dream_case(git_history, tmp_path, monkeypatch)
    case.tracker.add_issue(500, "Keep this lane's queue occupied", labels=["approved", "ready", "lane:orch"])
    dream_finish(case, 2, outcome="blocked")
    dream_finish(case, 3, outcome="retries-exceeded")
    for number in range(10, 19):
        dream_finish(case, number)
    assert dream_candidates(case) == set(), "Nine merges plus failed runs are still nine merges"
    dream_finish(case, 19)
    assert dream_candidates(case) == {"orch"}
    case.clock[0] += timedelta(seconds=1)
    assert dreamed(case, "orch") == 0
    assert dream_candidates(case) == set()
    for number in range(20, 29):
        dream_finish(case, number)
    assert dream_candidates(case) == set(), "The threshold counts from the completed dream"
    dream_finish(case, 29)
    assert dream_candidates(case) == {"orch"}
    assert "ready" in case.tracker.get_issue(500).labels
    assert dream_offline == []


@pytest.mark.parametrize("active_count", [2, 3], ids=["at-capacity", "over-capacity"])
def test_lane_dream_gate_respects_capacity_and_starts_only_one_per_tick_without_usage(
    git_history, tmp_path, monkeypatch, dream_offline, active_count,
):
    from foragent.scheduler import tick

    case = dream_case(git_history, tmp_path, monkeypatch)
    dream_finish(case, 10, "orch")
    dream_finish(case, 20, "view")
    for number in range(700, 700 + active_count):
        case.tracker.add_issue(number, "Already working", labels=["running"])
        case.store.start_run(number, branch=f"foragent/issue-{number}",
                             worktree=case.store.worktree_path(number))
    monkeypatch.setattr("foragent.scheduler.os.kill", lambda pid, signal: None)
    ctx = tick_context(case)
    starts = []
    ctx.launcher = dream_launcher(starts)
    report = tick(ctx)
    assert report.skipped is None and starts == [], report.actions
    for run in case.store.active_runs():
        case.store.finish_run(run.number, outcome="blocked")
        case.tracker.close_issue(run.number)
    # No executor has run or recorded usage in this case. Missing usage permits a dream.
    report = tick(ctx)
    assert len(starts) == 1, ("One of two due lanes must start, even with no ready issues", report.actions)
    active = case.store.active_runs()
    assert len(active) == 1 and active[0].kind == "dream" and active[0].lane == starts[0]["lane"]
    assert starts[0]["lane"] in {"orch", "view"}
    tick(ctx)
    assert len(starts) == 2 and {start["lane"] for start in starts} == {"orch", "view"}
    tick(ctx)
    assert len(starts) == 2, "An already dreaming lane must not start a duplicate run"
    assert dream_offline == []


def test_lane_dream_worker_receives_memory_only_prompt_and_completion_records_person_line(
    git_history, tmp_path, monkeypatch, dream_offline,
):
    from foragent import cli, executors, runner
    from foragent.scheduler import tick
    from foragent.state import StateStore
    from foragent.watch import recent_events

    case = dream_case(git_history, tmp_path, monkeypatch)
    dream_finish(case, 10)
    log = case.store.log_path(10)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("Review finding: keep the user's locale.\nFailure reason: stale configuration.\n")
    memory = case.root / ".claude/agent-memory/orch/MEMORY.md"
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_text("An old lesson.\nAn old lesson.\n")
    decisions = case.root / "docs/decisions.md"
    decisions.parent.mkdir(exist_ok=True)
    decisions.write_text("The current decision is authoritative.\n")
    code = case.root / "feature.py"
    code.write_text("ANSWER = 42\n")
    before = code.read_bytes()
    case.implementer.steps = ["Reviewed the lane's memory."]
    monkeypatch.setattr(cli, "RunContext", lambda **kwargs: case.ctx)
    for module in (executors, runner):
        monkeypatch.setattr(module, "executor_for", case.ctx.executor_factory)
    for executor in (executors.ClaudeExecutor, executors.CodexExecutor):
        monkeypatch.setattr(executor, "run", lambda self, **kwargs: case.implementer.run(**kwargs))

    def execute(lane):
        # Worker CLI mirrors gate run-issue; exercise production prompt/completion handling.
        assert cli_exit(case.main, ["gate", "run-dream", lane, "--repo-root", str(case.root)]) == 0

    starts = []
    ctx = tick_context(case)
    ctx.launcher = dream_launcher(starts, execute)
    tick(ctx)
    assert starts == [{"kind": "dream", "lane": "orch"}], "The due dream never reached the launcher"
    assert len(case.implementer.calls) == 1
    prompt = " ".join(case.implementer.calls[0]["prompt"].lower().split())
    for path in (".claude/agent-memory/orch/memory.md", "docs/decisions.md", ".local/share/foragent/media/"):
        assert path in prompt, (path, prompt)
    concepts = {
        "read run logs": r"(?:read|review|inspect|읽|검토).{0,160}(?:logs?|기록)",
        "review findings": r"review.{0,40}(?:findings?|comments?|feedback)|리뷰.{0,20}지적",
        "failure reasons": r"fail\w*.{0,40}(?:reasons?|causes?)|실패.{0,20}(?:이유|원인)",
        "add lessons": r"(?:add|record|capture|append).{0,60}(?:lessons?|learn)|(?:배운|교훈).{0,30}(?:더|추가)",
        "remove stale lessons": r"(?:remove|delete|drop|prune).{0,60}(?:stale|outdated|obsolete)|(?:낡은|오래된).{0,30}(?:지우|삭제)",
        "merge duplicates": r"deduplicat|(?:merge|consolidate).{0,50}(?:duplicat|overlap)|(?:겹친|중복).{0,30}(?:합치|통합)",
        "resolve contradictions": r"(?:resolv|reconcil).{0,60}(?:contradict|conflict)|(?:모순|어긋난).{0,40}정리",
        "decisions win": r"(?:favour|favor|prefer|defer to|follow).{0,60}docs/decisions\.md|docs/decisions\.md.{0,80}(?:authoritative|precedence|source of truth|기준|우선)",
        "cross-project promotion": r"(?:promot|올린|승격)",
        "repeated across projects": r"(?:recur|repeat).{0,80}projects?|projects?.{0,80}(?:recur|repeat)|프로젝트.{0,30}반복",
        "do not touch code": r"(?:do not|must not|never|don't).{0,40}(?:touch|edit|modify|change).{0,30}code|코드.{0,25}(?:수정|변경|건드리).{0,15}(?:말|금지|않)",
    }
    for concept, pattern in concepts.items():
        assert re.search(pattern, prompt), (concept, prompt)
    assert re.search(r"media/\S+\.md", prompt), "Promotion must name a medium Markdown bundle"
    assert code.read_bytes() == before
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    assert not any(run.kind == "dream" for run in case.store.active_runs())
    assert dream_candidates(case) == set(), "A completed dream must reset eligibility without a manual command"
    assert any("꿈을 꿨어요" in event.get("say", "") and "orch" in event["say"]
               for event in recent_events(case.store))
    tick(ctx)
    assert len(starts) == 1, "The next tick must not repeat a completed dream"
    assert dream_offline == []


def test_lane_dream_no_merges_ever_is_never_due_even_after_failed_runs_and_time(
    git_history, tmp_path, monkeypatch, dream_offline,
):
    from foragent.scheduler import tick

    case = dream_case(git_history, tmp_path, monkeypatch)
    starts = []
    ctx = tick_context(case)
    ctx.launcher = dream_launcher(starts)
    assert dream_candidates(case) == set()
    for number, outcome in enumerate(("blocked", "retries-exceeded", "killed"), 10):
        dream_finish(case, number, outcome=outcome)
    case.clock[0] += timedelta(days=365)
    assert dream_candidates(case) == set()
    tick(ctx)
    assert starts == []
    assert dream_offline == []
