"""The member card reads lane memory only inside the repository (#2822)."""

from foragent import watch_member_card
from foragent.config import Config


def _config():
    return Config(lanes={"api": {"paths": ["src/**"], "agent": "fa-api"}})


def _folder(root):
    folder = root / ".claude/agent-memory/fa-api"
    folder.mkdir(parents=True)
    return folder


def test_pending_lesson_is_newer_than_the_curated_one(tmp_path):
    folder = _folder(tmp_path)
    (folder / "MEMORY.md").write_text("# Memory\n\n- curated lesson\n", encoding="utf-8")
    (folder / "MEMORY.new.md").write_text("- [id:a] consumed lesson\n- [id:b] pending lesson\n", encoding="utf-8")
    (folder / "MEMORY.consumed.ids").write_text("a\n", encoding="utf-8")
    found = watch_member_card.memory(tmp_path, _config(), "api")
    assert (found["state"], found["lesson"]) == ("ok", "- pending lesson")
    (folder / "MEMORY.consumed.ids").write_text("a\nb\n", encoding="utf-8")
    assert watch_member_card.memory(tmp_path, _config(), "api")["lesson"] == "- curated lesson"


def test_oversized_or_linked_memory_file_reads_unknown(tmp_path):
    folder = _folder(tmp_path)
    (folder / "MEMORY.md").write_text("- x\n" * (watch_member_card.MEMORY_LIMIT // 4 + 1), encoding="utf-8")
    found = watch_member_card.memory(tmp_path, _config(), "api")
    assert (found["state"], found["lesson"]) == ("unreadable", None)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("- outside lesson\n", encoding="utf-8")
    (folder / "MEMORY.md").unlink()
    (folder / "MEMORY.md").symlink_to(outside)
    found = watch_member_card.memory(tmp_path, _config(), "api")
    assert (found["state"], found["lesson"]) == ("unreadable", None)


def test_missing_folder_keeps_the_runner_folder_name(tmp_path):
    found = watch_member_card.memory(tmp_path, _config(), "api")
    assert found == {"folder": ".claude/agent-memory/fa-api", "state": "missing", "lesson": None, "lesson_at": None}
    rows = watch_member_card.rows("api", {"memory": found}, ["src/**"], _config(), __import__("datetime").datetime.now().astimezone())
    assert len(rows) == 8 and rows[-1][0][0] == "Memory folder: .claude/agent-memory/fa-api (not found)"


# ---------------------------------------------------------------- review classes (#2822 round 2)

from datetime import datetime as _dt, timedelta as _td, timezone as _tz  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import foragent.state  # noqa: E402,F401  (state_runs needs the state package loaded first)
from foragent.state_runs import MergeFacts, record_merged  # noqa: E402

NOW = _dt(2026, 10, 20, 12, tzinfo=_tz.utc)


def _run(number, outcome, started, lane="api"):
    return SimpleNamespace(number=number, outcome=outcome, started_at=started.isoformat(), lane=lane,
                           last_progress_at=None, skills_used=[])


class _Store:
    """The store's merge rule (record_merged) over a fixed history and owner facts."""

    def __init__(self, owner):
        self.owner = owner

    def finish_times(self):
        return {}

    def owner_merge_facts(self):
        return self.owner

    def run_merged(self, run, *, owner_facts=None):
        return record_merged({"number": run.number, "started_at": run.started_at, "outcome": run.outcome},
                             self.owner if owner_facts is None else owner_facts)


def _today_merges(history, owner):
    found = watch_member_card.facts("api", {"active_runs": []}, _Store(owner), _config(), NOW, None,
                                    [], {}, history)
    return found["merges"]


def test_merge_owner_merge_counts_and_a_disowned_retry_does_not():
    start = NOW - _td(hours=2)
    owner_run, retry, plain = _run(21, "merge-queued", start), _run(22, "merged", start), _run(23, "merged", start)
    owner = MergeFacts({(21, owner_run.started_at): {"merged_at": (NOW - _td(hours=1)).isoformat()}})
    owner.disowned = frozenset({(22, retry.started_at)})
    plain.last_progress_at = (NOW - _td(minutes=30)).isoformat()
    retry.last_progress_at = plain.last_progress_at
    assert _today_merges([owner_run], owner) == 1, "a merge-owner merge left merge-queued counts"
    assert _today_merges([retry], owner) == 0, "a disowned retry is not a merge"
    assert _today_merges([plain], owner) == 1
    again = _run(23, "merged", start - _td(minutes=5))
    again.last_progress_at = plain.last_progress_at
    assert _today_merges([plain, again, owner_run, retry], owner) == 2, "each issue counts once"
    # A merge-owner merge recorded yesterday stays out of today's count.
    owner[(21, owner_run.started_at)] = {"merged_at": (NOW - _td(days=1)).isoformat()}
    assert _today_merges([owner_run], owner) == 0


def test_card_shows_the_executor_model_and_skills_a_run_on_the_lane_inherits():
    config = Config(executor="claude", executor_model="opus-global", lanes={"api": {"paths": ["src/**"]}},
                    default_skills={"workers": ["skill-for-workers"], "reviewers": ["skill-for-reviewers"]})
    found = watch_member_card.rows("api", {}, ["src/**"], config, NOW)
    text = [row[0][0] for row in found]
    assert text[1] == "Runs on Claude · opus-global"
    assert text[2] == "Fixed skills: skill-for-workers"
    pinned = Config(executor="claude", executor_model="opus-global",
                    lanes={"api": {"executor": "codex", "model": "gpt-lane", "skills": ["lane-skill"]}})
    assert [row[0][0] for row in watch_member_card.rows("api", {}, [], pinned, NOW)][1:3] == [
        "Runs on Codex · gpt-lane", "Fixed skills: lane-skill"]


# ---------------------------------------------------------------- one producer for both paths (#2822 round 3)


def test_collected_lane_facts_carry_the_card_so_a_click_shows_the_newest_lesson(tmp_path):
    """watch.collect's lane facts (no click-time detail read) already hold the card and its lesson."""
    from foragent import watch_lane_facts
    from foragent.watch_data import Snapshot
    from foragent.watch_sections import lane_explainers

    folder = _folder(tmp_path)
    (folder / "MEMORY.md").write_text("# Memory\nOld lesson\n\n- api lesson\n", encoding="utf-8")
    store = SimpleNamespace(read_history=list)  # No run end or merge readers: they read as none.
    facts = watch_lane_facts.lane_facts({"lanes": {}, "active_runs": []}, store, _config(), NOW, tmp_path,
                                        [], [], {}, [])
    assert facts["api"]["memory_latest"] == "- api lesson"
    assert facts["api"]["card"]["memory"]["lesson"] == "- api lesson"
    snapshot = Snapshot(status={"lanes": {}, "active_runs": []}, done={}, order=None, completed=0, total=0,
                        eta=None, events=[], captured_at=NOW, lane_facts=facts)
    rows = ["".join(value for value, _ in row) for row in lane_explainers.explain(
        ("squirrel", "api"), snapshot, _config(), tmp_path, now=NOW, width=200, state=None)]
    assert any(row.startswith("Latest lesson: - api lesson") for row in rows), rows
    assert "Old lesson" not in "\n".join(rows)
    assert rows[-1] == "Memory folder: .claude/agent-memory/fa-api"
