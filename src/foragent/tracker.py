"""Issue tracking interfaces, an in-memory fake, and an offline-testable gh adapter."""

from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
import json
import logging
from pathlib import Path
import re
import subprocess
import threading
import time
import zlib
from typing import Any, Literal, Protocol
from urllib.parse import quote
from zipfile import BadZipFile, ZipFile

from foragent import cuts, gh

log = logging.getLogger(__name__)


IssueState = Literal["open", "closed"]
CheckStatus = Literal["pass", "fail", "pending", "none"]
PRState = Literal["open", "merged"]


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    labels: frozenset[str]
    state: IssueState
    is_pull_request: bool = False
    updated_at: str | None = None
    # Link counts GitHub lists with each issue; they change when a link changes.
    sub_issues_summary: tuple[tuple[str, int], ...] | None = None
    dependencies_summary: tuple[tuple[str, int], ...] | None = None
    # Why GitHub closed the issue ("completed", "not_planned", ...); None while open or unknown.
    state_reason: str | None = None


@dataclass(frozen=True)
class LabelEvent:
    label: str
    action: Literal["labeled", "unlabeled"]
    actor: str
    created_at: str


@dataclass(frozen=True)
class Comment:
    id: int
    author: str
    body: str
    created_at: str


@dataclass(frozen=True)
class Dependency:
    number: int
    state: IssueState
    state_reason: str | None = None
    repository: str | None = None  # "owner/name" when GitHub names it; None means this repository.


@dataclass(frozen=True)
class LabelSpec:
    name: str
    color: str
    description: str


@dataclass(frozen=True)
class PullRequest:
    number: int
    head: str
    base: str
    title: str
    body: str
    merged_at: str | None = None
    url: str = ""


class Tracker(Protocol):
    def rate_limit_reset(self) -> datetime | None: ...
    def authenticated_login(self, *, refresh: bool = False) -> str: ...
    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]: ...
    def list_open_issues(self) -> list[Issue]: ...
    def list_issues(self, state: str = "all") -> list[Issue]: ...
    def update_issue(self, n: int, *, title: str, body: str) -> None: ...
    def add_sub_issue(self, parent: int, child: int) -> None: ...
    def add_blocked_by(self, n: int, blocker: int) -> None: ...
    def remove_blocked_by(self, n: int, blocker: int) -> None: ...
    def get_issue(self, n: int) -> Issue: ...
    def body_last_edited_at(self, n: int) -> str | None: ...
    def label_events(self, n: int) -> list[LabelEvent]: ...
    def blocked_by(self, n: int) -> list[Dependency]: ...
    def parent_of(self, n: int) -> int | None: ...
    def parent_issue(self, n: int) -> Issue | None: ...
    def sub_issues(self, n: int) -> list[Issue]: ...
    def add_labels(self, n: int, labels: Iterable[str]) -> Issue | None: ...
    def remove_labels(self, n: int, labels: Iterable[str]) -> Issue | None: ...
    def comment(self, n: int, body: str) -> int: ...
    def list_comments(self, n: int) -> list[Comment]: ...
    def create_issue(self, title: str, body: str, labels: Iterable[str]) -> int: ...
    def ensure_labels(self, specs: list[LabelSpec]) -> None: ...
    def close_issue(self, n: int, comment: str | None = None) -> None: ...
    def create_pr(self, *, head: str, base: str, title: str, body: str) -> int: ...
    def update_pr(self, pr: int, *, base: str, title: str, body: str) -> None: ...
    def close_pr(self, pr: int, comment: str | None = None) -> None: ...
    def find_pr(self, head: str) -> int | None: ...
    def list_prs(self, state: PRState = "open", head_prefix: str = "") -> list[PullRequest]: ...
    def pr_changed_files(self, pr: int) -> list[str]: ...
    def pr_checks(self, pr: int) -> CheckStatus: ...
    def pr_acceptance_checks(self, pr: int) -> list[dict]: ...
    def pr_acceptance_reports(self, head: str) -> list[dict]: ...
    def pr_default_branch(self) -> str: ...
    def pr_failed_logs(self, pr: int, *, full_text_dir: Path | None = None) -> str: ...
    def pr_head_sha(self, pr: int) -> str: ...
    def merge_pr(self, pr: int, *, expected_head_sha: str, method: str = "squash",
                 subject: str | None = None) -> None: ...


class TrackerError(RuntimeError):
    """A gh operation failed; the message contains its stderr."""

    def __init__(self, message: str, *, stdout: str = "", returncode: int | None = None):
        super().__init__(message)
        self.stdout = stdout
        self.returncode = returncode

    @property
    def is_rate_limit(self) -> bool:
        diagnostic = f"{self}\n{self.stdout}"
        return bool(re.search(
            r"(?:API rate limit (?:already )?exceeded|secondary rate limit|\bHTTP(?:/\S+)?\s+429\b)",
            diagnostic, re.I,
        ))

    @property
    def is_not_found(self) -> bool:
        return bool(re.search(r"\bHTTP(?:/\S+)?\s+404\b", f"{self}\n{self.stdout}", re.I))

    @property
    def reset_at(self) -> datetime | None:
        match = re.search(r"^x-ratelimit-reset:\s*(\d+)\s*$", f"{self.stdout}\n{self}", re.I | re.M)
        if match is not None:
            try:
                return datetime.fromtimestamp(int(match[1]), UTC)
            except (ValueError, OverflowError, OSError):
                pass
        return None


_FAILED_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
_NOT_STARTED = re.compile(r"\bjob was not started\b", re.I)


class IdentityError(TrackerError):
    """The signed-in GitHub principal is not a person who can order work (for example an App token)."""

    def __init__(self, login: str, kind: str):
        super().__init__(f"GitHub credential belongs to {login} ({kind}), not a person")
        self.login, self.kind = login, kind


class CIEvidence(str):
    """Failed CI text. ``not_started`` marks evidence where no failed job ever ran."""

    not_started: bool

    def __new__(cls, text: str = "", *, not_started: bool = False) -> "CIEvidence":
        evidence = super().__new__(cls, text)
        evidence.not_started = not_started
        return evidence


def pr_issue_number(head: str) -> int | None:
    match = re.fullmatch(r"foragent/(?:issue|batch)-(\d+)", head)
    return int(match[1]) if match else None


def local_issue_models(store) -> list[Issue]:
    return [Issue(row["number"], row["title"], row["body"], frozenset(row["labels"]), row["state"])
            for row in store.local_issues()]


class RecordingTracker:
    """Record writes locally, optionally deferring rate-limited gate/orchestrator writes."""

    def __init__(self, tracker, context, *, queue_writes=False):
        self._tracker = tracker._tracker if isinstance(tracker, RecordingTracker) else tracker
        self._context = context
        self.queue_writes = queue_writes
        # GitHub calls made for this context count in its own store and clock.
        store, now = getattr(context, "store", None), getattr(context, "now", None)
        if store is not None and now is not None:
            gh.set_recorder(store, now)

    @property
    def _now(self):
        return self._tracker._now

    @_now.setter
    def _now(self, value):
        self._tracker._now = value

    def __getattr__(self, name):
        value = getattr(self._tracker, name)
        if not callable(value):
            return value

        def observe(*args, **kwargs):
            started = self._started()
            result = value(*args, **kwargs)
            if isinstance(result, Issue):
                self._ingest([result], started)
            return result

        return observe

    def _started(self):
        """When a read or write begins: observations and intents are ordered by it, not by completion."""
        now = getattr(self._context, "now", None)
        return now() if now is not None else datetime.now(UTC)

    def _ingest(self, issues, started, *, complete=False):
        """Send what one read that began at ``started`` showed through the store's one ingest."""
        shown = {issue.number for issue in issues}
        for number in self._context.store.ingest(issues, started, self._context.config.lanes, complete=complete):
            self._outbox_dropped(number, "remote change" if number in shown else "issue not found")

    def _absent(self, number, started):
        if self._context.store.ingest_absence(number, started):
            self._outbox_dropped(number, "issue not found")

    def list_issues(self, state="all"):
        started = self._started()
        issues = self._tracker.list_issues(state)
        # The bulk refresh ingests its own listing with its comments, edges and PRs.
        if self.__dict__.get("_bulk_listing") != threading.get_ident():
            self._ingest(issues, started, complete=state == "all")
        return issues

    def get_issue(self, n):
        started = self._started()
        try:
            issue = self._tracker.get_issue(n)
        except TrackerError as exc:
            if exc.is_not_found:
                self._absent(n, started)
            raise
        self._ingest([issue], started)
        return issue

    def _pending_issues(self):
        pending = {entry["issue"] for entry in self._context.store.github_outbox()}
        if not pending:
            return {}
        return {issue.number: issue for issue in local_issue_models(self._context.store)
                if issue.number in pending}

    def current_issue(self, n):
        """Read local intent while delivery is pending, otherwise observe GitHub."""
        if (issue := self._pending_issues().get(n)) is not None:
            return issue
        return self.get_issue(n)

    def sub_issues(self, n):
        started = self._started()
        children = self._tracker.sub_issues(n)
        self._ingest(children, started)
        pending = self._pending_issues()
        return [pending.get(child.number, child) for child in children]

    def _outbox_dropped(self, number, reason):
        from foragent.orch_events import append_event

        append_event(self._context.store, self._context.now(), number, "outbox-dropped",
                     f"#{number}: dropped queued GitHub writes: {reason}")

    def refresh_issues(self, *, observed_at=None):
        """Publish a bulk observation once, preserving writes made during its reads.

        The snapshot is stamped when the observation began (``observed_at``, or
        now), never after its reads, so the next periodic tick finds it due.
        A failed read leaves the last snapshot intact and records the failure,
        so local readers report stale data instead of an empty queue.
        Returns whether the listing showed any change since the last one.
        """
        try:
            return self._refresh_issues(observed_at or self._context.now())
        except TrackerError as exc:
            self._context.store.record_issues_refresh_failure(" ".join(str(exc).split()), self._context.now())
            raise

    def _refresh_issues(self, observed_at):
        from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, READY
        from foragent.state import observed_issue_facts

        store = self._context.store
        revision = store.issues_revision()
        pending = {entry["issue"] for entry in store.github_outbox()}
        previous = {row["number"]: row for row in store.local_issues()}
        self._bulk_listing = threading.get_ident()
        try:
            issues = self.list_issues()
        finally:
            self._bulk_listing = None
        # The listing is the change signal: an issue whose listed facts match the
        # last listing keeps its last per-issue reads. A changed issue also marks
        # the issues it blocks, because a dependent does not change when its blocker closes.
        def listed_change(issue):
            before = (previous.get(issue.number) or {}).get("observed")
            return before is None or any(before.get(key) != value
                                         for key, value in observed_issue_facts(issue).items())

        changed = {issue.number for issue in issues if issue.number in pending or listed_change(issue)}
        dependents = {number for number, row in previous.items()
                      if any(dep["number"] in changed and dep.get("repository") is None
                             for dep in row.get("blocked_by") or [])}
        # These observations belong to the batch; recording them individually
        # would overwrite concurrent writes before the final version check.
        # Open PRs come last so a new PR wins over an older merged one for the same issue.
        merged, listing = self._merged_prs(store, observed_at)
        prs = merged + self._tracker.list_prs("open", "foragent/")
        comments, missing = {}, set()
        for issue in issues:
            if issue.is_pull_request or not (
                issue.number in pending or issue.state == "open" and issue.labels & {BLOCKED, NEEDS_HUMAN}
            ) or issue.number not in changed:
                continue
            try:
                comments[issue.number] = [asdict(comment) for comment in self._tracker.list_comments(issue.number)]
            except TrackerError as exc:
                if not exc.is_not_found or not self._missing(issue.number):
                    raise
                missing.add(issue.number)
        issues = [issue for issue in issues if issue.number not in missing]
        # Order edges and dependencies let views group work without per-frame reads.
        open_issues = [issue for issue in issues if issue.state == "open" and not issue.is_pull_request]
        children, child_reads = {}, []
        for issue in open_issues:
            known = (previous.get(issue.number) or {}).get("children")
            if issue.number in changed or known is None:
                started = self._started()  # Each child read is its own observation, ingested after the listing.
                read = self._tracker.sub_issues(issue.number)
                child_reads.append((read, started))
                known = [child.number for child in read]
            children[issue.number] = known
        parents = {child: parent for parent, numbers in children.items() for child in numbers}
        blocked_by = {}
        for issue in open_issues:
            if not issue.labels & {APPROVED, READY}:
                continue
            known = (previous.get(issue.number) or {}).get("blocked_by")
            if issue.number in changed | dependents or known is None:
                known = [{"number": dep.number, "state": dep.state, "state_reason": dep.state_reason,
                          **({"repository": dep.repository} if dep.repository else {})}
                         for dep in self._tracker.blocked_by(issue.number)]
            blocked_by[issue.number] = known
        dropped = store.refresh_local_issues(issues, prs, self._context.config.lanes, observed_at,
                                            comments=comments, since_revision=revision, parents=parents,
                                            blocked_by=blocked_by, children=children, complete_prs=True)
        for read, started in child_reads:
            dropped = sorted(set(dropped) | set(store.ingest(read, started, self._context.config.lanes)))
        if listing is not None:
            # Advanced only now: a failed read or refresh keeps the old watermark.
            store.record_merged_pr_listing(listing)
        observed = {issue.number for issue in issues}
        for number in dropped:
            reason = "remote change" if number in observed else "issue not found"
            self._outbox_dropped(number, reason)
        return bool(changed or dropped or previous.keys() - observed)

    def _merged_prs(self, store, observed_at):
        """Every merged foragent PR, newest update first, as a full listing returns them.

        With a stored listing only closed PRs updated since its watermark (less
        a margin for clock skew) are read; they replace their stored records.
        Returns the PRs and the listing to store after a successful refresh.
        """
        read = getattr(self._tracker, "merged_prs_updated_since", None)
        if read is None:
            return self._tracker.list_prs("merged", "foragent/"), None
        known = store.merged_pr_listing()
        since = None
        if known:
            watermark = datetime.fromisoformat(known["watermark"])
            since = (watermark if watermark.tzinfo else watermark.replace(tzinfo=UTC)) - _WATERMARK_MARGIN
        records = {record["number"]: record for record in (known or {}).get("prs", [])}
        records.update((record["number"], record) for record in read(since, "foragent/"))
        ordered = sorted(records.values(), key=lambda record: _parse_time(record.get("updated_at"))
                         or datetime.min.replace(tzinfo=UTC), reverse=True)
        prs = [PullRequest(record["number"], record["head"], record["base"], "", "",
                           record["merged_at"], record["url"]) for record in ordered]
        return prs, {"watermark": observed_at.isoformat(), "prs": ordered}

    def observe_closed_issues(self, *, observed_at):
        """Record issues closed since the stored watermark: one light read per tick.

        Without a watermark the read starts at the last full listing, which
        already showed every earlier close. Each closed issue the local record
        does not show closed is recorded, including one created and closed
        between listings, so this tick's board sync moves its card to Done.
        """
        read = getattr(self._tracker, "issues_closed_since", None)
        if read is None:
            return
        store = self._context.store
        start = store.closed_issue_watermark()
        if start is None and (listed := store.issues_observation()["as_of"]) is not None:
            start = datetime.fromisoformat(listed)
        if start is not None:
            since = (start if start.tzinfo else start.replace(tzinfo=UTC)) - _WATERMARK_MARGIN
            try:
                issues = read(since)
            except TrackerError as exc:
                if exc.is_rate_limit:
                    raise
                log.warning("Could not read recently closed issues: %s", exc)
                return  # Keep the watermark; the next tick reads this span again.
            # Every closed issue is an observation, so a changed close reason is seen too.
            self._ingest([issue for issue in issues if issue.state == "closed"], observed_at)
        store.record_closed_issue_watermark(observed_at)

    def _missing(self, number):
        """Whether the issue itself is gone, not only a write resource; both answers are observations."""
        started = self._started()
        try:
            issue = self._tracker.get_issue(number)
        except TrackerError as exc:
            if exc.is_not_found:
                self._absent(number, started)
                return True
            raise
        self._ingest([issue], started)
        return False

    def _premise(self, n):
        """The visible issue a write is based on, taken when the write starts."""
        from foragent.state_observations import observed_facts

        row = next((row for row in self._context.store.local_issues() if row["number"] == n), None)
        return observed_facts(row) if row is not None else None

    def _write(self, n, action, operation, started, **payload):
        """Send now, or append an intent that counts from ``started`` against the issue as it was then."""
        store = self._context.store
        premise = self._premise(n)
        pending = store.github_outbox()
        if not (any(entry["issue"] == n for entry in pending)
                or self.queue_writes and (store.github_wait_until(self._context.now()) is not None or pending)):
            try:
                return operation(), None
            except TrackerError as exc:
                if not self.queue_writes or not exc.is_rate_limit:
                    raise
                from foragent.orch_github import pause

                pause(self._context, exc)
                entry = self._queue(n, action, started, premise, payload)
                store.record_github_attempt(entry["id"], started, False)
                return None, entry
        return None, self._queue(n, action, started, premise, payload)

    def _queue(self, n, action, started, premise, payload):
        """Append the intent; a read made after it started may already have dropped it."""
        dropped = []
        entry = self._context.store.queue_github_write(n, action, lanes=self._context.config.lanes,
                                                       started_at=started, premise=premise, dropped=dropped,
                                                       **payload)
        if any(dropped):
            missing = not any(row["number"] == n for row in self._context.store.local_issues())
            self._outbox_dropped(n, "issue not found" if missing else "remote change")
        return entry

    def flush_outbox(self):
        """Deliver the pending intents in queue order; each attempt is appended to its intent."""
        from foragent.orch_github import pause

        store = self._context.store
        dropped = set()

        def current(identity):
            """The intent as stored now: a read during an earlier send may have retired it."""
            return next((entry for entry in store.github_outbox() if entry["id"] == identity), None)

        for queued in store.github_outbox():
            number, action = queued["issue"], queued["action"]
            if number in dropped:
                continue
            started = None
            try:
                if action == "remove_labels":
                    while (entry := current(queued["id"])) is not None and entry["labels"]:
                        label, started = entry["labels"][0], self._started()
                        self._tracker.remove_labels(number, [label])
                        store.record_github_attempt(entry["id"], started, True, labels=[label])
                    continue
                if (entry := current(queued["id"])) is None:
                    continue
                started = self._started()
                if action == "update_issue":
                    self._tracker.update_issue(number, title=entry["title"], body=entry["body"])
                    store.record_github_attempt(entry["id"], started, True)
                else:
                    payload = entry["labels"] if action == "add_labels" else entry["body"]
                    result = getattr(self._tracker, action)(number, payload)
                    store.record_github_attempt(entry["id"], started, True,
                                                comment=result if action == "comment" else None)
            except TrackerError as exc:
                if started is not None:
                    # Every send attempt is recorded, whatever its failure.
                    store.record_github_attempt(queued["id"], started, False,
                                                labels=[label] if action == "remove_labels" else None)
                try:
                    if exc.is_not_found and self._missing(number):
                        dropped.add(number)
                        continue
                except TrackerError as lookup_error:
                    exc = lookup_error
                if not exc.is_rate_limit:
                    raise exc
                pause(self._context, exc)
                return

    def _acknowledge(self, n, started, result, **facts):
        """A direct write that succeeded is an observation at its start; any read during the send wins."""
        if isinstance(result, Issue):
            self._ingest([result], started)
        if facts:
            self._context.store.update_local_issue(n, observed_at=started, **facts)

    def add_labels(self, n, labels):
        labels = list(labels)
        if not labels:
            return
        started = self._started()
        result, queued = self._write(n, "add_labels", lambda: self._tracker.add_labels(n, labels), started,
                                     labels=labels)
        if queued:
            return
        self._acknowledge(n, started, result)
        self._context.store.record_labels(n, labels, remove=False, lanes=self._context.config.lanes,
                                          observed_at=started)

    def remove_labels(self, n, labels):
        labels = list(labels)
        for index, label in enumerate(labels):
            started = self._started()
            result, queued = self._write(n, "remove_labels", lambda: self._tracker.remove_labels(n, [label]),
                                         started, labels=labels[index:])
            if queued:
                break
            self._acknowledge(n, started, result)
            self._context.store.record_labels(n, [label], remove=True, lanes=self._context.config.lanes,
                                              observed_at=started)

    def list_comments(self, n):
        comments = self._tracker.list_comments(n)
        self._context.store.record_comments(n, [asdict(comment) for comment in comments])
        return comments

    def comment(self, n, body):
        identity, queued = self._write(n, "comment", lambda: self._tracker.comment(n, body), self._started(),
                                       body=body)
        if queued:
            return -queued["id"]
        self._context.store.record_comments(n, [{"id": identity, "body": body,
            "author": "foragent", "created_at": self._context.now().astimezone(UTC).isoformat()}], append=True)
        return identity

    def create_issue(self, title, body, labels):
        labels = list(labels)
        started = self._started()
        n = self._tracker.create_issue(title, body, labels)
        self._ingest([Issue(n, title, body, frozenset(labels), "open")], started)
        return n

    def update_issue(self, n, *, title, body):
        started = self._started()
        _, queued = self._write(n, "update_issue", lambda: self._tracker.update_issue(n, title=title, body=body),
                                started, title=title, body=body)
        if not queued:
            self._acknowledge(n, started, None, title=title, body=body)

    def close_issue(self, n, comment=None):
        started = self._started()
        _, queued = self._write(n, "close_issue", lambda: self._tracker.close_issue(n) if comment is None
                               else self._tracker.close_issue(n, comment), started, body=comment)
        if not queued:
            self._acknowledge(n, started, None, state="closed")

    def record_board_value(self, n, project, field, value):
        self._context.store.record_board_value(n, project, field, value)

    def board_value(self, n, project, field):
        """The value this repository last confirmed setting on the issue's card."""
        return self._context.store.local_board_value(n, project, field)

    def _record_pr(self, pr, head, base=None, *, merged=False):
        if (number := pr_issue_number(head)) is not None:
            fields = {"pr_number": pr, "pr_head": head, "merged": merged}
            if base is not None:
                fields["pr_base"] = base
            self._context.store.update_local_issue(number, **fields)

    def find_pr(self, head):
        pr = self._tracker.find_pr(head)
        if pr is not None:
            self._record_pr(pr, head)
        return pr

    def list_prs(self, state="open", head_prefix=""):
        prs = self._tracker.list_prs(state, head_prefix)
        for pr in prs:
            self._record_pr(pr.number, pr.head, pr.base, merged=pr.merged_at is not None)
        return prs

    def create_pr(self, *, head, base, title, body):
        pr = self._tracker.create_pr(head=head, base=base, title=title, body=body)
        self._record_pr(pr, head, base)
        return pr

    def update_pr(self, pr, *, base, title, body):
        self._tracker.update_pr(pr, base=base, title=title, body=body)
        self._record_pr_state(pr, pr_base=base)

    def _record_pr_state(self, pr, **fields):
        for row in self._context.store.local_issues():
            if row["pr_number"] == pr:
                self._context.store.update_local_issue(row["number"], **fields)

    def close_pr(self, pr, comment=None):
        self._tracker.close_pr(pr, comment)
        self._record_pr_state(pr, pr_number=None, merged=False)

    def merge_pr(self, pr, *, expected_head_sha, method="squash", subject=None):
        if subject is None:
            self._tracker.merge_pr(pr, expected_head_sha=expected_head_sha, method=method)
        else:
            self._tracker.merge_pr(pr, expected_head_sha=expected_head_sha, method=method, subject=subject)
        self._record_pr_state(pr, merged=True)


def _merge_flag(method: str) -> str:
    if method not in {"squash", "merge", "rebase"}:
        raise ValueError(f"unsupported merge method: {method}")
    return f"--{method}"


class FakeTracker:
    def rate_limit_reset(self) -> datetime | None:
        return None

    def __init__(
        self, actor: str = "foragent-bot", now: Callable[[], datetime] | None = None,
        *, strict: bool = False, login: str | None = None,
    ):
        self.actor = actor
        # The injectable authenticated person (D78 revision); defaults to the actor.
        self.login = actor if login is None else login
        self._now = now or (lambda: datetime.now(UTC))
        self.strict = strict
        self.issues: dict[int, Issue] = {}
        self.labels: dict[str, LabelSpec] = {}
        self.comments: dict[int, list[Comment]] = {}
        self.created_prs: list[PullRequest] = []
        self.merged_prs: list[int] = []
        self.deleted_branches: list[str] = []
        self._events: dict[int, list[LabelEvent]] = {}
        self._body_edits: dict[int, str] = {}
        self._dependencies: dict[int, list[Dependency]] = {}
        self._parents: dict[int, int] = {}
        self._pr_files: dict[int, list[str]] = {}
        self._pr_checks: dict[int, CheckStatus] = {}
        self._pr_failed_logs: dict[int, str] = {}
        self._pr_heads: dict[int, str] = {}
        self._next_comment_id = 1
        from foragent.board_fake import FakeProjects
        self._projects = FakeProjects()

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return self._projects.graphql(query, variables, self)

    def authenticated_login(self, *, refresh: bool = False) -> str:
        if not self.login:
            raise TrackerError("not authenticated")
        return self.login.lower()

    def _timestamp(self) -> str:
        return self._now().astimezone(UTC).isoformat()

    def _touch(self, n: int) -> None:
        """Like GitHub, any change to an issue or its timeline advances updated_at."""
        if n in self.issues:
            self.issues[n] = replace(self.issues[n], updated_at=self._timestamp())

    def add_issue(
        self, number: int, title: str = "", body: str = "", labels: Iterable[str] = (),
        state: IssueState = "open", is_pull_request: bool = False,
    ) -> Issue:
        issue = Issue(number, title, body, frozenset(labels), state, is_pull_request)
        if number in self.issues and self.issues[number].body != body:
            self._body_edits[number] = self._timestamp()
        self.issues[number] = issue
        return issue

    def list_open_issues(self) -> list[Issue]:
        return [issue for issue in self.issues.values() if issue.state == "open" and not issue.is_pull_request]

    def list_issues(self, state: str = "all") -> list[Issue]:
        return [issue for issue in self.issues.values()
                if not issue.is_pull_request and (state == "all" or issue.state == state)]

    def issues_closed_since(self, since: datetime) -> list[Issue]:
        """Closed issues updated at or after ``since``; an unknown time counts as recent."""
        # Its own light read, not a full listing (like GhTracker's since-filtered call).
        return [issue for issue in self.issues.values()
                if issue.state == "closed" and not issue.is_pull_request
                and ((updated := _parse_time(issue.updated_at)) is None or updated >= since)]

    def update_issue(self, n: int, *, title: str, body: str) -> None:
        self.edit_body(n, body)
        self.issues[n] = replace(self.get_issue(n), title=title)

    def add_sub_issue(self, parent: int, child: int) -> None:
        self.set_parent(child, parent)

    def add_blocked_by(self, n: int, blocker: int) -> None:
        if not any(dep.number == blocker for dep in self.blocked_by(n)):
            self._dependencies.setdefault(n, []).append(Dependency(blocker, self.get_issue(blocker).state))
            self._touch(n)

    def remove_blocked_by(self, n: int, blocker: int) -> None:
        self._dependencies[n] = [dep for dep in self._dependencies.get(n, []) if dep.number != blocker]
        self._touch(n)

    def get_issue(self, n: int) -> Issue:
        return self.issues[n]

    def edit_body(self, n: int, body: str) -> Issue:
        issue = self.get_issue(n)
        if issue.body != body:
            self.issues[n] = replace(issue, body=body)
            self._body_edits[n] = self._timestamp()
            self._touch(n)
        return self.get_issue(n)

    def body_last_edited_at(self, n: int) -> str | None:
        return self._body_edits.get(n)

    def label_events(self, n: int) -> list[LabelEvent]:
        return sorted(self._events.get(n, []), key=lambda event: datetime.fromisoformat(event.created_at))

    def set_blocked_by(self, n: int, deps: list[Dependency]) -> None:
        self._dependencies[n] = list(deps)
        self._touch(n)

    def blocked_by(self, n: int) -> list[Dependency]:
        return [replace(dep, state=self.issues[dep.number].state) if dep.number in self.issues else dep
                for dep in self._dependencies.get(n, [])]

    def set_parent(self, n: int, parent: int) -> None:
        previous = self._parents.get(n)
        self._parents[n] = parent
        for number in {n, parent, previous} - {None}:
            self._touch(number)

    def parent_of(self, n: int) -> int | None:
        return self._parents.get(n)

    def parent_issue(self, n: int) -> Issue | None:
        parent = self._parents.get(n)
        return self.get_issue(parent) if parent is not None else None

    def sub_issues(self, n: int) -> list[Issue]:
        return [self.get_issue(child) for child, parent in self._parents.items() if parent == n]

    def record_label(self, n: int, label: str, actor: str) -> Issue:
        issue = self.get_issue(n)
        at = self._timestamp()
        self._events.setdefault(n, []).append(LabelEvent(label, "labeled", actor, at))
        self.issues[n] = replace(issue, labels=issue.labels | {label}, updated_at=at)
        return self.issues[n]

    def add_labels(self, n: int, labels: Iterable[str]) -> Issue:
        labels = list(labels)
        if self.strict and "approved" in labels:
            raise AssertionError("gate and runner must never add the approved label")
        for label in labels:
            if label not in self.get_issue(n).labels:
                self.record_label(n, label, self.actor)
        return self.get_issue(n)

    def remove_labels(self, n: int, labels: Iterable[str]) -> Issue:
        for label in labels:
            issue = self.get_issue(n)
            if label in issue.labels:
                at = self._timestamp()
                self._events.setdefault(n, []).append(LabelEvent(label, "unlabeled", self.actor, at))
                self.issues[n] = replace(issue, labels=issue.labels - {label}, updated_at=at)
        return self.get_issue(n)

    def comment(self, n: int, body: str) -> int:
        comment = Comment(self._next_comment_id, self.actor, body, self._timestamp())
        self._next_comment_id += 1
        self.comments.setdefault(n, []).append(comment)
        self._touch(n)
        return comment.id

    def list_comments(self, n: int) -> list[Comment]:
        return list(self.comments.get(n, []))

    def ensure_labels(self, specs: list[LabelSpec]) -> None:
        self.labels.update((spec.name, spec) for spec in specs)

    def close_issue(self, n: int, comment: str | None = None) -> None:
        if comment is not None:
            self.comment(n, comment)
        self.issues[n] = replace(self.get_issue(n), state="closed", updated_at=self._timestamp())

    def create_issue(self, title: str, body: str, labels: Iterable[str]) -> int:
        number = max([*self.issues, *(pr.number for pr in self.created_prs)], default=0) + 1
        self.add_issue(number, title, body, labels)
        return number

    def create_pr(self, *, head: str, base: str, title: str, body: str) -> int:
        number = max([*self.issues, *(pr.number for pr in self.created_prs)], default=0) + 1
        self.created_prs.append(PullRequest(number, head, base, title, body))
        self.add_issue(number, title, body, is_pull_request=True)
        return number

    def update_pr(self, pr: int, *, base: str, title: str, body: str) -> None:
        self.created_prs = [replace(item, base=base, title=title, body=body) if item.number == pr else item
                            for item in self.created_prs]
        self.issues[pr] = replace(self.get_issue(pr), title=title, body=body)

    def find_pr(self, head: str) -> int | None:
        return next((pr.number for pr in self.created_prs
                     if pr.head == head and self.get_issue(pr.number).state == "open"), None)

    def close_pr(self, pr: int, comment: str | None = None) -> None:
        self.close_issue(pr, comment)
        self.deleted_branches.append(next(item.head for item in self.created_prs if item.number == pr))

    def list_prs(self, state: PRState = "open", head_prefix: str = "") -> list[PullRequest]:
        if state not in {"open", "merged"}:
            raise ValueError(f"unsupported PR state: {state}")
        return [pr for pr in self.created_prs if pr.head.startswith(head_prefix) and (
            pr.merged_at is not None if state == "merged"
            else self.get_issue(pr.number).state == "open" and pr.merged_at is None
        )]

    def set_pr_files(self, pr: int, files: list[str]) -> None:
        self._pr_files[pr] = list(files)

    def pr_changed_files(self, pr: int) -> list[str]:
        return list(self._pr_files.get(pr, []))

    def set_pr_checks(self, pr: int, status: CheckStatus) -> None:
        self._pr_checks[pr] = status

    def pr_checks(self, pr: int) -> CheckStatus:
        return self._pr_checks.get(pr, "none")

    def pr_acceptance_checks(self, pr: int) -> list[dict]:
        return []

    def pr_acceptance_reports(self, head: str) -> list[dict]:
        return []

    def pr_default_branch(self) -> str:
        return "main"

    def set_pr_failed_logs(self, pr: int, text: str) -> None:
        self._pr_failed_logs[pr] = text

    def pr_failed_logs(self, pr: int, *, full_text_dir: Path | None = None) -> str:
        return self._pr_failed_logs.get(pr, "")

    def set_pr_head_sha(self, pr: int, sha: str) -> None:
        self._pr_heads[pr] = sha

    def pr_head_sha(self, pr: int) -> str:
        return self._pr_heads.get(pr, "0" * 40)

    def merge_pr(self, pr: int, *, expected_head_sha: str, method: str = "squash",
                 subject: str | None = None) -> None:
        _merge_flag(method)
        if self.pr_head_sha(pr) != expected_head_sha:
            raise TrackerError("PR head changed after verification")
        self.merged_prs.append(pr)
        self.issues[pr] = replace(self.get_issue(pr), state="closed")
        self.created_prs = [
            replace(item, merged_at=self._timestamp()) if item.number == pr else item
            for item in self.created_prs
        ]


_GH_TIMEOUT = 120  # Seconds for one gh subprocess outside a bounded tick step.


def _run_gh(args: list[str], input: str | None = None) -> bytes:
    return subprocess.run(
        ["gh", *args], input=input.encode() if input is not None else None,
        check=True, capture_output=True, timeout=gh.call_timeout(_GH_TIMEOUT),
    ).stdout


def _items(output: str) -> list[dict[str, Any]]:
    """gh --paginate prints consecutive JSON arrays, one per page."""
    decoder = json.JSONDecoder()
    items = []
    remaining = output.strip()
    try:
        while remaining:
            page, end = decoder.raw_decode(remaining)
            if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
                raise TrackerError("expected a gh JSON array of objects")
            items.extend(page)
            remaining = remaining[end:].lstrip()
    except json.JSONDecodeError as exc:
        raise TrackerError(f"invalid gh JSON: {exc}") from exc
    return items


def _summary(value: Any) -> tuple[tuple[str, int], ...] | None:
    if not isinstance(value, dict):
        return None
    return tuple(sorted((key, count) for key, count in value.items() if isinstance(count, int)))


def _repository(url: Any) -> str | None:
    match = re.fullmatch(r"https://[^/]+/repos/([^/]+/[^/]+)", url) if isinstance(url, str) else None
    return match[1] if match else None


def _issue(data: dict[str, Any]) -> Issue:
    return Issue(
        data["number"], data["title"], data.get("body") or "",
        frozenset(label["name"] for label in data.get("labels", [])),
        data["state"], "pull_request" in data, data.get("updated_at"),
        _summary(data.get("sub_issues_summary")), _summary(data.get("issue_dependencies_summary")),
        data.get("state_reason"),
    )


_EMPTY_RETRY_DELAY = 0.5
# Closed PRs updated this long before the last refresh began are read again.
_WATERMARK_MARGIN = timedelta(minutes=10)
_PR_PAGE_SIZE = 100


def _parse_time(value: Any) -> datetime | None:
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return when if when.tzinfo is not None else None


class GhTracker:
    def __init__(self, repo: str, run: Callable[..., str | bytes] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.repo = repo
        self._run = run or _run_gh
        self._sleep = sleep
        self._path = f"repos/{repo}"
        self._seen_states: set[str] = set()

    def _call(self, args: list[str], input: str | None = None, *, binary: bool = False) -> str | bytes:
        try:
            gh.call_timeout(_GH_TIMEOUT)  # A bounded tick step that ran out starts no more gh.
        except gh.DeadlineExhausted as exc:
            raise TrackerError(f"deferred: {exc}") from exc
        gh.count_call()  # Every attempt counts, injected runners and retries included.
        try:
            output = self._run(args, input=input)
            return output.decode() if isinstance(output, bytes) and not binary else output
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
            stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
            raise TrackerError(
                stderr or str(exc), stdout=stdout or "", returncode=exc.returncode,
            ) from exc
        except (OSError, subprocess.TimeoutExpired, gh.DeadlineExhausted) as exc:
            raise TrackerError(str(exc)) from exc

    def authenticated_login(self, *, refresh: bool = False) -> str:
        """The lowercase login of the person gh is signed in as on this machine.

        The answer is cached in the machine data root under a hash of the host and
        the active credential, so repositories and processes share one ``gh api user``
        read, and switching accounts or tokens never reuses another principal.
        ``refresh`` (fa init) reads it again. Credentials themselves are never stored.
        """
        from foragent import identity_cache

        key = identity_cache.credential_key()
        if not refresh and key is not None and (login := identity_cache.read(key)):
            return login
        try:
            data = json.loads(self._call(["api", "user"]))
        except json.JSONDecodeError as exc:
            raise TrackerError(f"invalid user JSON: {exc}") from exc
        login = data.get("login") if isinstance(data, dict) else None
        if not isinstance(login, str) or not login:
            raise TrackerError("gh api user returned no login")
        if data.get("type", "User") != "User":
            raise IdentityError(login, str(data.get("type")))
        login = login.lower()
        if key is not None:
            identity_cache.write(key, login)
        return login

    def rate_limit_reset(self) -> datetime | None:
        """Also keeps the core requests remaining from the same response in ``rate_limit_remaining``."""
        data = json.loads(self._call(["api", "rate_limit"]))
        remaining = (data.get("resources", {}).get("core") or {}).get("remaining")
        self.rate_limit_remaining = remaining if type(remaining) is int and remaining >= 0 else None
        resets = [
            datetime.fromtimestamp(window["reset"], UTC)
            for window in data.get("resources", {}).values()
            if window.get("remaining") == 0 and isinstance(window.get("reset"), (int, float))
        ]
        return max(resets, default=None)

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        output = self._call(["api", "graphql", "--input", "-"],
                            input=json.dumps({"query": query, "variables": variables}))
        try:
            result = json.loads(output)
            if not isinstance(result, dict):
                raise TrackerError("invalid GraphQL response: expected an object")
            if result.get("errors"):
                raise TrackerError(f"GraphQL errors: {result['errors']}")
            if not isinstance(result.get("data"), dict):
                raise TrackerError("invalid GraphQL response: missing data")
            return result
        except json.JSONDecodeError as exc:
            raise TrackerError(f"invalid GraphQL JSON: {exc}") from exc

    def list_open_issues(self) -> list[Issue]:
        return self.list_issues("open")

    def list_issues(self, state: str = "all") -> list[Issue]:
        if state not in {"open", "closed", "all"}:
            raise ValueError(f"unsupported issue state: {state}")
        issues = self._fetch_issues(state)
        if not issues:
            # GitHub sometimes answers a listing with an empty page by mistake.
            # One retry separates that hiccup from a repository that is empty.
            self._sleep(_EMPTY_RETRY_DELAY)
            issues = self._fetch_issues(state)
        if issues:
            states = {issue.state for issue in issues}
            # A listing of every issue is the whole truth; a filtered one adds to it.
            self._seen_states = states if state == "all" else self._seen_states | states
            return issues
        if state != "all" and state in self._seen_states:
            # Open and closed issues change state, so an earlier sighting can be
            # out of date. A listing of every issue settles it.
            if any(issue.state == state for issue in self.list_issues("all")):
                raise self._untrusted_empty(state, "a fresh listing of all issues")
            return issues
        if evidence := self._evidence_of_issues(state):
            raise self._untrusted_empty(state, evidence)
        self._seen_states.discard(state)
        return issues

    def _untrusted_empty(self, state: str, evidence: str) -> TrackerError:
        return TrackerError(
            f"GitHub returned an empty listing of {state} issues for {self.repo} twice, "
            f"but {evidence} shows such issues exist; not trusting it, try again later"
        )

    def _fetch_issues(self, state: str) -> list[Issue]:
        data = _items(self._call(["api", "--paginate", f"{self._path}/issues?state={state}&per_page=100"]))
        return [_issue(item) for item in data if "pull_request" not in item]

    def issues_closed_since(self, since: datetime) -> list[Issue]:
        """Closed issues updated since ``since``: one light read, no empty-listing retry."""
        stamp = quote(since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
        data = _items(self._call(["api", "--paginate",
                                  f"{self._path}/issues?state=closed&since={stamp}&per_page=100"]))
        return [_issue(item) for item in data if "pull_request" not in item]

    def _evidence_of_issues(self, state: str) -> str | None:
        """Name what proves `state` issues exist, or None when nothing does."""
        if state == "all" and self._seen_states:
            # Issues are not removed, so having seen any is lasting proof.
            return "an earlier listing"
        from foragent.state import StateStore

        try:
            rows = StateStore.for_repo(self.repo).local_issues()
        except (OSError, ValueError, KeyError, TypeError):
            return None
        # The gate refreshes this record from a listing of all issues, which this
        # check never blocks, so a stale open or closed row is corrected there.
        if any(state == "all" or row.get("state") == state for row in rows):
            return "the local issue record"
        return None

    def update_issue(self, n: int, *, title: str, body: str) -> None:
        self._call(["api", "-X", "PATCH", f"{self._path}/issues/{n}", "--input", "-"],
                   input=json.dumps({"title": title, "body": body}))

    def _add_issue_edge(self, path: str, field: str, number: int) -> None:
        identity = json.loads(self._call(["api", f"{self._path}/issues/{number}"]))["id"]
        try:
            self._call(["api", "-X", "POST", path, "-F", f"{field}={identity}"])
        except TrackerError as exc:
            if not re.search(r"already exists|already (?:a sub-issue|blocked by|has)", str(exc), re.I):
                raise

    def add_sub_issue(self, parent: int, child: int) -> None:
        self._add_issue_edge(f"{self._path}/issues/{parent}/sub_issues", "sub_issue_id", child)

    def add_blocked_by(self, n: int, blocker: int) -> None:
        self._add_issue_edge(f"{self._path}/issues/{n}/dependencies/blocked_by", "issue_id", blocker)

    def remove_blocked_by(self, n: int, blocker: int) -> None:
        identity = json.loads(self._call(["api", f"{self._path}/issues/{blocker}"]))["id"]
        self._call(["api", "-X", "DELETE", f"{self._path}/issues/{n}/dependencies/blocked_by/{identity}"])

    def get_issue(self, n: int) -> Issue:
        return _issue(json.loads(self._call(["api", f"{self._path}/issues/{n}"])))

    def body_last_edited_at(self, n: int) -> str | None:
        owner, name = self.repo.split("/", 1)
        query = (
            "query($owner: String!, $name: String!, $number: Int!) { "
            "repository(owner: $owner, name: $name) { issue(number: $number) { lastEditedAt } } }"
        )
        output = self._call([
            "api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}",
            "-f", f"name={name}", "-F", f"number={n}",
        ])
        try:
            data = json.loads(output)
            if data.get("errors"):
                raise TrackerError(f"cannot read issue #{n} lastEditedAt: {data['errors']}")
            edited_at = data["data"]["repository"]["issue"]["lastEditedAt"]
            if edited_at is not None and not isinstance(edited_at, str):
                raise TypeError("lastEditedAt must be a string or null")
            return edited_at
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
            raise TrackerError(f"invalid lastEditedAt response for issue #{n}") from exc

    def label_events(self, n: int) -> list[LabelEvent]:
        data = _items(self._call(["api", "--paginate", f"{self._path}/issues/{n}/events?per_page=100"]))
        events = [
            LabelEvent(item["label"]["name"], item["event"], item["actor"]["login"], item["created_at"])
            for item in data if item["event"] in {"labeled", "unlabeled"}
        ]
        return sorted(events, key=lambda event: datetime.fromisoformat(event.created_at))

    def blocked_by(self, n: int) -> list[Dependency]:
        data = _items(self._call(["api", "--paginate", f"{self._path}/issues/{n}/dependencies/blocked_by?per_page=100"]))
        # A blocker in another repository keeps its name; this repository's blockers stay bare numbers.
        return [Dependency(item["number"], item["state"], item.get("state_reason"),
                           None if (repo := _repository(item.get("repository_url"))) in {None, self.repo} else repo)
                for item in data]

    def parent_of(self, n: int) -> int | None:
        try:
            return json.loads(self._call(["api", f"{self._path}/issues/{n}/parent"]))["number"]
        except TrackerError as exc:
            if re.search(r"\bHTTP\s+404\b", str(exc)):
                return None
            raise

    def parent_issue(self, n: int) -> Issue | None:
        """The parent issue with its body, read in one GitHub call."""
        try:
            return _issue(json.loads(self._call(["api", f"{self._path}/issues/{n}/parent"])))
        except TrackerError as exc:
            if re.search(r"\bHTTP\s+404\b", str(exc)):
                return None
            raise

    def sub_issues(self, n: int) -> list[Issue]:
        data = _items(self._call(["api", "--paginate", f"{self._path}/issues/{n}/sub_issues?per_page=100"]))
        return [_issue(item) for item in data]

    def add_labels(self, n: int, labels: Iterable[str]) -> None:
        fields = [arg for label in labels for arg in ("-f", f"labels[]={label}")]
        if fields:
            self._call(["api", "-X", "POST", f"{self._path}/issues/{n}/labels", *fields])

    def remove_labels(self, n: int, labels: Iterable[str]) -> None:
        for label in labels:
            try:
                self._call(["api", "-X", "DELETE", f"{self._path}/issues/{n}/labels/{quote(label, safe='')}"])
            except TrackerError as exc:
                if not re.search(r"\bHTTP\s+404\b", str(exc)):
                    raise

    def comment(self, n: int, body: str) -> int:
        output = self._call(
            ["api", "-X", "POST", f"{self._path}/issues/{n}/comments", "--input", "-"],
            input=json.dumps({"body": body}),
        )
        return json.loads(output)["id"]

    def list_comments(self, n: int) -> list[Comment]:
        data = _items(self._call(["api", "--paginate", f"{self._path}/issues/{n}/comments?per_page=100"]))
        return [Comment(item["id"], item["user"]["login"], item["body"], item["created_at"]) for item in data]

    def ensure_labels(self, specs: list[LabelSpec]) -> None:
        for spec in specs:
            payload = json.dumps({"name": spec.name, "color": spec.color, "description": spec.description})
            try:
                self._call(["api", "-X", "POST", f"{self._path}/labels", "--input", "-"], input=payload)
            except TrackerError as exc:
                if not re.search(r"\bHTTP\s+422\b", str(exc)):
                    raise
                self._call(
                    ["api", "-X", "PATCH", f"{self._path}/labels/{quote(spec.name, safe='')}", "--input", "-"],
                    input=payload,
                )

    def close_issue(self, n: int, comment: str | None = None) -> None:
        args = ["issue", "close", str(n), "-R", self.repo]
        if comment is not None:
            args.extend(["--comment", comment])
        self._call(args)

    def search_issues(self, query: str) -> Any:
        """GitHub's issue search response for ``query``: ``items`` (up to 100), ``total_count``
        and ``incomplete_results``, so a caller can tell a complete "no match" from a partial one.

        GitHub matches titles, bodies and comments; text-match metadata (``text_matches``)
        shows the matching fragments, including comment fragments a body check cannot see.
        """
        data = json.loads(self._call([
            "api", "-X", "GET", "search/issues", "-H", "Accept: application/vnd.github.text-match+json",
            "-f", f"q={query}", "-f", "per_page=100",
        ]))
        return data  # The caller checks the shape and words any problem for a person.

    def create_issue(self, title: str, body: str, labels: Iterable[str]) -> int:
        output = self._call(
            ["api", "-X", "POST", f"{self._path}/issues", "--input", "-"],
            input=json.dumps({"title": title, "body": body, "labels": list(labels)}),
        )
        return json.loads(output)["number"]

    def list_prs(self, state: PRState = "open", head_prefix: str = "") -> list[PullRequest]:
        if state not in {"open", "merged"}:
            raise ValueError(f"unsupported PR state: {state}")
        api_state = "closed" if state == "merged" else "open"
        data = _items(self._call([
            "api", "--paginate", f"{self._path}/pulls?state={api_state}&per_page=100",
        ]))
        return [PullRequest(
            item["number"], item["head"]["ref"], item["base"]["ref"],
            item["title"], item.get("body") or "", item.get("merged_at"), item["html_url"],
        ) for item in data if item["head"]["ref"].startswith(head_prefix)
            and (state != "merged" or item.get("merged_at") is not None)]

    def merged_prs_updated_since(self, since: datetime | None, head_prefix: str = "") -> list[dict[str, Any]]:
        """Merged PRs from closed PRs listed newest update first, as stored records.

        Pages are read one at a time and reading stops after the first page whose
        oldest update is before ``since``; with no ``since`` every page is read.
        Each record is a PullRequest's fields plus GitHub's ``updated_at``, which
        may be missing; a page without known times never proves the rest is old.
        """
        records, page = [], 1
        while True:
            data = _items(self._call([
                "api", f"{self._path}/pulls?state=closed&sort=updated&direction=desc"
                f"&per_page={_PR_PAGE_SIZE}&page={page}",
            ]))
            records.extend({
                "number": item["number"], "head": item["head"]["ref"], "base": item["base"]["ref"],
                "merged_at": item["merged_at"], "url": item["html_url"], "updated_at": item.get("updated_at"),
            } for item in data if item["head"]["ref"].startswith(head_prefix) and item.get("merged_at"))
            # The cutoff uses the raw page, before filtering by head or merge.
            times = [_parse_time(item.get("updated_at")) for item in data]
            if len(data) < _PR_PAGE_SIZE or since is not None and any(
                    when is not None and when < since for when in times):
                return records
            page += 1

    def create_pr(self, *, head: str, base: str, title: str, body: str) -> int:
        output = self._call(
            ["pr", "create", "-R", self.repo, "--head", head, "--base", base,
             "--title", title, "--body-file", "-"], input=body,
        )
        match = re.search(r"/pull/(\d+)\s*$", output)
        if match is None:
            raise TrackerError(f"cannot parse PR number: {output.strip()}")
        return int(match[1])

    def close_pr(self, pr: int, comment: str | None = None) -> None:
        args = ["pr", "close", str(pr), "-R", self.repo, "--delete-branch"]
        if comment is not None:
            args.extend(["--comment", comment])
        self._call(args)

    def update_pr(self, pr: int, *, base: str, title: str, body: str) -> None:
        self._call(
            ["pr", "edit", str(pr), "-R", self.repo, "--base", base,
             "--title", title, "--body-file", "-"], input=body,
        )

    def pr_changed_files(self, pr: int) -> list[str]:
        data = _items(self._call(["api", "--paginate", f"{self._path}/pulls/{pr}/files?per_page=100"]))
        return list(dict.fromkeys(
            path for item in data for path in (item["filename"], item.get("previous_filename")) if path
        ))

    def find_pr(self, head: str) -> int | None:
        data = _items(self._call([
            "api", "--paginate",
            f"{self._path}/pulls?state=open&head={quote(self.repo.split('/')[0] + ':' + head, safe='')}&per_page=100",
        ]))
        return next((item["number"] for item in data), None)

    def pr_checks(self, pr: int) -> CheckStatus:
        try:
            output = self._call(["pr", "checks", str(pr), "-R", self.repo, "--json", "bucket,name,workflow,link"])
        except TrackerError as exc:
            if "no checks reported" in (str(exc) + "\n" + exc.stdout).lower():
                return "none"
            # gh uses exit 1 for failed checks and exit 8 for pending checks.
            if exc.returncode not in {1, 8} or not exc.stdout.strip():
                raise
            output = exc.stdout
        checks = _items(output)
        # gh supplies workflow only for CheckRun entries, never StatusContext.
        # Commit-status target URLs are caller-controlled and confer no identity.
        candidates = [item for item in checks if item.get("workflow") and "/job/" in (item.get("link") or "")]
        excluded = self._acceptance_check_keys(pr) if candidates else set()
        buckets = {item["bucket"] for item in checks
                   if not (item.get("workflow") and (item.get("name"), item.get("link")) in excluded)}
        if not buckets:
            return "none"
        if buckets & {"fail", "cancel"}:
            return "fail"
        if "pending" in buckets:
            return "pending"
        if buckets <= {"pass", "skipping"}:
            return "pass"
        raise TrackerError(f"unknown check buckets: {sorted(buckets)}")

    def _acceptance_check_keys(self, pr: int) -> set[tuple[str, str]]:
        """Exclude only Actions checks with API-verified workflow provenance."""
        from foragent.ci_workflow import WORKFLOW

        try:
            if any(path.startswith(".github/workflows/") for path in self.pr_changed_files(pr)):
                return set()
            checks = self.pr_acceptance_checks(pr)
            suites = {}
            excluded = set()
            counted = set()
            for check in checks:
                key = (check.get("name"), check.get("details_url"))
                suite = (check.get("check_suite") or {}).get("id")
                if ((check.get("app") or {}).get("slug") != "github-actions"
                        or type(suite) is not int or suite <= 0):
                    counted.add(key)
                    continue
                if suite not in suites:
                    runs = _items(self._call([
                        "api", "--paginate", "--jq", ".workflow_runs",
                        f"{self._path}/actions/runs?check_suite_id={suite}&per_page=100",
                    ]))
                    suites[suite] = bool(runs) and all(
                        run.get("check_suite_id") == suite and run.get("path") == WORKFLOW
                        and (run.get("repository") or {}).get("full_name") == self.repo
                        for run in runs
                    )
                if suites[suite] and all(key):
                    excluded.add(key)
                else:
                    counted.add(key)
            # gh exposes display keys, so an ambiguous match must count too.
            return excluded - counted
        except (TrackerError, AttributeError, TypeError, KeyError) as exc:
            if isinstance(exc, TrackerError) and exc.is_rate_limit:
                raise
            return set()  # Unverifiable checks remain ordinary CI.

    def _pr_workflow_runs(self, pr: int) -> list[dict]:
        head = self.pr_head_sha(pr)
        return _items(self._call([
            "api", "--paginate", "--jq", ".workflow_runs",
            f"{self._path}/actions/runs?head_sha={head}&per_page=100",
        ]))

    def _acceptance_run_ids(self, pr: int, runs: list[dict]) -> set[int]:
        """Only unchanged measurement workflows may be separated from ordinary CI."""
        from foragent.ci_workflow import WORKFLOW

        candidates = {run["id"] for run in runs if run.get("path") == WORKFLOW}
        if candidates and any(path.startswith(".github/workflows/") for path in self.pr_changed_files(pr)):
            return set()
        return candidates

    def pr_head_sha(self, pr: int) -> str:
        return json.loads(self._call(["api", f"{self._path}/pulls/{pr}"]))["head"]["sha"]

    def pr_acceptance_checks(self, pr: int) -> list[dict]:
        """Display data only; check runs cannot establish measurement provenance."""
        head = self.pr_head_sha(pr)
        return _items(self._call([
            "api", "--paginate", "--jq", ".check_runs",
            f"{self._path}/commits/{head}/check-runs?per_page=100&filter=latest",
        ]))

    def pr_default_branch(self) -> str:
        """The repository's default branch supplies trusted PR workflow code."""
        try:
            data = json.loads(self._call(["api", self._path]))
            branch = data["default_branch"]
            if data["full_name"] != self.repo or not isinstance(branch, str) or not branch:
                raise ValueError("invalid repository identity or default branch")
            return branch
        except (KeyError, TypeError, ValueError) as exc:
            raise TrackerError(f"Default branch unavailable: {exc}") from exc

    def pr_acceptance_reports(self, head: str) -> list[dict]:
        """Read artifacts of the newest measurement run for this exact head."""
        from foragent.ci_transport import decode_report
        from foragent.ci_workflow import WORKFLOW

        runs = _items(self._call([
            "api", "--paginate", "--jq", ".workflow_runs",
            f"{self._path}/actions/runs?head_sha={quote(head, safe='')}&per_page=100",
        ]))
        try:
            candidates = [run for run in runs if run.get("head_sha") == head
                          and run.get("path", "").split("@", 1)[0] == WORKFLOW
                          and run.get("event") == "pull_request"]
            if not candidates:
                return []
            if any(type(run.get("id")) is not int or run["id"] <= 0 for run in candidates):
                raise ValueError("invalid measurement run id")
            run = max(candidates, key=lambda value: value["id"])
            if run.get("status") in {"queued", "in_progress"}:
                return []  # No evidence yet; ci_mode retains its existing polling budget.
            if (run.get("status") != "completed" or run.get("conclusion") != "success"
                    or run["repository"]["full_name"] != self.repo
                    or run["head_repository"]["full_name"] != self.repo):
                raise ValueError("measurement run is foreign or unsuccessful")
            artifacts = _items(self._call([
                "api", "--paginate", "--jq", ".artifacts",
                f"{self._path}/actions/runs/{run['id']}/artifacts?per_page=100",
            ]))
            reports, names, ids = [], set(), set()
            for artifact in artifacts:
                name = artifact["name"]
                if not isinstance(name, str):
                    raise ValueError("invalid artifact name")
                if not name.startswith("acceptance-"):
                    continue
                index = int(name.removeprefix("acceptance-"))
                artifact_id = artifact["id"]
                if (name != f"acceptance-{index}" or index < 0 or name in names
                        or type(artifact_id) is not int or artifact_id <= 0 or artifact_id in ids
                        or artifact.get("expired") is not False
                        or type(artifact["workflow_run"]["id"]) is not int
                        or artifact["workflow_run"]["id"] != run["id"]
                        or artifact["workflow_run"]["head_sha"] != head):
                    raise ValueError("artifact identity, expiry or measurement run does not match")
                names.add(name)
                ids.add(artifact_id)
                data = self._call(["api", f"{self._path}/actions/artifacts/{artifact_id}/zip"], binary=True)
                with ZipFile(BytesIO(data)) as archive:
                    filename = f"shard-{index}.json"
                    if archive.namelist() != [filename]:
                        raise ValueError("artifact must contain exactly one matching shard report")
                    payload = decode_report(archive.read(filename).decode("utf-8"))
                reports.append({"run": run, "artifact": artifact, "report": payload})
            return reports
        except (KeyError, TypeError, ValueError, AttributeError, BadZipFile, OSError,
                EOFError, RuntimeError, zlib.error) as exc:
            # API/rate-limit errors retain their TrackerError classification.
            if isinstance(exc, TrackerError):
                raise
            raise TrackerError(f"Unusable acceptance artifacts: {exc}") from exc

    def pr_failed_logs(self, pr: int, *, full_text_dir: Path | None = None) -> CIEvidence:
        """Failed Actions evidence for the PR head: jobs, annotations and log tails.

        ``not_started`` is true only when every failed job never ran (no steps or a
        "job was not started" annotation); such jobs have no code failure to repair.
        """
        runs = self._pr_workflow_runs(pr)
        excluded_runs = self._acceptance_run_ids(pr, runs)
        parts = []
        failed_runs = 0
        code_failure = False
        for run in runs:
            if run["id"] in excluded_runs or run["conclusion"] not in _FAILED_CONCLUSIONS:
                continue
            failed_runs += 1
            jobs = _items(self._call([
                "api", "--paginate", "--jq", ".jobs",
                f"{self._path}/actions/runs/{run['id']}/jobs?per_page=100",
            ]))
            url = run.get("html_url") or run.get("url") or ""
            lines = [f"Actions run {run['id']} ({run.get('name') or 'workflow'}) {run['conclusion']} {url}".rstrip()]
            started_failure = False
            failed_jobs = 0
            for job in jobs:
                if job.get("conclusion") not in _FAILED_CONCLUSIONS:
                    continue
                failed_jobs += 1
                steps = job.get("steps") or []
                messages = self._job_annotations(job)
                job_url = job.get("html_url") or job.get("url") or ""
                if not steps or any(_NOT_STARTED.search(message) for message in messages):
                    lines.append(f"Job {job.get('name')} did not start ({len(steps)} steps) {job_url}".rstrip())
                else:
                    started_failure = True
                    lines.append(f"Job {job.get('name')} failed {job_url}".rstrip())
                lines.extend(f"  annotation: {message}" for message in messages)
            # A failed run without failed jobs (e.g. an invalid workflow file) is
            # still a failure the implementer may be able to repair.
            if started_failure or not failed_jobs:
                code_failure = True
                try:
                    output = self._call(["run", "view", str(run["id"]), "-R", self.repo, "--log-failed"])
                except TrackerError as exc:
                    if "log not found" not in f"{exc}\n{exc.stdout}".lower():
                        raise
                    output = f"(failed log unavailable: {str(exc).strip()})"
                lines.append(cuts.tail(output, 4000, full_text_dir, f"ci-run-{run['id']}.log"))
            parts.append("\n".join(lines))
        return CIEvidence("\n\n".join(parts), not_started=bool(failed_runs) and not code_failure)

    def _job_annotations(self, job: dict[str, Any]) -> list[str]:
        check_run = job.get("check_run_url") or f"{self._path}/check-runs/{job['id']}"
        annotations = _items(self._call(["api", "--paginate", f"{check_run}/annotations"]))
        return [item["message"] for item in annotations if isinstance(item.get("message"), str)]

    def merge_pr(self, pr: int, *, expected_head_sha: str, method: str = "squash",
                 subject: str | None = None) -> None:
        # Explicit subject/body prevent GitHub's defaults from copying branch
        # messages, including runner-only trailers, into the squash commit.
        if subject is None and method == "squash":
            subject = json.loads(self._call(["pr", "view", str(pr), "-R", self.repo, "--json", "title"]))["title"]
        self._call([
            "pr", "merge", str(pr), "-R", self.repo, _merge_flag(method), "--delete-branch",
            "--match-head-commit", expected_head_sha,
            *(["--subject", subject] if subject is not None else []),
            *(["--body", ""] if method == "squash" else []),
        ])
