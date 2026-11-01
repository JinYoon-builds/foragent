"""Order progress, approvals, flags, inspect state, plan usage, tidy state and the orchestrator note."""

from collections.abc import Callable
from typing import Any

from foragent.state_records import Approval, Flag, InspectState


# Enough observations to cover a five-hour window seen every few minutes, per executor.
USAGE_HISTORY_LINES = 1000


class LocalRecordsMixin:
    ORCH_NOTE_FILE = "orch-note.json"

    def orch_note(self) -> dict[str, Any] | None:
        """The orchestrator's latest note (``fa orch note``); None when missing or unreadable."""
        try:
            record = _state.json.loads((self.root / self.ORCH_NOTE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return record if isinstance(record, dict) else None

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
        parent_context: str | None = None,
    ) -> None:
        snapshot = {
            "number": number, "body": body, "body_sha": body_sha, "acceptance_blobs": acceptance_blobs,
            "approved_by": approved_by, "approved_at": approved_at,
        }
        if parent_context is not None:
            snapshot["parent_context"] = parent_context
        with self._state_lock():
            self._write_mapping(self.root / "approvals" / f"{number}.json", _state.Approval, snapshot)

    def freeze_parent_context(self, number: int, read_for: Approval, parent_context: str) -> Approval | None:
        """Store parent text only on the approval it was read for; a newer approval wins.

        Returns the current approval, which is a different one if it changed meanwhile.
        """
        with self._state_lock():
            current = self.load_approval(number)
            if current is None or _approval_identity(current) != _approval_identity(read_for):
                return current
            if current.parent_context is None:
                current = _state.replace(current, parent_context=parent_context)
                self._write_mapping(self.root / "approvals" / f"{number}.json", _state.Approval,
                                    _state.asdict(current))
            return current

    def load_approval(self, number: int) -> Approval | None:
        path = self.root / "approvals" / f"{number}.json"
        try:
            snapshot = _state.json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        return _state.load_record(_state.Approval, snapshot)

    def clear_approval(self, number: int) -> None:
        with self._state_lock():
            (self.root / "approvals" / f"{number}.json").unlink(missing_ok=True)

    def save_flag(self, number: int, reason: str, *, flagged_at: str | None = None,
                  local_only: bool = False) -> None:
        self._write_mapping(self.root / "flags" / f"{number}.json", _state.Flag, {
            "number": number, "reason": reason,
            "flagged_at": flagged_at if flagged_at is not None else self._timestamp(),
            **({"local_only": True} if local_only else {}),
        })

    def load_flag(self, number: int) -> Flag | None:
        """Project a shared blocker from current issue facts, including pending intent."""
        from foragent.reasons import current_blocker

        snapshot = self._issue_record()
        row = snapshot["issues"].get(str(number))
        if row is not None:
            visible = self._visible(snapshot, row)
            if (marker := current_blocker(visible, self._pending(snapshot))) is not None:
                comment, reason = marker
                return _state.Flag(number, reason, comment["created_at"])
        # Compatibility before the first observation; a known issue never falls back
        # to a historical shared blocker. Explicit machine-only facts stay local.
        local = self.load_local_flag(number)
        known = row is not None or snapshot.get("complete_at") or str(number) in snapshot.get("absent", {})
        return local if local is not None and (local.local_only or not known) else None

    def load_local_flag(self, number: int) -> Flag | None:
        """Read a machine-only flag; this record does not establish shared blockage."""
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

    def record_spend(self, executor: str, amount: float) -> None:
        """Append one charge in the executor's account currency; ``spend.jsonl`` is append-only (#786)."""
        line = _state.json.dumps({"at": self._timestamp(), "executor": executor, "amount": float(amount)})
        with self._state_lock(), (self.root / "spend.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    def executor_pauses(self, now: Any) -> dict[str, Any]:
        """Every executor's usage-limit pause still ahead of ``now``, from one read of the file."""
        try:
            pauses = _state.json.loads((self.root / "executor-pauses.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        ends = {name: _state.datetime.fromisoformat(value) for name, value in pauses.items()}
        return {name: until for name, until in ends.items() if now < until}

    def spend_today(self, executor: str, now: Any = None) -> float:
        """The executor's charges on the UTC day of ``now``, as providers count usage."""
        day = (now or self._now()).astimezone(_state.UTC).date()
        try:
            lines = (self.root / "spend.jsonl").read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return 0.0
        total = 0.0
        for line in lines:
            try:
                row = _state.json.loads(line)
                if (row["executor"] == executor
                        and _state.datetime.fromisoformat(row["at"]).astimezone(_state.UTC).date() == day):
                    total += float(row["amount"])
            except (ValueError, KeyError, TypeError):
                continue  # A torn last line is not a charge.
        return total

    def record_usage(self, executor: str, windows: list[dict[str, Any]]) -> None:
        """Keep the latest usage per executor and append it to a bounded observation history."""
        with self._state_lock():
            self._write_usage(executor, windows, self._timestamp())

    def _write_usage(self, executor: str, windows: list[dict[str, Any]], seen_at: str) -> None:
        """Store one usage reading; the caller holds the state lock."""
        usage = self.usage()
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

    def claude_quota_mode(self) -> str | None:
        """The local `fa quota mode` override, or None when none is stored or it is unreadable."""
        try:
            mode = _state.json.loads((self.root / "claude-quota.json").read_text(encoding="utf-8")).get("mode")
        except (OSError, ValueError, AttributeError):
            return None
        return mode if mode in ("keep", "burn") else None

    def set_claude_quota_mode(self, mode: str | None) -> None:
        """Store (or with None remove) the local override; only `fa quota mode` writes it."""
        path = self.root / "claude-quota.json"
        with self._state_lock():
            if mode is None:
                path.unlink(missing_ok=True)
            else:
                self._write_json(path, {"mode": mode})

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


def _approval_identity(approval: Approval) -> tuple:
    return approval.body_sha, approval.approved_by, approval.approved_at, approval.acceptance_blobs
