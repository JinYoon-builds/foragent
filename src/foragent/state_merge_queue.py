"""The durable FIFO merge queue of ready runs and merge-group claims (#459)."""

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

    def _live_entries(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop entries whose run stopped, and claims whose leader stopped."""
        def running(number: int) -> bool:
            run = self.get_run(number)
            return run is not None and run.status == "running"

        live = []
        for entry in entries:
            if not running(entry["number"]):
                continue
            claimer = entry.get("claimed_by")
            if claimer is not None and claimer != entry["number"] and not running(claimer):
                entry = {**entry, "claimed_by": None}
            live.append(entry)
        return live

    def enqueue_merge(self, entry: dict[str, Any]) -> None:
        """Record a ready run, keeping its queue position when it is already queued."""
        with self._state_lock():
            before = self.merge_queue()
            entries = self._live_entries(before)
            for index, current in enumerate(entries):
                if current["number"] == entry["number"]:
                    entries[index] = {**entry, "claimed_by": current.get("claimed_by")}
                    break
            else:
                entries.append({**entry, "claimed_by": None})
            self._save_queue(before, entries)

    def dequeue_merge(self, *numbers: int) -> None:
        with self._state_lock():
            before = self.merge_queue()
            if any(entry["number"] in numbers for entry in before):
                self._save_queue(before, [entry for entry in before if entry["number"] not in numbers])

    def claim_merge_group(self, number: int, base: str, maximum: int) -> list[dict[str, Any]]:
        """Claim this run and the oldest unclaimed peers; return members in queue order.

        An empty list means there is no peer to group with: proceed alone.
        """
        with self._state_lock():
            before = self.merge_queue()
            entries = self._live_entries(before)
            own = next((entry for entry in entries if entry["number"] == number), None)
            peers = [entry for entry in entries if entry["number"] != number and entry.get("base") == base
                     and entry.get("claimed_by") is None][:max(0, maximum - 1)]
            # A run claimed by another leader belongs to that group until it finishes.
            if own is None or own.get("claimed_by") not in (None, number) or not peers:
                self._save_queue(before, entries)
                return []
            chosen = {number, *(entry["number"] for entry in peers)}
            entries = [{**entry, "claimed_by": number} if entry["number"] in chosen else entry
                       for entry in entries]
            self._save_queue(before, entries)
            return [entry for entry in entries if entry["number"] in chosen]

    def release_merge_claims(self, number: int) -> None:
        with self._state_lock():
            before = self.merge_queue()
            entries = [{**entry, "claimed_by": None} if entry.get("claimed_by") == number else entry
                       for entry in before]
            self._save_queue(before, entries)

    def has_unclaimed_merge_peers(self, number: int, base: str) -> bool:
        with self._state_lock():
            entries = self._live_entries(self.merge_queue())
        own = next((entry for entry in entries if entry["number"] == number), None)
        if own is not None and own.get("claimed_by") not in (None, number):
            return False
        return any(entry["number"] != number and entry.get("base") == base and entry.get("claimed_by") is None
                   for entry in entries)

    def group_merged_head(self, number: int) -> str | None:
        """The head another run's merge group merged for this run, if any."""
        try:
            value = _state.json.loads((self.root / "runs" / f"{number}.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None
        return value.get("group_merged_head")

    def record_group_merge(self, number: int, head: str) -> None:
        with self._state_lock():
            path = self.root / "runs" / f"{number}.json"
            value = _state.json.loads(path.read_text(encoding="utf-8"))
            value["group_merged_head"] = head
            self._write_json(path, value)


# Bound last: foragent.state imports this module, and lookups happen at call time.
import foragent.state as _state  # noqa: E402
