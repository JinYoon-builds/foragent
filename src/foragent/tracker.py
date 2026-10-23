"""Issue tracking interfaces, an in-memory fake, and an offline-testable gh adapter."""

from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
import json
import re
import subprocess
from typing import Any, Literal, Protocol
from urllib.parse import quote


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
    def pr_failed_logs(self, pr: int) -> str: ...
    def pr_head_sha(self, pr: int) -> str: ...
    def merge_pr(self, pr: int, *, expected_head_sha: str, method: str = "squash") -> None: ...


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
            result = value(*args, **kwargs)
            if isinstance(result, Issue):
                self._context.store.record_issue(result, self._context.config.lanes)
            return result

        return observe

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
        children = self._tracker.sub_issues(n)
        pending = self._pending_issues()
        return [pending.get(child.number, child) for child in children]

    def _outbox_dropped(self, number, reason):
        from foragent.orch_events import append_event

        append_event(self._context.store, self._context.now(), number, "outbox-dropped",
                     f"#{number}: dropped queued GitHub writes: {reason}")

    def refresh_issues(self):
        """Publish a bulk observation once, preserving writes made during its reads."""
        from foragent.labels import BLOCKED

        store = self._context.store
        revision = store.issues_revision()
        pending = {entry["issue"] for entry in store.github_outbox()}
        issues = self.list_issues()
        # These observations belong to the batch; recording them individually
        # would overwrite concurrent writes before the final version check.
        prs = self._tracker.list_prs("merged", "foragent/")
        comments, missing = {}, set()
        for issue in issues:
            if issue.is_pull_request or not (
                issue.number in pending or issue.state == "open" and BLOCKED in issue.labels
            ):
                continue
            try:
                comments[issue.number] = [asdict(comment) for comment in self._tracker.list_comments(issue.number)]
            except TrackerError as exc:
                if not exc.is_not_found or not self._issue_missing(issue.number):
                    raise
                missing.add(issue.number)
        issues = [issue for issue in issues if issue.number not in missing]
        dropped = store.refresh_local_issues(issues, prs, self._context.config.lanes, self._context.now(),
                                            comments=comments, since_revision=revision)
        observed = {issue.number for issue in issues}
        for number in dropped:
            reason = "remote change" if number in observed else "issue not found"
            self._outbox_dropped(number, reason)

    def _issue_missing(self, number):
        """A missing write resource alone does not prove that its issue is gone."""
        try:
            self._tracker.get_issue(number)
        except TrackerError as exc:
            if exc.is_not_found:
                return True
            raise
        return False

    def _write(self, n, action, operation, **payload):
        store = self._context.store
        pending = store.github_outbox()
        if (any(entry["issue"] == n for entry in pending)
                or self.queue_writes and (store.github_wait_until(self._context.now()) is not None or pending)):
            return None, store.queue_github_write(n, action, lanes=self._context.config.lanes, **payload)
        try:
            return operation(), None
        except TrackerError as exc:
            if not self.queue_writes or not exc.is_rate_limit:
                raise
            from foragent.orch_github import pause

            pause(self._context, exc)
            return None, store.queue_github_write(n, action, lanes=self._context.config.lanes, **payload)

    def flush_outbox(self):
        """Deliver an already refreshed FIFO; a renewed limit leaves its tail intact."""
        from foragent.orch_github import pause

        store = self._context.store
        dropped = set()
        for entry in store.github_outbox():
            number, action = entry["issue"], entry["action"]
            if number in dropped:
                continue
            try:
                if action == "remove_labels":
                    labels = entry["labels"]
                    for index, label in enumerate(labels):
                        self._tracker.remove_labels(number, [label])
                        store.deliver_github_write(entry, remaining_labels=labels[index + 1:])
                elif action == "update_issue":
                    self._tracker.update_issue(number, title=entry["title"], body=entry["body"])
                    store.deliver_github_write(entry)
                else:
                    payload = entry["labels"] if action == "add_labels" else entry["body"]
                    result = getattr(self._tracker, action)(number, payload)
                    store.deliver_github_write(entry, comment_id=result if action == "comment" else None)
            except TrackerError as exc:
                try:
                    if exc.is_not_found and self._issue_missing(number):
                        store.drop_missing_github_issue(number)
                        self._outbox_dropped(number, "issue not found")
                        dropped.add(number)
                        continue
                except TrackerError as lookup_error:
                    exc = lookup_error
                if not exc.is_rate_limit:
                    raise exc
                pause(self._context, exc)
                return

    def add_labels(self, n, labels):
        labels = list(labels)
        if not labels:
            return
        result, queued = self._write(n, "add_labels", lambda: self._tracker.add_labels(n, labels), labels=labels)
        if queued:
            return
        if isinstance(result, Issue):
            self._context.store.record_issue(result, self._context.config.lanes)
        self._context.store.record_labels(n, labels, remove=False, lanes=self._context.config.lanes)

    def remove_labels(self, n, labels):
        labels = list(labels)
        for index, label in enumerate(labels):
            result, queued = self._write(n, "remove_labels", lambda: self._tracker.remove_labels(n, [label]),
                                         labels=labels[index:])
            if queued:
                break
            if isinstance(result, Issue):
                self._context.store.record_issue(result, self._context.config.lanes)
            self._context.store.record_labels(n, [label], remove=True, lanes=self._context.config.lanes)

    def list_comments(self, n):
        comments = self._tracker.list_comments(n)
        self._context.store.record_comments(n, [asdict(comment) for comment in comments])
        return comments

    def comment(self, n, body):
        identity, queued = self._write(n, "comment", lambda: self._tracker.comment(n, body), body=body)
        if queued:
            return -queued["id"]
        self._context.store.record_comments(n, [{"id": identity, "body": body,
            "author": "foragent", "created_at": self._context.now().astimezone(UTC).isoformat()}], append=True)
        return identity

    def create_issue(self, title, body, labels):
        labels = list(labels)
        n = self._tracker.create_issue(title, body, labels)
        self._context.store.record_issue(Issue(n, title, body, frozenset(labels), "open"),
                                         self._context.config.lanes)
        return n

    def update_issue(self, n, *, title, body):
        _, queued = self._write(n, "update_issue", lambda: self._tracker.update_issue(n, title=title, body=body),
                                title=title, body=body)
        if not queued:
            self._context.store.update_local_issue(n, title=title, body=body)

    def close_issue(self, n, comment=None):
        _, queued = self._write(n, "close_issue", lambda: self._tracker.close_issue(n) if comment is None
                               else self._tracker.close_issue(n, comment), body=comment)
        if not queued:
            self._context.store.update_local_issue(n, state="closed")

    def record_board_value(self, n, project, field, value):
        self._context.store.record_board_value(n, project, field, value)

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

    def merge_pr(self, pr, *, expected_head_sha, method="squash"):
        self._tracker.merge_pr(pr, expected_head_sha=expected_head_sha, method=method)
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
        *, strict: bool = False,
    ):
        self.actor = actor
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

    def _timestamp(self) -> str:
        return self._now().astimezone(UTC).isoformat()

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

    def update_issue(self, n: int, *, title: str, body: str) -> None:
        self.edit_body(n, body)
        self.issues[n] = replace(self.get_issue(n), title=title)

    def add_sub_issue(self, parent: int, child: int) -> None:
        self.set_parent(child, parent)

    def add_blocked_by(self, n: int, blocker: int) -> None:
        if not any(dep.number == blocker for dep in self.blocked_by(n)):
            self._dependencies.setdefault(n, []).append(Dependency(blocker, self.get_issue(blocker).state))

    def remove_blocked_by(self, n: int, blocker: int) -> None:
        self._dependencies[n] = [dep for dep in self._dependencies.get(n, []) if dep.number != blocker]

    def get_issue(self, n: int) -> Issue:
        return self.issues[n]

    def edit_body(self, n: int, body: str) -> Issue:
        issue = self.get_issue(n)
        if issue.body != body:
            self.issues[n] = replace(issue, body=body)
            self._body_edits[n] = self._timestamp()
        return self.get_issue(n)

    def body_last_edited_at(self, n: int) -> str | None:
        return self._body_edits.get(n)

    def label_events(self, n: int) -> list[LabelEvent]:
        return sorted(self._events.get(n, []), key=lambda event: datetime.fromisoformat(event.created_at))

    def set_blocked_by(self, n: int, deps: list[Dependency]) -> None:
        self._dependencies[n] = list(deps)

    def blocked_by(self, n: int) -> list[Dependency]:
        return [replace(dep, state=self.issues[dep.number].state) if dep.number in self.issues else dep
                for dep in self._dependencies.get(n, [])]

    def set_parent(self, n: int, parent: int) -> None:
        self._parents[n] = parent

    def parent_of(self, n: int) -> int | None:
        return self._parents.get(n)

    def sub_issues(self, n: int) -> list[Issue]:
        return [self.get_issue(child) for child, parent in self._parents.items() if parent == n]

    def record_label(self, n: int, label: str, actor: str) -> Issue:
        issue = self.get_issue(n)
        self._events.setdefault(n, []).append(LabelEvent(label, "labeled", actor, self._timestamp()))
        self.issues[n] = replace(issue, labels=issue.labels | {label})
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
                self._events.setdefault(n, []).append(LabelEvent(label, "unlabeled", self.actor, self._timestamp()))
                self.issues[n] = replace(issue, labels=issue.labels - {label})
        return self.get_issue(n)

    def comment(self, n: int, body: str) -> int:
        comment = Comment(self._next_comment_id, self.actor, body, self._timestamp())
        self._next_comment_id += 1
        self.comments.setdefault(n, []).append(comment)
        return comment.id

    def list_comments(self, n: int) -> list[Comment]:
        return list(self.comments.get(n, []))

    def ensure_labels(self, specs: list[LabelSpec]) -> None:
        self.labels.update((spec.name, spec) for spec in specs)

    def close_issue(self, n: int, comment: str | None = None) -> None:
        if comment is not None:
            self.comment(n, comment)
        self.issues[n] = replace(self.get_issue(n), state="closed")

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

    def set_pr_failed_logs(self, pr: int, text: str) -> None:
        self._pr_failed_logs[pr] = text

    def pr_failed_logs(self, pr: int) -> str:
        return self._pr_failed_logs.get(pr, "")

    def set_pr_head_sha(self, pr: int, sha: str) -> None:
        self._pr_heads[pr] = sha

    def pr_head_sha(self, pr: int) -> str:
        return self._pr_heads.get(pr, "0" * 40)

    def merge_pr(self, pr: int, *, expected_head_sha: str, method: str = "squash") -> None:
        _merge_flag(method)
        if self.pr_head_sha(pr) != expected_head_sha:
            raise TrackerError("PR head changed after verification")
        self.merged_prs.append(pr)
        self.issues[pr] = replace(self.get_issue(pr), state="closed")
        self.created_prs = [
            replace(item, merged_at=self._timestamp()) if item.number == pr else item
            for item in self.created_prs
        ]


def _run_gh(args: list[str], input: str | None = None) -> str:
    return subprocess.run(
        ["gh", *args], input=input, check=True, capture_output=True, text=True, timeout=120,
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


def _issue(data: dict[str, Any]) -> Issue:
    return Issue(
        data["number"], data["title"], data.get("body") or "",
        frozenset(label["name"] for label in data.get("labels", [])),
        data["state"], "pull_request" in data, data.get("updated_at"),
    )


class GhTracker:
    def __init__(self, repo: str, run: Callable[..., str] | None = None):
        self.repo = repo
        self._run = run or _run_gh
        self._path = f"repos/{repo}"

    def _call(self, args: list[str], input: str | None = None) -> str:
        try:
            return self._run(args, input=input)
        except subprocess.CalledProcessError as exc:
            raise TrackerError(
                exc.stderr or str(exc), stdout=exc.stdout or "", returncode=exc.returncode,
            ) from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TrackerError(str(exc)) from exc

    def rate_limit_reset(self) -> datetime | None:
        data = json.loads(self._call(["api", "rate_limit"]))
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
        data = _items(self._call(["api", "--paginate", f"{self._path}/issues?state={state}&per_page=100"]))
        return [_issue(item) for item in data if "pull_request" not in item]

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
        return [Dependency(item["number"], item["state"], item.get("state_reason")) for item in data]

    def parent_of(self, n: int) -> int | None:
        try:
            return json.loads(self._call(["api", f"{self._path}/issues/{n}/parent"]))["number"]
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
            output = self._call(["pr", "checks", str(pr), "-R", self.repo, "--json", "bucket"])
        except TrackerError as exc:
            if "no checks reported" in (str(exc) + "\n" + exc.stdout).lower():
                return "none"
            # gh uses exit 1 for failed checks and exit 8 for pending checks.
            if exc.returncode not in {1, 8} or not exc.stdout.strip():
                raise
            output = exc.stdout
        buckets = {item["bucket"] for item in _items(output)}
        if not buckets:
            return "none"
        if buckets & {"fail", "cancel"}:
            return "fail"
        if "pending" in buckets:
            return "pending"
        if buckets <= {"pass", "skipping"}:
            return "pass"
        raise TrackerError(f"unknown check buckets: {sorted(buckets)}")

    def pr_head_sha(self, pr: int) -> str:
        return json.loads(self._call(["api", f"{self._path}/pulls/{pr}"]))["head"]["sha"]

    def pr_failed_logs(self, pr: int) -> str:
        head = self.pr_head_sha(pr)
        runs = _items(self._call([
            "api", "--paginate", "--jq", ".workflow_runs",
            f"{self._path}/actions/runs?head_sha={head}&per_page=100",
        ]))
        logs = []
        for run in runs:
            if run["conclusion"] in {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}:
                output = self._call(["run", "view", str(run["id"]), "-R", self.repo, "--log-failed"])
                logs.append(output[-4000:])
        return "\n\n".join(logs)

    def merge_pr(self, pr: int, *, expected_head_sha: str, method: str = "squash") -> None:
        self._call([
            "pr", "merge", str(pr), "-R", self.repo, _merge_flag(method), "--delete-branch",
            "--match-head-commit", expected_head_sha,
        ])
