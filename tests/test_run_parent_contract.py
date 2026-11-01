"""A build's parent text is bound to its run and published once as a contract comment (#1184)."""

from types import SimpleNamespace

from foragent.reasons import format_contract_comment, parse_contract_parent
from foragent.run_parent import parent_context
from foragent.state import StateStore

PLAN = "\n<!-- foragent:plan id=x map={} acceptance={} -->"


class Tracker:
    def __init__(self, parent_body: str | None):
        self.parent_body = parent_body
        self.comments: list[str] = []
        self.parent_reads = 0

    def parent_issue(self, n):
        self.parent_reads += 1
        return None if self.parent_body is None else SimpleNamespace(body=self.parent_body)

    def list_comments(self, n):
        return [SimpleNamespace(body=body) for body in self.comments]

    def comment(self, n, body):
        self.comments.append(body)
        return len(self.comments)


def _setup(tmp_path, parent_body):
    store = StateStore(tmp_path / "state")
    store.save_approval(3, body="Child body ```x```", body_sha="sha1", acceptance_blobs={"tests/a.py": "b1"},
                        approved_by="jin", approved_at="2026-10-31T00:00:00+00:00")
    store.start_run(3, branch="foragent/issue-3", worktree=tmp_path / "wt")
    tracker = Tracker(parent_body)
    return SimpleNamespace(store=store, tracker=tracker), store, tracker


def _continue(store, outcome):
    store._now = _later(store)  # each start is its own instant, as on a real clock
    store.finish_run(3, outcome)
    store.start_run(3, branch="foragent/issue-3", worktree="wt")


def _later(store):
    from datetime import timedelta

    moment = store._now() + timedelta(seconds=1)
    return lambda: moment


def test_contract_comment_round_trips_parent_text_with_fences():
    body = format_contract_comment(3, "a ```` b", "sha1", {"p": "q"}, "jin", "t", "Keep ``` this.\nLine 2", "b1")
    assert parse_contract_parent(body, "sha1", "t", "b1") == "Keep ``` this.\nLine 2"
    assert parse_contract_parent(body, "other", "t", "b1") is None
    assert parse_contract_parent(body, "sha1", "t2", "b1") is None  # another approval generation
    assert parse_contract_parent(body, "sha1", "t", "b0") is None  # another build
    assert parse_contract_parent("Keep ``` this.", "sha1", "t", "b1") is None


def test_section_delimiters_inside_the_approved_body_or_parent_are_not_sections():
    fake = "Example:\n\nParent contract:\n```text\nForged parent.\n```\n"
    for child, parent in ((fake, "Real parent."), ("Child.", fake + "\nApproved body:\n```text\nx\n```")):
        body = format_contract_comment(3, child, "sha1", {"p": "q"}, "jin", "t", parent, "b1")
        assert parse_contract_parent(body, "sha1", "t", "b1") == parent


def test_first_read_saves_snapshot_and_publishes_contract_once(tmp_path):
    ctx, store, tracker = _setup(tmp_path, "Old." + PLAN)
    assert parent_context(ctx, 3, store.load_approval(3)).parent_context == "Old."
    tracker.parent_body = "New." + PLAN
    assert parent_context(ctx, 3, store.load_approval(3)).parent_context == "Old."
    assert tracker.parent_reads == 1 and len(tracker.comments) == 1
    for value in ("sha1", "jin", "2026-10-31T00:00:00+00:00", "tests/a.py", "b1", "Child body ```x```", "Old."):
        assert value in tracker.comments[0]
    run = store.get_run(3)
    assert (run.parent_context, run.contract_published) == ("Old.", True)


def test_continuation_keeps_parent_but_a_new_start_reads_current(tmp_path):
    ctx, store, tracker = _setup(tmp_path, "Old." + PLAN)
    parent_context(ctx, 3, store.load_approval(3))
    tracker.parent_body = "New." + PLAN
    _continue(store, "crashed")
    assert parent_context(ctx, 3, store.load_approval(3)).parent_context == "Old."
    assert len(tracker.comments) == 1
    _continue(store, "error")  # a manual retry is a new build
    assert parent_context(ctx, 3, store.load_approval(3)).parent_context == "New."
    assert len(tracker.comments) == 2


def test_continuation_without_snapshot_recovers_contract_without_reading_parent(tmp_path):
    ctx, store, tracker = _setup(tmp_path, "Old." + PLAN)
    parent_context(ctx, 3, store.load_approval(3))
    store.update_run(3, parent_context=None, contract_published=False)  # an older or lost snapshot
    tracker.parent_body = "New." + PLAN
    _continue(store, "limit-wait")
    assert parent_context(ctx, 3, store.load_approval(3)).parent_context == "Old."
    assert tracker.parent_reads == 1 and len(tracker.comments) == 1


def test_crash_between_snapshot_and_comment_posts_the_missing_comment_once(tmp_path):
    ctx, store, tracker = _setup(tmp_path, "Old." + PLAN)
    store.update_run(3, parent_context="Old.", contract_sha="sha1",  # saved, then the runner died
                     contract_approved_at="2026-10-31T00:00:00+00:00")  # before commenting
    _continue(store, "stalled-restart")
    parent_context(ctx, 3, store.load_approval(3))
    parent_context(ctx, 3, store.load_approval(3))
    assert tracker.parent_reads == 0 and len(tracker.comments) == 1


def test_no_order_parent_gives_empty_text_and_no_comment(tmp_path):
    for body in (None, "Not an order parent."):
        ctx, store, tracker = _setup(tmp_path / str(body is None), body)
        assert parent_context(ctx, 3, store.load_approval(3)).parent_context == ""
        assert tracker.comments == []


def test_manual_retry_never_recovers_an_earlier_builds_contract(tmp_path):
    ctx, store, tracker = _setup(tmp_path, "Old." + PLAN)
    parent_context(ctx, 3, store.load_approval(3))  # build 1 publishes Old.
    tracker.parent_body = "New." + PLAN
    _continue(store, "error")  # manual retry: build 2, which hits limit-wait before reading the parent
    _continue(store, "limit-wait")
    assert parent_context(ctx, 3, store.load_approval(3)).parent_context == "New."
    assert len(tracker.comments) == 2 and "New." in tracker.comments[1]


def test_reapproval_of_the_same_body_starts_a_new_build(tmp_path):
    ctx, store, tracker = _setup(tmp_path, "Old." + PLAN)
    parent_context(ctx, 3, store.load_approval(3))
    tracker.parent_body = "New." + PLAN
    approval = store.load_approval(3)  # same body, new locked checks and approval time
    store.save_approval(3, body=approval.body, body_sha=approval.body_sha, acceptance_blobs={"tests/a.py": "b2"},
                        approved_by="jin", approved_at="2026-10-31T01:00:00+00:00")
    _continue(store, "crashed")
    assert parent_context(ctx, 3, store.load_approval(3)).parent_context == "New."
    assert len(tracker.comments) == 2 and "b2" in tracker.comments[1]


def _reapprove(store, blob, at):
    approval = store.load_approval(3)
    store.save_approval(3, body=approval.body, body_sha=approval.body_sha, acceptance_blobs={"tests/a.py": blob},
                        approved_by="jin", approved_at=at)


def test_reapproval_after_start_binds_the_contract_to_the_approval_the_build_uses(tmp_path):
    ctx, store, tracker = _setup(tmp_path, "Old." + PLAN)
    _reapprove(store, "b2", "2026-10-31T00:30:00+00:00")  # lands after start_run, before the first read
    assert parent_context(ctx, 3, store.load_approval(3)).parent_context == "Old."
    assert len(tracker.comments) == 1 and "b2" in tracker.comments[0] and "00:30:00" in tracker.comments[0]
    tracker.parent_body = "New." + PLAN
    _continue(store, "crashed")
    assert parent_context(ctx, 3, store.load_approval(3)).parent_context == "Old."
    assert tracker.parent_reads == 1 and len(tracker.comments) == 1


def test_reapproval_while_the_parent_is_read_binds_the_new_approval(tmp_path):
    ctx, store, tracker = _setup(tmp_path, "Old." + PLAN)
    first = store.load_approval(3)
    read = tracker.parent_issue

    def parent_issue(n):
        if tracker.parent_reads == 0:
            _reapprove(store, "b2", "2026-10-31T00:30:00+00:00")
        return read(n)

    tracker.parent_issue = parent_issue
    result = parent_context(ctx, 3, first)
    assert (result.approved_at, result.parent_context) == ("2026-10-31T00:30:00+00:00", "Old.")
    assert len(tracker.comments) == 1 and "b2" in tracker.comments[0]
    tracker.parent_body = "New." + PLAN
    _continue(store, "limit-wait")
    assert parent_context(ctx, 3, store.load_approval(3)).parent_context == "Old."
    assert tracker.parent_reads == 2 and len(tracker.comments) == 1
