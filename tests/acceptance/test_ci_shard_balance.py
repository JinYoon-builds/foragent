"""Locked acceptance: CI acceptance shards are balanced by measured test time.

Acceptance CI used to split files by a hash of the path, so three heavy files
(196 s, 130 s, 112 s) shared one shard: 12.5 min against 2.7-5.1 min for the
others, and PRs stopped on "CI did not finish".

The source of the measured times is the lane's choice, but it must be readable
from the exact base commit's tree, as ci_shard already reads its configuration
only from the trusted base. This test therefore copies this repository's own
files (whatever timing source they carry) into an offline base commit, replaces
the acceptance tests with stub files at the measured paths, and runs the real
`ci_shard.main` once per shard. The head commit keeps only test files: the head
must reuse the base's assignment, or the base-vs-head comparison would report
moved tests as lost.

ORACLE holds per-file seconds from the issue-1511 acceptance JUnit run
(2026-11-02, files of 1 s or more). Under hash sharding those files give
shards of about 5.1 / 4.3 / 12.5 / 2.7 min.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

from foragent import ci_shard
from foragent.ci_workflow import EXCLUSIONS_TRAILER


ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / ".foragent.yml").is_file() and (p / "src/foragent/ci_shard.py").is_file())
SHARDS = 4
BOUND = 1.3  # slowest shard / mean shard, by the oracle times
PREFIX = "tests/acceptance/test_"
NEW_FILE = "tests/acceptance/test_zz_unmeasured_new_file.py"
ORACLE_TEXT = """
merge_path_real_settings=196.1 d85_flush_recovery=129.6 merge_group_bisect=111.9 retry_fixes_not_redoes=56.6
model_depth_choice=55.9 risk_map=48.6 base_move_neutral_more=30.5 baseline_follows_base=28.7
review_verdict_line=27.5 baseline_renamed_tests=26.6 wait_releases_slot=24.0 harness_guard=23.6
premerge_unmerged_locks=19.0 merge_wait_limit=18.2 session_resume_prompt=16.2 review_perspectives=13.4
role_signal_meaningful_change=12.9 pr_title_from_issue=12.7 usage_recheck=11.5 usage_read=11.2
executor_choice=11.0 facts_baseline_trend=10.7 review_parallel=10.6 related_tests_record=10.4
review_what_how=10.2 notify_summary_time=10.1 home_isolation=9.8 stale_branch_locks=9.5 local_view=9.4
fast_checks=9.2 agents_see_whole_picture=8.8 lane_test_lock=8.7 orch_refresh=8.5 claude_quota_mode=8.5
orch_stop=8.5 orch_plan=8.5 orch_review_handoff=7.7 view_open_triggers=7.6 statusline_wrapper=7.2
pstack_procedures=7.0 skills_per_issue=6.8 orch_reinstall=6.7 d80_assignment_goal_controls=6.7
ci_feedback=6.6 role_signal_not_now=6.5 update_check=6.0 skills_review_scope=5.9 view_usage_line=5.9
lane_test_review=5.8 gate_merge_reinstall=5.7 facts_recorded_sample=5.6 doctor_live=5.4 lane_queue=5.3
star_prompt=5.3 review_rework_prompt=5.2 lane_test_resume=4.9 worker_test_scope=4.8 review_spec_conflict=4.7
related_check_budget=4.6 role_source=4.5 review_budget_batches=4.5 orch_seat=4.4 presence_order_stall=4.4
review_merged_return=4.4 ssot_hints=4.4 ssot_advice_publication=4.4 lane_m2_bubble=4.3 lane_m2_controls=4.3
cross_lane_start=4.3 upstream_reports=4.3 view_open=4.3 measured_overlap_plan=4.1 fa_brief=4.0
listing_sanity=4.0 one_entrance_stop=4.0 review_perspective_fanout=3.8 ssot_parent=3.8
ask_through_planning=3.8 order_lane_proposal=3.7 tidy_cycle=3.7 first_stall_restarts=3.4
night_shared_locks=3.4 progress_words=3.3 orch_say=3.3 github_limit_wait=3.2
orchestrator_scorecard_skill=3.2 lane_test_statements=3.2 tick_admission_resilience=3.1
review_structure_proposals=3.1 order_design_references=3.1 gate_open_view=3.0 role_signals=2.9
issue_files=2.9 fa_init_allow=2.9 tick_config_from_base=2.8 limit_exclude_slot_wait=2.8 squirrel_running=2.7
orch_silent_reaches_person=2.6 talk_state=2.6 cli_split=2.6 limit_ci_duration=2.6 review_confirm_only=2.6
notify_blocked=2.5 orch_timeout_ack=2.5 overlap_lock_load=2.5 single_lane_view=2.4 init_role_agents=2.4
lane_test_writer=2.4 limit_cache_timings=2.4 design_session=2.4 view_status=2.3 claude_only=2.3
skills_local=2.3 fetch_race=2.3 orca_handle_on_session_start=2.3 versioned_install=2.3 settings_window=2.2
orch_request_answer_record=2.2 worker_launch_budget=2.1 machine_sleep=2.1
publish_refuses_stale_overwrite=2.1 stale_files=2.1 state_records_tolerant=2.1 one_entrance_lessons=2.1
adopt_baseline=2.1 design_first=2.1 heartbeats=2.1 lane_dream=2.0 skills_add_partial_pack=2.0
order_perspectives=1.9 notify_personal_decisions=1.9 view_open_terminals=1.8 stop_sees_running=1.8
d85_orders_wait_for_boundary=1.7 ci_unit_shards=1.7 one_entrance_lesson_context=1.7 watch_reexec=1.6
board_status_groups=1.6 board_projects=1.6 lane_talk_view=1.5 d77_main_test_quality=1.5 tick_step_times=1.5
skills_add=1.4 view_waiting_phases=1.4 orch_spawn=1.4 lanes_skills_keeps_comments=1.4 orch_facts=1.4
settings_runner_tests=1.4 cli_skeleton=1.4 eta_remaining_work=1.3 adopt_survey=1.3
presence_session_start=1.3 prep_scenes=1.3 park_admission_hold=1.3 notify_only_actions=1.2
launch_first_run_sprint=1.2 wake_grace=1.2 acorn_interact=1.1 lane_m2_blocks=1.1 vendored_prove_it_works=1.1
launch_init_github_preflight=1.1 pstack_principle_catalog=1.0 one_entrance_copy=1.0
"""
ORACLE = {f"{PREFIX}{name}.py": float(seconds)
          for name, seconds in (token.split("=") for token in ORACLE_TEXT.split())}
# Measured files that still exist; a renamed file has no measured time any more.
MEASURED = {path: seconds for path, seconds in ORACLE.items() if (ROOT / path).is_file()}
# One heavy file holds two tests: a file never splits across shards.
SPLIT_FILE = f"{PREFIX}merge_path_real_settings.py"


def git(root, *args):
    return subprocess.run(["git", "-c", "user.name=fa", "-c", "user.email=fa@example.invalid",
                           "-c", "commit.gpgsign=false", *args],
                          cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def stub(path):
    names = ["test_one", "test_two"] if path == SPLIT_FILE else ["test_one"]
    return "".join(f"def {name}():\n    pass\n" for name in names)


def nodes(path):
    return [f"{path}::{name}" for name in (["test_one", "test_two"] if path == SPLIT_FILE else ["test_one"])]


def repository_files():
    """This checkout's tracked and new files, except Python test code and the
    project install files (the offline base runs pytest without uv)."""
    listed = git(ROOT, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")
    for name in sorted(set(filter(None, listed))):
        if name.startswith("tests/") and name.endswith(".py"):
            continue
        if name in {"pyproject.toml", "uv.lock"} or not (ROOT / name).is_file():
            continue
        yield name


@pytest.fixture(scope="module")
def shard_runs(tmp_path_factory):
    work = tmp_path_factory.mktemp("shard-balance")
    root = work / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    for name in repository_files():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, root / name)
    config = yaml.safe_load((root / ".foragent.yml").read_text(encoding="utf-8"))
    config.update(checks="ci", ci_shards=SHARDS,
                  test_command=f"{sys.executable} -m pytest -o addopts= -p no:cacheprovider --rootdir=.")
    (root / ".foragent.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
    for path in MEASURED:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(stub(path), encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "Base with this repository's timing source")
    base = git(root, "rev-parse", "HEAD")
    # Head: only test files. Any timing data in the head tree is gone, and one
    # new file has no measured time at all.
    git(root, "rm", "-q", "-r", "--cached", ".")
    for child in root.iterdir():
        if child.name != ".git":
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    for path in [*MEASURED, NEW_FILE]:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(stub(path), encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", f"Head\n\n{EXCLUSIONS_TRAILER}: []")
    head = git(root, "rev-parse", "HEAD")

    runs = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
        for index in range(SHARDS):
            report = work / f"shard-{index}.json"
            output = work / f"outputs-{index}"
            for key, value in {"FA_CHECKOUT": str(root), "FA_REPOSITORY": "owner/repo",
                               "FA_BASE_SHA": base, "FA_HEAD_SHA": head,
                               "FA_SHARD": str(index), "FA_SHARDS": str(SHARDS),
                               "FA_REPORT_FILE": str(report), "GITHUB_OUTPUT": str(output)}.items():
                patch.setenv(key, value)
            for key in ("FA_BASE_CACHE_FILE", "FA_BASE_CACHE_MODE"):
                patch.delenv(key, raising=False)
            ci_shard.main()
            payload = json.loads(report.read_text(encoding="utf-8"))
            outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
            assert payload["shard_index"] == index and payload["shard_count"] == SHARDS
            runs.append((payload, outputs))
    return runs


def files_of(ids):
    return {node.split("::", 1)[0] for node in ids}


def test_slowest_shard_is_close_to_the_mean_by_measured_time(shard_runs):
    assert sum(MEASURED.values()) >= 0.8 * sum(ORACLE.values()), (
        "most measured files were renamed; refresh ORACLE from a recent acceptance JUnit run")
    expected = sorted(node for path in MEASURED for node in nodes(path))
    collected = [payload["base"]["collected_ids"] for payload, _ in shard_runs]
    flat = sorted(node for ids in collected for node in ids)
    # No loss and no duplicate: every test of the base runs in exactly one shard.
    assert flat == expected
    for ids in collected:
        if SPLIT_FILE in files_of(ids):
            assert set(nodes(SPLIT_FILE)) <= set(ids), "a file must not split across shards"
    loads = [sum(MEASURED[path] for path in files_of(ids)) for ids in collected]
    mean = sum(loads) / SHARDS
    shown = ", ".join(f"{seconds / 60:.1f}" for seconds in loads)
    assert max(loads) <= BOUND * mean, (
        f"shard minutes [{shown}]: slowest {max(loads) / mean:.2f}x the mean, bound {BOUND}x")


def test_head_without_timing_data_keeps_the_base_assignment(shard_runs):
    # Soundness is only asked of the balanced assignment, so this case fails until the
    # balance exists (a guard that passes on main would count as 'lost' on every open PR).
    test_slowest_shard_is_close_to_the_mean_by_measured_time(shard_runs)
    new_shards = []
    for index, (payload, outputs) in enumerate(shard_runs):
        base_ids = set(payload["base"]["collected_ids"])
        head_ids = set(payload["head"]["collected_ids"])
        if NEW_FILE in files_of(head_ids):
            new_shards.append(index)
        # Same files in the same shard on both sides, so the per-shard
        # comparison sees no moved test as lost.
        assert head_ids - set(nodes(NEW_FILE)) == base_ids, f"shard {index}"
        assert payload["head"]["passing_ids"] and set(payload["head"]["passing_ids"]) >= base_ids
        assert outputs.get("lost") == "0", f"shard {index}"
    # A file with no measured time still runs, in exactly one shard.
    assert len(new_shards) == 1
