"""One ownership snapshot per tick, command or run (architect r85, #1004).

Original labels decide ownership; the next operation observes relabels.
RecordingTracker gates sending and queueing. Local process/WIP cleanup stays outside.
"""

from __future__ import annotations

from contextlib import contextmanager

from foragent.i18n import text
from foragent.ownership import OWNED_MARKER, OwnershipError, effective_orderer, parse_orderer, require_owned
from foragent.scheduler_config import ownership_config
from foragent.tracker import Issue, RecordingTracker, TrackerError, pr_issue_number

ISSUE_VERBS = frozenset({"stop", "retry", "park", "unpark", "resolve", "rerun-review", "escalate"})
PR_VERBS = frozenset({"merge"})


def singleton(issue, config) -> bool:
    if getattr(config, "legacy_owner", None) is not None or OWNED_MARKER in (getattr(issue, "body", None) or ""):
        return False
    try:
        return parse_orderer(issue.labels) is None
    except OwnershipError:
        return False


def factory_login(tracker) -> str | None:
    read = getattr(tracker, "authenticated_login", None)
    try:
        return read() if read is not None else None
    except Exception:
        return None


def observe(tracker):
    """Read an entry snapshot without recording a refusal's lookup in local state."""
    while isinstance(tracker, RecordingTracker):
        tracker = tracker._tracker
    return tracker


def _rows(store) -> list[dict]:
    try:
        return store.local_issues()
    except (AttributeError, OSError, ValueError):
        return []


def _stored_rows(store) -> list[dict]:
    """Each issue's latest observation (any read), never a pending intent's effect."""
    return store.observed_issues() if hasattr(store, "observed_issues") else _rows(store)


def _observed_rows(store) -> list[dict]:
    """The stored rows a read has shown, without placeholder rows that only hold a link or a queued write."""
    return [row for row in _stored_rows(store)
            if row.get("observed_at") or row.get("observed") or row.get("title") or row.get("labels")]


def _model(number: int, facts: dict) -> Issue:
    return Issue(number, facts.get("title", ""), facts.get("body", ""), frozenset(facts.get("labels", ())),
                 facts.get("state", "open"))


def local_issue(store, number: int):
    row = next((row for row in _rows(store) if row["number"] == number), None)
    return None if row is None else _model(number, row)


class Unresolved(TrackerError):
    """The linked issue of a PR is unknown."""


def _linked_issue(store, tracker, pr: int):
    for row in _rows(store):
        if row.get("pr_number") == pr:
            return row["number"]
    for state in ("open", "merged"):
        found = next((item for item in tracker.list_prs(state, "") if item.number == pr), None)
        if found is not None:
            return pr_issue_number(found.head)
    raise Unresolved(f"cannot tell which issue PR #{pr} works")


def current_issue(ctx, number: int, tracker=None):
    """The command/run entry read; a PR resolves to the issue it works."""
    tracker = observe(tracker if tracker is not None else ctx.tracker)
    issue = tracker.get_issue(number)
    if not getattr(issue, "is_pull_request", False):
        return issue
    linked = _linked_issue(ctx.store, tracker, number)
    return None if linked is None else tracker.get_issue(linked)


def _target_issue(store, tracker, number: int):
    """The issue a number outside the snapshot works, or None when it exists nowhere or works no issue."""
    tracker = observe(tracker)
    try:
        issue = tracker.get_issue(number)
    except TrackerError as exc:
        if not exc.is_not_found:
            raise
        issue = None
    if issue is not None and not getattr(issue, "is_pull_request", False):
        return issue
    try:
        linked = _linked_issue(store, tracker, number)
    except Unresolved:
        if issue is None:
            return None
        raise
    return None if linked is None else tracker.get_issue(linked)


def _waiting(ctx) -> bool:
    try:
        return ctx.store.github_wait_until(ctx.now()) is not None
    except AttributeError:
        return False


def _unreadable(ctx, number: int, exc: Exception) -> str:
    return text("ownership_unreadable", getattr(ctx.config, "language", "en"),
                number=number, error=" ".join(str(exc).split())[:200])


class Scope:
    """Operation facts and decisions, with one lazy identity read.

    Ticks capture after refresh; commands supply their existing entry read.
    Explicit creation adds new targets; existing issue facts stay frozen.
    """

    def __init__(self, ctx, issue=None, *, issues=None):
        self._ctx, self._login, self._read = ctx, None, False
        self.reader = getattr(ctx, "tracker", None)
        self._config = None
        self.entry = False
        self._issues, self._prs, self._decisions = None, {}, {}
        if issues is not None or issue is not None:
            self._capture()
            self._issues.update((item.number, item) for item in issues or ())
            if issue is not None:
                self._issues[issue.number] = issue

    def _capture(self):
        if self._issues is not None:
            return
        store = getattr(self._ctx, "store", None)
        rows = _stored_rows(store)
        self._issues = {row["number"]: _model(row["number"], row) for row in _observed_rows(store)}
        self._prs = {row["pr_number"]: row["number"] for row in rows if row.get("pr_number")}

    def _read_config(self):
        local = self._ctx.config  # A tick swaps it for origin's config after its fetch.
        if self._config is None or self._config[0] is not local:
            self._config = (local, ownership_config(getattr(self._ctx, "store", None), local))
        return self._config[1]

    @property
    def config(self):
        """The config this operation decides ownership with, read once (#1004)."""
        return self._read_config()[0]

    @property
    def usable(self) -> bool:
        return self._read_config()[1]

    def login(self) -> str | None:
        if not self._read:
            self._login, self._read = factory_login(self._ctx.tracker), True
        return self._login

    def _target(self, number):
        self._capture()
        seen = set()
        while number in self._prs:
            target = self._prs[number]
            if target == number and number in self._issues:
                return number  # An observed issue can be its own canonical target.
            if number in seen:
                raise Unresolved("cyclic PR links in snapshot")
            seen.add(number)
            number = target
        return number

    def issue(self, number):
        try:
            target = self._target(number)
            return self._issues.get(target)
        except Unresolved:
            return None

    def link_pr(self, number, head):
        self._capture()
        self._prs[number] = pr_issue_number(head)

    def created(self, issue):
        """Extend a captured snapshot; first capture reads creations from the store."""
        if self._issues is not None and issue.number not in self._issues:
            self._issues[issue.number] = issue
            self._decisions.pop(issue.number, None)

    def refusal(self, issue) -> str | None:
        self._capture()
        self._issues.setdefault(issue.number, issue)
        return self.for_number(issue.number)

    def for_number(self, number) -> str | None:
        if not self.usable:
            return text("ownership_config_invalid", getattr(self.config, "language", "en"), number=number)
        try:
            target = self._target(number)
        except Unresolved as exc:
            return _unreadable(self._ctx, number, exc)
        if (target != number and target is not None and target not in self._issues
                and target not in self._decisions):
            return _unreadable(self._ctx, target, Unresolved("linked issue absent from snapshot"))
        number = target
        if number is None:  # A dream PR has no issue target.
            return None
        if number not in self._decisions:
            issue = self._issues.get(number)
            if issue is None:
                if _waiting(self._ctx):
                    return None  # Only a queue entry can result; its replay judges it after the wait.
                self._decisions[number] = self._outside_snapshot(number)
            else:
                self._decisions[number] = self._judge(issue)
        return self._decisions[number]

    def _outside_snapshot(self, number) -> str | None:
        """A target outside the snapshot: a later observation decides, else read it once; absent is nobody's."""
        row = next((row for row in _observed_rows(getattr(self._ctx, "store", None)) if row["number"] == number), None)
        if row is not None:
            self._issues[number] = _model(number, row)
            return self._judge(self._issues[number])
        if getattr(self.config, "legacy_owner", None) is not None:
            return _unreadable(self._ctx, number, Unresolved("issue absent from snapshot"))
        try:
            issue = _target_issue(getattr(self._ctx, "store", None), self.reader, number)
        except Unresolved as exc:
            return _unreadable(self._ctx, number, exc)
        except TrackerError:
            raise  # No decision: the caller's effect fails like a failed send.
        except Exception as exc:
            return _unreadable(self._ctx, number, exc)
        if issue is None:
            return None
        self._issues[issue.number] = issue
        if issue.number != number:
            self._prs[number] = issue.number
        return self.for_number(issue.number) if issue.number != number else self._judge(issue)

    def _judge(self, issue) -> str | None:
        if singleton(issue, self.config):
            return None
        try:
            require_owned(issue, self.config, self.login())
        except OwnershipError as exc:
            return str(exc)
        return None

    def unseen(self, number) -> bool:
        """No team setting and no read has shown this number: entry has nothing to judge yet."""
        self._capture()
        return (self.usable and getattr(self.config, "legacy_owner", None) is None
                and number not in self._issues and number not in self._prs)

    def owns(self, issue) -> bool:
        return self.refusal(issue) is None


def scope(ctx) -> Scope:
    if getattr(ctx, "_scope", None) is None:
        ctx._scope = Scope(ctx)
    return ctx._scope


def foreign_reason(issue, config, tracker) -> str | None:
    from types import SimpleNamespace

    return Scope(SimpleNamespace(config=config, tracker=tracker), issue).refusal(issue)


def stored_singleton(store, config, number: int) -> bool:
    rows = _rows(store)
    row = next((row for row in rows if row.get("pr_number") == number), None)
    row = row or next((row for row in rows if row["number"] == number), None)
    return getattr(config, "legacy_owner", None) is None and (row is None or singleton(_model(row["number"], row), config))


def check(ctx, number: int, issue=None, *, read=True) -> str | None:
    """Check once at entry, before any shared effect, and retain that snapshot."""
    try:
        if getattr(ctx, "_scope", None) is None or not ctx._scope.entry:
            config, usable = ownership_config(ctx.store, ctx.config)
            if read and usable and issue is None and not stored_singleton(ctx.store, config, number):
                issue = current_issue(ctx, number)
            ctx._scope = Scope(ctx, issue)
            ctx._scope.entry = True
            if issue is not None and issue.number != number:
                ctx._scope._prs[number] = issue.number
        if issue is None and ctx._scope.unseen(number):
            return None  # Entry adds no read here; the first shared effect needs the evidence.
        return ctx._scope.for_number(number)
    except Exception as exc:
        if isinstance(exc, TrackerError) and exc.is_rate_limit:
            raise
        return _unreadable(ctx, number, exc)


# Compatibility names for cleanup/publication callers: all use the same decision,
# never another ownership read. Cleanup itself remains outside the tracker gate.
def current_refusal(ctx, number: int) -> str | None:
    return scope(ctx).for_number(number)


def entry_issue(ctx, number: int):
    """A run's entry read. An issue GitHub cannot find is no entry fact: the run's own reads report it."""
    try:
        return observe(ctx.tracker).get_issue(number)
    except TrackerError as exc:
        if exc.is_not_found and stored_singleton(ctx.store, ownership_config(ctx.store, ctx.config)[0], number):
            return None
        raise


def begin_entry(ctx, number: int) -> None:
    """Until the entry read answers, a stored team row authorizes no shared effect."""
    ctx._scope = Scope(ctx)
    if not stored_singleton(ctx.store, ctx._scope.config, number):
        ctx._scope._decisions[number] = _unreadable(ctx, number, Unresolved("entry read did not finish"))


def renew(ctx) -> None:
    """A new snapshot within one tick, keeping the tick's one identity read."""
    previous = getattr(ctx, "_scope", None)
    ctx._scope = Scope(ctx)
    if previous is not None and previous._read:
        ctx._scope._login, ctx._scope._read = previous._login, True


def end_entry(ctx) -> None:
    """An entry's read ends with its run or review; decisions stay, a later entry reads again."""
    if getattr(ctx, "_scope", None) is not None:
        ctx._scope.entry = False


def write_refusal(context, tracker, number: int) -> str | None:
    snapshot = scope(context)
    snapshot.reader = tracker  # The tracker this write goes to shows the evidence it needs.
    return snapshot.for_number(number)


def may_write(ctx, number) -> bool:
    if not isinstance(number, int):
        return True
    try:
        return current_refusal(ctx, number) is None
    except Exception:
        return False


shares = may_write


def require(ctx, number: int, issue=None, *, read=True):
    from foragent.run_attempt import OwnershipLost

    if (refusal := check(ctx, number, issue, read=read)) is not None:
        raise OwnershipLost(refusal)
    return issue if issue is not None else scope(ctx).issue(number)


def merge_check(ctx, number: int, issue=None) -> str | None:
    """The separate merge preflight uses its existing fresh issue read once."""
    ctx._scope = None
    return check(ctx, number, issue)


def replay_refusal(context, tracker, number: int) -> str | None:
    return write_refusal(context, tracker, number)


@contextmanager
def replaying(ctx):
    """Replay uses the latest observed rows, frozen once for the whole FIFO."""
    previous = getattr(ctx, "_scope", None)
    issues = None
    if (getattr(ownership_config(ctx.store, ctx.config)[0], "legacy_owner", None) is not None
            and not any(row.get("observed") or row.get("title") or row.get("labels") for row in _rows(ctx.store))):
        issues = observe(ctx.tracker).list_issues()  # A direct replay has no preceding tick's listing.
    ctx._scope = Scope(ctx, issues=issues)
    if previous is not None and previous._read:
        ctx._scope._login, ctx._scope._read = previous._login, True
    yield  # The rest of this tick uses this same snapshot and identity.


def refuse_verb(ctx, action: str, number: int | None) -> str | None:
    if number is None or action not in ISSUE_VERBS | PR_VERBS:
        return None
    ctx._scope = None  # A new command never inherits an earlier operation's snapshot.
    # Stop has local entry facts. Other verbs supply _issue's existing read;
    # merge supplies its title read at the remote preflight, after its checks and wait.
    return check(ctx, number) if action == "stop" else None


def ask_orderer(ctx, number: int, summary: str) -> str:
    issue = scope(ctx).issue(number)
    if issue is None or singleton(issue, scope(ctx).config):
        return summary
    try:
        return f"@{effective_orderer(issue, scope(ctx).config, scope(ctx).login())} {summary}"
    except OwnershipError:
        return summary
