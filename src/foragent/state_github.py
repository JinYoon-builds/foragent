"""Local GitHub records: per-UTC-hour gh call counts (30 days) and the last remaining-request reading."""

from datetime import datetime
from typing import Any


class GithubCallsMixin:
    def count_github_call(self, at: datetime) -> None:
        """Add one gh attempt to its UTC hour; keep only the last 30 days of hours."""
        hour = at.astimezone(_state.UTC).strftime("%Y-%m-%dT%H")
        oldest = (self._now() - _state.timedelta(days=30)).astimezone(_state.UTC).strftime("%Y-%m-%dT%H")
        with self._state_lock():
            counts = self._github_call_counts()
            counts[hour] = counts.get(hour, 0) + 1
            self._write_json(self.root / "github-calls.json",
                             {key: value for key, value in sorted(counts.items()) if key >= oldest})

    def github_calls(self, since: datetime | None = None) -> list[dict[str, Any]]:
        """Return ``{"hour", "calls"}`` rows by UTC hour, ascending; ``since`` filters by hour start."""
        rows = []
        for key, calls in sorted(self._github_call_counts().items()):
            hour = _state.datetime.strptime(key, "%Y-%m-%dT%H").replace(tzinfo=_state.UTC)
            if since is None or hour >= since:
                rows.append({"hour": hour.isoformat(), "calls": calls})
        return rows

    def record_github_remaining(self, remaining: int, seen_at: datetime) -> None:
        """Replace the last known count of core GitHub requests left, with when it was read."""
        self._write_json(self.root / "github-rate-limit.json",
                         {"remaining": remaining, "seen_at": seen_at.isoformat()})

    def _github_call_counts(self) -> dict[str, int]:
        try:
            value = _state.json.loads((self.root / "github-calls.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        return {str(key): int(calls) for key, calls in value.items()}


import foragent.state as _state  # noqa: E402
