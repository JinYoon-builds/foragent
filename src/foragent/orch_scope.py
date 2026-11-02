"""One ownership snapshot per tick, command or run (architect r85, #1004).

Original labels decide ownership; the next operation observes relabels.
RecordingTracker gates sending and queueing. Local process/WIP cleanup stays outside.
"""

from __future__ import annotations

from contextlib import contextmanager
import re

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


def _model(number: int, facts: dict) -> Issue:
    return Issue(number, facts.get("title", ""), facts.get("body", ""), frozenset(facts.get("labels", ())),
                 facts.get("state", "open"))


def local_issue(store, number: int):
    row = next((row for row in _rows(store) if row["number"] == number), None)
    return None if row is None else _model(number, row)


class Unresolved(TrackerError):
    """The linked issue of a PR is unknown."""


class OrdererUnknown(TrackerError):
    """Nobody can be named, so a question is not posted (#1004)."""


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


def _read_target(store, tracker, number: int, pr: bool):
    """The issue behind an unseen target; None for a PR that works no issue."""
    if pr:
        try:
            number = _linked_issue(store, tracker, number)
        except Unresolved:
            pass  # Not listed as a PR: it may still be an issue number.
        if number is None:
            return None
    issue = tracker.get_issue(number)
    if not getattr(issue, "is_pull_request", False):
        return issue
    linked = _linked_issue(store, tracker, number)
    return None if linked is None else tracker.get_issue(linked)


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
        self.entry, self._unread = False, False
        self._issues, self._prs, self._decisions = None, {}, {}
        if issues is not None or issue is not None:
            self._capture()
            self._issues.update((item.number, item) for item in issues or ())
            if issue is not None:
                self._issues[issue.number] = issue

    def _capture(self):
        if self._issues is not None:
            return
        rows = _rows(getattr(self._ctx, "store", None))
        self._issues = {row["number"]: _model(row["number"], row.get("observed") or row) for row in rows
                        if row.get("observed") or row.get("title") or row.get("labels")}
        self._prs = {row["pr_number"]: row["number"] for row in rows if row.get("pr_number")}

    def login(self) -> str | None:
        if not self._read:
            self._login, self._read = factory_login(self._ctx.tracker), True
        return self._login

    def _target(self, number):
        self._capture()
        seen = set()
        while number in self._prs:
            target = self._prs[number]
            if target == number:
                return number  # A number can be its own canonical target; absence decides below.
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
        if not self._unread:  # Without a read, a caller's cached row is not evidence.
            self._issues.setdefault(issue.number, issue)
        return self.for_number(issue.number)

    def for_number(self, number) -> str | None:
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
            config = ownership_config(getattr(self._ctx, "store", None), self._ctx.config)
            if config is None:  # None refuses: an invalid team config cannot authorize the singleton path.
                self._decisions[number] = _unreadable(self._ctx, number, Unresolved("the committed team config is invalid"))
            elif issue is None and getattr(config, "legacy_owner", None) is not None:
                self._decisions[number] = _unreadable(self._ctx, number, Unresolved("issue absent from snapshot"))
            elif (issue := issue or local_issue(getattr(self._ctx, "store", None), number)) is None or singleton(issue, config):
                # No team setting keeps the singleton's path, unless a stored row names an owner.
                self._decisions[number] = None
            else:
                try:
                    require_owned(issue, config, self.login())
                except OwnershipError as exc:
                    self._decisions[number] = str(exc)
                else:
                    self._decisions[number] = None
        return self._decisions[number]

    def owns(self, issue) -> bool:
        return self.refusal(issue) is None

    def for_effect(self, number, tracker, *, pr=False) -> str | None:
        """Closing an issue or changing a branch or PR is refused regardless of the snapshot.

        A target no read has shown is read once. No such target has no owner to protect (the
        effect fails by itself); any other failed read refuses; a rate limit propagates.
        """
        if (refusal := self.for_number(number)) is not None or number is None:
            return refusal
        target = self._target(number)
        if target is None or target in self._issues or any(
                row["number"] == target and (row.get("observed") or row.get("title") or row.get("labels"))
                for row in _rows(getattr(self._ctx, "store", None))):
            return None
        try:
            issue = _read_target(getattr(self._ctx, "store", None), observe(tracker), target, pr)
        except KeyError:
            return None  # FakeTracker's missing number.
        except Exception as exc:
            if isinstance(exc, TrackerError) and exc.is_not_found:
                return None
            if isinstance(exc, TrackerError) and exc.is_rate_limit:
                raise
            return _unreadable(self._ctx, target, exc)
        if issue is None:
            return None
        self._issues[target] = issue
        self._decisions.pop(target, None)
        return self.for_number(number)


def no_snapshot(ctx) -> Scope:
    """Before a tick's read or after a failed one: nothing is written to team issues; the next tick decides."""
    empty = Scope(ctx)
    empty._issues, empty._unread = {}, True
    return empty


def entry_read(ctx, number: int):
    """A run's entry read; when it fails the run's cleanup has no snapshot either."""
    try:
        return observe(ctx.tracker).get_issue(number)
    except BaseException:
        ctx._scope = no_snapshot(ctx)
        raise


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
    if getattr(ctx, "_scope", None) is not None and ctx._scope.entry:
        return ctx._scope.for_number(number)
    try:
        if read and issue is None and not stored_singleton(ctx.store, ctx.config, number):
            issue = current_issue(ctx, number)
        ctx._scope = Scope(ctx, issue)
        ctx._scope.entry = True
        if issue is not None and issue.number != number:
            ctx._scope._prs[number] = issue.number
        return ctx._scope.for_number(number)
    except Exception as exc:
        if isinstance(exc, TrackerError) and exc.is_rate_limit:
            raise
        return _unreadable(ctx, number, exc)


# Compatibility names for cleanup/publication callers: all use the same decision,
# never another ownership read. Cleanup itself remains outside the tracker gate.
def current_refusal(ctx, number: int) -> str | None:
    return scope(ctx).for_number(number)


def write_refusal(context, tracker, number: int) -> str | None:
    return scope(context).for_number(number)


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
    if (getattr(ctx.config, "legacy_owner", None) is not None
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


def may_change(ctx, number: int) -> bool:
    """Whether this factory may close the issue or change its branch or PR."""
    try:
        return scope(ctx).for_effect(number, ctx.tracker) is None
    except Exception:
        return False


def batch_parent(ctx, number: int):
    """The parent whose batch branch this work may use; another person's branch stays theirs."""
    parent = ctx.tracker.parent_of(number)
    return None if parent is None or not may_change(ctx, parent) else parent


def branch_refusal(ctx, branch: str | None, tracker=None) -> str | None:
    """A batch branch belongs to the owner of its parent issue."""
    found = re.fullmatch(r"foragent/batch-(\d+)", branch or "")
    return None if found is None else scope(ctx).for_effect(int(found[1]), tracker or ctx.tracker)


def ask_orderer(ctx, number: int, summary: str) -> str:
    """The question text, naming its orderer; OrdererUnknown when nobody can be named."""
    issue = scope(ctx).issue(number)
    try:
        if issue is None or singleton(issue, ctx.config):
            orderer = scope(ctx).login()  # The singleton's orderer is the signed-in person.
        else:
            orderer = effective_orderer(issue, ctx.config, scope(ctx).login())
    except OwnershipError as exc:
        raise OrdererUnknown(f"Cannot name who to ask on #{number}: {exc}") from exc
    if not orderer:
        raise OrdererUnknown(f"Cannot name who to ask on #{number}: the signed-in person is unknown")
    return f"@{orderer} {summary}"
