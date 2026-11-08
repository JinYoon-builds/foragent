"""Log retention: the tick is the only deleter of <state>/logs/ files and old journal lines.

Runners own the log files of their active runs. Before deleting an issue's
files the tick takes that issue's worktree lock without waiting and re-reads
that the issue has no active run; every path that creates a run record holds
the same lock (see ``start_run``), so a run cannot start in between.
"""

from datetime import datetime, timedelta
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import logging
import re

from foragent.file_cache import read_json, read_jsonl
from foragent.file_locks import LockTimeout, isolated_timeouts, locked
from foragent.state_locks import _file_lock
from foragent.state_runs import record_merged
from foragent import shelf_io, shelf_retention

# Days a finished run's logs stay, by its newest run's outcome; any non-merged outcome keeps 30.
# Merged runs keep `fa settings` log_retention_days (#160), 7 by default.
MERGED_RETENTION_DAYS = 7
OTHER_RETENTION = timedelta(days=30)
JOURNAL_RETENTION = timedelta(days=30)
JOURNALS = ("orch-events.jsonl", "gate-events.jsonl")
# issue-N.log and its companions issue-N.<anything> and issue-N-<anything>.
_ISSUE_FILE = re.compile(r"issue-(\d+)[.-]")
# Global observations keep two bounded generations. Issue history is moved out of
# their rotation, so unrelated traffic cannot erase an approval-to-merge interval.
FLOW_FILE, FLOW_OLD_FILE, FLOW_LOCK = "flow-records.jsonl", "flow-records.1.jsonl", "flow.lock"
FLOW_RETAINED = "flow-retained"
FLOW_LATEST = "flow-latest"
FLOW_GENERATION_LINES = 1024
FLOW_GENERATION_BYTES = 3584 * 1024
FLOW_RECORD_BYTES = 1024 * 1024


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _flow_value(value: object) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


@contextmanager
def observing(kind: str, **fields):
    """Accounting is optional: even lock/parse failures must leave execution alone.

    A GitHub wait is not an accounting failure: it is the signal that pauses the
    caller (the owner's GitHub pause), so it always reaches the caller.
    """
    with isolated_timeouts():
        try:
            yield
        except Exception as exc:
            from foragent.tracker import TrackerError

            if isinstance(exc, TrackerError) and exc.waits:
                raise
            logging.getLogger(__name__).exception("%s record write failed: %s", kind, fields)


class FlowRecordsMixin:
    """The local, append-only observation log. Its leaf lock takes no other lock."""

    def record_flow(self, kind: str, *, issue: int | None = None, run_started_at: str | None = None,
                    **data: object) -> None:
        """Append one observation at the store's UTC clock; a failure never reaches the caller.

        Callable while holding the state lock. The time is read inside the lock, so file
        order is time order. An oversized record keeps only its size, its content unknown.
        """
        try:
            with isolated_timeouts(), _file_lock(self.root / FLOW_LOCK):
                self._write_flow(kind, issue, run_started_at, data)
        except Exception:
            logging.getLogger(__name__).exception("Flow record write failed: %s issue=%s", kind, issue)

    def record_flow_change(self, kind: str, *, issue: int | None = None, run_started_at: str | None = None,
                           across_runs: bool = False, only_if=None, **data: object) -> None:
        """Append one transition unless the latest same-kind record already says it.

        The latest record is this execution's, or the issue's with ``across_runs``. ``only_if`` sees its
        data (None without one) and can decline. Concurrent callers share the lock, so one change is one
        record. A retained-out record is unknown, and its repeat is recorded again.
        """
        try:
            with isolated_timeouts(), _file_lock(self.root / FLOW_LOCK):
                latest = self._latest_flow(kind, issue, None if across_runs else (run_started_at,))
                if latest is not None and latest["run_started_at"] == run_started_at and latest["data"] == data:
                    return
                if only_if is None or only_if(None if latest is None else latest["data"]):
                    self._write_flow(kind, issue, run_started_at, data)
        except Exception:
            logging.getLogger(__name__).exception("Flow record write failed: %s issue=%s", kind, issue)

    def record_phase(self, phase: str, started_at: str, *, issue: int | None = None,
                     at: datetime | None = None, locked: bool = False, **fields: object) -> None:
        """Journal a factual interval, including waits whose worker has already exited.

        Repeated observations keep the same start; scorecard readers replace that
        interval with its latest end. Recording failure does not stop publication.
        """
        from foragent.orch_events import append_event

        with observing("Phase", issue=issue, phase=phase):
            at = at or self._now()
            append_event(self, at, issue, "phase", phase, locked=locked, phase=phase,
                         phase_started_at=started_at,
                         seconds=max(0, round((at - datetime.fromisoformat(started_at)).total_seconds())), **fields)

    def _write_flow(self, kind: str, issue: int | None, run_started_at: str | None, data: dict) -> None:
        record = {"at": self._now().astimezone(_state.UTC).isoformat(), "kind": kind, "issue": issue,
                  "run_started_at": run_started_at, "data": data}
        line = _state.json.dumps(record, default=_flow_value).encode() + b"\n"
        if len(line) > FLOW_RECORD_BYTES:
            record["data"] = {"oversized_bytes": len(line)}
            line = _state.json.dumps(record, default=_flow_value).encode() + b"\n"
        if len(line) <= FLOW_RECORD_BYTES:
            self._append_flow_line(line)

    def _latest_flow(self, kind: str, issue: int | None, runs: tuple | None) -> dict | None:
        """The newest retained record of ``kind`` for ``issue``, limited to ``runs`` when given."""
        return deepcopy(self._latest_flows(kind, {issue}, runs).get(issue))

    def latest_flow_records(self, kind: str, issues=None) -> dict[int, dict]:
        """Latest facts for these issues; never replay their lifetime history."""
        with isolated_timeouts(), _file_lock(self.root / FLOW_LOCK):
            return deepcopy(self._latest_flows(kind, None if issues is None else set(issues), None))

    def _latest_flows(self, kind, issues, runs):
        # Upgrade pre-index archives once, under the exclusive leaf lock. New
        # rotations publish the index before moving the expiring generation.
        for name in self._retained_flows():
            if not (self.root / name.with_suffix(".range.json")).exists():
                self._index_flow_archive(name, self._flow_generation(name))
        generations = [read_jsonl(self.root / name, tolerant=True, errors="replace") for name in (FLOW_FILE, FLOW_OLD_FILE)]
        if issues is None:
            issues = {int(path.stem) for path in (self.root / FLOW_LATEST).glob("*.json")}
            issues.update(row["issue"] for rows in generations for row in rows if type(row.get("issue")) is int)
        found = {}
        for rows in generations:
            for row in reversed(rows):
                issue = row.get("issue")
                if ((issue is None or type(issue) is int) and issue in issues and issue not in found and row.get("kind") == kind
                        and isinstance(row.get("data"), dict)
                        and (runs is None or row.get("run_started_at") in runs)):
                    found[issue] = row
        for issue in issues - found.keys():
            if type(issue) is not int:
                continue  # Global observations have only the two bounded generations.
            try:
                entries = read_json(self.root / FLOW_LATEST / f"{issue}.json")
            except FileNotFoundError:
                continue
            matching = [entry for entry in entries.values() if entry["row"]["kind"] == kind
                        and (runs is None or entry["row"].get("run_started_at") in runs)]
            if matching:
                found[issue] = max(matching, key=lambda entry: entry["position"])["row"]
        return found

    def _index_flow_archive(self, name: Path, raw: bytes) -> None:
        """Small per-issue latest snapshots and a range, committed before rotation.

        Sequence/line positions make retries and interrupted upgrades idempotent,
        including distinct observations at exactly the same clock time.
        """
        updates, times = {}, []
        for offset, line in enumerate(raw.splitlines()):
            try:
                row = _state.json.loads(line)
            except ValueError:
                continue
            at = _parse_time(row.get("at")) if isinstance(row, dict) else None
            if at is None or not isinstance(row.get("kind"), str) or not isinstance(row.get("data"), dict):
                continue
            times.append(at)
            issue = row.get("issue")
            if type(issue) is int:
                key = _state.json.dumps([row["kind"], row.get("run_started_at")])
                updates.setdefault(issue, {})[key] = {"position": [int(name.stem), offset], "row": row}
        for issue, entries in updates.items():
            path = self.root / FLOW_LATEST / f"{issue}.json"
            try:
                previous = read_json(path)
            except FileNotFoundError:
                previous = {}
            merged = dict(previous)
            for key, entry in entries.items():
                if key not in merged or entry["position"] >= merged[key]["position"]:
                    merged[key] = entry
            self._write_json(path, merged)
        self._write_json(self.root / name.with_suffix(".range.json"),
                         {"first": min(times).isoformat(), "last": max(times).isoformat()} if times else {})

    def _append_flow_line(self, line: bytes) -> None:
        path = self.root / FLOW_FILE
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            current = b""
        torn = bool(current) and not current.endswith(b"\n")  # An earlier write ended early.
        lines = current.count(b"\n") + torn
        if lines >= FLOW_GENERATION_LINES or len(current) + torn + len(line) > FLOW_GENERATION_BYTES:
            self._retain_flow_generation()
            _state.os.replace(path, self.root / FLOW_OLD_FILE)
            torn = False
        with path.open("ab") as stream:
            stream.write(b"\n" + line if torn else line)

    def _retained_flows(self) -> list[Path]:
        return sorted(path.relative_to(self.root) for path in (self.root / FLOW_RETAINED).glob("*.jsonl"))

    def _retain_flow_generation(self) -> None:
        """Move an expiring generation before replacing it; no copy can be read twice.

        Only issue observations need lifetime retention. Renaming first also keeps
        them safe if the process stops before the filtered archive is written.
        Called only at rotation, under the leaf flow lock, never per observation.
        """
        kept = []
        for line in self._flow_generation(FLOW_OLD_FILE).splitlines(keepends=True):
            try:
                row = _state.json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and type(row.get("issue")) is int:
                kept.append(line)
        if not kept:
            return
        archives = self._retained_flows()
        sequence = int(archives[-1].stem) + 1 if archives else 1
        destination = self.root / FLOW_RETAINED / f"{sequence:012d}.jsonl"
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._index_flow_archive(destination.relative_to(self.root), self._flow_generation(FLOW_OLD_FILE))
        _state.os.replace(self.root / FLOW_OLD_FILE, destination)
        self._atomic_write(destination, b"".join(kept).decode("utf-8"))

    def flow_records(self, *, since: datetime | None = None, until: datetime | None = None,
                     include_archives: bool = True) -> list[dict]:
        """Retained observations in ``[since, until)``, oldest first; unreadable lines are unknown.

        Reading takes only a shared flow lock; a lock timeout propagates as unavailable history.
        """
        try:
            with (self.root / FLOW_LOCK).open("rb") as stream:
                with locked(stream.fileno(), self.root / FLOW_LOCK, mode=_state.fcntl.LOCK_SH,
                            journal_root=self.root):
                    raw = b"".join(self._flow_generation(name) for name in (
                        *(self._retained_flows() if include_archives else []), FLOW_OLD_FILE, FLOW_FILE)
                        if self._flow_overlaps(name, since, until))
        except LockTimeout:
            raise
        except OSError:
            return []  # No lock file: nothing was ever recorded here.
        rows = []
        for line in raw.split(b"\n"):
            try:
                value = _state.json.loads(line)
            except ValueError:
                continue
            at = _parse_time(value.get("at")) if isinstance(value, dict) else None
            if at is None or not isinstance(value.get("kind"), str) or not isinstance(value.get("data"), dict):
                continue
            if (since is None or at >= since) and (until is None or at < until):
                rows.append((at, {key: value.get(key) for key in ("at", "kind", "issue", "run_started_at", "data")}))
        rows.sort(key=lambda row: row[0])  # Stable: equal times keep their written order.
        return [row for _, row in rows]

    def _flow_overlaps(self, name, since, until):
        if not isinstance(name, Path) or since is None and until is None:
            return True
        try:
            bounds = read_json(self.root / name.with_suffix(".range.json"))
            first, last = _parse_time(bounds.get("first")), _parse_time(bounds.get("last"))
        except (OSError, ValueError):
            return True  # Older or interrupted archives remain readable.
        return (last is None or since is None or last >= since) and (first is None or until is None or first < until)

    def _flow_generation(self, name: str | Path) -> bytes:
        try:
            content = (self.root / name).read_bytes()
        except FileNotFoundError:
            return b""
        return content if content.endswith(b"\n") else content + b"\n"


class LogRetentionMixin:
    def trim_journals(self, now: datetime) -> list[Path]:
        """Drop expired journal lines and inactive issue flow archives; return changed paths.

        Journal lines with a missing or unparseable ``at`` stay. The rewrite runs under the
        state lock, which every journal append also takes. The talk events of a run
        still active stay whatever their age: its talk state and paused time are
        derived from them (D95), so trimming one would end a talk without evidence.
        The newest marked retrospective note also stays whatever its age: the last
        retrospective time is derived from it. Flow archives keep unfinished
        issues indefinitely; finished histories expire after JOURNAL_RETENTION.
        """
        cutoff, trimmed = now - JOURNAL_RETENTION, []
        with self._state_lock():
            active = {(run.number, run.started_at) for run in self.active_runs()}
            for name in JOURNALS:
                path = self.root / name
                try:
                    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
                except FileNotFoundError:
                    continue
                newest = self._newest_retrospective(lines)
                kept = [line for index, line in enumerate(lines)
                        if index == newest or not self._journal_line_expired(line, cutoff, active)]
                if len(kept) != len(lines):
                    self._atomic_write(path, "".join(kept))
                    trimmed.append(path)
            if self._retained_flows():
                with _file_lock(self.root / FLOW_LOCK):
                    trimmed.extend(self._trim_retained_flows(cutoff))
        self.trimmed_journals = trimmed  # The tick's housekeeping reports the trim prune_logs did.
        return trimmed

    def _trim_retained_flows(self, cutoff: datetime) -> list[Path]:
        """Expire whole archived issue histories under the exclusive flow lock.

        Generations remain bounded by rotation. Their observations, including a
        fresh one written just before this lock, protect the issue's older rows.
        Parse once, then rewrite only changed archives; unreadable rows are not
        observations, just as in flow_records().
        """
        archived = {name: [] for name in self._retained_flows()}
        # Pending outbox writes are intentions, not completion evidence. Use
        # accepted local observations rather than the projected board view.
        local = {row["number"]: row for row in self._issue_record()["issues"].values()}
        active = {run.number for run in self.active_runs()}
        finished = {}
        for number, row in local.items():
            if row.get("state") == "closed" or row.get("merged"):
                at = (_parse_time(row.get("closed_at")) or _parse_time(row.get("merged_at"))
                      or _parse_time(row.get("observed_at")))
                if at is not None:
                    finished[number] = at
        for (number, _), fact in self.owner_merge_facts().items():
            if (at := _parse_time(fact.get("merged_at"))) is not None:
                finished[number] = max(finished.get(number, at), at)
        for number, run in self._newest_finished_runs().items():
            if run.get("outcome") == "merged" and (at := _parse_time(run.get("finished_at"))) is not None:
                finished[number] = max(finished.get(number, at), at)

        def unfinished(issue):
            row = local.get(issue, {})
            # A currently open, unmerged observation can supersede an old merge
            # (a reopened issue). Missing local state alone never proves completion.
            reopened = (row.get("state") == "open" and not row.get("merged")
                        and (observed := _parse_time(row.get("observed_at"))) is not None
                        and observed > finished.get(issue, observed))
            return issue in active or reopened or (row.get("state") != "closed"
                    and not row.get("merged") and issue not in finished)
        latest = dict(finished)
        originals = {}
        for name in (*archived, FLOW_OLD_FILE, FLOW_FILE):
            raw = self._flow_generation(name)
            if name in archived:
                originals[name] = raw
            for line in raw.splitlines(keepends=True):
                try:
                    row = _state.json.loads(line)
                except ValueError:
                    continue
                at = _parse_time(row.get("at")) if isinstance(row, dict) else None
                if at is None or not isinstance(row.get("kind"), str) or not isinstance(row.get("data"), dict):
                    continue
                issue = row.get("issue")
                if type(issue) is int:
                    latest[issue] = max(latest.get(issue, at), at)
                if name in archived:
                    archived[name].append((line, issue, at))
        trimmed = []
        for name, rows in archived.items():
            kept = b"".join(line for line, issue, at in rows
                            if (type(issue) is int and unfinished(issue))
                            or (latest[issue] if type(issue) is int else at) >= cutoff)
            path = self.root / name
            if not kept:
                shelf_io.remove(path)
                shelf_io.remove(path.with_suffix(".range.json"))
                trimmed.append(path)
            elif kept != originals[name]:
                self._atomic_write(path, kept.decode("utf-8"))
                trimmed.append(path)
        for issue, at in latest.items():
            if not unfinished(issue) and at < cutoff:
                shelf_io.remove(self.root / FLOW_LATEST / f"{issue}.json")
        return trimmed

    @staticmethod
    def _newest_retrospective(lines: list[str]) -> int | None:
        """The index of the newest ``note`` line marked ``retrospective``, or None."""
        newest, newest_at = None, None
        for index, line in enumerate(lines):
            try:
                value = _state.json.loads(line)
            except ValueError:
                continue
            if not (isinstance(value, dict) and value.get("kind") == "note" and value.get("retrospective") is True):
                continue
            at = _parse_time(value.get("at"))
            if at is not None and (newest_at is None or at >= newest_at):
                newest, newest_at = index, at
        return newest

    @staticmethod
    def _journal_line_expired(line: str, cutoff: datetime, active: set = frozenset()) -> bool:
        try:
            value = _state.json.loads(line)
        except ValueError:
            return False
        if not isinstance(value, dict):
            return False
        if value.get("kind") in ("talk-opened", "talk-closed") and (
                value.get("issue"), value.get("run_started_at")) in active:
            return False  # An active run's talk is read from these lines.
        at = _parse_time(value.get("at"))
        return at is not None and at < cutoff

    def prune_logs(self, now: datetime) -> list[Path]:
        """Prune logs, their finished issue snapshots, and expired journal lines."""
        deleted = self._prune_logs(now)
        shelf_retention.prune_records(self, now)
        return deleted

    def _prune_logs(self, now: datetime) -> list[Path]:
        """Delete expired issue logs, then the oldest above the size cap; return every deleted path.

        An issue's files form one group, kept or deleted together. Retention
        follows its newest finished run in runs.jsonl: merged keeps
        ``log_retention_days`` (7 by default), any other outcome 30. The group's
        age is that run's ``finished_at``, else its newest file mtime (its last
        activity), so a group still being written is never the oldest. Issues
        with an active run are never touched; issues without a finished run are
        kept by age but may go by size. Files under logs/ that are not issue
        files count toward the cap only.
        """
        self.trim_journals(now)
        logs = self.root / "logs"
        if not logs.is_dir():
            return []
        groups: dict[int, list[Path]] = {}
        for path in sorted(logs.iterdir()):
            if path.is_file() and (match := _ISSUE_FILE.match(path.name)):
                groups.setdefault(int(match[1]), []).append(path)
        newest = self._newest_finished_runs()
        merged_keep = timedelta(days=self._merged_retention_days())
        owner_facts = self.owner_merge_facts()

        def expired(run: dict, paths: list[Path]) -> list[Path]:
            keep = merged_keep if record_merged(run, owner_facts) else OTHER_RETENTION
            return paths if paths and now - self._group_age(run, paths) > keep else []

        deleted: list[Path] = []
        for number in sorted(groups):
            if (run := newest.get(number)) is not None:
                deleted += [path for path, _ in self._delete_issue_files(number, run, expired)]
        limit = _state.LOG_LIMIT_BYTES
        total = self.logs_total()
        if total <= limit:
            return deleted

        def age(number: int) -> tuple[datetime, int]:
            return self._group_age(newest.get(number), [path for path in groups[number] if path.exists()]), number

        for number in sorted((n for n in groups if any(path.exists() for path in groups[n])), key=age):
            if total <= limit:
                break
            removed = self._delete_issue_files(number, newest.get(number), lambda fresh, paths: paths)
            total -= sum(size for _, size in removed)
            deleted += [path for path, _ in removed]
        return deleted

    @classmethod
    def _group_age(cls, run: dict | None, paths: list[Path]) -> datetime:
        """The run's finished_at, else the group's newest file mtime (its last activity)."""
        return _parse_time((run or {}).get("finished_at")) or max(cls._mtime(path) for path in paths)

    @staticmethod
    def _merged_retention_days() -> int:
        from foragent import settings

        return settings.read_machine().get("log_retention_days", MERGED_RETENTION_DAYS)

    def logs_total(self) -> int:
        """Bytes of every file under <state>/logs/."""
        logs = self.root / "logs"
        return sum(path.stat().st_size for path in logs.rglob("*") if path.is_file()) if logs.is_dir() else 0

    def _newest_finished_runs(self) -> dict[int, dict]:
        """Each issue's newest finished run from runs.jsonl (appended in finish order)."""
        newest: dict[int, dict] = {}
        for value in read_jsonl(self.root / "runs.jsonl", tolerant=True):
            if isinstance(value.get("number"), int):
                newest[value["number"]] = value
        return newest

    def _delete_issue_files(self, number: int, seen: dict | None, choose) -> list[tuple[Path, int]]:
        """Delete the issue's files ``choose(run, paths)`` picks; return (path, size) of each.

        Every fact is read again under the issue's worktree lock: no run record
        can be created while it is held and only a running record can finish,
        so the issue's history, files and sizes cannot change meanwhile. If its
        newest finished run is no longer ``seen`` (a run finished since the
        tick decided), the issue is skipped and the next tick decides again.
        """
        with self.try_worktree_lock(number) as acquired:
            if not acquired:
                return []  # A run may be starting; the next tick decides.
            run = self.get_run(number)
            if (run is not None and run.status == "running") or self.run_start_pending(number):
                return []
            fresh = self._newest_finished_runs().get(number)
            if fresh != seen:
                return []
            removed = []
            for path in choose(fresh, self._issue_files(number)):
                try:
                    size = path.stat().st_size
                    shelf_io.remove(path)
                except FileNotFoundError:
                    continue
                removed.append((path, size))
            if removed:
                shelf_retention.remove_with_logs(self, number, fresh)
            return removed

    def _issue_files(self, number: int) -> list[Path]:
        logs = self.root / "logs"
        return [path for path in sorted(logs.iterdir()) if path.is_file()
                and (match := _ISSUE_FILE.match(path.name)) and int(match[1]) == number]

    @staticmethod
    def _mtime(path: Path) -> datetime:
        return _state.datetime.fromtimestamp(path.stat().st_mtime, _state.UTC)


# Bound last: foragent.state imports this module, and lookups happen at call time
# so names patched on the facade reach this implementation.
import foragent.state as _state
