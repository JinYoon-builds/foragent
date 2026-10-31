"""The person boundary for this factory's effects (D78 revision, 2026-10-30).

Admission, orchestrator verbs, direct runs, queued-write replay, recovery and
publication all ask one question here: may this factory act on this issue now?
Ownership comes from ``foragent.ownership``; this module supplies the factory's
login, the current issue (a PR resolves to the issue it works), and the unchanged
singleton path, so callers never build their own owner rule.

The singleton path: with no ``legacy_owner`` setting, an issue without any ``by:``
label (and not published under the owner-label contract) keeps today's behaviour
and needs no identity read. Every other case compares the issue's effective
orderer with the authenticated login and refuses on any doubt.

Two kinds of evidence, by use:
- admission reads the bulk listing this tick just took (``Scope``);
- every later shared effect rereads the issue at mutation time (``current_refusal``,
  ``may_write``, ``require``). A failed read refuses; only a rate limit propagates,
  so the existing limit wait still applies.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from foragent.i18n import text
from foragent.ownership import OWNED_MARKER, OwnershipError, effective_orderer, parse_orderer, require_owned
from foragent.tracker import Issue, RecordingTracker, TrackerError, pr_issue_number

# Verbs that act on an issue; ``merge`` names a PR, resolved to its issue first.
ISSUE_VERBS = frozenset({"stop", "retry", "park", "unpark", "resolve", "rerun-review", "escalate"})
PR_VERBS = frozenset({"merge"})


def singleton(issue, config) -> bool:
    """The unchanged single-factory path: no ``legacy_owner`` and no owner on the issue."""
    if getattr(config, "legacy_owner", None) is not None:
        return False
    if OWNED_MARKER in (getattr(issue, "body", None) or ""):
        return False
    try:
        return parse_orderer(issue.labels) is None
    except OwnershipError:
        return False


def factory_login(tracker) -> str | None:
    """This factory's authenticated login, or None when it cannot be read."""
    read = getattr(tracker, "authenticated_login", None)
    if read is None:
        return None
    try:
        return read()
    except (TrackerError, OSError, ValueError):
        return None


def foreign_reason(issue, config, tracker) -> str | None:
    """None when this factory may act on ``issue``; otherwise the one-line refusal."""
    if singleton(issue, config):
        return None
    try:
        require_owned(issue, config, factory_login(tracker))
    except OwnershipError as exc:
        return str(exc)
    return None


def observe(tracker):
    """The tracker to read ownership through without recording the read locally."""
    while isinstance(tracker, (RecordingTracker, RunTracker)):
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
    """The stored row of ``#number`` as an issue model, or None when it is not stored."""
    row = next((row for row in _rows(store) if row["number"] == number), None)
    return None if row is None else _model(number, row)


class Unresolved(TrackerError):
    """A PR whose Foragent issue cannot be found; its owner is unknown, so effects refuse."""


def _linked_issue(store, tracker, pr: int):
    """The Foragent issue a PR works: an issue number, None for a non-issue PR (dream, batch-less), or Unresolved."""
    for row in _rows(store):
        if row.get("pr_number") == pr:
            return row["number"]
    for state in ("open", "merged"):
        found = next((item for item in tracker.list_prs(state, "") if item.number == pr), None)
        if found is not None:
            return pr_issue_number(found.head)
    raise Unresolved(f"cannot tell which issue PR #{pr} works")


def current_issue(ctx, number: int, tracker=None):
    """``#number`` as GitHub shows it now; a PR becomes the issue it works (None: a PR with no issue).

    Raises TrackerError when GitHub cannot answer or a PR cannot be resolved.
    """
    tracker = observe(tracker if tracker is not None else ctx.tracker)
    issue = tracker.get_issue(number)
    if not issue.is_pull_request:
        return issue
    linked = _linked_issue(ctx.store, tracker, number)
    return None if linked is None else tracker.get_issue(linked)


def _rate_limited(exc: Exception) -> bool:
    return isinstance(exc, TrackerError) and exc.is_rate_limit


def _refusal(ctx, issue, tracker=None) -> str | None:
    scope = getattr(ctx, "_scope", None)
    if scope is not None and tracker is None:
        return scope.refusal(issue)
    return foreign_reason(issue, ctx.config, tracker if tracker is not None else ctx.tracker)


def _unreadable(ctx, number: int, exc: Exception) -> str:
    return text("ownership_unreadable", getattr(ctx.config, "language", "en"),
                number=number, error=" ".join(str(exc).split())[:200])


def stored_singleton(store, config, number: int) -> bool:
    """The unchanged single-factory path, decided without a new read.

    Team mode requires ``legacy_owner`` before a second factory joins (D78 revision,
    decision 2), so with no setting this factory is the only one and the facts already
    held decide: a stored row with an owner (in local intent or in what the last
    listing observed) still goes through the full check, anything else keeps today's
    path. With ``legacy_owner`` set, every effect reads the issue now.
    """
    if getattr(config, "legacy_owner", None) is not None:
        return False
    row = next((row for row in _rows(store) if row["number"] == number), None)
    if row is None:
        return True
    return all(singleton(_model(number, facts), config) for facts in (row, row.get("observed") or row))


def current_refusal(ctx, number: int) -> str | None:
    """The refusal for an effect on ``#number`` now; a read failure refuses, a rate limit raises."""
    if stored_singleton(ctx.store, ctx.config, number):
        return None
    try:
        issue = current_issue(ctx, number)
    except Exception as exc:  # A malformed answer is no evidence either (#1004).
        if _rate_limited(exc):
            raise
        return _unreadable(ctx, number, exc)
    return None if issue is None else _refusal(ctx, issue)


def check(ctx, number: int) -> str | None:
    """Recheck ``#number``'s current owner on GitHub at mutation time."""
    return current_refusal(ctx, number)


def may_write(ctx, number) -> bool:
    """Whether a cleanup or error path may still write GitHub for ``#number``; any doubt says no.

    Cleanup of a process tree this factory launched, and keeping its work on the local
    branch, never depend on this; only the GitHub side of cleanup does.
    """
    if not isinstance(number, int):
        return True  # Dreams and other keyed runs have no issue owner.
    try:
        return current_refusal(ctx, number) is None
    except Exception:  # Never raises: callers use it inside cleanup that must still keep local work.
        return False


# The one name recovery and parent aggregation use for the same question.
shares = may_write


def require(ctx, number: int, issue=None):
    """``issue`` (or a fresh read) when this factory still owns ``#number``; else OwnershipLost."""
    from foragent.run_attempt import OwnershipLost

    if issue is None or issue.is_pull_request:
        try:
            issue = current_issue(ctx, number)
        except Exception as exc:
            if _rate_limited(exc):
                raise
            raise OwnershipLost(_unreadable(ctx, number, exc)) from exc
    if issue is not None and (refusal := _refusal(ctx, issue)) is not None:
        raise OwnershipLost(refusal)
    return issue


def replay_refusal(context, tracker, number: int) -> str | None:
    """The refusal for replaying the next queued write on ``#number``.

    Checked before every entry, so an owner edit between two writes stops the second.
    The unchanged singleton path decides from the stored row (a refresh just moved
    aside the writes of any issue whose owner label changed). A rate limit or a
    confirmed-missing target propagates to the flush's own handling; any other failed
    or malformed read refuses this issue's writes, so other issues still go out.
    """
    if stored_singleton(context.store, context.config, number):
        return None
    ctx = SimpleNamespace(store=context.store, config=context.config, tracker=tracker)
    try:
        issue = current_issue(ctx, number, tracker)
    except Exception as exc:
        if _rate_limited(exc) or (isinstance(exc, TrackerError) and exc.is_not_found
                                  and not isinstance(exc, Unresolved)):
            raise
        return _unreadable(ctx, number, exc)
    return None if issue is None else foreign_reason(issue, context.config, tracker)


def refuse_verb(ctx, action: str, number: int | None) -> str | None:
    """The one-line refusal for an orchestrator verb on another person's work, else None."""
    if number is None or action not in ISSUE_VERBS | PR_VERBS:
        return None
    try:
        if action in PR_VERBS:
            linked = _linked_issue(ctx.store, observe(ctx.tracker), number)
            if linked is None:
                return None  # A PR with no Foragent issue (a dream); the verb's own checks decide.
            number = linked
        return current_refusal(ctx, number)
    except Exception as exc:
        return _unreadable(ctx, number, exc)


class Scope:
    """One tick's admission boundary: reads this factory's login at most once, and only when needed."""

    def __init__(self, ctx):
        self._ctx, self._login, self._read = ctx, None, False

    def login(self) -> str | None:
        if not self._read:
            self._login, self._read = factory_login(self._ctx.tracker), True
        return self._login

    def refusal(self, issue) -> str | None:
        if singleton(issue, self._ctx.config):
            return None
        try:
            require_owned(issue, self._ctx.config, self.login())
        except OwnershipError as exc:
            return str(exc)
        return None

    def owns(self, issue) -> bool:
        return self.refusal(issue) is None


def ask_orderer(ctx, number: int, summary: str) -> str:
    """``summary`` addressed to the issue's effective orderer, for a question on its thread.

    The unchanged singleton path has nobody else to address and keeps the summary as is.
    """
    try:
        issue = observe(ctx.tracker).get_issue(number)
        if singleton(issue, ctx.config):
            return summary
        return f"@{effective_orderer(issue, ctx.config, factory_login(ctx.tracker))} {summary}"
    except (TrackerError, OwnershipError):
        return summary  # The question still reaches the thread; nobody is guessed.


# Tracker calls that change an issue, its PR or its order on GitHub.
WRITES = frozenset({"add_labels", "remove_labels", "comment", "close_issue", "update_issue", "create_pr",
                    "update_pr", "close_pr", "merge_pr", "add_sub_issue", "add_blocked_by", "remove_blocked_by"})


class RunTracker:
    """A runner's tracker for issue ``number``: each write first rechecks the issue's owner.

    Whatever path a run takes (publication, a harness hold, a question, a policy hold,
    CI repair), its next GitHub write after an owner edit raises OwnershipLost instead
    of landing on another person's issue or PR. Reads pass through. On the unchanged
    single-factory path the check reads nothing (``stored_singleton``).
    """

    def __init__(self, ctx, number: int):
        self._tracker = ctx.tracker._tracker if isinstance(ctx.tracker, RunTracker) else ctx.tracker
        self._number = number
        self._check = SimpleNamespace(store=ctx.store, config=ctx.config, tracker=self._tracker,
                                      _scope=getattr(ctx, "_scope", None))

    def __getattr__(self, name):
        value = getattr(self._tracker, name)
        if not callable(value) or name not in WRITES and name != "graphql":
            return value

        def guarded(*args, **kwargs):
            if name != "graphql" or str(args[0] if args else kwargs.get("query", "")).lstrip().startswith("mutation"):
                require(self._check, self._number)
            return value(*args, **kwargs)

        return guarded


@contextmanager
def guarding(ctx, number: int):
    """Guard every write of ``ctx``'s tracker for the run of ``#number`` while the block runs.

    The guard sits inside the run's existing recorder, so the context, its recorder and
    its local records stay the same objects; it is removed again when the block ends.
    """
    recorder = ctx.tracker
    if not isinstance(recorder, RecordingTracker):
        yield
        return
    inner = recorder._tracker
    recorder._tracker = RunTracker(SimpleNamespace(tracker=inner, store=ctx.store, config=ctx.config,
                                                   _scope=getattr(ctx, "_scope", None)), number)
    try:
        yield
    finally:
        recorder._tracker = inner
