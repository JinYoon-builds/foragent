"""Order progress, approvals, flags, inspect state, plan usage and tidy state."""

from collections.abc import Callable
from typing import Any

from foragent.state_records import Approval, Flag, InspectState


# Enough observations to cover a five-hour window seen every few minutes, per executor.
USAGE_HISTORY_LINES = 1000


class LocalRecordsMixin:
    def order_progress(self) -> dict[str, Any] | None:
        """Read order preparation facts shared by the CLI and lane view."""
        try:
            return _state.json.loads((self.root / "order-progress.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def update_order_progress(
        self, update: Callable[[dict[str, Any] | None], dict[str, Any] | None],
    ) -> dict[str, Any] | None:
        """Apply one local fact under a lock; None ends preparation."""
        with self._state_lock():
            updated = update(self.order_progress())
            path = self.root / "order-progress.json"
            if updated is None:
                path.unlink(missing_ok=True)
            else:
                self._write_json(path, updated)
            return updated

    def save_approval(
        self, number: int, *, body: str, body_sha: str,
        acceptance_blobs: dict[str, str], approved_by: str, approved_at: str,
    ) -> None:
        snapshot = {
            "number": number, "body": body, "body_sha": body_sha, "acceptance_blobs": acceptance_blobs,
            "approved_by": approved_by, "approved_at": approved_at,
        }
        self._write_mapping(self.root / "approvals" / f"{number}.json", _state.Approval, snapshot)

    def load_approval(self, number: int) -> Approval | None:
        path = self.root / "approvals" / f"{number}.json"
        try:
            snapshot = _state.json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        return _state.load_record(_state.Approval, snapshot)

    def clear_approval(self, number: int) -> None:
        (self.root / "approvals" / f"{number}.json").unlink(missing_ok=True)

    def save_flag(self, number: int, reason: str, *, flagged_at: str | None = None) -> None:
        self._write_mapping(self.root / "flags" / f"{number}.json", _state.Flag, {
            "number": number, "reason": reason,
            "flagged_at": flagged_at if flagged_at is not None else self._timestamp(),
        })

    def load_flag(self, number: int) -> Flag | None:
        try:
            value = (self.root / "flags" / f"{number}.json").read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return _state.load_record(_state.Flag, _state.json.loads(value))

    def load_inspect(self) -> InspectState:
        try:
            value = (self.root / "inspect.json").read_text(encoding="utf-8")
        except FileNotFoundError:
            return _state.InspectState()
        return _state.load_record(_state.InspectState, _state.json.loads(value))

    def update_inspect(self, **fields: Any) -> InspectState:
        with self._state_lock():
            updated = _state.replace(self.load_inspect(), **fields)
            self._write_record(self.root / "inspect.json", updated)
            return updated

    def usage(self) -> dict[str, Any]:
        """The latest recorded plan usage per executor, as executors reported it."""
        try:
            return _state.json.loads((self.root / "usage.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def record_usage(self, executor: str, windows: list[dict[str, Any]]) -> None:
        """Keep the latest usage per executor and append it to a bounded observation history."""
        with self._state_lock():
            usage = self.usage()
            seen_at = self._timestamp()
            usage[executor] = {"seen_at": seen_at, "windows": windows}
            self._write_json(self.root / "usage.json", usage)
            # The history lets `fa orch facts` measure the rate within one window.
            path = self.root / "usage-history.jsonl"
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                lines = []
            lines.append(_state.json.dumps({"executor": executor, "seen_at": seen_at, "windows": windows},
                                           ensure_ascii=False))
            self._atomic_write(path, "\n".join(lines[-USAGE_HISTORY_LINES:]) + "\n")

    def tidy_state(self) -> dict[str, Any]:
        try:
            return _state.json.loads((self.root / "tidy.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def update_tidy_state(self, change: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        """Apply ``change`` under the state lock; it returns whether to save."""
        with self._state_lock():
            state = self.tidy_state()
            if change(state):
                self._write_json(self.root / "tidy.json", state)
            return state


# Bound last: foragent.state imports this module, and lookups happen at call time
# so names patched on the facade reach this implementation.
import foragent.state as _state
