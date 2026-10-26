"""The durable FIFO merge queue of ready runs and merge-group claims (#459).

Entries, claims and group-merge markers name one execution of an issue (its
run's started_at), so state left by an earlier execution never applies to a
restarted run of the same issue.
"""

from typing import Any

QUEUE = "merge-queue.json"


class MergeQueueMixin:
    def merge_queue(self) -> list[dict[str, Any]]:
        """Entries in FIFO order; each carries number, head_sha and its publish inputs."""
        try:
            value = _state.json.loads((self.root / QUEUE).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return []
        entries = value.get("entries") if isinstance(value, dict) else None
        return [entry for entry in entries or [] if isinstance(entry, dict) and "number" in entry]

    def _save_queue(self, before: list[dict[str, Any]], entries: list[dict[str, Any]]) -> None:
        if entries != before or not (self.root / QUEUE).exists():
            self._write_json(self.root / QUEUE, {"entries": entries})

    def _current_execution(self, number: int) -> str | None:
        """started_at of the issue's running execution, or None when none runs."""
        run = self.get_run(number)
        return run.started_at if run is not None and run.status == "running" else None

    def _live_entries(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop entries of stopped or earlier executions, and claims of such leaders."""
        live = []
        for entry in entries:
            if self._current_execution(entry["number"]) != entry.get("started_at"):
                continue
            claimer = entry.get("claimed_by")
            if claimer is not None and self._current_execution(claimer) != entry.get("claimed_started_at"):
                entry = {**entry, "claimed_by": None, "claimed_started_at": None}
            live.append(entry)
        return live

    def _live_queue(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """(entries as stored, live entries); callers save the live list under the lock."""
        before = self.merge_queue()
        return before, self._live_entries(before)

    def enqueue_merge(self, entry: dict[str, Any]) -> None:
        """Record a ready run, keeping its queue position when it is already queued."""
        with self._state_lock():
            before, entries = self._live_queue()
            entry = {**entry, "started_at": self._current_execution(entry["number"])}
            for index, current in enumerate(entries):
                if current["number"] == entry["number"]:
                    entries[index] = {**entry, "claimed_by": current.get("claimed_by"),
                                      "claimed_started_at": current.get("claimed_started_at")}
                    break
            else:
                entries.append({**entry, "claimed_by": None, "claimed_started_at": None})
            self._save_queue(before, entries)

    def dequeue_merge(self, *numbers: int) -> None:
        with self._state_lock():
            before, entries = self._live_queue()
            self._save_queue(before, [entry for entry in entries if entry["number"] not in numbers])

    def claim_merge_group(self, number: int, base: str, maximum: int) -> list[dict[str, Any]]:
        """Claim this run and the oldest unclaimed peers; return members in queue order.

        An empty list means there is no peer to group with: proceed alone.
        """
        with self._state_lock():
            before, entries = self._live_queue()
            own = next((entry for entry in entries if entry["number"] == number), None)
            peers = [entry for entry in entries if entry["number"] != number and entry.get("base") == base
                     and entry.get("claimed_by") is None][:max(0, maximum - 1)]
            # A run claimed by another leader belongs to that group until it finishes.
            if own is None or own.get("claimed_by") not in (None, number) or not peers:
                self._save_queue(before, entries)
                return []
            chosen = {number, *(entry["number"] for entry in peers)}
            started = own["started_at"]
            entries = [{**entry, "claimed_by": number, "claimed_started_at": started}
                       if entry["number"] in chosen else entry for entry in entries]
            self._save_queue(before, entries)
            return [entry for entry in entries if entry["number"] in chosen]

    def release_merge_claims(self, number: int) -> None:
        with self._state_lock():
            before, entries = self._live_queue()
            entries = [{**entry, "claimed_by": None, "claimed_started_at": None}
                       if entry.get("claimed_by") == number else entry for entry in entries]
            self._save_queue(before, entries)

    def has_unclaimed_merge_peers(self, number: int, base: str) -> bool:
        with self._state_lock():
            before, entries = self._live_queue()
            self._save_queue(before, entries)  # A reader's cleanup is durable too.
        own = next((entry for entry in entries if entry["number"] == number), None)
        if own is not None and own.get("claimed_by") not in (None, number):
            return False
        return any(entry["number"] != number and entry.get("base") == base and entry.get("claimed_by") is None
                   for entry in entries)

    def group_merged_head(self, number: int) -> str | None:
        """The head another run's merge group merged for this execution, if any."""
        try:
            value = _state.json.loads((self.root / "runs" / f"{number}.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None
        marker = value.get("group_merged")
        if not isinstance(marker, dict) or marker.get("started_at") != value.get("started_at"):
            return None
        return marker.get("head")

    def record_group_merge(self, number: int, head: str, started_at: str | None) -> None:
        with self._state_lock():
            path = self.root / "runs" / f"{number}.json"
            value = _state.json.loads(path.read_text(encoding="utf-8"))
            value["group_merged"] = {"head": head, "started_at": started_at}
            self._write_json(path, value)


# Bound last: foragent.state imports this module, and lookups happen at call time.
import foragent.state as _state  # noqa: E402
