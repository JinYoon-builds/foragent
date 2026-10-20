"""Issue tracking interfaces, an in-memory fake, and an offline-testable gh adapter."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import json
import re
import subprocess
from typing import Any, Literal, Protocol
from urllib.parse import quote


IssueState = Literal["open", "closed"]
CheckStatus = Literal["pass", "fail", "pending", "none"]


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    labels: frozenset[str]
    state: IssueState
    is_pull_request: bool = False


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


class Tracker(Protocol):
    def list_open_issues(self) -> list[Issue]: ...
    def get_issue(self, n: int) -> Issue: ...
    def label_events(self, n: int) -> list[LabelEvent]: ...
    def blocked_by(self, n: int) -> list[Dependency]: ...
    def add_labels(self, n: int, labels: Iterable[str]) -> None: ...
    def remove_labels(self, n: int, labels: Iterable[str]) -> None: ...
    def comment(self, n: int, body: str) -> int: ...
    def list_comments(self, n: int) -> list[Comment]: ...
    def ensure_labels(self, specs: list[LabelSpec]) -> None: ...
    def close_issue(self, n: int, comment: str | None = None) -> None: ...
    def create_pr(self, *, head: str, base: str, title: str, body: str) -> int: ...
    def pr_changed_files(self, pr: int) -> list[str]: ...
    def pr_checks(self, pr: int) -> CheckStatus: ...
    def merge_pr(self, pr: int, *, method: str = "squash") -> None: ...


class TrackerError(RuntimeError):
    """A gh operation failed; the message contains its stderr."""

    def __init__(self, message: str, *, stdout: str = "", returncode: int | None = None):
        super().__init__(message)
        self.stdout = stdout
        self.returncode = returncode


def _merge_flag(method: str) -> str:
    if method not in {"squash", "merge", "rebase"}:
        raise ValueError(f"unsupported merge method: {method}")
    return f"--{method}"


class FakeTracker:
    def __init__(self, actor: str = "foragent-bot", now: Callable[[], datetime] | None = None):
        self.actor = actor
        self._now = now or (lambda: datetime.now(UTC))
        self.issues: dict[int, Issue] = {}
        self.labels: dict[str, LabelSpec] = {}
        self.comments: dict[int, list[Comment]] = {}
        self.created_prs: list[PullRequest] = []
        self.merged_prs: list[int] = []
        self._events: dict[int, list[LabelEvent]] = {}
        self._dependencies: dict[int, list[Dependency]] = {}
        self._pr_files: dict[int, list[str]] = {}
        self._pr_checks: dict[int, CheckStatus] = {}
        self._next_comment_id = 1

    def _timestamp(self) -> str:
        return self._now().astimezone(UTC).isoformat()

    def add_issue(
        self, number: int, title: str = "", body: str = "", labels: Iterable[str] = (),
        state: IssueState = "open", is_pull_request: bool = False,
    ) -> Issue:
        issue = Issue(number, title, body, frozenset(labels), state, is_pull_request)
        self.issues[number] = issue
        return issue

    def list_open_issues(self) -> list[Issue]:
        return [issue for issue in self.issues.values() if issue.state == "open" and not issue.is_pull_request]

    def get_issue(self, n: int) -> Issue:
        return self.issues[n]

    def label_events(self, n: int) -> list[LabelEvent]:
        return sorted(self._events.get(n, []), key=lambda event: datetime.fromisoformat(event.created_at))

    def set_blocked_by(self, n: int, deps: list[Dependency]) -> None:
        self._dependencies[n] = list(deps)

    def blocked_by(self, n: int) -> list[Dependency]:
        return list(self._dependencies.get(n, []))

    def record_label(self, n: int, label: str, actor: str) -> None:
        issue = self.get_issue(n)
        self._events.setdefault(n, []).append(LabelEvent(label, "labeled", actor, self._timestamp()))
        self.issues[n] = replace(issue, labels=issue.labels | {label})

    def add_labels(self, n: int, labels: Iterable[str]) -> None:
        for label in labels:
            if label not in self.get_issue(n).labels:
                self.record_label(n, label, self.actor)

    def remove_labels(self, n: int, labels: Iterable[str]) -> None:
        for label in labels:
            issue = self.get_issue(n)
            if label in issue.labels:
                self._events.setdefault(n, []).append(LabelEvent(label, "unlabeled", self.actor, self._timestamp()))
                self.issues[n] = replace(issue, labels=issue.labels - {label})

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

    def create_pr(self, *, head: str, base: str, title: str, body: str) -> int:
        number = max([*self.issues, *(pr.number for pr in self.created_prs)], default=0) + 1
        self.created_prs.append(PullRequest(number, head, base, title, body))
        self.add_issue(number, title, body, is_pull_request=True)
        return number

    def set_pr_files(self, pr: int, files: list[str]) -> None:
        self._pr_files[pr] = list(files)

    def pr_changed_files(self, pr: int) -> list[str]:
        return list(self._pr_files.get(pr, []))

    def set_pr_checks(self, pr: int, status: CheckStatus) -> None:
        self._pr_checks[pr] = status

    def pr_checks(self, pr: int) -> CheckStatus:
        return self._pr_checks.get(pr, "none")

    def merge_pr(self, pr: int, *, method: str = "squash") -> None:
        _merge_flag(method)
        self.merged_prs.append(pr)
        self.issues[pr] = replace(self.get_issue(pr), state="closed")


def _run_gh(args: list[str], input: str | None = None) -> str:
    return subprocess.run(["gh", *args], input=input, check=True, capture_output=True, text=True).stdout


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
        data["state"], "pull_request" in data,
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
        except OSError as exc:
            raise TrackerError(str(exc)) from exc

    def list_open_issues(self) -> list[Issue]:
        data = _items(self._call(["api", "--paginate", f"{self._path}/issues?state=open&per_page=100"]))
        return [_issue(item) for item in data if "pull_request" not in item]

    def get_issue(self, n: int) -> Issue:
        return _issue(json.loads(self._call(["api", f"{self._path}/issues/{n}"])))

    def label_events(self, n: int) -> list[LabelEvent]:
        data = _items(self._call(["api", "--paginate", f"{self._path}/issues/{n}/events?per_page=100"]))
        events = [
            LabelEvent(item["label"]["name"], item["event"], item["actor"]["login"], item["created_at"])
            for item in data if item["event"] in {"labeled", "unlabeled"}
        ]
        return sorted(events, key=lambda event: datetime.fromisoformat(event.created_at))

    def blocked_by(self, n: int) -> list[Dependency]:
        data = _items(self._call(["api", "--paginate", f"{self._path}/issues/{n}/dependencies/blocked_by?per_page=100"]))
        return [Dependency(item["number"], item["state"]) for item in data]

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

    def create_pr(self, *, head: str, base: str, title: str, body: str) -> int:
        output = self._call(
            ["pr", "create", "-R", self.repo, "--head", head, "--base", base,
             "--title", title, "--body-file", "-"], input=body,
        )
        match = re.search(r"/pull/(\d+)\s*$", output)
        if match is None:
            raise TrackerError(f"cannot parse PR number: {output.strip()}")
        return int(match[1])

    def pr_changed_files(self, pr: int) -> list[str]:
        data = _items(self._call(["api", "--paginate", f"{self._path}/pulls/{pr}/files?per_page=100"]))
        return [item["filename"] for item in data]

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

    def merge_pr(self, pr: int, *, method: str = "squash") -> None:
        self._call(["pr", "merge", str(pr), "-R", self.repo, _merge_flag(method), "--delete-branch"])
