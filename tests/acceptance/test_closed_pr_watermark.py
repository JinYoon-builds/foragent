"""Acceptance test for closed-pr-watermark. Locked by Foragent; do not edit."""

from datetime import UTC, datetime, timedelta
import json
import subprocess
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from foragent.config import Config
from foragent.state import StateStore
from foragent.tracker import GhTracker, Issue, PullRequest, RecordingTracker, TrackerError


REPO = "owner/repo"
NOW = datetime(2026, 10, 26, 9, tzinfo=UTC)


def _stamp(at):
    return at.isoformat().replace("+00:00", "Z")


def _pull(number, issue, at, *, merged=True, head=None, created_at=None):
    return {
        "number": number, "title": f"PR {number}", "body": f"Closes #{issue}",
        "head": {"ref": head or f"foragent/issue-{issue}"}, "base": {"ref": "main"},
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "merged_at": _stamp(at) if merged else None, "updated_at": _stamp(at),
        "created_at": _stamp(created_at or at - timedelta(days=30)),
    }


def _issue_json(number):
    return {
        "number": number, "title": f"Issue {number}", "body": "", "labels": [],
        "state": "closed" if number <= 6 else "open", "updated_at": _stamp(NOW),
    }


class PagedGh:
    """Serve real API pages, including the pages consumed inside gh --paginate.

    One --paginate argv reads every remaining page, as gh does; it cannot count
    as a single-page read merely because only one subprocess was requested.
    """

    def __init__(self):
        self.issues = [_issue_json(number) for number in range(1, 9)]
        # Issues 2-5 have only old merges beyond page one, so losing their merge
        # flags cannot be hidden by re-listing another PR of the same issue.
        self.closed = [
            _pull(1000 + index, 1 if index < 100 else 2 + index % 4,
                  NOW - timedelta(days=10, minutes=index))
            for index in range(250)
        ]
        self.open = [_pull(8000 + index, 7 + index, NOW, merged=False) for index in range(2)]
        self.calls = []
        self.pages = []
        self.fail_on = None

    def clear_calls(self):
        self.calls.clear()
        self.pages.clear()

    def page_numbers(self, state):
        return [page for listed_state, page in self.pages if listed_state == state]

    def __call__(self, argv, input=None):
        self.calls.append(["gh", *argv])
        assert argv[0] == "api" and input is None, argv
        for flag in ("-X", "--method"):
            assert flag not in argv or argv[argv.index(flag) + 1] == "GET", argv
        endpoint = urlsplit(next(arg for arg in argv if arg.startswith(f"repos/{REPO}/")))
        query = parse_qs(endpoint.query)
        if endpoint.path == f"repos/{REPO}/issues":
            assert query.get("state") == ["all"], argv
            rows, state = self.issues, "issues"
        elif endpoint.path == f"repos/{REPO}/pulls":
            state = query["state"][0]
            assert state in {"closed", "open"}, argv
            rows = self.closed if state == "closed" else self.open
            sort = query.get("sort", ["created"])[0]
            assert sort in {"created", "updated"}, argv
            rows = sorted(rows, key=lambda row: row[f"{sort}_at"],
                          reverse=query.get("direction", ["desc"])[0] == "desc")
        elif endpoint.path.endswith("/sub_issues"):
            return "[]"
        else:
            pytest.fail(f"Unexpected offline gh request: {argv!r}")
        size = int(query.get("per_page", ["30"])[0])
        assert 1 <= size <= 100, argv
        first = int(query.get("page", ["1"])[0])
        last = max(1, (len(rows) + size - 1) // size) if "--paginate" in argv else first
        output = []
        for page in range(first, last + 1):
            self.pages.append((state, page))
            if self.fail_on == (state, page):
                raise subprocess.CalledProcessError(
                    1, ["gh", *argv], output="\n".join(output),
                    stderr="gh: offline page failure (HTTP 502)",
                )
            output.append(json.dumps(rows[(page - 1) * size:page * size]))
        return "\n".join(output)


@pytest.fixture
def watermark_case(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name, relative in (("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"),
                           ("XDG_CACHE_HOME", ".cache")):
        monkeypatch.setenv(name, str(home / relative))

    def forbidden_gh(*args, **kwargs):
        pytest.fail("Use the injected offline gh runner")

    monkeypatch.setattr("foragent.tracker._run_gh", forbidden_gh)
    return SimpleNamespace(remote=PagedGh(), clock=[NOW], root=StateStore.for_repo(REPO).root,
                           reference=tmp_path / "full-listing-reference")


def _refresh(case):
    # Recreate both objects on every call: the watermark must survive a tick
    # process exiting and cannot live only in a GhTracker instance.
    context = SimpleNamespace(config=Config(), now=lambda: case.clock[0],
                              store=StateStore(case.root, now=lambda: case.clock[0]))
    RecordingTracker(GhTracker(REPO, run=case.remote), context).refresh_issues()
    return _rows(case)


def _rows(case):
    return {row["number"]: row for row in StateStore(case.root).local_issues()}


def _merge_facts(rows):
    return {number: (row["merged"], row.get("merged_at"), row.get("merges", {}))
            for number, row in rows.items()}


def _full_listing_facts(case):
    """Independent complete input to the same public issue-record operation."""
    issues = [Issue(row["number"], row["title"], row["body"], frozenset(), row["state"],
                    updated_at=row["updated_at"]) for row in case.remote.issues]
    closed = sorted(case.remote.closed, key=lambda row: row["updated_at"], reverse=True)
    prs = [PullRequest(row["number"], row["head"]["ref"], row["base"]["ref"],
                       row["title"], row["body"], row["merged_at"], row["html_url"])
           for row in [*(row for row in closed if row["merged_at"]), *case.remote.open]
           if row["head"]["ref"].startswith("foragent/")]
    store = StateStore(case.reference, now=lambda: case.clock[0])
    store.refresh_local_issues(issues, prs, {}, case.clock[0], complete_prs=True)
    return _merge_facts({row["number"]: row for row in store.local_issues()})


def _closed_queries(case):
    return [parse_qs(urlsplit(arg).query) for argv in case.remote.calls for arg in argv
            if arg.startswith(f"repos/{REPO}/pulls?")
            and parse_qs(urlsplit(arg).query).get("state") == ["closed"]]


def test_closed_pr_watermark_second_refresh_reads_one_page_and_keeps_all_merges(watermark_case):
    case = watermark_case
    before = _refresh(case)
    assert sum(len(row.get("merges", {})) for row in before.values()) == 250
    case.remote.closed.extend([
        _pull(2000, 1, NOW + timedelta(minutes=20)),
        _pull(2001, 6, NOW + timedelta(minutes=21)),
    ])
    case.clock[0] += timedelta(hours=1)
    case.remote.clear_calls()
    after = _refresh(case)

    for number, row in before.items():
        for pr, facts in row.get("merges", {}).items():
            assert after[number]["merges"].get(pr) == facts, f"Lost earlier merge #{pr}"
    assert after[1]["merges"]["2000"]["merged_at"] == _stamp(NOW + timedelta(minutes=20))
    assert after[6]["merges"]["2001"]["merged_at"] == _stamp(NOW + timedelta(minutes=21))
    assert all(after[number]["merged"] for number in range(1, 7))
    assert [after[number]["pr_number"] for number in (7, 8)] == [8000, 8001]
    assert case.remote.page_numbers("open") == [1], "Open PRs must still be refreshed"
    assert case.remote.page_numbers("closed") == [1], (
        "After the first successful refresh, 250 old PRs must not cause page 2/3 reads"
    )


def test_closed_pr_watermark_matches_full_listing_and_orders_by_update_time(watermark_case):
    case = watermark_case
    _refresh(case)
    # A newly visible merge just before the watermark needs the safety margin.
    # Its ancient creation date also makes sorting by creation incorrect.
    case.remote.closed.append(_pull(2000, 6, NOW - timedelta(seconds=1),
                                    created_at=NOW - timedelta(days=60)))
    # No relevant merge appears on page one. The raw page's updated times must
    # decide whether to continue, before head/merged filtering.
    case.remote.closed.extend(
        _pull(3000 + index, 6, NOW + timedelta(minutes=10 + index), merged=False,
              head=f"other/feature-{index}") for index in range(100)
    )
    case.remote.issues.extend(_issue_json(number) for number in range(100, 201))
    case.remote.open = [_pull(8000 + number, number, NOW, merged=False)
                        for number in range(100, 201)]
    case.clock[0] += timedelta(hours=3)
    case.remote.clear_calls()
    after = _refresh(case)

    assert _merge_facts(after) == _full_listing_facts(case), (
        "Incremental refresh must preserve the merged flags, latest times and every merge"
    )
    assert after[6]["merges"]["2000"]["merged_at"] == _stamp(NOW - timedelta(seconds=1))
    assert all(after[number]["pr_number"] == 8000 + number for number in range(100, 201))
    assert all(after[number]["pr_number"] is None for number in (7, 8)), "Clear vanished open PRs"
    assert case.remote.page_numbers("open") == [1, 2], "Do not watermark open PR listings"
    queries = _closed_queries(case)
    assert queries and all(query.get("sort") == ["updated"]
                           and query.get("direction") == ["desc"] for query in queries), (
        "The closed-PR cutoff requires sort=updated&direction=desc on every requested page"
    )
    assert case.remote.page_numbers("closed") == [1, 2]


def test_closed_pr_watermark_failed_page_retries_from_last_success(watermark_case):
    case = watermark_case
    before = _refresh(case)
    successful_at = StateStore(case.root).issues_observation()["as_of"]
    case.remote.closed.extend(
        _pull(3000 + index, 1, NOW + timedelta(minutes=60 + index)) for index in range(100)
    )
    missed = _pull(2000, 6, NOW + timedelta(minutes=30))
    case.remote.closed.append(missed)
    case.clock[0] += timedelta(hours=4)
    case.remote.clear_calls()
    case.remote.fail_on = ("closed", 2)
    with pytest.raises(TrackerError, match="offline page failure"):
        _refresh(case)
    assert case.remote.page_numbers("closed") == [1, 2]
    assert _rows(case) == before, "A partial listing must leave successful issue facts intact"
    assert StateStore(case.root).issues_observation()["as_of"] == successful_at

    case.remote.fail_on = None
    case.clock[0] += timedelta(hours=4)
    case.remote.clear_calls()
    after = _refresh(case)
    assert after[6]["merged"] and after[6]["merged_at"] == missed["merged_at"], (
        "Advancing the watermark on failure would skip the missed merge on page two"
    )
    assert _merge_facts(after) == _full_listing_facts(case)
    assert case.remote.page_numbers("closed") == [1, 2], (
        "Retry from the old successful watermark, then stop before the remaining old pages"
    )

    case.clock[0] += timedelta(hours=1)
    case.remote.clear_calls()
    assert _merge_facts(_refresh(case)) == _merge_facts(after)
    assert case.remote.page_numbers("closed") == [1], (
        "The successful retry must advance the watermark beyond the recovered merges"
    )


def test_closed_pr_watermark_first_run_lists_every_page_then_persists_cutoff(watermark_case):
    case = watermark_case
    assert StateStore(case.root).issues_observation()["as_of"] is None
    first = _refresh(case)
    assert case.remote.page_numbers("closed") == [1, 2, 3], (
        "Without a stored watermark even ancient PRs on the last page must be read"
    )
    assert sum(len(row.get("merges", {})) for row in first.values()) == 250
    assert _merge_facts(first) == _full_listing_facts(case)

    case.clock[0] += timedelta(hours=1)
    case.remote.clear_calls()
    second = _refresh(case)
    assert _merge_facts(second) == _merge_facts(first), "Old-only issues retain their merge facts"
    assert case.remote.page_numbers("closed") == [1], (
        "A fresh tracker must load the successful first run's watermark from local state"
    )
