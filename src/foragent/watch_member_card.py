"""A lane squirrel's member card: the facts behind one lane's agent, read from local records.

``facts`` runs once when a squirrel is clicked (watch_data.collect_details) and stores nothing.
The configuration, as the runner resolves it (run_implementation.lane_config, the lane's skills
and the workers' default skills), gives the paths, executor, model and fixed skills. The run
history gives the skills recent runs read. The run history, read through the store's own merge
rule, and the review journal give today's merges and review rejections, counted from local
midnight. The collected status gives the issue the lane works on now, and the lane memory
directory the runner uses gives the newest lesson. Every value that cannot be read stays None,
so ``rows`` keeps the same rows for every lane. Rows built from a record carry that record's
time, which ``rows`` shows as an age (D67). No local record attributes reverts to a lane, so
the card says they are not recorded.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import Any

from foragent.config import Config
from foragent.watch_cells import Line

RECENT_RUNS = 5  # Runs whose recorded skill reads make "recently used".
MEMORY_LIMIT = 256 * 1024  # A lane memory file larger than this is not read.
FILES = ("MEMORY.new.md", "MEMORY.consumed.ids", "MEMORY.md")


def _time(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.utcoffset() is not None else None


def _texts(values: Any) -> list[str]:
    return [str(value) for value in values if str(value).strip()] if isinstance(values, (list, tuple)) else []


def _ended(run: Any, finished: dict) -> datetime | None:
    return (_time(finished.get((run.number, run.started_at))) or _time(run.last_progress_at)
            or _time(run.started_at))


def _used_skills(slug: str, history: list | None, finished: dict) -> tuple[list[str] | None, str | None]:
    """Skills the lane's newest recorded runs read, newest run first, and that run's end."""
    if history is None:
        return None, None
    runs = [run for run in history if run.lane == slug and run.skills_used]

    def newest_first(run: Any) -> float:
        ended = _ended(run, finished)
        return ended.timestamp() if ended else float("-inf")

    runs.sort(key=newest_first, reverse=True)
    names = list(dict.fromkeys(name for run in runs[:RECENT_RUNS] for name in _texts(run.skills_used)))
    newest = _ended(runs[0], finished) if runs else None
    return names, newest.isoformat() if newest else None


def _today(slug: str, status: dict, config: Config, now: datetime, events: list | None, rows: dict,
           history: list | None, finished: dict, owner: Any, merged_run: Any) -> dict[str, Any]:
    """Today's merges and review rejections from local midnight through ``now``.

    A run counts as merged by the store's own rule (``merged_run``: a merged outcome or a
    recorded merge of that execution, such as a merge-owner merge left ``merge-queued``, and
    never a disowned retry); its time is the recorded merge, else the run's end.
    """
    from foragent.labels import issue_lane
    from foragent.watch_lane_facts import _reviews

    midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    newest: list[datetime] = []
    lanes_at, lane_of = {}, {}
    for run in history or []:
        if run.lane:
            lanes_at[(run.number, _time(run.started_at))] = run.lane
            lane_of[run.number] = run.lane
    for run in status.get("active_runs", []):
        if run.get("lane"):
            lanes_at[(run["number"], _time(run.get("started_at")))] = run["lane"]
            lane_of[run["number"]] = run["lane"]

    def lane(number: int, start: datetime | None = None) -> str | None:
        return (lanes_at.get((number, start)) or lane_of.get(number)
                or issue_lane(rows.get(number, {}).get("labels", ()), config.lanes))

    merges = None
    if history is not None:
        merges, counted = 0, set()
        for run in history:
            if run.lane != slug or run.number in counted or not merged_run(run):
                continue
            fact = owner.get((run.number, run.started_at)) if hasattr(owner, "get") else None
            end = _time(fact.get("merged_at") if isinstance(fact, dict) else None) or _ended(run, finished)
            if end is None or not midnight <= end <= now:
                continue
            counted.add(run.number)
            merges += 1
            newest.append(end)
    rejections = None
    if events is not None:
        rejections = 0
        for number, verdicts in _reviews(events).items():
            for at, _round, verdict in verdicts:
                if verdict == "FAIL" and midnight <= at <= now and lane(number) == slug:
                    rejections += 1
                    newest.append(at)
    return {"merges": merges, "rejections": rejections,
            "today_at": max(newest).isoformat() if newest else None}


def _current(slug: str, status: dict) -> dict[str, Any] | None:
    """The lane's live run (the newest start first), as the view's working facts list it."""
    runs = [run for run in status.get("active_runs", [])
            if run.get("lane") == slug and _time(run.get("started_at")) is not None]
    if not runs:
        return None
    run = max(runs, key=lambda run: _time(run["started_at"]))
    return {"number": run["number"], "started_at": run["started_at"], "phase": run.get("phase")}


def _inside(path: Path, root: Path) -> bool:
    """A real file or folder under ``root`` reached without any link."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _lesson_line(text: str) -> str | None:
    """The newest lesson as written: the last nonempty line that is not a heading.

    A pending line's ``[id:...]`` tag is the buffer's bookkeeping, not the lesson, so it is left out.
    """
    for line in reversed(text.splitlines()):
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        return re.sub(r"\[id:[^\]]*\]\s*", "", value, count=1)
    return None


def memory(root: Path | None, config: Config, slug: str) -> dict[str, Any]:
    """The lane memory folder the runner uses and its newest lesson.

    ``state`` is "ok", "missing" or "unreadable": a folder or file reached through a link,
    outside the repository, too large or undecodable is never read.
    """
    from foragent import lane_memory

    folder = lane_memory.lane_directory(config, slug)
    found: dict[str, Any] = {"folder": folder, "state": "unreadable", "lesson": None, "lesson_at": None}
    if root is None:
        return found
    path = root / folder
    if not path.exists() and not path.is_symlink():
        found["state"] = "missing"
        return found
    if not _inside(path, root) or not path.is_dir():
        return found
    texts: dict[str, tuple[str, float]] = {}
    for name in FILES:
        file = path / name
        if not file.exists() and not file.is_symlink():
            continue
        try:
            if not _inside(file, root) or not file.is_file() or file.stat().st_size > MEMORY_LIMIT:
                return found
            texts[name] = (file.read_text(encoding="utf-8"), file.stat().st_mtime)
        except (OSError, UnicodeDecodeError):
            return found
    found["state"] = "ok"
    pending = lane_memory.pending_view(texts.get("MEMORY.new.md", ("", 0))[0] or None,
                                       texts.get("MEMORY.consumed.ids", ("", 0))[0] or None)
    for name, text in (("MEMORY.new.md", pending), ("MEMORY.md", texts.get("MEMORY.md", ("", 0))[0])):
        lesson = _lesson_line(text) if text else None
        if lesson:
            found["lesson"] = lesson
            found["lesson_at"] = datetime.fromtimestamp(texts[name][1]).astimezone().isoformat()
            break
    return found


def facts(slug: str, status: dict, store: Any, config: Config, now: datetime, root: Path | None,
          events: list | None, rows: dict, history: list | None, records: Any = None) -> dict[str, Any]:
    """Every member-card fact of one lane; a value that cannot be read is None.

    ``records`` is ``Records(store)`` shared by several lanes, so the run end and merge
    records are read once per collection.
    """
    records = Records(store) if records is None else records
    used, used_at = _used_skills(slug, history, records.finished)
    return {"used_skills": used, "used_at": used_at,
            **_today(slug, status, config, now, events, rows, history, records.finished, records.owner,
                     records.merged),
            "current": _current(slug, status), "memory": memory(root, config, slug)}


class Records:
    """The store's run end times and merge facts, read once; unreadable records read as none."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.finished, self.owner = self._read(store, "finish_times"), self._read(store, "owner_merge_facts")

    @staticmethod
    def _read(store: Any, name: str) -> dict:
        """A store reader's records; a missing or failing reader reads as none."""
        try:
            return getattr(store, name)()
        except (OSError, ValueError, AttributeError):
            return {}

    def merged(self, run: Any) -> bool:
        """The store's own merge rule for one execution."""
        try:
            return self.store.run_merged(run, owner_facts=self.owner)
        except (OSError, ValueError, AttributeError):
            return run.outcome == "merged"


def _age(stamp: str | None, now: datetime, language: str) -> str | None:
    from foragent.watch_sections.lane_explainers import _age as age, _age_parts

    parts = _age_parts(stamp, now)
    return age(parts, language) if parts is not None else None


def _with_age(value: str, stamp: str | None, now: datetime, language: str) -> str:
    from foragent import watch

    age = _age(stamp, now, language)
    return value if age is None else watch.text("member_card_aged", language, value=value, age=age)


def _count(key: str, count: int | None, language: str) -> str:
    from foragent import watch

    if count is None:
        return watch.text(f"member_card_{key}_unknown", language)
    return watch.text(f"member_card_{key}{'_one' if count == 1 else ''}", language, count=count)


def rows(slug: str, card: dict | None, paths: list[str], config: Config, now: datetime) -> list[Line]:
    """The card's rows, the same rows for every lane: owned paths, executor and model, fixed
    skills, recently used skills, today's counts, the current issue, the lesson and the folder."""
    from foragent import lane_memory, watch

    language = config.language
    card = card if isinstance(card, dict) else {}

    def t(key: str, **values: Any) -> str:
        return watch.text(key, language, **values)

    # What a run on this lane actually gets: the runner's own lane setup, global values inherited.
    from foragent import run_implementation, run_lane_skills, session_skills
    from foragent.skills import entry_directory

    effective = run_implementation.lane_config(config, slug)
    executor = effective.executor if isinstance(effective.executor, str) and effective.executor.strip() else None
    model = (effective.executor_model if isinstance(effective.executor_model, str)
             and effective.executor_model.strip() else None)
    fixed = list(dict.fromkeys([*_texts(run_lane_skills.lane_skill_names(config, slug, Path("."), None)),
                                *(entry_directory(entry) for entry in session_skills.role_entries(config, "workers"))]))
    used = card.get("used_skills")
    found = []
    found.append(t("lane_explainers_owns", paths=", ".join(paths)) if paths else t("lane_explainers_no_paths"))
    found.append(t("member_card_runs", executor=executor or t("member_card_unknown"),
                   model=model or t("member_card_no_model")))
    found.append(t("member_card_fixed", skills=", ".join(fixed) if fixed else t("member_card_none")))
    if used is None:
        found.append(t("member_card_used", skills=t("member_card_unknown")))
    elif not used:
        found.append(t("member_card_used", skills=t("member_card_none_recorded")))
    else:
        found.append(_with_age(t("member_card_used", skills=", ".join(used)), card.get("used_at"), now, language))
    today = t("member_card_today", merges=_count("merges", card.get("merges"), language),
              rejections=_count("rejections", card.get("rejections"), language),
              reverts=t("member_card_reverts_unknown"))
    found.append(_with_age(today, card.get("today_at"), now, language))
    current = card.get("current")
    started = _time(current.get("started_at")) if isinstance(current, dict) else None
    if started is None:
        found.append(t("member_card_no_current"))
    else:
        minutes = max(0, int((now - started).total_seconds()) // 60)
        found.append(t("member_card_current", number=current["number"], minutes=minutes))
    remembered = card.get("memory") if isinstance(card.get("memory"), dict) else {}
    folder = remembered.get("folder") or lane_memory.lane_directory(config, slug)
    state = remembered.get("state")
    lesson = remembered.get("lesson")
    if lesson:
        found.append(_with_age(t("member_card_lesson", lesson=lesson), remembered.get("lesson_at"), now, language))
    elif state == "ok":
        found.append(t("member_card_lesson", lesson=t("member_card_none_recorded")))
    else:
        found.append(t("member_card_lesson", lesson=t("member_card_unknown")))
    folder_key = {"ok": "member_card_folder", "missing": "member_card_folder_missing"}.get(
        state, "member_card_folder_unreadable")
    found.append(t(folder_key, folder=folder))
    return [[(value, "")] for value in found]
