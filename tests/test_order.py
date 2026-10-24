from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
import hashlib
import json
import re
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from foragent import cli, order
from foragent.config import Config, load_config
from foragent.git import GitError, GitRepo
from foragent.issue import parse_body
from foragent.reasons import Reason, format_comment
from foragent.scheduler import TickContext, tick
from foragent.skills import SkillManager
from foragent.state import StateStore
from foragent.tracker import FakeTracker, TrackerError

from conftest import BASE_TIME, git_command


FILE = "tests/acceptance/test_login.py"

REQUIRE_CLEAN_PREREAD = order.require_clean_preread


@pytest.fixture(autouse=True)
def preread_done(monkeypatch):
    """Publication tests here assume a clean preread; the gate is tested on its own."""
    monkeypatch.setattr(order, "require_clean_preread", lambda plan, root, config, items: None)


class PublishingGit:
    """Record authoring operations, keeping all real Git writes offline."""
    def __init__(self, root, events):
        self.root, self.events = root, events
        self.saved = {}
        self.branch = "main"

    def current_branch(self):
        return self.branch

    def ensure_tracked(self, paths):
        pass

    def head_sha(self):
        return "published"

    def blob_shas(self, ref, paths):
        assert ref == "published"
        return {path: self._blob(self.saved[path]) if path in self.saved else "missing" for path in paths}

    @staticmethod
    def _blob(content):
        data = content.encode()
        return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()

    def uncommitted_paths(self, paths):
        return [path for path in paths if self.saved.get(path) != (self.root / path).read_text()]

    def commit_paths(self, paths, title):
        contents = {path: (self.root / path).read_text() for path in paths}
        self.events.append(("commit", title, contents))
        self.saved.update(contents)

    def push_base(self, branch):
        self.events.append(("push", branch))

    @contextmanager
    def publish_lock(self):
        yield

    def update_base(self, branch):
        pass


class PublishingTracker(FakeTracker):
    def __init__(self, events):
        super().__init__(actor="human")
        self.events = events

    def create_issue(self, title, body, labels):
        self.events.append(("issue", title))
        return super().create_issue(title, body, labels)


@pytest.fixture
def publishing(tmp_path):
    (tmp_path / ".foragent.yml").write_text("lanes: {api: {paths: ['api/**'], agent: fa-api}}\n")
    events = []
    return tmp_path, load_config(tmp_path), PublishingTracker(events), PublishingGit(tmp_path, events), events


@pytest.fixture
def offline_publication(git_history):
    """Exercise real Git files and trees while emulating commit/push commands."""
    root, calls = git_history.clone, []
    (root / ".foragent.yml").write_text("lanes: {api: {paths: ['api/**'], agent: fa-api}}\n")

    def run(args):
        calls.append(args)
        if "commit" in args:
            parent = git_command(root, "rev-parse", "HEAD")
            stream = ["commit refs/heads/main\n", "committer Offline Tests <tests@example.com> 1792483200 +0000\n",
                      "data 7\nPublish\n", f"from {parent}\n"]
            for name in args[args.index("--") + 1:]:
                if name == ":!CLAUDE.local.md":
                    continue
                entry = git_command(root, "ls-files", "--stage", "-z", "--", name)
                metadata, filename = entry.rstrip("\0").split("\t", 1)
                mode, sha, stage = metadata.split()
                assert stage == "0"
                stream.append(f"M {mode} {sha} {json.dumps(filename)}\n")
            git_command(root, "fast-import", "--quiet", input="".join(stream) + "\ndone\n")
            return ""
        if args[0] == "push":
            source, destination = args[-1].split(":")
            git_command(git_history.origin, "fetch", str(root), f"{source}:{destination}")
            return ""
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout

    git = GitRepo(root, run)
    return root, load_config(root), FakeTracker(actor="human"), git, calls, git_history.origin


@pytest.fixture
def plan():
    return {"id": "2026-10-20-login", "title": "Login MVP", "summary": "로그인", "spec_markdown": "# Login\n",
            "new_lanes": [], "issues": [
                {"key": "api", "title": "Login API", "goal": "Implement login.", "out_of_scope": "Payments.",
                 "done_when": [f"uv run pytest -q {FILE}", "true"], "checklist": ["Looks right"],
                 "lane": "api", "size": "m", "blocked_by": [], "acceptance_files": {FILE: "assert True\n"}},
                {"key": "client", "title": "Login client", "goal": "Call login.", "out_of_scope": "Payments.", "done_when": ["true"],
                 "checklist": [], "lane": "main", "size": "s", "blocked_by": ["api"], "acceptance_files": {}},
            ], "edits": []}


def publish(case, plan, **kwargs):
    root, config, tracker, git, _ = case
    return order.publish(plan, root, config, tracker, repo="owner/repo", git=git, **kwargs)


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
@pytest.mark.parametrize("dry_run", [False, True])
def test_interactive_requires_boolean_before_writes(publishing, plan, value, dry_run):
    plan["issues"][0]["interactive"] = value
    with pytest.raises(order.PlanError, match=r"issues\[0\]\.interactive must be a boolean"):
        publish(publishing, plan, dry_run=dry_run)
    assert publishing[4] == []
    assert publishing[2].issues == {} and publishing[2].labels == {}


@pytest.mark.parametrize("disable", [False, None])
def test_interactive_republication_syncs_label_without_duplicate_children(publishing, plan, disable):
    tracker = publishing[2]
    plan["issues"][0]["interactive"] = True
    publish(publishing, plan, approve=True)
    assert "interactive" in tracker.labels
    assert "interactive" in tracker.get_issue(2).labels
    assert "interactive" not in tracker.get_issue(3).labels
    tracker.add_labels(2, ["ready", "custom"])
    prior_events = tracker.label_events(2)
    publish(publishing, plan)
    assert tracker.label_events(2) == prior_events
    if disable is None:
        del plan["issues"][0]["interactive"]
    else:
        plan["issues"][0]["interactive"] = disable
    preview = publish(publishing, plan, dry_run=True)
    assert preview.rows[0]["interactive"] is False
    assert "interactive" in tracker.get_issue(2).labels
    publish(publishing, plan)
    assert len(tracker.issues) == 3
    assert tracker.get_issue(2).labels == {"lane:api", "size:m", "custom"}
    plan["issues"][0]["interactive"] = True
    publish(publishing, plan)
    assert "interactive" in tracker.get_issue(2).labels


def test_publish_order_contract_labels_native_links_and_summary(publishing, plan):
    root, _, tracker, git, events = publishing
    report = publish(publishing, plan, approve=True)
    assert [event[0] for event in events] == ["commit", "push", "issue", "issue", "issue"]
    assert events[0][1] == "Foragent order 2026-10-20-login: acceptance tests and spec"
    assert set(events[0][2]) == {FILE, "docs/specs/2026-10-20-login.md"}
    assert (root / FILE).read_text() == "assert True\n"
    assert report.parent == 1
    expected = (
        "<!-- foragent:v1 -->\n## Goal\nImplement login.\n\n## Out of scope\nPayments.\n\n## Done when\n"
        f"```sh\nuv run pytest -q {FILE}\n```\n```sh\ntrue\n```\n- [ ] Looks right\n"
        f"[{FILE}](https://github.com/owner/repo/blob/main/{FILE})\n\n"
        "<!-- foragent:plan-child id=2026-10-20-login key=api -->\n"
    )
    assert tracker.get_issue(2).body == expected
    assert parse_body(expected).done_when_commands == plan["issues"][0]["done_when"]
    assert tracker.get_issue(2).labels == {"lane:api", "size:m", "approved"}
    assert tracker.get_issue(3).labels == {"size:s", "approved"}
    assert tracker.parent_of(2) == tracker.parent_of(3) == 1
    assert [dep.number for dep in tracker.blocked_by(3)] == [2]
    assert {"size:s", "size:m", "size:l", "lane:api"} <= tracker.labels.keys()
    assert 'map={"api": 2, "client": 3}' in tracker.get_issue(1).body
    assert report.summary().index("lane:api") < report.summary().index("lane:main")
    assert "| 3 | Login client | s | 2 | yes |" in report.summary()


@pytest.mark.parametrize("offline", [False, True])
def test_publish_projects_are_idempotent_and_failure_does_not_stop_publication(publishing, plan, caplog, offline):
    from foragent import board
    from test_board import item_values, snapshot

    root, config, tracker, git, events = publishing
    project_id = board.ensure(tracker, "owner/repo", config.lanes)
    config = replace(config, board=project_id)
    if offline:
        def unavailable(*args):
            raise OSError("offline project")
        tracker.graphql = unavailable
    for _ in range(2):
        report = order.publish(plan, root, config, tracker, repo="owner/repo", git=git, approve=True)
        assert report.parent == 1
    assert len(tracker.issues) == 3 and tracker.get_issue(2).labels == {"lane:api", "size:m", "approved"}
    if offline:
        assert "Could not add issue" in caplog.text and "Could not set board Status" in caplog.text
    else:
        assert len(snapshot(tracker, project_id)["items"]["nodes"]) == 3
        assert item_values(tracker, project_id, 2) == {"Lane": "api", "Size": "m", "Status": "🌰 Up next"}
        assert item_values(tracker, project_id, 3) == {"Lane": "main", "Size": "s", "Status": "🌰 Up next"}


@pytest.mark.parametrize("options,approved", [
    ({}, set()), ({"approve": True}, {2, 3}),
    ({"approve_keys": ["api"]}, {2}), ({"approve_numbers": [3]}, {3}),
])
def test_publish_kicks_once_after_requested_approvals(publishing, plan, monkeypatch, options, approved):
    root, _, tracker, _, _ = publishing
    calls = []

    def kick(repo_root):
        assert repo_root == root
        assert {n for n in (2, 3) if "approved" in tracker.get_issue(n).labels} == approved
        calls.append(repo_root)

    monkeypatch.setattr(order, "kick_tick", kick)
    publish(publishing, plan, **options)
    assert calls == ([root] if approved else [])
    publish(publishing, plan, **options)
    assert calls == ([root, root] if approved else [])


def test_dry_run_has_no_writes(publishing, plan, kicks):
    root, _, tracker, _, events = publishing
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    plan["new_lanes"] = [{"name": "web", "paths": ["web/**"], "skills": []}]
    plan["issues"][1]["lane"] = "web"
    report = publish(publishing, plan, dry_run=True, approve_keys=["client"])
    assert report.dry_run and "dry run" in report.summary()
    assert events == [] and tracker.issues == {} and tracker.labels == {}
    assert kicks == []
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("kind", ["issues", "edits"])
def test_publish_preserves_issue_files_as_metadata(publishing, plan, kind):
    paths = ["src/new module.py", "skills/**", "src/[ab].py", "docs/설계.md"]
    if kind == "edits":
        publish(publishing, plan)
        item = {"number": 2, "goal": "Updated login.", "out_of_scope": "Payments.",
                "done_when": ["true"], "checklist": ["Looks right"], "files": paths}
        plan["id"] = "2026-10-21-login-update"
        plan["issues"], plan["edits"] = [], [item]
    else:
        item = plan["issues"][0]
        item["files"] = paths
    report = publish(publishing, plan)
    body = publishing[2].get_issue(2).body
    assert "## Files\n" + "\n".join(f"- {path}" for path in paths) in body
    parsed = parse_body(body)
    assert parsed.files == paths
    assert parsed.goal == item["goal"] and parsed.out_of_scope == item["out_of_scope"]
    assert parsed.done_when_commands == item["done_when"]
    assert [check.text for check in parsed.done_when_checklist] == item["checklist"]
    assert not set(paths) & set(report.paths)
    assert all(not (publishing[0] / path).exists() for path in paths)


@pytest.mark.parametrize("kind", ["issues", "edits"])
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("files", [None, ["../outside.py"], ["src/../outside.py"],
                                   ["/outside.py"], ["src\\file.py"], ["src/\0file.py"],
                                   ["src/file.py\n## Goal\nWrong goal"]])
def test_invalid_issue_files_are_rejected_before_writes(publishing, plan, kind, dry_run, files):
    if kind == "edits":
        plan["edits"] = [{"number": 9, "goal": "Updated goal", "out_of_scope": "Payments.",
                          "done_when": ["true"], "files": files}]
    else:
        plan["issues"][0]["files"] = files
    with pytest.raises(order.PlanError, match=rf"{kind}\[\d+\]\.files"):
        publish(publishing, plan, dry_run=dry_run)
    assert publishing[-1] == [] and publishing[2].issues == {}
    assert not (publishing[0] / "docs").exists()


def test_files_section_keeps_first_list_and_respects_other_sections():
    parsed = parse_body(
        "## Goal\nChange the code.\n## Files\n- src/file.py\n* skills/**\n"
        "## Notes\n- excluded.py\n## Files\n- also-excluded.py\n"
        "## Done when\n```sh\n## Files\n- command-not-path\n```\n"
    )
    assert parsed.files == ["src/file.py", "skills/**"]
    assert parsed.done_when_commands == ["## Files\n- command-not-path"]


@pytest.mark.parametrize("mutate,error", [
    (lambda p: p.update(extra=True), "unknown fields"),
    (lambda p: p.update(id="../escape"), "plan.id"),
    (lambda p: p.update(issues="oops"), "issues.*array"),
    (lambda p: p["issues"][0].update(done_when=[]), "done_when"),
    (lambda p: p["issues"][0].update(done_when=[" "]), "done_when"),
    (lambda p: p["issues"][0].update(size="xl"), "size"),
    (lambda p: p["issues"][0].update(lane="missing"), "unknown lane"),
    (lambda p: p["issues"][0].update(blocked_by=["missing"]), "unknown blocked_by"),
    (lambda p: p["issues"][0].update(blocked_by=["client"]), "cycle"),
    (lambda p: p["issues"][1].update(key="api"), "duplicate issue key"),
    (lambda p: p["issues"][0].update(acceptance_files={"elsewhere.py": "bad"}), "must be under"),
    (lambda p: p["issues"][0].update(acceptance_files={"tests/acceptance/../bad": "bad"}), "relative path"),
    (lambda p: p["issues"][0].update(done_when=["pytest tests/acceptance/missing.py"]), "missing acceptance"),
    (lambda p: p["issues"][0].update(done_when=["pytest 'tests/acceptance/missing.py::test_x[a=b]'"]), "missing acceptance"),
    (lambda p: p["issues"][0].update(done_when=["pytest --file=tests/acceptance/missing.py; true"]), "missing acceptance"),
    (lambda p: p["issues"][0].update(checklist=["a\nb"]), "single line"),
    (lambda p: p["issues"][0].update(done_when=["true\n```\nfalse"]), "code fences"),
    (lambda p: p.update(edits=[{"number": True, "goal": "g", "out_of_scope": "Payments.", "done_when": ["true"]}]), "number"),
])
def test_validation_errors_precede_writes(publishing, plan, mutate, error):
    mutate(plan)
    with pytest.raises(order.PlanError, match=error):
        publish(publishing, plan)
    assert publishing[-1] == [] and publishing[2].issues == {}


@pytest.mark.parametrize("kind", ["issues", "edits"])
@pytest.mark.parametrize("value", ["", "   ", "\n\t "])
@pytest.mark.parametrize("dry_run", [False, True])
def test_empty_out_of_scope_is_rejected_before_writes(publishing, plan, kind, value, dry_run):
    if kind == "edits":
        plan["edits"] = [{"number": 9, "goal": "Fixed goal", "out_of_scope": value, "done_when": ["true"]}]
    else:
        plan["issues"][1]["out_of_scope"] = value
    with pytest.raises(order.PlanError, match=rf"{kind}\[\d+\]\.out_of_scope must be a nonempty string"):
        publish(publishing, plan, dry_run=dry_run)
    root, _, tracker, _, events = publishing
    assert events == [] and tracker.issues == {}
    assert not (root / "docs").exists()
    assert not (root / "tests").exists()


def test_acceptance_symlink_escape_is_rejected(publishing, plan, tmp_path):
    root = publishing[0]
    (root / "outside").mkdir()
    (root / "tests").mkdir()
    (root / "tests/acceptance").symlink_to(root / "outside", target_is_directory=True)
    # A configured directory resolving within the repository is allowed; an escaping file is not.
    (root / "outside/test_login.py").symlink_to(root.parent / "not-in-repo")
    with pytest.raises(order.PlanError, match="escapes repository"):
        publish(publishing, plan)


def test_existing_acceptance_file_is_published_and_linked(publishing, plan):
    root = publishing[0]
    (root / FILE).parent.mkdir(parents=True)
    (root / FILE).write_text("existing\n")
    plan["issues"][0]["acceptance_files"] = {}
    publish(publishing, plan)
    assert FILE in publishing[-1][0][2]
    assert f"[{FILE}]" in publishing[2].get_issue(2).body


@pytest.mark.parametrize("preexisting", [False, True])
def test_ignored_acceptance_is_in_remote_commit_before_issues(offline_publication, plan, preexisting):
    root, config, tracker, git, calls, origin = offline_publication
    (root / ".gitignore").write_text("tests/acceptance/\n")
    if preexisting:
        (root / FILE).write_text(plan["issues"][0]["acceptance_files"].pop(FILE))
    create = tracker.create_issue

    def create_after_publication(*args):
        assert git_command(origin, "show", f"main:{FILE}") == "assert True"
        return create(*args)

    tracker.create_issue = create_after_publication
    order.publish(plan, root, config, tracker, repo="owner/repo", git=git)
    assert ["add", "-f", "--", f"docs/specs/{plan['id']}.md", FILE] in calls
    assert len(tracker.issues) == 3


def test_missing_file_in_published_commit_prevents_push_and_issues(publishing, plan, monkeypatch):
    git = publishing[3]
    monkeypatch.setattr(git, "commit_paths", lambda *args: None)
    with pytest.raises(order.PlanError, match="published commit is missing files"):
        publish(publishing, plan)
    assert publishing[2].issues == {} and publishing[-1] == []


def test_force_add_failure_prevents_push_and_issues(publishing, plan, monkeypatch):
    def fail(paths):
        raise GitError("published files are not tracked")

    monkeypatch.setattr(publishing[3], "ensure_tracked", fail)
    with pytest.raises(GitError, match="not tracked"):
        publish(publishing, plan)
    assert publishing[2].issues == {} and publishing[-1] == []


def test_rerun_updates_parent_children_and_closed_issues_without_duplicates(publishing, plan):
    tracker = publishing[2]
    publish(publishing, plan, approve=True)
    tracker.close_issue(1)
    tracker.close_issue(3)
    publish(publishing, plan, approve=True)
    assert len(tracker.issues) == 3
    assert len([event for event in publishing[-1] if event[0] == "commit"]) == 1
    assert len(tracker.blocked_by(3)) == 1
    plan["title"] = "Updated epic"
    plan["issues"][0]["title"] = "Updated task"
    plan["issues"][0]["goal"] = "Updated goal"
    publish(publishing, plan)
    assert tracker.get_issue(1).title == "Updated epic"
    assert "Updated task" in tracker.get_issue(1).body
    assert tracker.get_issue(2).title == "Updated task"
    assert "approved" not in tracker.get_issue(2).labels
    assert "- [x] #3" in tracker.get_issue(1).body


@pytest.mark.parametrize("options,approved", [
    ({}, False), ({"approve": True}, True), ({"approve_keys": ["api"]}, True),
    ({"approve_numbers": [2]}, True),
])
@pytest.mark.parametrize("already_committed", [False, True])
def test_preexisting_acceptance_change_uses_persisted_blobs(publishing, plan, options, approved, already_committed):
    root, _, tracker, git, _ = publishing
    publish(publishing, plan, approve=True)
    first_events = tracker.label_events(2)
    other_events = tracker.label_events(3)
    tracker.add_labels(2, ["ready"])
    content = "assert 1 == 1\n"
    (root / FILE).write_text(content)
    plan["issues"][0]["acceptance_files"][FILE] = content
    if already_committed:
        git.saved[FILE] = content
    publish(publishing, plan, **options)
    assert ("approved" in tracker.get_issue(2).labels) == approved
    assert "ready" not in tracker.get_issue(2).labels
    events = [event for event in tracker.label_events(2) if event.label == "approved"]
    assert [event.action for event in events] == ["labeled", "unlabeled", *(["labeled"] if approved else [])]
    if approved:
        assert events[-1].created_at != first_events[-1].created_at
    assert tracker.label_events(3) == other_events
    snapshot = json.loads(re.search(r"acceptance=(\{[^\n]*\}) -->", tracker.get_issue(1).body)[1])
    assert snapshot == {"2": {FILE: git.blob_shas(git.head_sha(), [FILE])[FILE]}, "3": {}}
    prior_events = tracker.label_events(2)
    publish(publishing, plan, **options)
    assert tracker.label_events(2) == prior_events


def test_unchanged_republication_preserves_approval_and_ready(publishing, plan):
    tracker = publishing[2]
    publish(publishing, plan, approve=True)
    tracker.add_labels(2, ["ready"])
    events = tracker.label_events(2)
    publish(publishing, plan, approve=True)
    assert tracker.label_events(2) == events
    assert {"approved", "ready"} <= tracker.get_issue(2).labels


def test_reapproval_of_preexisting_acceptance_change_passes_gate(offline_publication, plan):
    root, config, tracker, git, _, _ = offline_publication
    config = replace(config, approvers=["human"])
    tracker._now = lambda: BASE_TIME + timedelta(hours=1)
    plan["issues"][0]["done_when"] = [f"pytest '{FILE}::test_login[token=valid]'"]
    order.publish(plan, root, config, tracker, repo="owner/repo", git=git, approve=True)
    ctx = TickContext(repo="owner/repo", repo_root=root, tracker=tracker, store=StateStore(root / ".foragent/state"),
                      config=config, git=git, now=lambda: tracker._now(), launcher=lambda number: None,
                      start_runs=False, runner_process_exists=lambda number: False)
    tick(ctx)
    original = ctx.store.load_approval(2)
    assert original.acceptance_blobs == {FILE: git.blob_sha("origin/main", FILE)}
    assert "ready" in tracker.get_issue(2).labels
    tracker._now = lambda: BASE_TIME + timedelta(hours=2)
    content = "assert 1 == 1\n"
    (root / FILE).write_text(content)
    plan["issues"][0]["acceptance_files"][FILE] = content
    order.publish(plan, root, config, tracker, repo="owner/repo", git=git, approve=True)
    tick(ctx)
    renewed = ctx.store.load_approval(2)
    assert renewed.approved_at != original.approved_at
    assert renewed.acceptance_blobs != original.acceptance_blobs
    assert renewed.acceptance_blobs == {FILE: git.blob_sha("origin/main", FILE)}
    assert {"approved", "ready"} <= tracker.get_issue(2).labels


def test_legacy_plan_map_migrates_without_duplicate_issues(publishing, plan):
    tracker = publishing[2]
    publish(publishing, plan, approve=True)
    parent = tracker.get_issue(1)
    tracker.update_issue(1, title=parent.title, body=re.sub(r" acceptance=\{[^\n]*\}(?= -->)", "", parent.body))
    publish(publishing, plan, approve=True)
    assert len(tracker.issues) == 3
    assert "acceptance=" in tracker.get_issue(1).body
    assert [event.action for event in tracker.label_events(2) if event.label == "approved"] == ["labeled", "unlabeled", "labeled"]


@pytest.mark.parametrize("blockers", [[], ["other"]])
def test_republication_reconciles_removed_and_replaced_dependencies(publishing, plan, blockers):
    tracker = publishing[2]
    other = {**deepcopy(plan["issues"][1]), "key": "other", "title": "Other", "blocked_by": []}
    plan["issues"].append(other)
    publish(publishing, plan)
    tracker.add_blocked_by(3, 4)
    plan["issues"][1]["blocked_by"] = blockers
    report = publish(publishing, plan)
    expected = [4] if blockers else []
    assert [dep.number for dep in tracker.blocked_by(3)] == expected
    assert report.rows[1]["blocked_by"] == expected
    publish(publishing, plan)
    assert [dep.number for dep in tracker.blocked_by(3)] == expected


def test_partial_failure_before_parent_map_update_recovers_child(publishing, plan, monkeypatch):
    tracker = publishing[2]
    update = tracker.update_issue
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise TrackerError("offline failure")
        update(*args, **kwargs)

    monkeypatch.setattr(tracker, "update_issue", fail)
    with pytest.raises(TrackerError):
        publish(publishing, plan)
    assert len(tracker.issues) == 2
    publish(publishing, plan)
    assert len(tracker.issues) == 3 and tracker.parent_of(2) == 1


def test_push_failure_prevents_any_issue_creation(publishing, plan, monkeypatch):
    def fail(branch):
        raise GitError("push failed")

    monkeypatch.setattr(publishing[3], "push_base", fail)
    with pytest.raises(GitError, match="push failed"):
        publish(publishing, plan)
    assert publishing[2].issues == {}


@pytest.mark.parametrize("options,approved", [
    ({}, set()), ({"approve": True}, {5, 6, 3}),
    ({"approve_keys": ["client"]}, {6}), ({"approve_numbers": [3]}, {3}),
    ({"approve_keys": ["api"], "approve_numbers": [3]}, {5, 3}),
])
def test_approval_variants_and_edits_invalidate_approval(publishing, plan, options, approved):
    tracker = publishing[2]
    tracker.add_issue(3, "Keep this title", "old", ["approved", "needs-human", "ready", "size:l"])
    plan["edits"] = [{"number": 3, "goal": "New goal", "out_of_scope": "Payments.", "done_when": ["true"],
                      "checklist": [], "acceptance_files": {"tests/acceptance/edited.py": "edited\n"}}]
    publish(publishing, plan, **options)
    # Parent is #4, children are #5 and #6 because #3 existed before publication.
    assert {n for n in (3, 5, 6) if "approved" in tracker.get_issue(n).labels} == approved
    assert tracker.get_issue(3).title == "Keep this title"
    assert not {"needs-human", "ready"} & tracker.get_issue(3).labels
    assert tracker.list_comments(3)[0].body.startswith("<!-- foragent:edited -->\nUpdated by order from your decision; needs approval.")
    assert "tests/acceptance/edited.py" in publishing[-1][0][2]
    publish(publishing, plan, **options)
    assert len(tracker.list_comments(3)) == 1


def test_approval_selector_validation_is_read_only(publishing, plan):
    publishing[2].add_issue(123, "Not part of this plan", "")
    for options in ({"approve_keys": ["typo"]}, {"approve_numbers": [123]}):
        with pytest.raises(order.PlanError, match="approve"):
            publish(publishing, plan, **options)
    assert publishing[-1] == []


def test_approval_by_number_also_resolves_new_children(publishing, plan):
    publish(publishing, plan, approve_numbers=[3])
    assert "approved" not in publishing[2].get_issue(2).labels
    assert "approved" in publishing[2].get_issue(3).labels


def test_json_duplicate_fields_are_rejected(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text('{"id":"one", "id":"two"}')
    with pytest.raises(order.PlanError, match="duplicate JSON field: id"):
        order.read_plan(path)


def test_publish_new_lane_is_created_once(publishing, plan):
    plan["new_lanes"] = [{"name": "web", "paths": ["web/**"], "skills": []}]
    plan["issues"][1]["lane"] = "web"
    publish(publishing, plan)
    root = publishing[0]
    assert (root / ".claude/agents/fa-web.md").exists()
    assert load_config(root).lanes["web"]["paths"] == ["web/**"]
    updated = (root, load_config(root), *publishing[2:])
    publish(updated, plan)
    assert [event[1] for event in publishing[-1] if event[0] == "commit"].count("Foragent lane web") == 1


def test_interrupted_lane_commit_is_completed_before_issues(publishing, plan, monkeypatch):
    root, _, tracker, git, events = publishing
    plan["new_lanes"] = [{"name": "web", "paths": ["web/**"], "skills": []}]
    original = git.commit_paths

    def fail(*args):
        raise GitError("interrupted")

    monkeypatch.setattr(git, "commit_paths", fail)
    with pytest.raises(GitError):
        publish(publishing, plan)
    assert tracker.issues == {} and (root / ".claude/agents/fa-web.md").is_file()
    monkeypatch.setattr(git, "commit_paths", original)
    publish((root, load_config(root), tracker, git, events), plan)
    assert events[0][1] == "Foragent lane web"
    assert set(events[0][2]) == {".foragent.yml", ".claude/agents/fa-web.md"}


def test_edit_with_changed_acceptance_invalidates_approval_on_retry(publishing, plan):
    tracker = publishing[2]
    tracker.add_issue(9, "Existing", "old", ["approved", "needs-human"])
    edit = {"number": 9, "goal": "Fixed goal", "out_of_scope": "Payments.", "done_when": ["true"],
            "acceptance_files": {"tests/acceptance/edit.py": "first\n"}}
    plan["edits"] = [edit]
    publish(publishing, plan, approve=True)
    edit["acceptance_files"]["tests/acceptance/edit.py"] = "second\n"
    # Simulate a push completing before issue updates, followed by a process interruption.
    (publishing[0] / "tests/acceptance/edit.py").write_text("second\n")
    publish(publishing, plan)
    assert "approved" not in tracker.get_issue(9).labels
    assert len(tracker.list_comments(9)) == 2


def test_edit_reverting_to_a_prior_revision_still_refreshes_approval(publishing, plan):
    root, _, tracker, git, _ = publishing
    tracker.add_issue(9, "Existing", "old", ["approved", "needs-human"])
    name = "tests/acceptance/edit.py"
    edit = {"number": 9, "goal": "Fixed goal", "out_of_scope": "Payments.", "done_when": ["true"],
            "acceptance_files": {name: "first\n"}}
    plan["edits"] = [edit]
    publish(publishing, plan, approve=True)
    edit["acceptance_files"][name] = "second\n"
    publish(publishing, plan, approve=True)
    events = tracker.label_events(9)
    edit["acceptance_files"][name] = "first\n"
    (root / name).write_text("first\n")
    git.saved[name] = "first\n"
    publish(publishing, plan, approve_numbers=[9])
    assert [event.action for event in tracker.label_events(9)[len(events):]] == ["unlabeled", "labeled"]
    assert len(tracker.list_comments(9)) == 2
    snapshot = json.loads(re.search(r"acceptance=(\{[^\n]*\}) -->", tracker.get_issue(10).body)[1])
    assert snapshot["9"] == {name: git.blob_shas(git.head_sha(), [name])[name]}


@pytest.mark.parametrize("interrupted", [False, True])
def test_lane_skills_stay_local_and_copy_into_runner_worktree(offline_publication, plan, monkeypatch, interrupted):
    from foragent.skills import copy_lane_skills

    root, config, tracker, git, calls, _ = offline_publication
    library = root.parent / "library"
    skill = library / "skills/testing-strategy"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: testing-strategy\ndescription: Tests\n---\nUse tests.\n")
    (skill / "references/example.md").write_text("Useful example\n")
    (library / "LICENSE").write_text("SPDX-License-Identifier: MIT\nMIT License\n")
    manager = SkillManager(Config(skill_libraries=[{"name": "local", "repo": "local", "path": str(library)}]),
                           run=lambda args: "a" * 40 + "\n")
    (root / ".gitignore").write_text(".claude/skills/\nTHIRD_PARTY_NOTICES.md\n")
    plan["new_lanes"] = [{"name": "web", "paths": ["web/**"], "skills": ["local:testing-strategy"]}]
    plan["issues"][1]["lane"] = "web"
    if interrupted:
        commit = git.commit_paths

        def fail(*args):
            raise GitError("interrupted")

        monkeypatch.setattr(git, "commit_paths", fail)
        with pytest.raises(GitError, match="interrupted"):
            order.publish(plan, root, config, tracker, repo="owner/repo", git=git, manager=manager)
        assert tracker.issues == {}
        monkeypatch.setattr(git, "commit_paths", commit)
        config = load_config(root)
    order.publish(plan, root, config, tracker, repo="owner/repo", git=git, manager=manager)
    git.fetch("origin", "main")
    runner = root.parent / "runner"
    git.add_detached_worktree(runner, "origin/main")
    assert not (runner / ".claude/skills/testing-strategy").exists()
    assert not (runner / "THIRD_PARTY_NOTICES.md").exists()
    assert "/.claude/skills/testing-strategy/" in (runner / ".gitignore").read_text().splitlines()
    assert copy_lane_skills(root, runner, ["testing-strategy"], manager) == [".claude/skills/testing-strategy"]
    assert (runner / ".claude/skills/testing-strategy/SKILL.md").read_bytes() == (skill / "SKILL.md").read_bytes()
    assert (runner / ".claude/skills/testing-strategy/references/example.md").read_text() == "Useful example\n"
    assert "skills: [testing-strategy]" in (runner / ".claude/agents/fa-web.md").read_text()
    lane_commit = next(args for args in calls if "commit" in args and args[args.index("-m") + 1] == "Foragent lane web")
    assert set(lane_commit[lane_commit.index("--") + 1:]) == {
        ".foragent.yml", ".claude/agents/fa-web.md", ".gitignore", ":!CLAUDE.local.md",
    }
    assert len([args for args in calls if args[0] == "push"]) == 1
    order.publish(plan, root, load_config(root), tracker, repo="owner/repo", git=git, manager=manager)
    assert len(tracker.issues) == 3


@pytest.mark.parametrize("requested,pin,engine,reason", [
    ("ouroboros", None, "ouroboros", None),
    ("ouroboros", "0.52.0", "ouroboros", None),
    ("ouroboros", "0.51.1", "native", "mismatch"),
    ("ouroboros", "", "native", "mismatch"),
    ("native", "0.51.1", "native", None),
])
def test_context_resolves_interview_from_first_plugin_entry(tmp_path, monkeypatch, requested, pin, engine, reason):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".claude/plugins/installed_plugins.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"plugins": {
        "other@market": [{"version": "1.0"}],
        "ouroboros@custom-market": [{"version": "0.52.0"}, {"version": "0.51.1"}],
    }}))
    result = order.context(tmp_path, Config(interview=requested, ouroboros_version=pin), FakeTracker())["interview"]
    assert result == {"requested": requested, "engine": engine, "installed_version": "0.52.0",
                      "pinned_version": pin, "reason": result["reason"]}
    if reason is None:
        assert result["reason"] is None
    else:
        assert reason in result["reason"]
        assert "0.52.0" in result["reason"] and pin in result["reason"]


@pytest.mark.parametrize("contents", [
    None, b"{", b"\xff", b"null", b"[]", b"{}", b'{"plugins": []}',
    b'{"plugins": {"other-ouroboros@market": [{"version": "0.52.0"}]}}',
    b'{"plugins": {"ouroboros@market": []}}',
    b'{"plugins": {"ouroboros@market": {}}}',
    b'{"plugins": {"ouroboros@market": [null]}}',
    b'{"plugins": {"ouroboros@market": [{}]}}',
    b'{"plugins": {"ouroboros@market": [{"version": 52}]}}',
])
def test_context_missing_or_broken_plugins_falls_back(tmp_path, monkeypatch, contents):
    monkeypatch.setenv("HOME", str(tmp_path))
    if contents is not None:
        path = tmp_path / ".claude/plugins/installed_plugins.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(contents)
    result = order.context(tmp_path, Config(interview="ouroboros"), FakeTracker())["interview"]
    assert result == {"requested": "ouroboros", "engine": "native", "installed_version": None,
                      "pinned_version": None, "reason": "Ouroboros not installed"}


def test_context_unreadable_plugins_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude/plugins/installed_plugins.json").mkdir(parents=True)
    result = order.context(tmp_path, Config(interview="ouroboros"), FakeTracker())["interview"]
    assert result["engine"] == "native" and "not installed" in result["reason"]


def test_context_all_sections_and_gate_groups(tmp_path):
    tracker = FakeTracker()
    tracker.add_issue(1, "Feedback", "Fix it\n\nAbout #8.", ["feedback"])
    tracker.add_issue(2, "Question", "Current issue body", ["needs-human", "lane:api", "size:l"])
    tracker.comment(2, format_comment(Reason.SPEC_QUESTION, "Choose", "A or B?"))
    tracker.comment(2, "<!-- foragent:decision -->\n**Decision:**\nA")
    tracker.add_issue(3, "No check", "body", ["needs-human"])
    tracker.comment(3, format_comment(Reason.NO_MACHINE_CHECK, "Add check"))
    tracker.add_issue(4, "Other reason", "", ["needs-human"])
    tracker.comment(4, format_comment(Reason.TIMEOUT, "Too big"))
    tracker.add_issue(5, "Running", "", ["running"])
    tracker.add_issue(6, "Waiting", "", ["approved"])
    (tmp_path / "docs/specs").mkdir(parents=True)
    (tmp_path / "docs/specs/one.md").write_text("text\n## First heading\n# Later\n")
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest"}, "packageManager":"pnpm@9"}')
    (tmp_path / "go.mod").write_text("module test")
    (tmp_path / ".github/workflows").mkdir(parents=True)
    for name in (".git", ".venv", "node_modules"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "hidden.js").write_text("")
    config = Config(language="ko", lanes={"api": {"paths": ["api/**"], "agent": "fa-api", "skills": ["testing"]}})
    result = order.context(tmp_path, config, tracker)
    assert set(result) == {"language", "lanes", "tiers", "interview", "profile", "ui", "acceptance_dir", "base_branch", "feedback", "decisions", "open_issues", "specs", "repo"}
    assert result["language"] == "ko" and result["tiers"] == config.tiers
    assert result["lanes"]["api"]["skills"] == ["testing"]
    assert result["feedback"] == [{"number": 1, "title": "Feedback", "body": "Fix it\n\nAbout #8.", "about": 8}]
    assert [item["number"] for item in result["decisions"]] == [2, 3]
    assert "A or B?" in result["decisions"][0]["question"]
    assert result["decisions"][0]["answer"].endswith("A")
    assert result["decisions"][0]["body"] == "Current issue body"
    assert [item["state_group"] for item in result["open_issues"]] == ["unapproved", "needs-human", "needs-human", "needs-human", "running", "approved-waiting"]
    assert result["open_issues"][1]["lane"] == "api" and result["open_issues"][1]["size"] == "l"
    assert result["specs"] == [{"path": "docs/specs/one.md", "heading": "First heading"}]
    assert result["repo"]["test_commands"] == ["uv run pytest", "pnpm test", "go test ./..."]
    assert result["repo"]["languages"] == ["Go", "JavaScript", "Python"]
    assert result["repo"]["has_workflows"] is True
    assert "docs/specs/one.md" not in result["repo"]["tree"]
    assert not any(path.startswith((".git/", ".venv/", "node_modules/")) for path in result["repo"]["tree"])
    (tmp_path / "package.json").write_text('{"scripts":{"test":"test"}}')
    assert "npm test" in order.context(tmp_path, config, tracker)["repo"]["test_commands"]


def test_order_cli_context_and_publish_dry_run(publishing, plan, monkeypatch, capsys):
    root, config, tracker, git, events = publishing
    monkeypatch.setattr(cli, "load_config", lambda root: config)
    monkeypatch.setattr(cli, "GitRepo", lambda root: git)
    monkeypatch.setattr(git, "remote_slug", lambda: "owner/repo", raising=False)
    monkeypatch.setattr(cli, "GhTracker", lambda repo: tracker)
    assert cli.main(["order", "context", "--repo-root", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["tiers"] == config.tiers
    path = root / "PLAN.json"
    path.write_text(json.dumps(plan))
    assert cli.main(["order", "publish", str(path), "--repo-root", str(root), "--dry-run", "--approve-keys", "api"]) == 0
    assert "dry run" in capsys.readouterr().out and events == []
    path.write_text("{")
    assert cli.main(["order", "publish", str(path), "--repo-root", str(root)]) == 1
    assert "foragent:" in capsys.readouterr().err


def test_profile_cli_updates_local_config_without_github(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    global_path = tmp_path / "home/.config/foragent/config.yml"
    global_path.parent.mkdir(parents=True)
    global_path.write_text("executor: claude\n")
    path = tmp_path / ".foragent.yml"
    path.write_text("language: ko\nlanes:\n  api:\n    paths: [src/**]\nprofile:\n  kind: old\n")

    def unexpected(*args):
        pytest.fail("profile set must work without Git or GitHub")

    monkeypatch.setattr(cli, "GitRepo", unexpected)
    monkeypatch.setattr(cli, "GhTracker", unexpected)
    assert cli.main(["profile", "set", "--kind", "daily-app", "--stage", "build",
                     "--priority", "speed", "--repo-root", str(tmp_path)]) == 0
    expected = {"kind": "daily-app", "stage": "build", "priority": "speed",
                "confirmed_at": date.today().isoformat()}
    assert json.loads(capsys.readouterr().out) == expected
    assert yaml.safe_load(path.read_text()) == {
        "language": "ko", "lanes": {"api": {"paths": ["src/**"]}}, "profile": expected,
    }
    config = load_config(tmp_path)
    assert config.executor == "claude"
    assert order.context(tmp_path, config, FakeTracker())["profile"] == expected


def test_profile_cli_rejects_invalid_config_without_overwriting(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / ".foragent.yml"
    path.write_text("review: invalid\n")
    assert cli.main(["profile", "set", "--kind", "daily-app", "--stage", "build",
                     "--priority", "speed", "--repo-root", str(tmp_path)]) == 1
    assert "review" in capsys.readouterr().err
    assert path.read_text() == "review: invalid\n"


@pytest.mark.parametrize("step,fields", [
    ("unknown", {}), ("start", {"title": "  "}),
    ("ledger", {"add": ""}), ("chunk", {"add": "\n"}),
    ("cell", {"chunk": "missing", "slot": "goal", "filled": True}),
    ("cell", {"chunk": "API", "slot": "unknown", "filled": True}),
    ("cell", {"chunk": "API", "slot": "goal"}),
    ("cell", {"chunk": "API", "slot": "goal", "ask": "?", "filled": True}),
    ("cell", {"chunk": "API", "slot": "goal", "ask": "  "}),
    ("tests", {"done": -1, "total": 2}), ("tests", {"done": 3, "total": 2}),
    ("tests", {"done": 0, "total": -1}), ("tests", {"done": True, "total": 2}),
    ("tests", {"done": 0}), ("publish", {"issue": 0}), ("publish", {"issue": True}),
])
def test_order_progress_rejected_fact_preserves_saved_bytes(tmp_path, step, fields):
    store = StateStore(tmp_path / "state")
    order.progress(store, "start", title="API order")
    order.progress(store, "chunk", add="API")
    path = store.root / "order-progress.json"
    before = path.read_bytes()
    with pytest.raises(order.PlanError):
        order.progress(store, step, **fields)
    assert path.read_bytes() == before


def test_order_progress_duplicate_chunk_preserves_answers_and_questions_reopen_cells(tmp_path):
    store = StateStore(tmp_path)
    order.progress(store, "start", title="API order")
    order.progress(store, "chunk", add="API")
    order.progress(store, "cell", chunk="API", slot="goal", filled=True)
    order.progress(store, "chunk", add="API")
    assert store.order_progress()["cells_left"] == 4
    order.progress(store, "cell", chunk="API", slot="goal", ask="Which endpoint?")
    assert store.order_progress()["cells_left"] == 5
    assert store.order_progress()["cells"]["API"]["goal"]["ask"] == "Which endpoint?"
    order.progress(store, "cell", chunk="API", slot="goal", filled=True)
    assert store.order_progress()["cells"]["API"]["goal"] == {"filled": True, "ask": None}
    for _ in range(2):
        order.progress(store, "ledger", add="D1: REST")
        order.progress(store, "publish", issue=42)
    assert store.order_progress()["ledger"] == ["D1: REST"]
    assert store.order_progress()["published"] == [42]


def test_order_progress_requires_start_and_new_start_resets_only_preparation(tmp_path):
    store = StateStore(tmp_path)
    with pytest.raises(order.PlanError, match="progress start"):
        order.progress(store, "check")
    assert store.order_progress() is None
    store.start_run(42, branch="issue-42", worktree=tmp_path / "worktree")
    order.progress(store, "start", title="Old")
    order.progress(store, "ledger", add="Old decision")
    order.progress(store, "start", title="New")
    state = StateStore(tmp_path).order_progress()
    assert state["title"] == "New" and state["ledger"] == [] and state["cells_left"] == 0
    order.progress(store, "end")
    order.progress(store, "end")
    assert StateStore(tmp_path).order_progress() is None
    assert store.get_run(42).status == "running"


def test_order_progress_concurrent_writers_do_not_lose_facts(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    order.progress(StateStore(tmp_path), "start", title="Concurrent order")

    def add_decision(number):
        order.progress(StateStore(tmp_path), "ledger", add=f"Decision {number}")

    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(add_decision, range(20)))
    assert set(StateStore(tmp_path).order_progress()["ledger"]) == {
        f"Decision {number}" for number in range(20)}


def test_order_progress_cli_uses_configured_repo_without_external_services(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda root: Config(repo="owner/repo"))
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo: cls(tmp_path / repo)))

    def unexpected(*args, **kwargs):
        pytest.fail("progress must not initialize Git/GitHub, call an LLM or open a view")

    monkeypatch.setattr(cli, "GitRepo", unexpected)
    monkeypatch.setattr(cli, "GhTracker", unexpected)
    monkeypatch.setattr(cli.watch_open, "open_view", unexpected)
    monkeypatch.setattr(subprocess, "Popen", unexpected)
    assert cli.main(["order", "progress", "start", "--title", "주문 준비"]) == 0
    assert json.loads(capsys.readouterr().out)["title"] == "주문 준비"
    assert cli.main(["order", "progress", "tests", "--done", "3", "--total", "2"]) == 1
    assert "0 <= done <= total" in capsys.readouterr().err
    assert StateStore(tmp_path / "owner/repo").order_progress()["step"] == "start"


def _record_preread(root, config, plan, item, unknowns):
    path = root / order.PREREAD_FILE
    records = json.loads(path.read_text()) if path.exists() else {}
    records[order.preread_target(plan, item, config, root)[2]] = {"key": item["key"], "unknowns": unknowns}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records))


def test_preread_gate_checks_only_the_approved_keys(publishing, plan, monkeypatch):
    monkeypatch.setattr(order, "require_clean_preread", REQUIRE_CLEAN_PREREAD)
    root = publishing[0]
    _record_preread(root, publishing[1], plan, plan["issues"][0], [])
    _record_preread(root, publishing[1], plan, plan["issues"][1], ["Which locale?"])
    publish(publishing, plan, approve_keys=["api"])
    assert publishing[4]
    with pytest.raises(order.PlanError, match=r"client: Which locale\?"):
        publish(publishing, deepcopy(plan), approve=True)


def test_preread_gate_does_not_apply_without_approval(publishing, plan, monkeypatch):
    monkeypatch.setattr(order, "require_clean_preread", REQUIRE_CLEAN_PREREAD)
    publish(publishing, plan)
    with pytest.raises(order.PlanError, match="run fa order preread"):
        REQUIRE_CLEAN_PREREAD(plan, publishing[0], publishing[1], plan["issues"])


@pytest.mark.parametrize("message,unknowns", [
    ("NONE", []), ("  NONE\n", []),
    ("- Which region?\n\n2. Which error?\n", ["Which region?", "Which error?"]),
])
def test_preread_reply_lists_one_unknown_per_line(message, unknowns):
    assert order._unknowns(message) == unknowns


def test_preread_failed_execution_records_nothing(publishing, plan, monkeypatch):
    from foragent.executors import ExecResult

    class Failing:
        def run(self, **kwargs):
            assert kwargs["read_only"] is True
            return ExecResult(1, False, "", kwargs["log_path"])

    monkeypatch.setattr(order, "executor_for", lambda name: Failing())
    root, config = publishing[0], publishing[1]
    with pytest.raises(order.PlanError, match="preread of issue api"):
        order.preread(plan, root, config)
    assert not (root / order.PREREAD_FILE).exists()
    assert (root / ".foragent/.gitignore").read_text() == "*\n"


class _NoUnknowns:
    def __init__(self):
        self.calls = []

    def run(self, **kwargs):
        from foragent.executors import ExecResult

        self.calls.append(kwargs)
        return ExecResult(0, False, "NONE", kwargs["log_path"])


def test_preread_reads_and_gates_approved_edits(publishing, plan, monkeypatch):
    root, config, tracker = publishing[0], publishing[1], publishing[2]
    tracker.add_issue(3, "Keep this title", "old", ["size:l", "lane:api"])
    plan["issues"] = []
    plan["edits"] = [{"number": 3, "goal": "New goal", "out_of_scope": "Payments.", "done_when": ["true"],
                      "checklist": [], "acceptance_files": {}}]
    monkeypatch.setattr(order, "require_clean_preread", REQUIRE_CLEAN_PREREAD)
    with pytest.raises(order.PlanError, match="#3"):
        publish(publishing, plan, approve_numbers=[3])
    assert publishing[4] == []
    worker = _NoUnknowns()
    monkeypatch.setattr(order, "executor_for", lambda name: worker)
    assert order.preread(plan, root, config, lambda: tracker) == [{"key": "#3", "unknowns": []}]
    assert "Edit of existing issue #3: Keep this title" in worker.calls[0]["prompt"]
    assert "Lane: api" in worker.calls[0]["prompt"]
    publish(publishing, plan, approve_numbers=[3])
    assert "approved" in tracker.get_issue(3).labels


@pytest.mark.parametrize("change", ["blocked_by", "interactive", "new_lanes"])
def test_preread_evidence_follows_dependency_interactive_and_lane_changes(publishing, plan, change):
    config, item = publishing[1], plan["issues"][1]
    before = order.preread_target(plan, item, config, publishing[0])[2]
    if change == "blocked_by":
        item["blocked_by"] = []
    elif change == "interactive":
        item["interactive"] = True
    else:
        plan["new_lanes"] = [{"name": "web", "paths": ["web/**"], "skills": []}]
    assert order.preread_target(plan, item, config, publishing[0])[2] != before


def _implementation_setup(tmp_path, config, lane, size, monkeypatch):
    """What the runner's first implementation attempt passes to the executor."""
    from foragent import run_implementation, runner

    seen = {}

    class Stop(Exception):
        pass

    def capture(n, ctx, executor, **kwargs):
        seen.update(kwargs, executor=ctx.config.executor)
        raise Stop

    monkeypatch.setattr(run_implementation, "_available_executor", capture)
    monkeypatch.setattr(runner, "_remaining", lambda n, ctx: 60)
    monkeypatch.setattr(run_implementation, "_phase", lambda *a, **kw: None)
    store = StateStore(tmp_path / "state", now=lambda: BASE_TIME)
    body = "<!-- foragent:v1 -->\n## Goal\nG\n## Out of scope\nO\n## Done when\n```sh\ntrue\n```\n"
    store.save_approval(1, body=body, body_sha="x", acceptance_blobs={}, approved_by="human",
                        approved_at=BASE_TIME.isoformat())
    store.start_run(1, branch="b", worktree=tmp_path / "w", lane=lane)
    ctx = SimpleNamespace(config=run_implementation.lane_config(config, lane), store=store)
    attempt = SimpleNamespace(n=1, ctx=ctx, worktree=tmp_path / "repo", log=tmp_path / "log",
                              size=size, options={}, missing_skills=[], native_claude=ctx.config.executor == "claude",
                              parsed=parse_body(body), baseline=None, executor=None,
                              approval=store.load_approval(1))
    monkeypatch.setattr(run_implementation, "build_prompt", lambda *a, **kw: "PROMPT")
    monkeypatch.setattr(run_implementation, "build_resume_prompt", lambda *a, **kw: "RESUME")
    with pytest.raises(Stop):
        run_implementation._implement(attempt, "")
    return seen


@pytest.mark.parametrize("executor,lane,lane_model", [
    ("codex", "api", None), ("claude", "api", None), ("claude", "api", "lane-model"), ("codex", "main", None),
])
def test_preread_setup_matches_the_implementation_setup(publishing, plan, tmp_path, monkeypatch,
                                                        executor, lane, lane_model):
    repo = tmp_path / "repo"
    (repo / ".claude/agents").mkdir(parents=True)
    (repo / ".claude/agents/fa-api.md").write_text("---\nname: fa-api\nskills: [polish]\n---\nAPI lane rules.\n")
    (repo / ".claude/skills/polish").mkdir(parents=True)
    (repo / ".claude/skills/polish/SKILL.md").write_text("---\nname: polish\ndescription: Polish text\n---\nBody\n")
    settings = {"paths": ["api/**"], "agent": "fa-api", **({"model": lane_model} if lane_model else {})}
    tiers = {"s": {"codex_effort": "medium", "claude_model": "small-model"},
             "m": {"codex_effort": "high", "claude_model": "tier-m-model"},
             "l": {"codex_effort": "xhigh", "claude_model": "large-model"}}
    config = replace(publishing[1], executor=executor, executor_model="base-model", tiers=tiers,
                     lanes={"api": settings})
    item = {**plan["issues"][0], "lane": lane, "size": "m"}
    setup, prompt, _ = order.preread_target(plan, item, config, repo)
    implementation = _implementation_setup(tmp_path, config, lane, "m", monkeypatch)
    assert setup.executor == implementation["executor"] == executor
    assert setup.model == implementation["model"]
    if executor == "claude":
        assert setup.model == (lane_model or "tier-m-model")
    assert {key: implementation[key] for key in setup.options} == setup.options
    if lane == "api":
        assert ("agent" in setup.options) == (executor == "claude")
    expected_prefix = implementation["prompt"].removesuffix("PROMPT")
    assert prompt.startswith(expected_prefix) and prompt.removeprefix(expected_prefix) == order.preread_prompt(plan, item)
    if executor == "codex" and lane == "api":
        assert "API lane rules." in expected_prefix and "polish" in expected_prefix


def test_preread_evidence_follows_the_lane_model(publishing, plan, tmp_path):
    config = replace(publishing[1], lanes={"api": {"paths": ["api/**"], "agent": "fa-api", "model": "one"}})
    digest = order.preread_target(plan, plan["issues"][0], config, tmp_path)[2]
    other = replace(config, lanes={"api": {**config.lanes["api"], "model": "two"}})
    assert order.preread_target(plan, plan["issues"][0], other, tmp_path)[2] != digest


@pytest.mark.parametrize("change", ["own_size", "other_goal", "other_size", "other_acceptance"])
def test_preread_evidence_covers_the_whole_plan(publishing, plan, change):
    config, item = publishing[1], plan["issues"][0]
    before = order.preread_target(plan, item, config, publishing[0])[2]
    if change == "own_size":
        item["size"] = "l"
    elif change == "other_goal":
        plan["issues"][1]["goal"] += " Keep the locale."
    elif change == "other_size":
        plan["issues"][1]["size"] = "m"
    else:
        plan["issues"][1]["acceptance_files"] = {"tests/acceptance/other.py": "assert True\n"}
    assert order.preread_target(plan, item, config, publishing[0])[2] != before


def test_preread_gate_rejects_stale_evidence_after_another_issue_changes(publishing, plan, monkeypatch):
    monkeypatch.setattr(order, "require_clean_preread", REQUIRE_CLEAN_PREREAD)
    root, config = publishing[0], publishing[1]
    for item in plan["issues"]:
        _record_preread(root, config, plan, item, [])
    plan["issues"][1]["size"] = "l"
    with pytest.raises(order.PlanError, match="no preread of the current content"):
        publish(publishing, plan, approve_keys=["api"])
    assert publishing[4] == []


@pytest.mark.parametrize("recorded", [None, "client_unknown"])
def test_preread_gate_covers_numbers_of_children_created_now(publishing, plan, monkeypatch, recorded):
    monkeypatch.setattr(order, "require_clean_preread", REQUIRE_CLEAN_PREREAD)
    root, config = publishing[0], publishing[1]
    if recorded:
        _record_preread(root, config, plan, plan["issues"][0], [])
        _record_preread(root, config, plan, plan["issues"][1], ["Which locale?"])
    # First publication: #3 will be the new client child, so the gate must not let it through.
    with pytest.raises(order.PlanError, match="client"):
        publish(publishing, plan, approve_numbers=[3])
    assert publishing[4] == [] and publishing[2].issues == {}


def test_preread_gate_passes_numbers_of_new_children_after_clean_preread(publishing, plan, monkeypatch):
    monkeypatch.setattr(order, "require_clean_preread", REQUIRE_CLEAN_PREREAD)
    root, config = publishing[0], publishing[1]
    for item in plan["issues"]:
        _record_preread(root, config, plan, item, [])
    publish(publishing, plan, approve_numbers=[3])
    assert "approved" in publishing[2].get_issue(3).labels
    assert "approved" not in publishing[2].get_issue(2).labels


def test_new_lane_issue_is_prereaded_only_after_its_lane_exists(publishing, plan, monkeypatch):
    monkeypatch.setattr(order, "require_clean_preread", REQUIRE_CLEAN_PREREAD)
    root, config, tracker, _, events = publishing
    plan["new_lanes"] = [{"name": "web", "paths": ["web/**"], "skills": []}]
    plan["issues"][1]["lane"] = "web"
    worker = _NoUnknowns()
    monkeypatch.setattr(order, "executor_for", lambda name: worker)
    # Before publication there is no web implementer: no generic preread, no evidence.
    with pytest.raises(order.PlanError, match=r"client.*new lane\(s\) web.*without --approve"):
        order.preread(plan, root, config)
    assert worker.calls == [] and not (root / order.PREREAD_FILE).exists()
    with pytest.raises(order.PlanError, match=r"client.*new lane\(s\) web"):
        publish(publishing, plan, approve=True)
    assert events == [] and tracker.issues == {}
    publish(publishing, plan)
    created = load_config(root)
    assert created.lanes["web"]["agent"] == "fa-web"
    assert [item["key"] for item in order.preread(plan, root, created)] == ["api", "client"]
    client = worker.calls[1]
    setup, prompt, _ = order.preread_target(plan, plan["issues"][1], created, root)
    assert client["prompt"] == prompt and setup.executor == created.executor
    if created.executor == "codex":
        assert "repository's web lane" in prompt, "The created lane agent's instructions reach the preread"
    else:
        assert client["agent"] == "fa-web"
    publish((root, created, tracker, publishing[3], events), plan, approve=True)
    assert all("approved" in tracker.get_issue(n).labels for n in (2, 3))
