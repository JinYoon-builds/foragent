"""Log retention: the tick is the only deleter of <state>/logs/ files and old journal lines.

Runners own the log files of their active runs. Before deleting an issue's
files the tick takes that issue's worktree lock without waiting and re-reads
that the issue has no active run; every path that creates a run record holds
the same lock (see ``start_run``), so a run cannot start in between.
"""

from datetime import datetime, timedelta
from pathlib import Path
import re

from foragent.state_locks import _file_lock

# Days a finished run's logs stay, by its newest run's outcome; any non-merged outcome keeps 30.
# Merged runs keep `fa settings` log_retention_days (#160), 7 by default.
MERGED_RETENTION_DAYS = 7
OTHER_RETENTION = timedelta(days=30)
JOURNAL_RETENTION = timedelta(days=30)
JOURNALS = ("orch-events.jsonl", "gate-events.jsonl")
# issue-N.log and its companions issue-N.<anything> and issue-N-<anything>.
_ISSUE_FILE = re.compile(r"issue-(\d+)[.-]")
# Flow observations: two whole generations, so every retained file set stays finite
# (2,048 lines and 7 MiB) and rotation never rewrites an accepted line.
FLOW_FILE, FLOW_OLD_FILE, FLOW_LOCK = "flow-records.jsonl", "flow-records.1.jsonl", "flow.lock"
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


class FlowRecordsMixin:
    """The local, append-only observation log. Its leaf lock takes no other lock."""

    def record_flow(self, kind: str, *, issue: int | None = None, run_started_at: str | None = None,
                    **data: object) -> None:
        """Append one observation at the store's UTC clock; a failure never reaches the caller.

        Callable while holding the state lock. The time is read inside the lock, so file
        order is time order. An oversized record keeps only its size, its content unknown.
        """
        try:
            with _file_lock(self.root / FLOW_LOCK):
                record = {"at": self._now().astimezone(_state.UTC).isoformat(), "kind": kind, "issue": issue,
                          "run_started_at": run_started_at, "data": data}
                line = _state.json.dumps(record, default=_flow_value).encode() + b"\n"
                if len(line) > FLOW_RECORD_BYTES:
                    record["data"] = {"oversized_bytes": len(line)}
                    line = _state.json.dumps(record, default=_flow_value).encode() + b"\n"
                if len(line) <= FLOW_RECORD_BYTES:
                    self._append_flow_line(line)
        except Exception:
            return

    def _append_flow_line(self, line: bytes) -> None:
        path = self.root / FLOW_FILE
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            current = b""
        torn = bool(current) and not current.endswith(b"\n")  # An earlier write ended early.
        lines = current.count(b"\n") + torn
        if lines >= FLOW_GENERATION_LINES or len(current) + torn + len(line) > FLOW_GENERATION_BYTES:
            _state.os.replace(path, self.root / FLOW_OLD_FILE)
            torn = False
        with path.open("ab") as stream:
            stream.write(b"\n" + line if torn else line)

    def flow_records(self, *, since: datetime | None = None, until: datetime | None = None) -> list[dict]:
        """Retained observations in ``[since, until)``, oldest first; unreadable lines are unknown.

        Reading creates and changes nothing, and takes only a shared flow lock.
        """
        try:
            with (self.root / FLOW_LOCK).open("rb") as stream:
                _state.fcntl.flock(stream.fileno(), _state.fcntl.LOCK_SH)
                raw = b"".join(self._flow_generation(name) for name in (FLOW_OLD_FILE, FLOW_FILE))
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

    def _flow_generation(self, name: str) -> bytes:
        try:
            content = (self.root / name).read_bytes()
        except FileNotFoundError:
            return b""
        return content if content.endswith(b"\n") else content + b"\n"


class LogRetentionMixin:
    def trim_journals(self, now: datetime) -> list[Path]:
        """Drop journal lines older than 30 days; return the journals that changed.

        Lines with a missing or unparseable ``at`` stay. The rewrite runs under the
        state lock, which every journal append also takes. The talk events of a run
        still active stay whatever their age: its talk state and paused time are
        derived from them (D95), so trimming one would end a talk without evidence.
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
                kept = [line for line in lines if not self._journal_line_expired(line, cutoff, active)]
                if len(kept) != len(lines):
                    self._atomic_write(path, "".join(kept))
                    trimmed.append(path)
        self.trimmed_journals = trimmed  # The tick's housekeeping reports the trim prune_logs did.
        return trimmed

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

        def expired(run: dict, paths: list[Path]) -> list[Path]:
            keep = merged_keep if run.get("outcome") == "merged" else OTHER_RETENTION
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
        try:
            history = (self.root / "runs.jsonl").read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        newest: dict[int, dict] = {}
        for line in history.splitlines():
            try:
                value = _state.json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and isinstance(value.get("number"), int):
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
                    path.unlink()
                except FileNotFoundError:
                    continue
                removed.append((path, size))
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
