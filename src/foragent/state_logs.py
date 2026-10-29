"""Log retention: the tick is the only deleter of <state>/logs/ files and old journal lines.

Runners own the log files of their active runs. Before deleting an issue's
files the tick takes that issue's worktree lock without waiting and re-reads
that the issue has no active run; every path that creates a run record holds
the same lock (see ``start_run``), so a run cannot start in between.
"""

from datetime import datetime, timedelta
from pathlib import Path
import re

# Days a finished run's logs stay, by its newest run's outcome; any non-merged outcome keeps 30.
MERGED_RETENTION = timedelta(days=7)
OTHER_RETENTION = timedelta(days=30)
JOURNAL_RETENTION = timedelta(days=30)
JOURNALS = ("orch-events.jsonl", "gate-events.jsonl")
# issue-N.log and its companions issue-N.<anything> and issue-N-<anything>.
_ISSUE_FILE = re.compile(r"issue-(\d+)[.-]")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


class LogRetentionMixin:
    def trim_journals(self, now: datetime) -> list[Path]:
        """Drop journal lines older than 30 days; return the journals that changed.

        Lines with a missing or unparseable ``at`` stay. The rewrite runs under the
        state lock, which every journal append also takes.
        """
        cutoff, trimmed = now - JOURNAL_RETENTION, []
        with self._state_lock():
            for name in JOURNALS:
                path = self.root / name
                try:
                    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
                except FileNotFoundError:
                    continue
                kept = [line for line in lines if not self._journal_line_expired(line, cutoff)]
                if len(kept) != len(lines):
                    self._atomic_write(path, "".join(kept))
                    trimmed.append(path)
        return trimmed

    @staticmethod
    def _journal_line_expired(line: str, cutoff: datetime) -> bool:
        try:
            value = _state.json.loads(line)
        except ValueError:
            return False
        at = _parse_time(value.get("at")) if isinstance(value, dict) else None
        return at is not None and at < cutoff

    def prune_logs(self, now: datetime) -> list[Path]:
        """Delete expired issue logs, then the oldest above the size cap; return every deleted path.

        Retention follows each issue's newest finished run in runs.jsonl: merged
        keeps 7 days, any other outcome 30, measured from its ``finished_at``
        (else each file's mtime). Issues with an active run are never touched;
        issues without a finished run are kept by age but may go by size.
        Files under logs/ that are not issue files count toward the cap only.
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
        deleted: list[Path] = []
        for number in sorted(groups):
            if (run := newest.get(number)) is not None:
                deleted += [path for path, _ in self._delete_issue_files(
                    number, run, lambda fresh, paths: [path for path in paths if self._expired(fresh, path, now)])]
        limit = _state.LOG_LIMIT_BYTES
        total = self.logs_total()
        if total <= limit:
            return deleted

        def age(number: int) -> tuple[datetime, int]:
            finished = _parse_time((newest.get(number) or {}).get("finished_at"))
            paths = [path for path in groups[number] if path.exists()]
            return finished or max(self._mtime(path) for path in paths), number

        for number in sorted((n for n in groups if any(path.exists() for path in groups[n])), key=age):
            if total <= limit:
                break
            removed = self._delete_issue_files(number, newest.get(number), lambda fresh, paths: paths)
            total -= sum(size for _, size in removed)
            deleted += [path for path, _ in removed]
        return deleted

    @classmethod
    def _expired(cls, run: dict, path: Path, now: datetime) -> bool:
        keep = MERGED_RETENTION if run.get("outcome") == "merged" else OTHER_RETENTION
        return now - (_parse_time(run.get("finished_at")) or cls._mtime(path)) > keep

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
