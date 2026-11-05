"""Skill lifecycle facts (#1040): admission origin, trial, comparison and retirement."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from foragent import skills
from foragent.skills import Library, Skill, SkillError

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
TRUSTED = Library("anthropic", "https://github.com/anthropics/skills", Path("/libs/anthropic"))
OTHER = Library("stranger", "https://github.com/someone/pack", Path("/libs/stranger"))


def _skill(tmp_path: Path, name: str, library: Library, *, role=None, scripts=False) -> Skill:
    path = tmp_path / library.name / "skills" / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n# Use\n## Check\n", encoding="utf-8")
    if scripts:
        (path / "scripts").mkdir()
        (path / "scripts" / "run.sh").write_text("echo\n", encoding="utf-8")
    return Skill(f"{library.name}:{name}", name, "Does  things", library, path, "MIT", None, (path,),
                 scripts, role)


def _store(tmp_path: Path, runs: list[tuple], waited: dict[tuple[int, int], float] | None = None,
           unreported: set[int] = frozenset()):
    """runs: (issue, lane, start hour offset, skills used, outcome[:hold reason]); each lasts
    number-of-skills+1 hours. Every execution reported its skills unless its issue is unreported."""
    history, finished = [], {}
    for number, lane, hour, used, outcome in runs:
        started = (T0 + timedelta(hours=hour)).isoformat()
        outcome, _, reason = outcome.partition(":")
        history.append(SimpleNamespace(number=number, lane=lane, kind="issue", status="finished",
                                       started_at=started, skills_used=used, outcome=outcome,
                                       hold_reason=reason or None, skills_reported=number not in unreported,
                                       waited_seconds=(waited or {}).get((number, hour), 0), slept_seconds=0))
        finished[number, started] = (T0 + timedelta(hours=hour + 1 + len(used))).isoformat()
    return SimpleNamespace(root=tmp_path / "state", read_history=lambda: history,
                           finish_times=lambda: finished, merge_times=lambda: {})


def test_comparison_counts_issues_with_and_without_in_lanes_that_used_the_skill(tmp_path):
    skill = _skill(tmp_path, "tdd", TRUSTED)
    store = _store(tmp_path, [
        (1, "api", 0, ["tdd"], "merged"), (2, "api", 1, ["tdd"], "merged"),
        (3, "api", 2, ["tdd"], "held:review-defects"),
        (4, "api", 3, [], "held:review-defects"), (4, "api", 4, [], "held:review-dispute"),
        (5, "api", 5, [], "merged"), (6, "api", 6, [], "merged"),
        (7, "view", 7, [], "held:review-defects"), (7, "view", 8, [], "held:review-defects"),  # Another lane.
    ])
    facts = skills.lifecycle_facts([skill], store, now=T0 + timedelta(days=2))
    entry = facts["skills"][0]
    assert entry["origin"] == "trusted" and entry["needs_approval"] is False and "review" not in entry
    assert entry["used_in"] == [1, 2, 3]
    assert entry["comparison"]["lanes"] == ["api"]
    assert entry["comparison"]["with"] == {"issues": 3, "executions": 3, "merged": 2, "median_seconds": 7200.0,
                                           "outcomes": {"held": 1, "merged": 2},
                                           "hold_reasons": {"review-defects": 1}, "repeated_failure_issues": []}
    assert entry["comparison"]["without"] == {"issues": 3, "executions": 4, "merged": 2, "median_seconds": 3600.0,
                                              "outcomes": {"held": 2, "merged": 2},
                                              "hold_reasons": {"review-defects": 1, "review-dispute": 1},
                                              "repeated_failure_issues": [4]}
    assert entry["evidence"] == "sufficient"
    assert entry["last_used"] == (T0 + timedelta(hours=2)).isoformat()
    assert entry["days_since_used"] == round((2 * 24 - 2) / 24, 1)
    json.dumps(facts)  # The facts are JSON.


def test_interruptions_are_not_failures_and_do_not_inflate_counts_or_time(tmp_path):
    skill = _skill(tmp_path, "tdd", TRUSTED)
    store = _store(tmp_path, [
        (1, "api", 0, ["tdd"], "limit-wait"), (1, "api", 2, ["tdd"], "limit-wait"),
        (1, "api", 4, ["tdd"], "merged"),
        (2, "api", 10, [], "stopped"), (2, "api", 11, [], "crashed"), (2, "api", 12, [], "hash-mismatch"),
    ], waited={(1, 0): 3600, (1, 2): 3600})
    entry = skills.lifecycle_facts([skill], store)["skills"][0]
    assert entry["comparison"]["with"]["repeated_failure_issues"] == []
    assert entry["comparison"]["without"]["repeated_failure_issues"] == []
    assert entry["comparison"]["with"]["issues"] == 1 and entry["comparison"]["with"]["executions"] == 3
    # Three 2-hour executions, two of which waited an hour each: 4 working hours to merge.
    assert entry["comparison"]["with"]["median_seconds"] == 4 * 3600.0
    assert entry["comparison"]["without"]["merged"] == 0 and entry["comparison"]["without"]["median_seconds"] is None
    assert entry["evidence"] == "insufficient"  # One issue a side, however many executions.


def test_only_reviewer_holds_are_failures_of_the_work(tmp_path):
    skill = _skill(tmp_path, "tdd", TRUSTED)
    store = _store(tmp_path, [
        (1, "api", 0, ["tdd"], "held:ci-not-started"), (1, "api", 1, ["tdd"], "held:ci-not-started"),
        (2, "api", 2, ["tdd"], "retries-exceeded"), (2, "api", 3, ["tdd"], "retries-exceeded"),
        (3, "api", 4, [], "review-failed"), (3, "api", 5, [], "review-failed"),
        (4, "api", 6, [], "held:review-defects"), (4, "api", 7, [], "held:review-defects"),
    ])
    comparison = skills.lifecycle_facts([skill], store)["skills"][0]["comparison"]
    assert comparison["with"]["repeated_failure_issues"] == []
    assert comparison["with"]["hold_reasons"] == {"ci-not-started": 2}  # Shown, not judged.
    assert comparison["without"]["repeated_failure_issues"] == [4]


def test_without_side_holds_only_confirmed_non_use(tmp_path):
    ours, theirs = _skill(tmp_path, "lint", TRUSTED), _skill(tmp_path, "lint", OTHER)
    store = _store(tmp_path, [
        (1, "api", 0, ["anthropic:lint"], "merged"), (2, "api", 1, ["anthropic:lint"], "merged"),
        (3, "api", 2, ["anthropic:lint"], "merged"),
        (4, "api", 3, ["lint"], "merged"), (5, "api", 4, ["lint"], "merged"), (6, "api", 5, ["lint"], "merged"),
        (7, "api", 6, [], "merged"), (8, "api", 7, [], "merged"),  # 8 never reported its skills.
        (9, "api", 8, ["other"], "merged"),
    ], unreported={8})
    entry = {item["id"]: item for item in skills.lifecycle_facts([ours, theirs], store)["skills"]}["anthropic:lint"]
    assert entry["comparison"]["without"]["issues"] == 2  # Issues 7 and 9.
    assert entry["comparison"]["unconfirmed_issues"] == [4, 5, 6, 8]
    assert entry["evidence"] == "insufficient"


def test_thin_evidence_is_reported_as_insufficient(tmp_path):
    skill = _skill(tmp_path, "tdd", TRUSTED)
    store = _store(tmp_path, [(1, "api", 0, ["tdd"], "merged"), (2, "api", 1, ["tdd"], "merged"),
                              (3, "api", 2, ["tdd"], "merged"), (4, "api", 3, [], "merged"),
                              (5, "api", 4, [], "merged"), (5, "api", 5, [], "merged")])
    assert skills.lifecycle_facts([skill], store)["skills"][0]["evidence"] == "insufficient"
    # Three executions but two issues with the skill: retried work is not more evidence.
    retried = _store(tmp_path, [(1, "api", 0, ["tdd"], "limit-wait"), (1, "api", 1, ["tdd"], "merged"),
                                (2, "api", 2, ["tdd"], "merged"), (3, "api", 3, [], "merged"),
                                (4, "api", 4, [], "merged"), (5, "api", 5, [], "merged")])
    assert skills.lifecycle_facts([skill], retried)["skills"][0]["evidence"] == "insufficient"
    unused = skills.lifecycle_facts([skill], _store(tmp_path, []))["skills"][0]
    assert unused["used_in"] == [] and unused["last_used"] is None and unused["evidence"] == "insufficient"


def test_a_name_shared_by_two_libraries_credits_neither_skill(tmp_path):
    ours, theirs = _skill(tmp_path, "lint", TRUSTED), _skill(tmp_path, "lint", OTHER)
    solo = _skill(tmp_path, "tdd", OTHER)
    store = _store(tmp_path, [(1, "api", 0, ["lint"], "merged"), (2, "api", 1, ["anthropic:lint"], "merged"),
                              (3, "api", 2, ["tdd"], "merged")])
    facts = skills.lifecycle_facts([ours, theirs, solo], store)
    entries = {entry["id"]: entry for entry in facts["skills"]}
    assert entries["anthropic:lint"]["used_in"] == [2]  # Only the full ID names it.
    assert entries["stranger:lint"]["used_in"] == [] and entries["stranger:lint"]["last_used"] is None
    assert entries["stranger:lint"]["last_ambiguous_mention"] == T0.isoformat()  # Maybe used: issue 1.
    assert entries["stranger:tdd"]["last_ambiguous_mention"] is None
    assert entries["stranger:lint"]["comparison"]["with"]["issues"] == 0
    assert entries["stranger:tdd"]["used_in"] == [3]  # A name no other listed skill has still counts.
    assert facts["ambiguous_uses"] == {"lint": ["anthropic:lint", "stranger:lint"]}
    assert facts["ambiguous_records"] == 1


def test_unfamiliar_source_carries_review_facts_and_needs_approval(tmp_path):
    stranger = _skill(tmp_path, "deploy", OTHER, scripts=True)
    approved = _skill(tmp_path, "lint", OTHER, role="tool")
    project = Skill("repo:mine", "mine", "d", Library("repo", str(tmp_path), tmp_path), stranger.path, "MIT", None)
    facts = {entry["id"]: entry for entry in skills.lifecycle_facts([stranger, approved, project],
                                                                    _store(tmp_path, []))["skills"]}
    review = facts["stranger:deploy"]["review"]
    assert facts["stranger:deploy"]["needs_approval"] is True
    assert review["headings"] == ["Use", "Check"] and review["scripts"] == ["scripts/run.sh"]
    assert review["license"] == "MIT" and review["skill_md"].endswith("deploy/SKILL.md")
    assert facts["stranger:lint"]["origin"] == "approved" and "review" not in facts["stranger:lint"]
    assert facts["repo:mine"]["origin"] == "project" and facts["repo:mine"]["needs_approval"] is False


def test_trial_window_starts_at_admission_and_retirement_stops_offering(tmp_path):
    skill = _skill(tmp_path, "tdd", TRUSTED)
    store = _store(tmp_path, [(1, "api", 0, ["tdd"], "merged"), (2, "api", 48, ["tdd"], "merged")])
    clock = iter([T0 + timedelta(hours=24), T0 + timedelta(hours=72)])
    skills.record_lifecycle(store.root, skill.id, "trial", "repeated test findings in api", issues=[2],
                            now=lambda: next(clock))
    entry = skills.lifecycle_facts([skill], store)["skills"][0]
    assert entry["status"] == "trial" and entry["since"] == (T0 + timedelta(hours=24)).isoformat()
    assert entry["used_in"] == [2] and entry["offered"] is True  # Run 1 predates the trial.
    assert skills.offered_for_new_work(skill, skills.retired_skills(store.root))

    skills.record_lifecycle(store.root, skill.id, "retired", "no gain over 6 runs", evidence={"with": 3},
                            now=lambda: next(clock))
    entry = skills.lifecycle_facts([skill], store)["skills"][0]
    assert entry["status"] == "retired" and entry["status_reason"] == "no gain over 6 runs"
    assert entry["since"] == (T0 + timedelta(hours=24)).isoformat() and entry["offered"] is False
    assert skills.retired_skills(store.root) == {skill.id}
    assert not skills.offered_for_new_work(skill, skills.retired_skills(store.root))
    namesake = _skill(tmp_path, "tdd", OTHER)
    assert skills.offered_for_new_work(namesake, skills.retired_skills(store.root))  # Same name, other skill.
    lines = skills.lifecycle_path(store.root).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["status"] for line in lines] == ["trial", "retired"]  # Appended, not rewritten.


def test_lifecycle_entries_need_a_full_id_a_known_status_and_a_reason(tmp_path):
    with pytest.raises(SkillError):
        skills.record_lifecycle(tmp_path, "lint", "retired", "why")  # A bare name may name several skills.
    with pytest.raises(SkillError):
        skills.record_lifecycle(tmp_path, "a:b", "paused", "why")
    with pytest.raises(SkillError):
        skills.record_lifecycle(tmp_path, "a:b", "retired", "  ")
    assert not skills.lifecycle_path(tmp_path).exists()
    skills.lifecycle_path(tmp_path).write_text('not json\n{"skill": "lint", "status": "retired", "reason": "x"}\n',
                                               encoding="utf-8")
    assert skills.lifecycle_states(tmp_path) == {}


# Each case is one issue: a first execution in another lane records the name, then a merge in
# the compared lane records nothing. Its bucket for anthropic:lint must not depend on the outcome.
NAME_CASES = [
    ("full-id", ["anthropic:lint"], True, "used"),
    ("ambiguous", ["lint"], True, "unconfirmed"),
    ("ambiguous-with-other", ["lint", "tdd"], True, "unconfirmed"),
    ("namesake-full-id", ["stranger:lint"], True, "unused"),
    ("unique-other", ["tdd"], True, "unused"),
    ("unknown", ["zzz"], True, "unused"),
    ("nothing", [], True, "unused"),
    ("unreported", [], False, "unconfirmed"),
]


@pytest.mark.parametrize("outcome", ["merged", "held:review-defects", "limit-wait", "stopped"])
@pytest.mark.parametrize("case, recorded, reported, bucket", NAME_CASES, ids=[case[0] for case in NAME_CASES])
def test_recorded_name_resolves_to_one_bucket_whatever_the_outcome(tmp_path, outcome, case, recorded, reported,
                                                                    bucket):
    ours, theirs = _skill(tmp_path, "lint", TRUSTED), _skill(tmp_path, "lint", OTHER)
    tdd = _skill(tmp_path, "tdd", OTHER)
    runs = [(1, "api", 0, ["anthropic:lint"], "merged"), (2, "api", 1, ["anthropic:lint"], "merged"),
            (3, "api", 2, ["anthropic:lint"], "merged"),
            (4, "api", 3, [], "merged"), (5, "api", 4, [], "merged"),  # Two confirmed non-uses.
            (9, "view", 10, recorded, outcome), (9, "api", 11, [], "merged")]
    store = _store(tmp_path, runs)
    if not reported:
        next(run for run in store.read_history() if run.lane == "view").skills_reported = False
    facts = skills.lifecycle_facts([ours, theirs, tdd], store)
    entry = {item["id"]: item for item in facts["skills"]}["anthropic:lint"]
    assert (9 in entry["used_in"]) == (bucket == "used")
    assert (9 in entry["comparison"]["unconfirmed_issues"]) == (bucket == "unconfirmed")
    assert entry["comparison"]["without"]["issues"] == 2 + (bucket == "unused")
    assert entry["comparison"]["with"]["issues"] == 3 + (bucket == "used")
    # Three issues with the skill; only a confirmed non-use makes the third without it.
    assert entry["evidence"] == ("sufficient" if bucket == "unused" else "insufficient")
    assert facts["ambiguous_records"] == recorded.count("lint")


def test_ambiguous_record_in_another_lane_keeps_a_later_empty_merge_unconfirmed(tmp_path):
    """Review 3: held in view with `lint`, then merged in api reporting nothing, three times."""
    ours, theirs = _skill(tmp_path, "lint", TRUSTED), _skill(tmp_path, "lint", OTHER)
    runs = [(n, "api", n, ["anthropic:lint"], "merged") for n in (1, 2, 3)]
    for n in (4, 5, 6):
        runs += [(n, "view", 10 + 2 * n, ["lint"], "held:review-defects"), (n, "api", 11 + 2 * n, [], "merged")]
    facts = skills.lifecycle_facts([ours, theirs], _store(tmp_path, runs))
    assert facts["ambiguous_records"] == 3  # Every record, not every distinct name.
    entry = {item["id"]: item for item in facts["skills"]}["anthropic:lint"]
    assert entry["comparison"]["without"]["issues"] == 0
    assert entry["comparison"]["unconfirmed_issues"] == [4, 5, 6]
    assert entry["evidence"] == "insufficient"


def test_use_before_the_trial_leaves_a_later_empty_execution_unconfirmed(tmp_path):
    skill = _skill(tmp_path, "tdd", TRUSTED)
    store = _store(tmp_path, [(1, "api", 0, ["tdd"], "held:review-defects"), (1, "api", 30, [], "merged"),
                              (2, "api", 31, ["tdd"], "merged"), (3, "api", 32, [], "merged")])
    skills.record_lifecycle(store.root, skill.id, "trial", "repeated test findings", now=lambda: T0 + timedelta(hours=24))
    entry = skills.lifecycle_facts([skill], store)["skills"][0]
    assert entry["used_in"] == [2]
    assert entry["comparison"]["unconfirmed_issues"] == [1]  # It used the skill, only before the trial.
    assert entry["comparison"]["without"]["issues"] == 1
