from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
import hashlib
import json
import re
import subprocess
from pathlib import Path
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
from foragent.ownership import OwnershipError, effective_orderer
from foragent.tracker import FakeTracker, TrackerError

from conftest import BASE_TIME, git_command


FILE = "tests/acceptance/test_login.py"

REQUIRE_CLEAN_PREREAD = order.require_clean_preread


@pytest.fixture(autouse=True)
def preread_done(monkeypatch):
    """Publication tests here assume a clean preread; the gate is tested on its own."""
    monkeypatch.setattr(order, "require_clean_preread", lambda plan, root, config, items: None)


class PublishingGit:
    """Record authoring operations, keeping all real Git writes offline.

    ``saved`` is HEAD's tree; ``history`` holds each commit's sha and tree in order, so
    ancestry and older commits' blobs are real facts; ``index`` holds staged paths that
    differ from HEAD (None stages a deletion).
    """
    def __init__(self, root, events):
        self.root, self.events = root, events
        self.saved = {}
        self.branch = "main"
        self.history = [("published", None)]  # the base commit's tree is ``saved`` until a commit
        self.index = {}
        self.serial = 0  # like real shas, a commit id is never reused, even after a failed commit

    def _new_sha(self):
        self.serial += 1
        return f"commit-{self.serial}"

    def current_branch(self):
        return self.branch

    def ensure_tracked(self, paths):
        pass

    def head_sha(self):
        return self.history[-1][0]

    def _tree(self, ref):
        if ref in ("HEAD", self.head_sha()):
            return self.saved
        trees = dict(self.history)
        assert ref in trees, f"unknown commit {ref}"
        return trees[ref] if trees[ref] is not None else self.saved

    def blob_shas(self, ref, paths):
        tree = self._tree(ref)
        return {path: self._blob(tree[path]) if path in tree else "missing" for path in paths}

    @staticmethod
    def _blob(content):
        data = content.encode()
        return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()

    def _worktree(self, path):
        target = self.root / path
        return target.read_text() if target.is_file() else None

    def uncommitted_paths(self, paths):
        return [path for path in paths if path in self.index or self.saved.get(path) != self._worktree(path)]

    def stage(self, paths):
        """What `git add` does before the commit itself; a failed commit leaves this behind."""
        for path in paths:
            self.index[path] = self._worktree(path)

    def commit_paths(self, paths, title):
        self.stage(paths)
        if all(self.index[path] == self.saved.get(path) for path in paths):
            for path in paths:
                self.index.pop(path)
            raise GitError("nothing to commit")  # real `git commit --only` rejects an empty commit
        self._commit(paths, title)

    def _freeze_base(self):
        if self.history[-1][1] is None:  # the base's tree is fixed once a commit follows it
            self.history[-1] = (self.history[-1][0], dict(self.saved))

    def _commit(self, paths, title, sha=None):
        sha = sha or self._new_sha()
        self._freeze_base()
        contents = {path: self.index.pop(path) for path in paths}
        self.events.append(("commit", title, {path: text for path, text in contents.items() if text is not None}))
        for path, text in contents.items():
            if text is None:
                self.saved.pop(path, None)
            else:
                self.saved[path] = text
        self.history.append((sha, dict(self.saved)))

    def commit_recorded(self, paths, title, record, contents=None):
        """Like commit_paths, but the new sha goes to ``record`` before HEAD moves (``_commit``).

        Paths in ``contents`` are committed from those bytes, not from the working tree."""
        contents = contents or {}
        self.stage([path for path in paths if path not in contents])
        self.index.update({path: data.decode() for path, data in contents.items()})
        if all(self.index[path] == self.saved.get(path) for path in paths):
            for path in paths:
                self.index.pop(path)
            raise GitError("nothing to commit")
        sha = self._new_sha()
        record(sha)
        self._commit(paths, title, sha)
        assert self.head_sha() == sha
        return sha

    def unrelated_commit(self, path, text):
        """Someone else's later commit on the base."""
        (self.root / path).parent.mkdir(parents=True, exist_ok=True)
        (self.root / path).write_text(text)
        self._freeze_base()
        self.stage([path])
        contents = {path: self.index.pop(path)}
        self.saved.update(contents)
        self.history.append((self._new_sha(), dict(self.saved)))

    def reset_to(self, sha):
        """Move HEAD back, as a reset or a rewritten base would; later commits leave history."""
        shas = [entry[0] for entry in self.history]
        self.history = self.history[:shas.index(sha) + 1]
        if self.history[-1][1] is not None:
            self.saved = dict(self.history[-1][1])

    def blob_bytes(self, sha):
        for tree in [self.saved, *(tree for _, tree in self.history if tree is not None)]:
            for text in tree.values():
                if self._blob(text) == sha.decode():
                    return text.encode()
        raise AssertionError(f"unknown blob {sha}")

    def is_ancestor(self, ancestor, head):
        assert head == "HEAD"
        return ancestor in [entry[0] for entry in self.history]

    def restore_staged_paths(self, ref, paths):
        tree = self._tree(ref)
        for path in paths:
            if tree.get(path) == self.saved.get(path):
                self.index.pop(path, None)
            else:
                self.index[path] = tree.get(path)

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
    assert tracker.get_issue(2).labels == {"by:human", "lane:api", "size:m", "custom"}
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
        "<!-- foragent:owner-label -->\n"
    )
    assert tracker.get_issue(2).body == expected
    assert parse_body(expected).done_when_commands == plan["issues"][0]["done_when"]
    assert tracker.get_issue(2).labels == {"by:human", "lane:api", "size:m", "approved"}
    assert tracker.get_issue(3).labels == {"by:human", "size:s", "approved"}
    assert tracker.get_issue(1).labels == {"by:human"}
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
    assert len(tracker.issues) == 3 and tracker.get_issue(2).labels == {"by:human", "lane:api", "size:m", "approved"}
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
    assert set(events[0][2]) == {".foragent.yml", ".claude/agents/fa-web.md", ".gitattributes"}


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
        ".foragent.yml", ".claude/agents/fa-web.md", ".gitignore", ".gitattributes", ":!CLAUDE.local.md",
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
    assert set(result) == {"language", "lanes", "tiers", "interview", "profile", "ui", "acceptance_dir", "base_branch", "feedback", "decisions", "open_issues", "pull_requests", "specs", "repo"}
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


def test_context_lane_skills_come_only_from_configuration(tmp_path):
    # D95: configured skills: [] stays empty even when the agent frontmatter lists a stale skill.
    (tmp_path / ".claude/agents").mkdir(parents=True)
    (tmp_path / ".claude/agents/fa-api.md").write_text("---\nname: fa-api\nskills: [stale]\n---\nRules.\n")
    config = Config(lanes={"api": {"paths": ["api/**"], "agent": "fa-api", "skills": []},
                           "web": {"paths": ["web/**"], "agent": "fa-api"}})
    lanes = order.context(tmp_path, config, FakeTracker())["lanes"]
    assert lanes["api"]["skills"] == [] and lanes["web"]["skills"] == []


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


class _Proposing(_NoUnknowns):
    reply = "APPROACH: Reuse the writer.\nDECIDE: Name the helper while implementing."

    def run(self, **kwargs):
        from foragent.executors import ExecResult

        self.calls.append(kwargs)
        return ExecResult(0, False, self.reply, kwargs["log_path"])


REPO = "owner/repo"


def _child_body(plan, item, config):
    """The body publish writes for a new plan child, its plan and owner-label markers included."""
    return (order._published_body(plan, item, REPO, config) + "\n" + order._child_marker(plan["id"], item["key"])
            + "\n" + order.OWNED_MARKER + "\n")


API_FILES = {FILE: "assert True\n"}


def _handover(root, n, body, files, approved=None):
    """The proposal a run gets when its worktree holds ``files`` and approval recorded ``approved``."""
    worktree = root.parent / f"run-{hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:12]}"
    for name, content in files.items():
        (worktree / name).parent.mkdir(parents=True, exist_ok=True)
        (worktree / name).write_text(content)
    if approved is None:
        approved = {name: order._blob(content.encode()) for name, content in files.items()}
    return order.lane_proposal(root, n, body, approved, worktree)


def test_preread_proposal_is_recorded_and_bound_to_the_published_issue(publishing, plan, monkeypatch):
    root, config = publishing[0], publishing[1]
    monkeypatch.setattr(order, "executor_for", lambda name: _Proposing())
    rows = order.preread(plan, root, config, repo=REPO)
    assert rows[0] == {"key": "api", "unknowns": [], "decisions": ["Name the helper while implementing."],
                       "approach": ["Reuse the writer."]}
    item = plan["issues"][0]
    body = _child_body(plan, item, config)
    proposal = _handover(root, 7, body, API_FILES)
    assert proposal["approach"] == ["Reuse the writer."]
    assert proposal["decisions"] == ["Name the helper while implementing."]
    assert _handover(root, 7, body.replace("\n", "\r\n"), API_FILES) is not None
    legacy = body.replace(order.OWNED_MARKER + "\n", "")  # A child published before owner labels.
    assert _handover(root, 7, legacy, API_FILES) == proposal
    # The body publish writes is exactly the one the run is handed the proposal for.
    tracker, git = publishing[2], publishing[3]
    publish(publishing, plan)
    assert tracker.get_issue(2).body == body
    assert _handover(root, 7, body + "\nAppended by a person.\n", API_FILES) is None
    other = order._published_body(plan, plan["issues"][1], REPO, config) + order._child_marker(plan["id"], "api")
    assert _handover(root, 7, other, API_FILES) is None, "another issue's content is not this proposal"
    assert _handover(root, 7, body.replace(f"id={plan['id']}", "id=another-order"), API_FILES) is None
    for change in ({"goal": item["goal"] + " Also more."}, {"exclusive": ["api/**"]},
                   {"checklist": ["Looks right", "And more"]}, {"files": ["api/login.py"]}):
        assert _handover(root, 7, _child_body(plan, {**item, **change}, config), API_FILES) is None, change
    other_repo = order._published_body(plan, item, "someone/else", config) + order._child_marker(plan["id"], "api")
    assert _handover(root, 7, other_repo, API_FILES) is None
    assert _handover(root, 7, body, {}) is None, "a run without the acceptance file did not get what preread read"


@pytest.mark.parametrize("section", ["goal", "out_of_scope"])
def test_preread_proposal_binds_authored_github_links_byte_for_byte(publishing, plan, monkeypatch, section):
    """An authored link is part of the WHAT: approving a changed link must not hand over the old proposal."""
    root, config = publishing[0], publishing[1]
    item = plan["issues"][0]
    link = "[API contract](https://github.com/org/contracts/blob/main/{}.md)"
    original = item[section]
    item[section] = original + "\n" + link.format("v1")
    _proposals(root, config, plan, [("s", "APPROACH: Follow v1.")])
    assert _handover(root, 7, _child_body(plan, item, config), API_FILES)["approach"] == ["Follow v1."]
    changed = {**item, section: original + "\n" + link.format("v2")}
    assert _handover(root, 7, _child_body(plan, changed, config), API_FILES) is None
    # A link into this repository's own base branch is authored text too, not a generated line.
    own = {**item, section: original + f"\n[Spec](https://github.com/{REPO}/blob/main/docs/spec.md)"}
    assert _handover(root, 7, _child_body(plan, own, config), API_FILES) is None


def test_preread_without_a_repository_records_but_never_hands_over(publishing, plan, monkeypatch):
    root, config = publishing[0], publishing[1]
    monkeypatch.setattr(order, "executor_for", lambda name: _Proposing())
    assert order.preread(plan, root, config)[0]["approach"] == ["Reuse the writer."]
    assert _handover(root, 7, _child_body(plan, plan["issues"][0], config), API_FILES) is None


def test_preread_of_changed_acceptance_tests_never_replaces_the_approved_handoff(publishing, plan, monkeypatch):
    """Approval binds the acceptance files; a preread of other test contents is not the approved proposal."""
    root, config, item = publishing[0], publishing[1], plan["issues"][0]
    body = _child_body(plan, item, config)
    _proposals(root, config, plan, [("s", "APPROACH: Approved plan.")])
    changed = deepcopy(plan)
    changed["issues"][0]["acceptance_files"][FILE] = "assert 1 == 1\n"
    _proposals(root, config, changed, [("s", "APPROACH: Plan for other tests.")])
    assert _handover(root, 7, body, API_FILES)["approach"] == ["Approved plan."]
    assert _handover(root, 7, body, {FILE: "assert 1 == 1\n"})["approach"] == ["Plan for other tests."]
    # The approval snapshot and the run's files must both match what preread read.
    assert _handover(root, 7, body, API_FILES, approved={FILE: order._blob(b"assert 1 == 1\n")}) is None


def test_preread_binds_an_acceptance_file_another_issue_supplies(publishing, plan, monkeypatch):
    """client runs the api issue's new acceptance file: bind the content publication writes, not the disk."""
    root, config = publishing[0], publishing[1]
    client = plan["issues"][1]
    client["done_when"] = [f"uv run pytest -q {FILE}"]
    assert not (root / FILE).exists()
    _proposals(root, config, plan, [("s", "APPROACH: Client plan.")])
    body = _child_body(plan, client, config)
    assert _handover(root, 8, body, API_FILES)["approach"] == ["Client plan."]
    assert _handover(root, 8, body, {FILE: "assert 1 == 1\n"}) is None


def test_preread_proposal_of_an_edit_is_bound_to_its_number(publishing, plan, monkeypatch):
    root, config, tracker = publishing[0], publishing[1], publishing[2]
    tracker.add_issue(3, "Keep this title", "old", ["size:l", "lane:api"])
    edit = {"number": 3, "goal": "New goal", "out_of_scope": "Payments.", "done_when": [f"uv run pytest -q {FILE}"],
            "checklist": [], "acceptance_files": {}}
    plan["edits"] = [edit]
    worker = _Proposing()
    monkeypatch.setattr(order, "executor_for", lambda name: worker)
    order.preread(plan, root, config, lambda: tracker, repo=REPO)
    body = order._published_body(plan, edit, REPO, config)
    assert _handover(root, 3, body, API_FILES)["approach"] == ["Reuse the writer."], "the api issue supplies FILE"
    # An edit of an issue published under owner labels keeps that contract line at its end.
    contract = order._with_contract(body, f"old\n{order.OWNED_MARKER}\n")
    assert contract != body and _handover(root, 3, contract, API_FILES)["approach"] == ["Reuse the writer."]
    assert _handover(root, 4, body, API_FILES) is None
    assert _handover(root, 3, body, {FILE: "assert 1 == 1\n"}) is None


def test_preread_binds_selected_reference_contents(publishing, plan, monkeypatch):
    """References are what the implementer reads; a newer preread of changed references is not the approved one."""
    root, config, item = publishing[0], publishing[1], plan["issues"][0]
    name = "docs/design/a/HANDOFF.md"
    plan["reference_files"] = {name: "approved handoff\n"}
    item["references"] = [name]
    _proposals(root, config, plan, [("s", "APPROACH: Approved references.")])
    changed = deepcopy(plan)
    changed["reference_files"][name] = "changed handoff\n"
    _proposals(root, config, changed, [("s", "APPROACH: Changed references.")])
    body = _child_body(plan, item, config)
    assert body == _child_body(changed, changed["issues"][0], config), "the body alone cannot tell them apart"
    approved = {**API_FILES, name: "approved handoff\n"}
    assert _handover(root, 7, body, approved)["approach"] == ["Approved references."]
    assert _handover(root, 7, body, {**API_FILES, name: "changed handoff\n"})["approach"] == ["Changed references."]
    assert _handover(root, 7, body, API_FILES) is None, "a run without the reference did not get what preread read"


def _proposals(root, config, plan, replies):
    """Preread the plan once per (summary, reply); a summary change gives a new digest, same issue content."""
    for summary, reply in replies:
        worker = _Proposing()
        worker.reply = reply
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(order, "executor_for", lambda name: worker)
            order.preread({**plan, "summary": summary}, root, config, repo=REPO)


def test_preread_proposal_handover_follows_the_latest_write_even_within_one_second(publishing, plan):
    root, config, item = publishing[0], publishing[1], plan["issues"][0]
    body = _child_body(plan, item, config)
    first, second = "APPROACH: First plan.", "APPROACH: Second plan.\nDECIDE: Second choice."
    _proposals(root, config, plan, [("one", first), ("two", second)])
    assert _handover(root, 7, body, API_FILES)["approach"] == ["Second plan."]
    # Prereading the first digest again makes it the latest, although its key already exists.
    _proposals(root, config, plan, [("one", "APPROACH: Third plan.")])
    proposal = _handover(root, 7, body, API_FILES)
    assert proposal["approach"] == ["Third plan."] and proposal["decisions"] == []
    # A newer answer with nothing to propose replaces the older proposal instead of reviving it.
    _proposals(root, config, plan, [("two", "NONE")])
    assert _handover(root, 7, body, API_FILES) is None
    _proposals(root, config, plan, [("three", "DECIDE: Only a choice.")])
    proposal = _handover(root, 7, body, API_FILES)
    assert proposal["decisions"] == ["Only a choice."] and proposal["approach"] == []


def test_preread_proposal_reaches_fresh_and_resumed_implementation_prompts(publishing, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".foragent").mkdir(parents=True)
    body = "<!-- foragent:v1 -->\n## Goal\nG\n## Out of scope\nO\n## Done when\n```sh\ntrue\n```\n"
    content = order._body_digest(body)
    (repo / order.PREREAD_FILE).write_text(json.dumps({"d": {
        "key": "#1", "plan": "p", "content": content, "files": {}, "unknowns": [],
        "approach": ["Reuse the writer."], "decisions": ["Name the helper later."]}}))
    seen = _implementation_setup(tmp_path, publishing[1], "main", "m", monkeypatch, real_prompts=True)
    for prompt in (seen["prompt"], seen["resume_prompt"]):
        assert prompt.count("Your lane's earlier plan") == 1
        assert "- Reuse the writer." in prompt and "- Name the helper later." in prompt


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


def _implementation_setup(tmp_path, config, lane, size, monkeypatch, real_prompts=False):
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
    ctx = SimpleNamespace(config=run_implementation.lane_config(config, lane), store=store, now=lambda: BASE_TIME,
                          repo_root=tmp_path / "repo")
    attempt = SimpleNamespace(n=1, ctx=ctx, worktree=tmp_path / "repo", log=tmp_path / "log", rebuilt=False,
                              size=size, options={}, missing_skills=[], native_claude=ctx.config.executor == "claude",
                              parsed=parse_body(body), baseline=None, executor=None,
                              approval=store.load_approval(1), review_feedback=False,
                              issue=SimpleNamespace(labels=set()))
    if not real_prompts:
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
    # D95: lane skills come from the configuration; the agent frontmatter mirrors it.
    settings = {"paths": ["api/**"], "agent": "fa-api", "skills": ["polish"],
                **({"model": lane_model} if lane_model else {})}
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
    # The lane-memory notice (the writer contract, D95) follows the task in both prompts.
    expected_prefix, _, notice = implementation["prompt"].partition("PROMPT")
    assert prompt == expected_prefix + order.preread_prompt(plan, item) + notice
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


def test_perspectives_decisions_cut_keeps_titles_and_latest_twenty(tmp_path):
    from foragent import order_perspectives

    (tmp_path / "docs").mkdir()
    entries = "".join(f"### D{n}. Decision {n}\nBody {n}\n\n#### Addendum {n}\nMore {n}\n\n" for n in range(1, 26))
    (tmp_path / "docs/decisions.md").write_text("# Decisions\nPreamble\n\n" + entries, encoding="utf-8")
    text = order_perspectives.decisions_text(tmp_path)
    assert "Cut notice: docs/decisions.md has 25 decisions" in text
    assert all(f"### D{n}. Decision {n}" in text for n in range(1, 26))
    assert "Body 5" not in text and "Body 6" in text and "More 25" in text
    assert "Preamble" not in text


@pytest.mark.parametrize("make_rules", [
    lambda path: path.write_bytes(b"# Rules\n\xff\xfe broken\n"),
    lambda path: path.mkdir(),
], ids=["invalid-utf8", "directory"])
def test_perspectives_unreadable_rules_never_falls_back_to_history(tmp_path, make_rules):
    from foragent import order_perspectives

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/decisions.md").write_text("HISTORY_BEHIND_UNREADABLE_RULES\n", encoding="utf-8")
    make_rules(tmp_path / "docs/rules.md")
    prompt = order_perspectives.prompt("incidents", {}, tmp_path)
    assert "HISTORY_BEHIND_UNREADABLE_RULES" not in prompt
    assert "docs/rules.md exists but cannot be read" in prompt


def test_perspectives_config_rejects_unknown_seat_or_executor():
    from foragent.config import ConfigError, DEFAULTS
    from foragent.config_validation import _validate

    for value in ({"wizard": "codex"}, {"skeptic": "gpt"}, {"skeptic": ["codex"]}, ["skeptic"]):
        with pytest.raises(ConfigError, match="perspectives"):
            _validate({**DEFAULTS, "perspectives": value})
    _validate({**DEFAULTS, "perspectives": {"skeptic": "claude"}})


def test_perspectives_without_executors_fail_every_seat(tmp_path, monkeypatch):
    import shutil
    from foragent import executors, order_perspectives
    from foragent.state import StateStore

    monkeypatch.setattr(shutil, "which", lambda name, *a, **kw: None)
    monkeypatch.setattr(executors, "executor_for", lambda name: pytest.fail("no executor may run"))
    result = order_perspectives.run({"id": "p"}, tmp_path, Config(), StateStore(tmp_path / "state"))
    assert result["questions"] == []
    assert {failure["perspective"] for failure in result["failures"]} == set(order_perspectives.SEATS)


def test_perspectives_rerun_replaces_questions_and_keeps_existing_marks(tmp_path, monkeypatch):
    import shutil
    from foragent import executors, order_perspectives
    from foragent.executors import ExecResult
    from foragent.state import StateStore

    seats = len(order_perspectives.SEATS)
    answers = iter(["First\nSecond"] * seats + ["Only"] * seats)

    class Worker:
        def run(self, **kwargs):
            assert kwargs["model"] is None and kwargs["read_only"] is True and "effort" not in kwargs
            return ExecResult(0, False, next(answers), kwargs["log_path"])

    monkeypatch.setattr(shutil, "which", lambda name, *a, **kw: f"/bin/{name}")
    monkeypatch.setattr(executors, "executor_for", lambda name: Worker())
    store = StateStore(tmp_path / "state")
    first = order_perspectives.run({"id": "p"}, tmp_path, Config(), store)
    ids = [question["id"] for question in first["questions"] if question["perspective"] == "skeptic"]
    order_perspectives.mark(store, ids[0], True)
    order_perspectives.mark(store, ids[1], True)
    again = order_perspectives.run({"id": "p"}, tmp_path, Config(), store)
    assert again["plan_hash"] == first["plan_hash"] and len(again["questions"]) == seats
    rows = order_perspectives.stats(store)["perspectives"]
    assert rows["skeptic"] == {"asked": 1, "changed": 1}
    with pytest.raises(order.PlanError):
        order_perspectives.mark(store, ids[1], False)


def test_perspectives_human_errors_follow_the_configured_language(tmp_path, monkeypatch):
    import shutil
    from foragent import executors, order_perspectives
    from foragent.config import ConfigError, DEFAULTS
    from foragent.config_validation import _validate
    from foragent.executors import ExecResult

    store = StateStore(tmp_path / "state")
    monkeypatch.setattr(shutil, "which", lambda name, *a, **kw: None)
    missing = order_perspectives.run({"id": "p"}, tmp_path, Config(language="ko"), store)["failures"]
    assert all("설치되어 있지 않아" in failure["error"] for failure in missing)

    replies = iter([ExecResult(7, False, "boom", tmp_path / "log"), ExecResult(0, True, "", tmp_path / "log")]
                   + [OSError("down")] * 4)

    class Worker:
        def run(self, **kwargs):
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply

    monkeypatch.setattr(shutil, "which", lambda name, *a, **kw: f"/bin/{name}")
    monkeypatch.setattr(executors, "executor_for", lambda name: Worker())
    errors = [failure["error"] for failure in
              order_perspectives.run({"id": "q"}, tmp_path, Config(language="ko"), store)["failures"]]
    assert sum("종료 코드 7" in error and "boom" in error for error in errors) == 1
    assert sum("초 안에 답이 없었어요" in error for error in errors) == 1
    assert sum("멈췄어요" in error and "down" in error for error in errors) == 4
    with pytest.raises(order.PlanError, match="관점 질문이 없어요"):
        order_perspectives.mark(store, "missing", True, "ko")
    with pytest.raises(ConfigError, match="자리"):
        _validate({**DEFAULTS, "language": "ko", "perspectives": {"wizard": "codex"}})


@pytest.mark.parametrize("language, expected", [("ko", "질문 id (mark에서만)"), ("en", "question id (only with mark)")])
def test_perspectives_help_follows_the_repository_language(tmp_path, monkeypatch, capsys, language, expected):
    (tmp_path / ".foragent.yml").write_text(yaml.safe_dump({"repo": "owner/repo", "language": language}))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["order", "perspectives", "--help"])
    assert exit_info.value.code == 0
    assert expected in " ".join(capsys.readouterr().out.split())


def test_perspectives_korean_config_yields_korean_questions_end_to_end(tmp_path, monkeypatch, capsys):
    import shutil
    from foragent import executors
    from foragent.executors import ExecResult
    from foragent.order_perspectives import SEATS

    (tmp_path / ".foragent.yml").write_text(yaml.safe_dump({"repo": "owner/repo", "language": "ko"}))
    (tmp_path / "PLAN.json").write_text(json.dumps({"id": "p", "title": "복구"}), encoding="utf-8")
    store = StateStore(tmp_path / "state")
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kw: store))
    prompts = []

    class Worker:
        def run(self, **kwargs):
            prompts.append(kwargs["prompt"])
            korean = "Write every question in Korean" in kwargs["prompt"]
            return ExecResult(0, False, "선택한 언어를 유지해야 하나요?" if korean else "Keep the locale?",
                              kwargs["log_path"])

    monkeypatch.setattr(shutil, "which", lambda name, *a, **kw: f"/bin/{name}")
    monkeypatch.setattr(executors, "executor_for", lambda name: Worker())
    argv = ["order", "perspectives", str(tmp_path / "PLAN.json"), "--repo-root", str(tmp_path)]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert len(prompts) == len(SEATS)
    assert out.count("선택한 언어를 유지해야 하나요?") == len(SEATS) and "Keep the locale?" not in out
    labels = {line.split(" (", 1)[0] for line in out.splitlines() if " (" in line}
    assert labels == {"회의론자", "처음 쓰는 사람", "공격자", "운영자", "6개월 뒤 유지보수자", "사고 기록",
                      "다른 사용자"}
    assert f"질문 {len(SEATS)}개" in out
    assert cli.main([*argv, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["language"] == "ko" and {q["language"] for q in payload["questions"]} == {"ko"}
    ledger = json.loads((store.root / "perspectives" / f"{payload['plan_hash']}.json").read_text(encoding="utf-8"))
    assert ledger["language"] == "ko" and all(q["language"] == "ko" for q in ledger["questions"])
    assert cli.main(["order", "perspectives", "stats", "--repo-root", str(tmp_path)]) == 0
    assert "회의론자: 질문 1개" in capsys.readouterr().out


def test_perspectives_prompt_names_the_configured_language(tmp_path):
    from foragent import order_perspectives

    assert "Write every question in Korean" in order_perspectives.prompt("skeptic", {}, tmp_path, "ko-KR")
    assert "Write every question in English" in order_perspectives.prompt("skeptic", {}, tmp_path)
    assert "'fr'" in order_perspectives.prompt("skeptic", {}, tmp_path, "fr")


def test_context_supplies_open_and_merged_pr_history(tmp_path):
    tracker = FakeTracker()
    human = tracker.create_pr(head="human/readme", base="main", title="Human docs", body="")
    tracker.merge_pr(human, expected_head_sha=tracker.pr_head_sha(human))
    ours = tracker.create_pr(head="foragent/issue-7", base="main", title="Lane work", body="Closes #7")
    tracker.merge_pr(ours, expected_head_sha=tracker.pr_head_sha(ours))
    pending = tracker.create_pr(head="human/wip", base="main", title="Open work", body="")
    prs = order.context(tmp_path, Config(), tracker)["pull_requests"]
    assert [(pr["number"], pr["state"], pr["foragent"]) for pr in prs] == [
        (pending, "open", False), (ours, "merged", True), (human, "merged", False)]
    assert [pr["title"] for pr in prs] == ["Open work", "Lane work", "Human docs"]
    assert all(pr["merged_at"] for pr in prs[1:]) and prs[0]["merged_at"] is None


def test_reference_image_extension_rejects_plain_string_and_matches_case_insensitively(tmp_path):
    import base64

    from foragent import order_references

    with pytest.raises(order.PlanError, match="path and base64"):
        order_references.validate({"reference_files": {"docs/design/a/shot.PNG": "not an object"}}, tmp_path)
    name = "docs/design/a/shot.JPEG"
    data = b"\xff\xd8\xff\xd9"
    files = order_references.validate(
        {"reference_files": {name: {"path": name, "base64": base64.b64encode(data).decode()}}}, tmp_path)
    assert files == {name: data}


def test_preread_reference_snapshot_holds_only_selected_files_and_is_removed_after_reader(
    publishing, plan, monkeypatch,
):
    import base64

    root, config = publishing[0], publishing[1]
    image = "docs/design/a/shot.gif"
    plan["reference_files"] = {"docs/design/a/HANDOFF.md": "keep",
                               image: {"path": image, "base64": base64.b64encode(b"GIF89a").decode()}}
    for item in plan["issues"]:
        item["references"] = [image]
    snapshots = root / ".foragent/references"
    seen = []

    class Reader:
        def run(self, **kwargs):
            seen.append(sorted(p.relative_to(snapshots).as_posix().split("/", 2)[2]
                               for p in snapshots.rglob("*") if p.is_file()))
            return SimpleNamespace(exit_code=0, timed_out=False, last_message="NONE")

    monkeypatch.setattr(order, "executor_for", lambda name: Reader())
    order.preread(plan, root, config)
    assert seen == [[image]] * len(plan["issues"])
    # Only the empty content-addressed parent stays; each reader removed its own directory.
    parents = list(snapshots.iterdir())
    assert len(parents) == 1 and not any(parents[0].iterdir()) and not (root / image).exists()


@pytest.mark.parametrize("names", [
    ["docs/design/a", "docs/design/a/HANDOFF.md"],
    ["docs/design/a/HANDOFF.md", "docs/design/a"],
])
def test_conflicting_reference_destinations_are_rejected_before_any_write(publishing, plan, names, monkeypatch):
    root, config, tracker, git, events = publishing
    plan["reference_files"] = {name: "approved" for name in names}
    monkeypatch.setattr(order, "executor_for", lambda name: pytest.fail("no reader may start"))
    with pytest.raises(order.PlanError, match="conflicting published paths: docs/design/a "):
        order.publish(plan, root, config, tracker, repo="owner/repo", git=git)
    with pytest.raises(order.PlanError, match="conflicting published paths"):
        order.preread(plan, root, config)
    assert events == [] and not (root / "docs").exists()
    assert not (root / ".foragent/preread-references").exists()


@pytest.mark.parametrize("existing", ["directory", "file-ancestor", "dangling-ancestor", "symlink"])
def test_reference_destination_blocked_on_disk_is_rejected_before_any_write(publishing, plan, existing):
    root, config, tracker, git, events = publishing
    name = "docs/design/a/HANDOFF.md"
    (root / "docs/design").mkdir(parents=True)
    if existing == "directory":
        (root / name).mkdir(parents=True)
    elif existing == "file-ancestor":
        (root / "docs/design/a").write_text("a file")
    elif existing == "dangling-ancestor":
        (root / "docs/design/a").symlink_to(root / "missing")
    else:
        (root / "docs/design/a").mkdir()
        (root / name).symlink_to(root / "docs/design/target.md")
    plan["reference_files"] = {name: "approved", "docs/design/b.md": "other"}
    with pytest.raises(order.PlanError, match="docs/design/"):
        order.publish(plan, root, config, tracker, repo="owner/repo", git=git)
    assert events == [] and not (root / "docs/design/b.md").exists()
    assert not (root / f"docs/specs/{plan['id']}.md").exists()


def test_acceptance_destination_that_is_a_directory_is_rejected_before_any_write(publishing, plan):
    root, config, tracker, git, events = publishing
    name = next(iter(plan["issues"][0]["acceptance_files"]))
    (root / name).mkdir(parents=True)
    with pytest.raises(order.PlanError, match="existing directory"):
        order.publish(plan, root, config, tracker, repo="owner/repo", git=git)
    assert events == [] and not (root / f"docs/specs/{plan['id']}.md").exists()


def test_preread_snapshot_is_removed_when_materializing_fails(tmp_path, monkeypatch):
    from foragent import order_references

    plan = {"reference_files": {"docs/design/a/one.md": "1", "docs/design/b/two.md": "2"}}
    real = order_references._write_new
    calls = []

    def failing(path, data):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("disk full")
        return real(path, data)

    monkeypatch.setattr(order_references, "_write_new", failing)
    with pytest.raises(OSError):
        order_references.materialize(plan, {}, tmp_path)
    parent = order_references.snapshot_dir(tmp_path, {"docs/design/a/one.md": "1", "docs/design/b/two.md": "2"})
    assert len(calls) == 2 and parent.is_dir() and not any(parent.iterdir())


@pytest.mark.parametrize("references,expected", [
    (None, ["docs/design/a/HANDOFF.md", "docs/design/a/draft.html"]),
    ([], []),
])
def test_edit_references_default_to_every_plan_reference_and_empty_selects_none(publishing, plan, references, expected):
    root, config, tracker, git, _events = publishing
    tracker.add_issue(9, "Existing visual issue", "old", [])
    plan["reference_files"] = {name: "approved" for name in ["docs/design/a/HANDOFF.md", "docs/design/a/draft.html"]}
    edit = {"number": 9, "goal": "Use the design", "out_of_scope": "Other screens", "done_when": ["true"]}
    if references is not None:
        edit["references"] = references
    plan["edits"] = [edit]
    order.publish(plan, root, config, tracker, repo="owner/repo", git=git)
    body = tracker.get_issue(9).body
    links = re.findall(r"^- \[(docs/design/[^\]]+)\]\(https://github\.com/owner/repo/blob/main/", body, re.M)
    assert links == expected
    assert ("## References\n" in body) == bool(expected)



SNAPSHOT_PLAN = {"reference_files": {"docs/design/a/HANDOFF.md": "approved"}}


def _outside(tmp_path):
    outside = tmp_path / "outside"
    (outside / "docs/design/a").mkdir(parents=True)
    (outside / "docs/design/a/HANDOFF.md").write_text("keep me")
    return outside


def _snapshot_name(root):
    from foragent import order_references

    return order_references.snapshot_dir(root, {"docs/design/a/HANDOFF.md": "approved"}).name


@pytest.mark.parametrize("link", [".foragent", ".foragent/references", "hash", "inner"])
def test_preread_snapshot_never_writes_or_deletes_through_symlinks(tmp_path, link):
    from foragent import order_references

    root = tmp_path / "repo"
    root.mkdir()
    outside = _outside(tmp_path)
    snapshots = root / order_references.SNAPSHOTS
    if link == ".foragent":
        (root / ".foragent").symlink_to(outside, target_is_directory=True)
    elif link == ".foragent/references":
        (root / ".foragent").mkdir()
        (outside / _snapshot_name(root)).mkdir()
        (outside / _snapshot_name(root) / "keep.txt").write_text("keep me")
        snapshots.symlink_to(outside, target_is_directory=True)
    elif link == "hash":
        snapshots.mkdir(parents=True)
        (snapshots / _snapshot_name(root)).symlink_to(outside, target_is_directory=True)
    else:
        # Another reader's directory, holding a link outside, is not ours to touch.
        other = snapshots / _snapshot_name(root) / "reader-other"
        other.mkdir(parents=True)
        (other / "docs").symlink_to(outside / "docs", target_is_directory=True)
    if link == "inner":
        snapshot = order_references.materialize(SNAPSHOT_PLAN, {}, root)
        assert snapshot.parent == other.parent and snapshot != other
        assert (snapshot / "docs").is_dir() and not (snapshot / "docs").is_symlink()
        assert (snapshot / "docs/design/a/HANDOFF.md").read_text() == "approved"
        order_references.remove(root, snapshot)
        assert not snapshot.exists() and (other / "docs").is_symlink()
    else:
        with pytest.raises(order.PlanError, match="real directory"):
            order_references.materialize(SNAPSHOT_PLAN, {}, root)
    assert (outside / "docs/design/a/HANDOFF.md").read_text() == "keep me"
    assert sorted(p.relative_to(outside).as_posix() for p in outside.rglob("*")) == sorted(
        ["docs", "docs/design", "docs/design/a", "docs/design/a/HANDOFF.md",
         *([_snapshot_name(root), f"{_snapshot_name(root)}/keep.txt"]
           if link == ".foragent/references" else [])])


def test_preread_snapshot_removal_skips_a_path_redirected_while_the_reader_ran(tmp_path):
    import shutil

    from foragent import order_references

    root = tmp_path / "repo"
    root.mkdir()
    outside = _outside(tmp_path)
    snapshot = order_references.materialize(SNAPSHOT_PLAN, {}, root)
    parent = snapshot.parent
    shutil.rmtree(parent)
    parent.symlink_to(outside, target_is_directory=True)
    (outside / snapshot.name).mkdir()
    with pytest.raises(order.PlanError, match="real directory"):
        order_references.remove(root, snapshot)
    assert (outside / snapshot.name).is_dir()


def test_overlapping_prereads_own_separate_reader_directories(tmp_path):
    from foragent import order_references

    first = order_references.materialize(SNAPSHOT_PLAN, {}, tmp_path)
    second = order_references.materialize(SNAPSHOT_PLAN, {}, tmp_path)
    assert first != second and first.parent == second.parent
    order_references.remove(tmp_path, second)
    assert (first / "docs/design/a/HANDOFF.md").read_text() == "approved"
    order_references.remove(tmp_path, first)
    assert first.parent.is_dir() and not any(first.parent.iterdir())


def test_preread_prompt_points_to_the_reader_directory_but_digest_uses_the_shared_path(publishing, plan):
    import base64

    from foragent import order_references

    root, config = publishing[0], publishing[1]
    image = "docs/design/a/shot.gif"
    plan["reference_files"] = {image: {"path": image, "base64": base64.b64encode(b"GIF89a").decode()}}
    item = plan["issues"][0]
    shared = order_references.snapshot_dir(root, {image: b"GIF89a"})
    _setup, free, digest = order.preread_target(plan, item, config, root)
    assert str(shared / image) in free.splitlines()
    first = order_references.materialize(plan, item, root)
    second = order_references.materialize(plan, item, root)
    prompts = []
    for reader in (first, second):
        _setup, sent, key = order.preread_target(plan, item, config, root, reader)
        assert str(reader / image) in sent.splitlines() and str(shared / image) not in sent.splitlines()
        assert key == digest and sent.replace(str(reader), str(shared)) == free
        prompts.append(sent)
    assert prompts[0] != prompts[1]


class RefreshingGit(PublishingGit):
    """A base refresh that brings in a symlinked destination directory."""

    def __init__(self, root, events, link):
        super().__init__(root, events)
        self.link = link

    def update_base(self, branch):
        target = self.root / "elsewhere"
        target.mkdir(exist_ok=True)
        (target / "keep.txt").write_text("keep me")
        path = self.root / self.link
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("link", ["docs/design", "docs/design/a", "docs/specs", "tests/acceptance", "docs"])
def test_publication_checks_destinations_after_the_base_refresh_before_any_write(publishing, plan, link):
    root, config, tracker, _git, events = publishing
    plan["reference_files"] = {"docs/design/a/HANDOFF.md": "approved"}
    git = RefreshingGit(root, events, link)
    with pytest.raises(order.PlanError, match="symlink"):
        order.publish(plan, root, config, tracker, repo="owner/repo", git=git)
    assert sorted(p.name for p in (root / "elsewhere").iterdir()) == ["keep.txt"]
    assert (root / "elsewhere/keep.txt").read_text() == "keep me"
    assert events == []
    written = [p for p in root.rglob("*") if p.is_file() and not p.is_relative_to(root / "elsewhere")
               and p.suffix in {".md", ".py"}]
    assert written == [], written


def test_publication_rechecks_each_destination_right_before_writing_it(publishing, plan, monkeypatch):
    from pathlib import Path

    root, config, tracker, git, events = publishing
    plan["reference_files"] = {"docs/design/a/HANDOFF.md": "approved"}
    from foragent import order_files

    elsewhere = root / "elsewhere"
    elsewhere.mkdir()
    real = order_files.os.replace

    def write_then_swap(source, destination):
        # Swap a parent for a link right after the first published file lands.
        result = real(source, destination)
        if not (root / "docs/design").exists():
            (root / "docs").mkdir(exist_ok=True)
            (root / "docs/design").symlink_to(elsewhere, target_is_directory=True)
        return result

    monkeypatch.setattr(order_files.os, "replace", write_then_swap)
    with pytest.raises(order.PlanError, match="symlink"):
        order.publish(plan, root, config, tracker, repo="owner/repo", git=git)
    assert list(elsewhere.iterdir()) == [] and events == []


@pytest.mark.parametrize("name", [".gitignore", "preread.json.tmp"])
def test_preread_local_files_never_write_through_symlinks(publishing, plan, monkeypatch, tmp_path, name):
    root, config = publishing[0], publishing[1]
    target = tmp_path / f"outside-{name}"
    (root / ".foragent").mkdir()
    (root / ".foragent" / name).symlink_to(target)

    class Reader:
        def run(self, **kwargs):
            return SimpleNamespace(exit_code=0, timed_out=False, last_message="NONE")

    monkeypatch.setattr(order, "executor_for", lambda executor: Reader())
    if name == ".gitignore":
        with pytest.raises(order.PlanError, match="symlink"):
            order.preread(plan, root, config)
    else:
        order.preread(plan, root, config)
        assert json.loads((root / ".foragent/preread.json").read_text())
    assert not target.exists()


SPECIAL_NAMES = ["docs/design/topic/draft].html", "docs/design/topic/a[b].md", "docs/design/topic/co`de`.md",
                 "docs/design/topic/*bold*.md", "docs/design/topic/<b>x.md", "docs/design/topic/a&amp;b.md",
                 "docs/design/topic/_lead_.md", "docs/design/topic/~x~.md", "docs/design/topic/(p).md"]
LINK = re.compile(r"^(?:- )?\[((?:\\[!-/:-@\[-`{-~]|[^\\\[\]`<&*~])*)\]\((https://github\.com/owner/repo/blob/main/[^\s()<>]+)\)$")


def _link_labels(body):
    """Read links the way CommonMark does: only escaped specials may appear in the label."""
    labels = {}
    for line in body.splitlines():
        if match := LINK.match(line):
            label = re.sub(r"\\(.)", r"\1", match[1])
            labels[label] = match[2]
    return labels


def test_reference_and_edit_links_escape_markdown_specials_in_file_names(publishing, plan):
    from urllib.parse import quote

    root, config, tracker, git, _events = publishing
    tracker.add_issue(9, "Existing visual issue", "old", [])
    plan["reference_files"] = {name: "approved" for name in SPECIAL_NAMES}
    plan["edits"] = [{"number": 9, "goal": "Use the design", "out_of_scope": "Other screens", "done_when": ["true"]}]
    order.publish(plan, root, config, tracker, repo="owner/repo", git=git)
    child = next(row.number for row in tracker.list_issues() if row.title == plan["issues"][0]["title"])
    for number in (child, 9):
        labels = _link_labels(tracker.get_issue(number).body)
        for name in SPECIAL_NAMES:
            assert labels.get(name) == f"https://github.com/owner/repo/blob/main/{quote(name)}", (number, name)


def test_acceptance_links_escape_specials_but_keep_ordinary_names_unchanged():
    from foragent.order_files import issue_body

    item = {"goal": "g", "out_of_scope": "o", "done_when": ["true"],
            "acceptance_files": {"tests/acceptance/test_a]b.py": "", "tests/acceptance/test_login_flow.py": ""}}
    body = issue_body(item, "owner/repo", "main")
    assert _link_labels(body)["tests/acceptance/test_a]b.py"].endswith("/test_a%5Db.py")
    assert ("[tests/acceptance/test_login_flow.py](https://github.com/owner/repo/blob/main/"
            "tests/acceptance/test_login_flow.py)") in body.splitlines()


def test_publication_write_must_stay_inside_its_own_root(tmp_path):
    from foragent.order_files import check_write

    (tmp_path / "docs/design").mkdir(parents=True)
    check_write(tmp_path, "docs/design/a.md", "docs/design")
    check_write(tmp_path, "tests/acceptance/test_a.py", "tests/acceptance/")
    for name, own in (("docs/specs/x.md", "docs/design"), ("docs/design-other/a.md", "docs/design"),
                      ("tests/test_a.py", "tests/acceptance/")):
        with pytest.raises(order.PlanError, match="outside"):
            check_write(tmp_path, name, own)


def _batch(publishing, plan, boundary="next-code-merge", **kwargs):
    from foragent import order_batch

    root, config, tracker, _git, _ = publishing
    return order_batch.defer(plan, boundary, root, config, tracker, repo="owner/repo", **kwargs)


def _flush(publishing, boundary="next-code-merge"):
    from foragent import order_batch

    root, config, tracker, git, _ = publishing
    return order_batch.flush(boundary, root, config, tracker, repo="owner/repo", git=git)


def test_batch_redeferred_order_replaces_its_waiting_version(publishing, plan):
    from foragent import order_batch

    _batch(publishing, plan, approve=True)
    changed = deepcopy(plan)
    changed["title"] = "Login MVP v2"
    _batch(publishing, changed)
    rows = order_batch.pending(publishing[0])["orders"]
    assert [(row["title"], row["approve"]) for row in rows] == [("Login MVP v2", False)]
    _flush(publishing)
    parent, = [issue for issue in publishing[2].list_issues() if issue.title == "Login MVP v2"]
    assert not any("approved" in issue.labels for issue in publishing[2].list_issues())
    assert order_batch.pending(publishing[0])["orders"][0]["parent"] == parent.number


def test_batch_rejects_two_waiting_orders_writing_different_content_before_writes(publishing, plan):
    other = deepcopy(plan)
    other["id"], other["title"] = "2026-10-31-other", "Other"
    other["issues"][0]["acceptance_files"] = {FILE: "assert 1\n"}
    _batch(publishing, plan)
    _batch(publishing, other)
    with pytest.raises(order.PlanError, match=FILE):
        _flush(publishing)
    assert publishing[4] == [] and publishing[2].issues == {}


def test_batch_note_is_appended_once_even_when_its_commit_is_retried(publishing, monkeypatch):
    from foragent import order_batch

    root, git = publishing[0], publishing[3]
    (root / "docs").mkdir()
    (root / "docs/ideas.md").write_text("# Ideas\nOld.\n")
    order_batch.note(root, "idea", "A new idea.", "next-code-merge", "en")
    order_batch.note(root, "idea", "Later idea.", "a-later-time", "en")
    original = git.push_base
    monkeypatch.setattr(git, "push_base", lambda branch: (_ for _ in ()).throw(GitError("interrupted")))
    with pytest.raises(GitError):
        _flush(publishing)
    monkeypatch.setattr(git, "push_base", original)
    _flush(publishing)  # The resume pushes the existing commit; it does not append again.
    content = (root / "docs/ideas.md").read_text()
    assert content == "# Ideas\nOld.\n\nA new idea.\n"
    assert [event[0] for event in publishing[4]] == ["commit", "push"]
    statuses = {row["text"]: row["status"] for row in order_batch.pending(root)["notes"]}
    assert statuses == {"A new idea.": "published", "Later idea.": "waiting"}


@pytest.mark.parametrize("boundary", ["", "  ", "a\nb"])
def test_batch_boundary_must_be_one_named_line(publishing, plan, boundary):
    from foragent import order_batch

    with pytest.raises(order.PlanError, match="boundary"):
        _batch(publishing, plan, boundary)
    with pytest.raises(order.PlanError, match="boundary"):
        order_batch.note(publishing[0], "decision", "Text", boundary, "en")
    assert not (publishing[0] / ".foragent/order-batch.json").exists()


def _same_second(monkeypatch):
    from foragent import order_batch

    monkeypatch.setattr(order_batch, "_now", lambda: "2026-10-31T00:00:00+00:00")


def test_batch_redeferred_during_its_flush_in_the_same_second_stays_waiting(publishing, plan, monkeypatch):
    from foragent import order_batch

    _same_second(monkeypatch)
    _batch(publishing, plan)
    tracker, replacement = publishing[2], deepcopy(plan)
    replacement["title"] = "Login MVP v2"
    original = tracker.create_issue

    def redefer_once(*args, **kwargs):
        if not order_batch.pending(publishing[0])["orders"][-1]["title"].endswith("v2"):
            _batch(publishing, replacement)
        return original(*args, **kwargs)

    monkeypatch.setattr(tracker, "create_issue", redefer_once)
    _flush(publishing)
    rows = order_batch.pending(publishing[0])["orders"]
    assert [(row["title"], row["status"]) for row in rows] == [("Login MVP v2", "waiting")]
    monkeypatch.setattr(tracker, "create_issue", original)
    _flush(publishing)
    assert [row["status"] for row in order_batch.pending(publishing[0])["orders"]] == ["published"]
    assert any(issue.title == "Login MVP v2" for issue in tracker.list_issues())


def test_batch_identical_notes_in_the_same_second_are_both_published(publishing, monkeypatch):
    from foragent import order_batch

    _same_second(monkeypatch)
    root = publishing[0]
    first = order_batch.note(root, "decision", "Same text.", "next-code-merge", "en")
    second = order_batch.note(root, "decision", "Same text.", "next-code-merge", "en")
    assert first["id"] != second["id"]
    _flush(publishing)
    assert (root / "docs/decisions.md").read_text().count("Same text.") == 2


def _lane_plan(plan, identity):
    value = deepcopy(plan)
    value["id"], value["title"] = f"2026-10-31-{identity}", f"Order {identity}"
    value["new_lanes"] = [{"name": "web", "paths": ["web/**"], "skills": []}]
    value["issues"] = [dict(value["issues"][1], key=identity, title=f"{identity} web", lane="web", blocked_by=[])]
    return value


@pytest.mark.parametrize("count", [1, 2])
def test_batch_orders_creating_the_same_new_lane_publish_with_the_created_lane(publishing, plan, count):
    plans = [_lane_plan(plan, f"web{index}") for index in range(count)]
    for value in plans:
        _batch(publishing, value)
    _flush(publishing)
    root, tracker, events = publishing[0], publishing[2], publishing[4]
    assert load_config(root).lanes["web"]["paths"] == ["web/**"]
    _one_batch_commit(events, ".foragent.yml", ".claude/agents/fa-web.md")
    titles = {issue.title: issue for issue in tracker.list_issues()}
    for value in plans:
        assert "lane:web" in titles[value["issues"][0]["title"]].labels


def test_batch_rejects_one_new_lane_declared_two_ways_before_writes(publishing, plan):
    first, second = _lane_plan(plan, "one"), _lane_plan(plan, "two")
    second["new_lanes"][0]["paths"] = ["site/**"]
    _batch(publishing, first)
    _batch(publishing, second)
    with pytest.raises(order.PlanError, match="web"):
        _flush(publishing)
    assert publishing[4] == [] and not (publishing[0] / ".claude/agents/fa-web.md").exists()


def test_batch_refuses_a_done_when_file_that_changed_after_deferral(publishing, plan):
    from foragent import order_batch

    root = publishing[0]
    existing = "tests/acceptance/test_existing.py"
    (root / "tests/acceptance").mkdir(parents=True, exist_ok=True)
    (root / existing).write_text("def test_strong():\n    assert False\n")
    # Done when names a test already in the repository, so the plan JSON has no copy of it.
    plan["issues"][1]["done_when"] = [f"uv run pytest -q {existing}"]
    _batch(publishing, plan, approve=True)
    (root / existing).write_text("def test_strong():\n    pass\n")
    with pytest.raises(order.PlanError, match=f"changed since.*{existing}"):
        _flush(publishing)
    assert publishing[4] == [] and publishing[2].issues == {}
    assert order_batch.pending(root)["orders"][0]["status"] == "waiting"
    _batch(publishing, plan, approve=True)  # Deferring again snapshots the current files.
    _flush(publishing)
    assert [event[0] for event in publishing[4]][:2] == ["commit", "push"]
    assert all("approved" in issue.labels for issue in publishing[2].list_issues()
               if issue.title in {item["title"] for item in plan["issues"]})


def test_batch_rule_note_replaces_the_current_rule_line_once_with_its_order(publishing, plan, monkeypatch):
    from foragent import order_batch

    root, git = publishing[0], publishing[3]
    (root / "docs").mkdir()
    (root / "docs/rules.md").write_text("# Rules\n- Publish at once. (D10)\n- Keep tests locked. (D11)\n")
    _batch(publishing, plan)
    order_batch.note(root, "decision", "D12. Batch publication (supersedes D10).", "next-code-merge", "en")
    order_batch.note(root, "rule", "- Publish in batches. (D12)", "next-code-merge", "en",
                     replace="- Publish at once. (D10)")
    assert (root / "docs/rules.md").read_text().count("Publish at once") == 1  # waiting, unchanged
    original = git.push_base
    monkeypatch.setattr(git, "push_base", lambda branch: (_ for _ in ()).throw(GitError("interrupted")))
    with pytest.raises(GitError):
        _flush(publishing)
    monkeypatch.setattr(git, "push_base", original)
    _flush(publishing)  # The resume finds the replaced line gone and must not need it.
    rules = (root / "docs/rules.md").read_text()
    assert rules == "# Rules\n- Publish in batches. (D12)\n- Keep tests locked. (D11)\n"
    assert (root / "docs/decisions.md").read_text().count("D12. Batch publication") == 1
    commit, = [event for event in publishing[4] if event[0] == "commit"]
    assert {"docs/rules.md", "docs/decisions.md", f"docs/specs/{plan['id']}.md"} <= commit[2].keys()


@pytest.mark.parametrize("rules", [None, "# Rules\n- Something else. (D9)\n"])
def test_batch_rule_note_without_its_line_is_refused_before_writes(publishing, plan, rules):
    from foragent import order_batch

    root = publishing[0]
    (root / "docs").mkdir()
    if rules is not None:
        (root / "docs/rules.md").write_text(rules)
    _batch(publishing, plan)
    order_batch.note(root, "rule", "- New. (D12)", "next-code-merge", "en", replace="- Old. (D10)")
    with pytest.raises(order.PlanError, match="docs/rules.md"):
        _flush(publishing)
    assert publishing[4] == [] and publishing[2].issues == {}
    assert not (root / f"docs/specs/{plan['id']}.md").exists()


@pytest.mark.parametrize("kind,text,replace", [
    ("rule", "two\nlines", None), ("rule", "- New.", "old\nlines"), ("decision", "D12.", "- Old."),
])
def test_batch_rule_lines_are_single_and_replace_is_rule_only(publishing, kind, text, replace):
    from foragent import order_batch

    with pytest.raises(order.PlanError):
        order_batch.note(publishing[0], kind, text, "next-code-merge", "en", replace=replace)


def _rules_case(publishing, plan):
    from foragent import order_batch

    root = publishing[0]
    (root / "docs").mkdir()
    (root / "docs/rules.md").write_text("# Rules\n- A. (D1)\n")
    publishing[3].saved["docs/rules.md"] = (root / "docs/rules.md").read_text()  # committed on the base
    _batch(publishing, plan)
    order_batch.note(root, "rule", "- B. (D2)", "next-code-merge", "en", replace="- A. (D1)")
    order_batch.note(root, "rule", "- C. (D3)", "next-code-merge", "en", replace="- B. (D2)")
    order_batch.note(root, "rule", "- Extra. (D4)", "next-code-merge", "en")
    return root


@pytest.mark.parametrize("interrupt", ["push_base", "commit_paths"])
def test_batch_resume_recognizes_superseded_rule_replacements(publishing, plan, monkeypatch, interrupt):
    from foragent import order_batch

    root, git = _rules_case(publishing, plan), publishing[3]
    interrupt = {"commit_paths": "commit_recorded"}.get(interrupt, interrupt)  # the batch commit
    original = getattr(git, interrupt)

    def fail(*args):
        raise GitError("interrupted")

    monkeypatch.setattr(git, interrupt, fail)
    with pytest.raises(GitError):
        _flush(publishing)
    monkeypatch.setattr(git, interrupt, original)
    _flush(publishing)
    assert (root / "docs/rules.md").read_text() == "# Rules\n- C. (D3)\n- Extra. (D4)\n"
    state = order_batch.pending(root)
    assert {row["status"] for row in [*state["orders"], *state["notes"]]} == {"published"}
    assert [event[0] for event in publishing[4]].count("commit") == 1


def test_batch_resume_keeps_a_later_change_to_a_committed_rule_line(publishing, plan, monkeypatch):
    from foragent import order_batch

    root, git = _rules_case(publishing, plan), publishing[3]
    original = git.push_base
    monkeypatch.setattr(git, "push_base", lambda branch: (_ for _ in ()).throw(GitError("interrupted")))
    with pytest.raises(GitError):
        _flush(publishing)
    monkeypatch.setattr(git, "push_base", original)
    # A later boundary edits the same line before the interrupted batch resumes.
    order_batch.note(root, "rule", "- D. (D5)", "later", "en", replace="- C. (D3)")
    order_batch.note(root, "rule", "- Extra2. (D6)", "later", "en", replace="- Extra. (D4)")
    _flush(publishing, "later")
    spec = root / f"docs/specs/{plan['id']}.md"  # someone also edits the committed order's spec
    spec.write_text("# Login\nClarified later.\n")
    git.saved[f"docs/specs/{plan['id']}.md"] = spec.read_text()
    _flush(publishing)
    assert (root / "docs/rules.md").read_text() == "# Rules\n- D. (D5)\n- Extra2. (D6)\n"
    assert spec.read_text() == "# Login\nClarified later.\n"
    assert order_batch.pending(root)["orders"][0]["status"] == "published"
    assert any(issue.title == plan["issues"][0]["title"] for issue in publishing[2].list_issues())
    assert {row["status"] for row in order_batch.pending(root)["notes"]} == {"published"}



def _checkout(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and not {".git", ".foragent", "home"} & set(p.relative_to(root).parts)}


def test_batch_failed_commit_leaves_the_checkout_and_a_later_boundary_cannot_strand_it(
    publishing, plan, monkeypatch,
):
    from foragent import order_batch

    root, git = _rules_case(publishing, plan), publishing[3]
    before = _checkout(root)
    original = git.commit_recorded
    monkeypatch.setattr(git, "commit_recorded", lambda *args: (_ for _ in ()).throw(GitError("interrupted")))
    with pytest.raises(GitError):
        _flush(publishing)
    assert _checkout(root) == before, "An uncommitted batch write is undone, specs and tests included"
    monkeypatch.setattr(git, "commit_recorded", original)
    order_batch.note(root, "rule", "- Unrelated. (D9)", "later", "en")
    _flush(publishing, "later")
    _flush(publishing)
    assert (root / "docs/rules.md").read_text() == "# Rules\n- C. (D3)\n- Unrelated. (D9)\n- Extra. (D4)\n"
    state = order_batch.pending(root)
    assert {row["status"] for row in [*state["orders"], *state["notes"]]} == {"published"}


@pytest.mark.parametrize("stop", ["before_commit", "after_commit"])
def test_batch_killed_flush_is_settled_from_head_by_the_next_flush(publishing, plan, monkeypatch, stop):
    from foragent import order_batch, order_batch_journal

    root, git = _rules_case(publishing, plan), publishing[3]
    recover = order_batch_journal.recover
    monkeypatch.setattr(order_batch_journal, "recover", lambda *args: None)  # the process dies: no cleanup
    if stop == "before_commit":
        monkeypatch.setattr(git, "commit_recorded", lambda *args: (_ for _ in ()).throw(SystemExit(9)))
    else:
        monkeypatch.setattr(order_batch, "_committed", lambda *args: (_ for _ in ()).throw(SystemExit(9)))
    with pytest.raises(SystemExit):
        _flush(publishing)
    monkeypatch.undo()
    monkeypatch.setattr(order_batch_journal, "recover", recover)
    order_batch.note(root, "rule", "- Unrelated. (D9)", "later", "en")
    _flush(publishing, "later")  # any boundary settles the journal before reading files
    _flush(publishing)
    assert (root / "docs/rules.md").read_text().count("- C. (D3)") == 1
    assert "- A. (D1)" not in (root / "docs/rules.md").read_text()
    assert "- Unrelated. (D9)" in (root / "docs/rules.md").read_text()
    commits = [event[1] for event in publishing[4] if event[0] == "commit"]
    assert len(commits) == 2, commits  # one per boundary, never a repeat of the first batch
    state = order_batch.pending(root)
    assert {row["status"] for row in [*state["orders"], *state["notes"]]} == {"published"}
    assert not (root / ".foragent/order-batch-journal.json").exists()


def test_batch_recovery_refuses_a_file_edited_after_the_killed_write(publishing, plan, monkeypatch):
    from foragent import order_batch, order_batch_journal

    root, git = _rules_case(publishing, plan), publishing[3]
    monkeypatch.setattr(order_batch_journal, "recover", lambda *args: None)
    monkeypatch.setattr(git, "commit_recorded", lambda *args: (_ for _ in ()).throw(SystemExit(9)))
    with pytest.raises(SystemExit):
        _flush(publishing)
    monkeypatch.undo()
    (root / "docs/rules.md").write_text((root / "docs/rules.md").read_text() + "- Hand edit.\n")
    # Name kept from the earlier rule. Since the 2026-10-31 rule, a missing batch commit means
    # every listed path goes back to its recorded bytes; the flush then runs again.
    _flush(publishing)
    assert (root / "docs/rules.md").read_text() == "# Rules\n- C. (D3)\n- Extra. (D4)\n"
    assert not (root / ".foragent/order-batch-journal.json").exists()


def _skill_manager(root):
    library = root.parent / f"{root.name}-library"
    skill = library / "skills/testing-strategy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: testing-strategy\ndescription: Tests\n---\nUse tests.\n")
    (library / "LICENSE").write_text("SPDX-License-Identifier: MIT\nMIT License\n")
    return SkillManager(Config(skill_libraries=[{"name": "local", "repo": "local", "path": str(library)}]),
                        run=lambda args: "a" * 40 + "\n")


LANE_STEPS = ["skill_copy", "gitignore_written", "config_saved", "agent_written", "lane_commit",
              "batch_commit", "batch_unrecorded", "push"]


@pytest.mark.parametrize("killed", [False, True], ids=["raised", "killed"])
@pytest.mark.parametrize("step", LANE_STEPS)
def test_batch_lane_creation_interrupted_at_each_write_resumes(publishing, plan, monkeypatch, step, killed):
    import pathlib

    from foragent import lanes, order_batch, order_batch_journal, skills

    root, config, tracker, git, events = publishing
    (root / ".gitignore").write_text("THIRD_PARTY_NOTICES.md\n")
    git.saved.update({name: (root / name).read_text() for name in (".foragent.yml", ".gitignore")})
    manager = _skill_manager(root)
    value = _lane_plan(plan, "web0")
    value["new_lanes"][0]["skills"] = ["local:testing-strategy"]
    order_batch.defer(value, "next-code-merge", root, config, tracker, repo="owner/repo", manager=manager)
    attributes = (root / ".gitattributes").read_bytes() if (root / ".gitattributes").exists() else None
    stop = SystemExit(9)
    write_text, commit_paths = pathlib.Path.write_text, git.commit_paths
    write_bytes = pathlib.Path.write_bytes

    def raise_stop(*args, **kwargs):
        raise stop

    if step == "skill_copy":  # the process dies part way through copying the skill
        def partial_copy(source, destination, *args, **kwargs):
            destination.mkdir(parents=True)
            (destination / "SKILL.md").write_text("---\nname: testing-str")
            raise stop
        monkeypatch.setattr(skills.shutil, "copytree", partial_copy)
    elif step == "gitignore_written":  # .gitignore is cut short before .foragent.yml is saved
        def truncated(path, data, *args, **kwargs):
            if path.name == ".gitignore":
                write_bytes(path, data[:len(data) // 2])
                raise stop
            return write_bytes(path, data, *args, **kwargs)
        monkeypatch.setattr(pathlib.Path, "write_bytes", truncated)
    elif step == "config_saved":  # .foragent.yml is saved; the agent file is not written yet
        def agent_fails(path, *args, **kwargs):
            if path.name == "fa-web.md":
                raise stop
            return write_text(path, *args, **kwargs)
        monkeypatch.setattr(pathlib.Path, "write_text", agent_fails)
    elif step == "agent_written":
        monkeypatch.setattr(lanes, "lane_files", raise_stop)
    elif step == "lane_commit":  # where add_lane commits: the batch defers that commit to its own
        monkeypatch.setattr(order_batch._DeferredCommits, "commit_paths", raise_stop)
    elif step == "batch_commit":
        # `git add` runs before the failing commit, so its paths are left staged.
        monkeypatch.setattr(git, "commit_recorded", lambda paths, title, record, contents=None: (git.stage(paths), raise_stop()))
    elif step == "batch_unrecorded":  # the batch commit landed; its sha is not recorded yet
        monkeypatch.setattr(order_batch_journal, "batch_done", raise_stop)
    else:
        monkeypatch.setattr(git, "push_base", raise_stop)
    if killed:
        monkeypatch.setattr(order_batch_journal, "recover", lambda *args: None)
    with pytest.raises(SystemExit):
        order_batch.flush("next-code-merge", root, config, tracker, repo="owner/repo", git=git, manager=manager)
    monkeypatch.undo()
    monkeypatch.setattr(order, "require_clean_preread", lambda *args: None)
    if not killed and step in {"skill_copy", "gitignore_written", "config_saved", "agent_written", "lane_commit",
                               "batch_commit"}:
        assert "web" not in load_config(root).lanes, "An uncommitted lane is undone with the batch"
        assert not (root / ".claude/agents/fa-web.md").exists()
        assert not (root / ".claude/skills/testing-strategy").exists()
        assert (root / ".gitignore").read_text() == "THIRD_PARTY_NOTICES.md\n"
        # add_lane also writes the lane-memory attributes; they go back with the rest.
        assert ((root / ".gitattributes").read_bytes() if (root / ".gitattributes").exists() else None) == attributes
    if not killed:
        assert git.index == {}, "rollback unstages lane and batch paths alike"
    stale = load_config(root)  # the CLI loads config before the flush settles the journal
    order_batch.flush("next-code-merge", root, stale, tracker, repo="owner/repo", git=git, manager=manager)
    assert load_config(root).lanes["web"]["paths"] == ["web/**"]
    assert (root / ".claude/agents/fa-web.md").is_file()
    assert (root / ".claude/skills/testing-strategy/SKILL.md").is_file()
    assert (root / ".gitignore").read_text().count("/.claude/skills/testing-strategy/") == 1
    _one_batch_commit(events, ".foragent.yml", ".claude/agents/fa-web.md", ".gitignore", ".gitattributes")
    issue, = [issue for issue in tracker.list_issues() if issue.title == "web0 web"]
    assert "lane:web" in issue.labels
    assert order_batch.pending(root)["orders"][0]["status"] == "published"
    assert not (root / ".foragent/order-batch-journal.json").exists()
    assert git.index == {}


@pytest.mark.parametrize("target", ["spec", "rules"])
def test_batch_write_killed_mid_file_leaves_old_bytes_and_resumes(publishing, plan, monkeypatch, target):
    import os

    from foragent import order_batch, order_batch_journal, order_files

    root = _rules_case(publishing, plan)
    name = f"docs/specs/{plan['id']}.md" if target == "spec" else "docs/rules.md"
    before = (root / name).read_bytes() if (root / name).exists() else None
    replace = os.replace

    def dies_before_rename(source, destination):
        if Path(destination).name == Path(name).name:
            raise SystemExit(9)  # the new bytes sit in the temporary file only
        return replace(source, destination)

    monkeypatch.setattr(order_files.os, "replace", dies_before_rename)
    monkeypatch.setattr(order_batch_journal, "recover", lambda *args: None)
    with pytest.raises(SystemExit):
        _flush(publishing)
    assert ((root / name).read_bytes() if (root / name).exists() else None) == before
    # A killed process skips its own cleanup: the half-written temporary file stays behind.
    order_files.temporary_path(root, name).parent.mkdir(parents=True, exist_ok=True)
    order_files.temporary_path(root, name).write_bytes(b"# Rules\n- C. (D")
    monkeypatch.undo()
    monkeypatch.setattr(order, "require_clean_preread", lambda *args: None)
    # The checkout is written only after the commit, so this batch landed: recovery alone
    # finishes the write from HEAD and leaves nothing staged or half written.
    assert order_batch.recover(root, publishing[3]) is True
    assert not order_files.temporary_path(root, name).exists()
    assert (root / name).read_text() == publishing[3].saved[name] and publishing[3].index == {}
    _flush(publishing)
    assert (root / "docs/rules.md").read_text() == "# Rules\n- C. (D3)\n- Extra. (D4)\n"
    assert {row["status"] for row in order_batch.pending(root)["notes"]} == {"published"}
    leftovers = [p for p in root.rglob("*.foragent-tmp")]
    assert leftovers == [], leftovers


@pytest.mark.parametrize("killed", [False, True], ids=["raised", "killed"])
def test_batch_second_new_lane_interrupted_keeps_the_committed_first_lane(publishing, plan, monkeypatch, killed):
    import pathlib

    from foragent import order_batch, order_batch_journal

    root, config, tracker, git, events = publishing
    git.saved[".foragent.yml"] = (root / ".foragent.yml").read_text()
    value = _lane_plan(plan, "two")
    value["new_lanes"].append({"name": "site", "paths": ["site/**"], "skills": []})
    value["issues"].append(dict(value["issues"][0], key="site", title="two site", lane="site"))
    _batch(publishing, value)
    write_text = pathlib.Path.write_text

    def second_agent_dies(path, *args, **kwargs):
        if path.name == "fa-site.md":
            raise SystemExit(9)
        return write_text(path, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", second_agent_dies)
    if killed:
        monkeypatch.setattr(order_batch_journal, "recover", lambda *args: None)
    with pytest.raises(SystemExit):
        _flush(publishing)
    monkeypatch.undo()
    monkeypatch.setattr(order, "require_clean_preread", lambda *args: None)
    if not killed:
        assert set(load_config(root).lanes) == {"api"}, "nothing of an uncommitted batch stays"
    order_batch.flush("next-code-merge", root, load_config(root), tracker, repo="owner/repo", git=git)
    assert {"web", "site"} <= set(load_config(root).lanes)
    _one_batch_commit(events, ".foragent.yml", ".claude/agents/fa-web.md", ".claude/agents/fa-site.md")
    assert order_batch.pending(root)["orders"][0]["status"] == "published"


def _order_cli(publishing, monkeypatch):
    """The real `fa order` CLI on the offline tracker and Git, reading .foragent.yml from disk."""
    root, _config, tracker, git, _events = publishing
    monkeypatch.setattr(cli, "GitRepo", lambda root: git)
    monkeypatch.setattr(git, "remote_slug", lambda: "owner/repo", raising=False)
    monkeypatch.setattr(cli, "GhTracker", lambda repo: tracker)
    monkeypatch.setenv("FORAGENT_VIEW", "off")
    return lambda *args: cli.main(["order", *args, "--repo-root", str(root)])


@pytest.mark.parametrize("damage", ["truncated", "empty"])
def test_batch_cli_flush_recovers_a_damaged_configuration_before_reading_it(publishing, plan, monkeypatch,
                                                                           capsys, damage):
    from foragent import config as config_module, order_batch, order_batch_journal

    root, config, tracker, git, events = publishing
    original = (root / ".foragent.yml").read_text()
    git.saved[".foragent.yml"] = original
    _batch(publishing, _lane_plan(plan, "web0"))
    save = config_module.LaneSettings.save

    def dies_mid_save(self):  # a writer killed part way leaves damaged YAML behind
        text = self.check()
        self.path.write_text(text[:len(text) // 3] + "lanes: {web: [" if damage == "truncated" else "")
        raise SystemExit(9)

    real_recover = order_batch_journal.recover
    monkeypatch.setattr(config_module.LaneSettings, "save", dies_mid_save)
    monkeypatch.setattr(order_batch_journal, "recover", lambda *args: None)  # killed: no cleanup
    with pytest.raises(SystemExit):
        _flush(publishing)
    monkeypatch.setattr(config_module.LaneSettings, "save", save)
    monkeypatch.setattr(order_batch_journal, "recover", real_recover)
    run = _order_cli(publishing, monkeypatch)
    assert run("pending", "--json") == 0  # the queue stays visible while the file is damaged
    capsys.readouterr()
    assert run("flush", "--boundary", "next-code-merge") == 0, capsys.readouterr().err
    assert load_config(root).lanes["web"]["paths"] == ["web/**"]
    assert order_batch.pending(root)["orders"][0]["status"] == "published"
    _one_batch_commit(events, ".foragent.yml", ".claude/agents/fa-web.md")


def test_lane_settings_save_is_atomic(tmp_path, monkeypatch):
    import os

    from foragent import config as config_module

    path = tmp_path / ".foragent.yml"
    path.write_text("lanes: {api: {paths: ['api/**'], agent: fa-api}}\n")
    settings = config_module.LaneSettings(path)
    settings.add_lane("web", {"paths": ["web/**"], "agent": "fa-web", "skills": []}, load_config(tmp_path).lanes)
    replace = os.replace
    monkeypatch.setattr(config_module.os, "replace", lambda *args: (_ for _ in ()).throw(SystemExit(9)))
    with pytest.raises(SystemExit):
        settings.save()
    assert path.read_text() == "lanes: {api: {paths: ['api/**'], agent: fa-api}}\n"
    assert [p.name for p in tmp_path.iterdir()] == [".foragent.yml"]
    monkeypatch.setattr(config_module.os, "replace", replace)
    settings.save()
    assert "web" in load_config(tmp_path).lanes


def test_batch_recovery_runs_only_on_the_branch_it_wrote_on(publishing, plan, monkeypatch):
    from foragent import order_batch, order_batch_journal

    root, git = _rules_case(publishing, plan), publishing[3]
    monkeypatch.setattr(order_batch_journal, "recover", lambda *args: None)
    monkeypatch.setattr(git, "commit_recorded", lambda *args: (_ for _ in ()).throw(SystemExit(9)))
    with pytest.raises(SystemExit):
        _flush(publishing)
    monkeypatch.undo()
    written = (root / "docs/rules.md").read_text()
    git.branch = "feature"
    with pytest.raises(order.PlanError, match="main"):
        order_batch.recover(root, git)
    assert (root / "docs/rules.md").read_text() == written
    git.branch = "main"
    assert order_batch.recover(root, git) is True
    assert (root / "docs/rules.md").read_text() == "# Rules\n- A. (D1)\n"


def make_legacy(tracker, number):
    """Turn an issue into one published before owner labels existed."""
    issue = tracker.get_issue(number)
    tracker.remove_labels(number, [label for label in issue.labels if label.startswith("by:")])
    tracker.update_issue(number, title=issue.title, body=issue.body.replace(order.OWNED_MARKER + "\n", ""))


def owners(tracker):
    return {n: {label for label in issue.labels if label.startswith("by:")} for n, issue in tracker.issues.items()}


def test_publish_without_an_authenticated_person_changes_nothing(publishing, plan):
    tracker, events = publishing[2], publishing[4]
    tracker.login = ""
    with pytest.raises(order.PlanError, match="GitHub account"):
        publish(publishing, plan, approve=True)
    assert events == [] and tracker.issues == {} and tracker.labels == {}


def test_owner_label_dropped_by_github_closes_the_issue_and_stops(publishing, plan):
    tracker = publishing[2]
    created = tracker.create_issue

    def drop_owner(title, body, labels):  # GitHub ignores labels its caller may not apply.
        return created(title, body, [label for label in labels if not label.startswith("by:")])

    tracker.create_issue = drop_owner
    with pytest.raises(order.PlanError, match="by:human"):
        publish(publishing, plan)
    assert [(n, issue.state) for n, issue in tracker.issues.items()] == [(1, "closed")]
    assert "by:human" in tracker.list_comments(1)[0].body


def test_republication_and_approval_keep_the_publishers_owner(publishing, plan):
    tracker = publishing[2]
    publish(publishing, plan)
    tracker.login = "Bob"
    plan["issues"].append({**deepcopy(plan["issues"][1]), "key": "late", "title": "Late child"})
    publish(publishing, plan, approve=True)
    assert owners(tracker) == {1: {"by:human"}, 2: {"by:human"}, 3: {"by:human"}, 4: {"by:human"}}
    assert "by:bob" not in tracker.labels


def test_republication_refuses_contradictory_owner_labels(publishing, plan):
    tracker = publishing[2]
    publish(publishing, plan)
    tracker.add_labels(3, ["by:bob"])
    before = deepcopy(tracker.issues)
    with pytest.raises(order.PlanError, match="by:human|bob"):
        publish(publishing, plan, approve=True)
    assert tracker.issues == before


def test_unlabelled_order_gets_new_children_for_its_legacy_owner(publishing, plan):
    root, config, tracker, git, _ = publishing
    publish(publishing, plan)
    for number in (1, 2, 3):  # An order published before owner labels existed.
        make_legacy(tracker, number)
    tracker.login = "bob"
    plan["issues"].append({**deepcopy(plan["issues"][1]), "key": "late", "title": "Late child"})
    order.publish(plan, root, replace(config, legacy_owner="Jin"), tracker, repo="owner/repo", git=git)
    assert owners(tracker) == {1: set(), 2: set(), 3: set(), 4: {"by:jin"}}


def test_edits_keep_the_existing_issue_owner(publishing, plan):
    tracker = publishing[2]
    tracker.add_issue(9, "Legacy", "old body", labels=["size:s"])
    tracker.add_issue(10, "Alice's", f"old body\n{order.OWNED_MARKER}\n", labels=["size:s", "by:alice"])
    tracker.add_issue(11, "Ownerless", f"old body\n{order.OWNED_MARKER}\n", labels=["size:s"])
    edit = {key: plan["issues"][1][key] for key in ("goal", "out_of_scope", "done_when", "checklist")}
    plan["issues"], plan["edits"] = [], [{**edit, "number": n} for n in (9, 10, 11)]
    publish(publishing, plan, approve=True)
    assert owners(tracker)[9] == set() and owners(tracker)[10] == {"by:alice"} and owners(tracker)[11] == set()
    # The rewritten body keeps each issue's owner-label contract, and only theirs.
    assert [order.OWNED_MARKER in tracker.get_issue(n).body for n in (9, 10, 11)] == [False, True, True]
    with pytest.raises(OwnershipError):
        effective_orderer(tracker.get_issue(11), replace(publishing[1], legacy_owner="jin"), "jin")
    events = {n: list(tracker.label_events(n)) for n in (9, 10, 11)}
    publish(publishing, plan, approve=True)  # Idempotent: the kept contract does not look like a change.
    assert {n: list(tracker.label_events(n)) for n in (9, 10, 11)} == events


@pytest.mark.parametrize("refused", ["parent", "child"])
@pytest.mark.parametrize("fault", ["label", "read", "read-unlabelled", "retire", "close", "retire-and-close"])
def test_retry_after_any_unconfirmed_creation_never_reuses_that_issue(publishing, plan, refused, fault):
    tracker = publishing[2]
    target = plan["title"] if refused == "parent" else plan["issues"][1]["title"]
    created, read, write, close = tracker.create_issue, tracker.get_issue, tracker.update_issue, tracker.close_issue
    victim = []

    def create(title, body, labels):
        if title == target and not victim:
            if fault != "read":  # GitHub drops a label its caller may not apply.
                labels = [label for label in labels if not label.startswith("by:")]
            victim.append(created(title, body, labels))
            return victim[0]
        return created(title, body, labels)

    def faulty(method, kinds, once=False):
        failed = []

        def call(n, *args, **kwargs):
            if victim and n == victim[0] and fault in kinds and not (once and failed):
                failed.append(n)
                raise TrackerError(f"{method.__name__} failed")
            return method(n, *args, **kwargs)
        return call

    tracker.create_issue = create
    # One transient read failure: any rollback after it would succeed, so a wrong one shows.
    tracker.get_issue = faulty(read, {"read", "read-unlabelled"}, once=True)
    tracker.update_issue = faulty(write, {"retire", "retire-and-close"})
    tracker.close_issue = faulty(close, {"close", "retire-and-close"})
    with pytest.raises((order.PlanError, TrackerError)):
        publish(publishing, plan)
    number = victim[0]
    for name, original in (("create_issue", created), ("get_issue", read),
                           ("update_issue", write), ("close_issue", close)):
        setattr(tracker, name, original)
    first = tracker.get_issue(number)
    # Each cleanup step runs even when the other one fails.
    assert (first.state == "closed") == (fault in ("label", "retire"))
    assert ("<!-- foragent:plan" not in first.body) == (fault in ("label", "close"))
    if fault == "read":  # The outcome was unknown, and it was a labelled issue: nothing undone.
        assert owners(tracker)[number] == {"by:human"} and first.state == "open"
        assert "<!-- foragent:plan" in first.body, "its live markers let a retry find it"
    if fault != "read":  # Whatever rollback failed, an unlabelled leftover is nobody's work, never legacy work.
        assert not owners(tracker)[number] and order.OWNED_MARKER in first.body
        for config in (publishing[1], replace(publishing[1], legacy_owner="jin")):
            with pytest.raises(OwnershipError, match=f"#{number} was published for an owner label"):
                effective_orderer(first, config, "human")
    tracker.login = "bob"  # Anyone may retry; the unconfirmed issue must not decide the owner.
    report = publish(publishing, plan)
    numbers = {report.parent, *(row["number"] for row in report.rows)}
    labelled = "by:human" in tracker.get_issue(number).labels
    assert labelled == (fault == "read")
    assert (number in numbers) == labelled, "A labelled issue is reused, an unlabelled one never"
    # A reused labelled parent keeps its original owner; a fresh parent belongs to the retrying person.
    expected = {"by:bob"} if refused == "parent" and number not in numbers else {"by:human"}
    assert all(owners(tracker)[n] == expected for n in numbers)
    assert all(tracker.get_issue(n).state == "open" for n in numbers)
    leftover = tracker.get_issue(number)
    if fault != "read":  # A labelled (confirmed but unread) issue stays a normal owned issue.
        assert leftover.state == "closed" and "<!-- foragent:plan" not in leftover.body
        assert order.OWNED_MARKER in leftover.body
    publish(publishing, plan)  # Stable afterwards: no duplicate parents or children.
    assert {report.parent, *(row["number"] for row in publish(publishing, plan).rows)} == numbers


STATEMENT = "A caller observes one receipt per login."
# A test the lane has not written yet: a statement draft may name it before preparation.
UNWRITTEN = "uv run pytest -q tests/acceptance/test_receipt.py"


@pytest.mark.parametrize("statements", [False, True])
def test_only_statement_drafts_may_name_unwritten_tests(publishing, plan, statements):
    plan["issues"][1]["done_when"] = [UNWRITTEN]
    if statements:
        plan["issues"][1]["statements"] = [STATEMENT]
        report = publish(publishing, plan, dry_run=True)
        assert "tests/acceptance/test_receipt.py" not in report.paths
    else:
        with pytest.raises(order.PlanError, match="missing acceptance file: tests/acceptance/test_receipt.py"):
            publish(publishing, plan, dry_run=True)


@pytest.mark.parametrize("kind", ["issues", "edits"])
@pytest.mark.parametrize("options", [{}, {"approve": True}])
def test_statement_issue_without_reviewed_tests_is_refused_before_writes(
        publishing, plan, monkeypatch, kind, options):
    monkeypatch.setattr(order, "require_clean_preread", REQUIRE_CLEAN_PREREAD)
    root, config, tracker, _, events = publishing
    if kind == "edits":
        tracker.add_issue(9, "Existing", "old", ["needs-human"])
        plan["id"] = "2026-10-21-login-update"
        plan["issues"] = []
        plan["edits"] = [{"number": 9, "goal": "Fixed goal", "out_of_scope": "Payments.",
                          "done_when": [UNWRITTEN], "statements": [STATEMENT]}]
        key = "#9"
    else:
        # Tests already in the plan and a clean old preread do not turn it into legacy work.
        plan["issues"][0]["statements"] = [STATEMENT]
        plan["issues"][0]["done_when"].append(UNWRITTEN)
        for item in plan["issues"]:
            _record_preread(root, config, plan, item, [])
        key = "api"
    assert publish(publishing, deepcopy(plan), dry_run=True).dry_run
    before = dict(tracker.issues)
    with pytest.raises(order.PlanError, match=rf"statement issue\(s\) {re.escape(key)} have no reviewed tests"):
        publish(publishing, plan, **options)
    assert events == [] and tracker.issues == before
    assert not (root / "docs").exists()


def test_legacy_issue_beside_a_statement_issue_keeps_the_preread_gate(publishing, plan, monkeypatch):
    monkeypatch.setattr(order, "require_clean_preread", REQUIRE_CLEAN_PREREAD)
    monkeypatch.setattr(order, "require_reviewed_tests", lambda plan: None)
    plan["issues"][0]["statements"] = [STATEMENT]
    with pytest.raises(order.PlanError, match=r"no preread of the current content for issue\(s\) client;"):
        publish(publishing, plan, approve=True)


@pytest.mark.parametrize("value", [[], [""], [" "], "one", [7], ["two\nlines"]])
def test_invalid_statements_name_the_field(publishing, plan, value):
    plan["issues"][1]["statements"] = value
    with pytest.raises(order.PlanError, match=r"issues\[1\]\.statements"):
        publish(publishing, plan, dry_run=True)


def test_statements_appear_verbatim_in_the_issue_body(publishing, plan, monkeypatch):
    monkeypatch.setattr(order, "require_reviewed_tests", lambda plan: None)
    plan["issues"][0]["statements"] = [STATEMENT, "Nothing else changes."]
    publish(publishing, plan)
    body = publishing[2].get_issue(2).body
    assert f"## Behaviour\n- {STATEMENT}\n- Nothing else changes.\n" in body
    assert parse_body(body).goal == "Implement login."
    assert "## Behaviour" not in publishing[2].get_issue(3).body



def _die(*args, **kwargs):
    raise SystemExit(9)


BATCH_STEPS = ["journal_written", "files_written", "commit_left_staged", "batch_committed_unrecorded",
               "batch_recorded", "push"]


@pytest.mark.parametrize("killed", [False, True], ids=["raised", "killed"])
@pytest.mark.parametrize("step", BATCH_STEPS)
def test_batch_interrupted_at_each_step_resumes_from_recorded_facts(publishing, plan, monkeypatch, step, killed):
    from foragent import order_batch, order_batch_journal

    root, git, events = _rules_case(publishing, plan), publishing[3], publishing[4]
    (root / "docs/ideas.md").write_text("# Ideas\n")
    git.saved["docs/ideas.md"] = "# Ideas\n"
    order_batch.note(root, "idea", "An idea.", "next-code-merge", "en")
    before = _checkout(root)
    if step == "journal_written":
        monkeypatch.setattr(order, "write_order_files", _die)
    elif step == "files_written":
        monkeypatch.setattr(order, "commit_and_push", _die)
    elif step == "commit_left_staged":  # `git add` ran, then the commit itself failed
        monkeypatch.setattr(git, "commit_recorded", lambda paths, title, record, contents=None: (git.stage(paths), _die()))
    elif step == "batch_committed_unrecorded":  # died between the commit and recording its sha
        monkeypatch.setattr(order_batch_journal, "batch_done", _die)
    elif step == "batch_recorded":  # the sha is recorded; the queue checkpoint is not written
        monkeypatch.setattr(order_batch, "_committed", _die)
    else:
        monkeypatch.setattr(git, "push_base", _die)
    if killed:
        monkeypatch.setattr(order_batch_journal, "recover", lambda *args: None)
    with pytest.raises(SystemExit):
        _flush(publishing)
    monkeypatch.undo()
    monkeypatch.setattr(order, "require_clean_preread", lambda *args: None)
    committed = any(event[0] == "commit" for event in events)
    if not killed and not committed:
        assert _checkout(root) == before and git.index == {}, "an uncommitted batch leaves nothing behind"
    # Someone else commits on the base before the resume; that must not hide the batch.
    git.unrelated_commit("docs/other.md", "Unrelated.\n")
    _flush(publishing)
    assert git.index == {}
    assert (root / "docs/rules.md").read_text() == "# Rules\n- C. (D3)\n- Extra. (D4)\n"
    assert (root / "docs/ideas.md").read_text() == "# Ideas\n\nAn idea.\n"
    batch = [event for event in events if event[0] == "commit"]
    assert len(batch) == 1, [event[1] for event in batch]
    state = order_batch.pending(root)
    assert {row["status"] for row in [*state["orders"], *state["notes"]]} == {"published"}
    assert not (root / ".foragent/order-batch-journal.json").exists()


def test_batch_recovery_keeps_a_recorded_commit_after_a_later_edit_is_committed(publishing, plan, monkeypatch):
    from foragent import order_batch, order_batch_journal

    root, git, events = _rules_case(publishing, plan), publishing[3], publishing[4]
    (root / "docs/ideas.md").write_text("# Ideas\n")
    git.saved["docs/ideas.md"] = "# Ideas\n"
    order_batch.note(root, "idea", "An idea.", "next-code-merge", "en")
    monkeypatch.setattr(order_batch, "_committed", _die)
    monkeypatch.setattr(order_batch_journal, "recover", lambda *args: None)
    with pytest.raises(SystemExit):
        _flush(publishing)
    monkeypatch.undo()
    # A later commit edits a file the batch wrote, before the batch is settled.
    git.unrelated_commit("docs/ideas.md", "# Ideas\n\nAn idea.\n\nA later idea.\n")
    _flush(publishing)
    assert (root / "docs/ideas.md").read_text().endswith("A later idea.\n")
    assert [event[0] for event in events].count("commit") == 1
    assert order_batch.pending(root)["orders"][0]["status"] == "published"


def test_batch_recorded_commit_no_longer_in_history_is_undone_and_retried(publishing, plan, monkeypatch):
    from foragent import order_batch, order_batch_journal

    root, git, events = _rules_case(publishing, plan), publishing[3], publishing[4]
    base = git.head_sha()
    before = _checkout(root)
    monkeypatch.setattr(order_batch, "_committed", _die)
    monkeypatch.setattr(order_batch_journal, "recover", lambda *args: None)
    with pytest.raises(SystemExit):
        _flush(publishing)
    monkeypatch.undo()
    git.reset_to(base)  # the recorded batch commit is gone from HEAD's history
    order_batch.recover(root, git)
    assert _checkout(root) == before and git.index == {}
    _flush(publishing)
    assert [event[0] for event in events].count("commit") == 2  # the first one was dropped
    assert (root / "docs/rules.md").read_text() == "# Rules\n- C. (D3)\n- Extra. (D4)\n"
    assert order_batch.pending(root)["orders"][0]["status"] == "published"


def test_batch_failed_commit_leaves_nothing_staged_in_a_real_repository(offline_publication, plan, monkeypatch):
    from foragent import order_batch

    root, config, tracker, git, _calls, _origin = offline_publication
    order_batch.note(root, "idea", "An idea.", "next-code-merge", "en")
    order_batch.defer(plan, "next-code-merge", root, config, tracker, repo="owner/repo")
    before = git_command(root, "status", "--porcelain=v1", "--untracked-files=all")

    def add_then_fail(paths, title, record, contents=None):
        # A failed commit can leave paths staged, even ones only meant for the commit.
        for name in paths:
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            if name in (contents or {}):
                (root / name).write_bytes(contents[name])
        git_command(root, "add", "-f", "--", *paths)
        raise GitError("commit interrupted")

    monkeypatch.setattr(git, "commit_recorded", add_then_fail)
    with pytest.raises(GitError, match="commit interrupted"):
        order_batch.flush("next-code-merge", root, config, tracker, repo="owner/repo", git=git)
    # Neither the index nor the working tree keeps the batch (no `AD docs/ideas.md`).
    assert git_command(root, "status", "--porcelain=v1", "--untracked-files=all") == before
    assert not (root / "docs/ideas.md").exists()



def _one_batch_commit(events, *lane_files):
    """A batch with new lanes makes exactly one commit, and it carries the lanes' files."""
    commits = [event for event in events if event[0] == "commit"]
    assert [event[1].split(":")[0] for event in commits] == ["Foragent batch next-code-merge"], commits
    assert set(lane_files) <= commits[0][2].keys(), sorted(commits[0][2])



def test_batch_recovery_keeps_lane_files_someone_else_committed_meanwhile(publishing, plan, monkeypatch):
    from foragent import order_batch, order_batch_journal

    root, config, tracker, git, events = publishing
    (root / ".gitignore").write_text("THIRD_PARTY_NOTICES.md\n")
    git.saved.update({name: (root / name).read_text() for name in (".foragent.yml", ".gitignore")})
    _batch(publishing, _lane_plan(plan, "web0"))
    monkeypatch.setattr(git, "commit_recorded", lambda paths, title, record, contents=None: (git.stage(paths), _die()))
    monkeypatch.setattr(order_batch_journal, "recover", lambda *args: None)  # killed before cleanup
    with pytest.raises(SystemExit):
        _flush(publishing)
    monkeypatch.undo()
    monkeypatch.setattr(order, "require_clean_preread", lambda *args: None)
    git.index.clear()
    # Meanwhile someone commits another lane and an ignore rule on the same base.
    site = "lanes: {api: {paths: ['api/**'], agent: fa-api}, site: {paths: ['site/**'], agent: fa-site}}\n"
    git.unrelated_commit(".foragent.yml", site)
    git.unrelated_commit(".gitignore", "THIRD_PARTY_NOTICES.md\n/site-cache/\n")
    git.unrelated_commit(".claude/agents/fa-site.md", "site agent\n")
    order_batch.recover(root, git)
    assert (root / ".foragent.yml").read_text() == site, "committed work is never rolled back"
    assert "/site-cache/" in (root / ".gitignore").read_text()
    assert not (root / ".claude/agents/fa-web.md").exists() and git.index == {}
    _flush(publishing)
    assert set(load_config(root).lanes) == {"api", "site", "web"}
    assert (root / ".claude/agents/fa-site.md").read_text() == "site agent\n"
    _one_batch_commit(events, ".foragent.yml", ".claude/agents/fa-web.md")


def _notes_case(publishing, plan):
    from foragent import order_batch

    root, git = _rules_case(publishing, plan), publishing[3]
    for name, text in (("docs/ideas.md", "# Ideas\n"), ("docs/decisions.md", "# Decisions\n")):
        (root / name).write_text(text)
        git.saved[name] = text
    order_batch.note(root, "idea", "An idea.", "next-code-merge", "en")
    order_batch.note(root, "decision", "D9. A decision.", "next-code-merge", "en")
    return root, git


@pytest.mark.parametrize("moved", [False, True], ids=["before_head_moves", "after_head_moves"])
def test_batch_commit_sha_is_recorded_before_head_moves_so_later_appends_never_duplicate(
        publishing, plan, monkeypatch, moved):
    from foragent import order_batch, order_batch_journal

    root, git = _notes_case(publishing, plan)
    events = publishing[4]
    if moved:  # HEAD holds the recorded commit; the queue checkpoint is not written yet
        monkeypatch.setattr(order_batch, "_committed", _die)
    else:  # the sha is recorded; the process dies before HEAD moves to that commit
        monkeypatch.setattr(git, "_commit", _die)
    monkeypatch.setattr(order_batch_journal, "recover", lambda *args: None)
    with pytest.raises(SystemExit):
        _flush(publishing)
    monkeypatch.undo()
    monkeypatch.setattr(order, "require_clean_preread", lambda *args: None)
    assert json.loads((root / ".foragent/order-batch-journal.json").read_text())["batch_commit"]
    # Later commits append to the same files before the resume.
    for name, extra in (("docs/ideas.md", "Later idea.\n"), ("docs/decisions.md", "D10. Later.\n"),
                        ("docs/rules.md", "- Later rule. (D10)\n")):
        current = git.saved[name] if not moved else (root / name).read_text()
        git.unrelated_commit(name, current + extra)
    _flush(publishing)
    ideas, decisions, rules = ((root / name).read_text() for name in
                               ("docs/ideas.md", "docs/decisions.md", "docs/rules.md"))
    assert ideas.count("An idea.") == 1 and decisions.count("D9. A decision.") == 1
    assert "Later idea." in ideas and "D10. Later." in decisions and "- Later rule. (D10)" in rules
    assert rules.count("- C. (D3)") == 1 and "- A. (D1)" not in rules
    assert [event[0] for event in events].count("commit") == 1
    state = order_batch.pending(root)
    assert {row["status"] for row in [*state["orders"], *state["notes"]]} == {"published"}


@pytest.mark.parametrize("stop", ["before_clear", "push"])
def test_batch_checkpoint_of_a_commit_that_left_history_waits_again(publishing, plan, monkeypatch, stop):
    from foragent import order_batch, order_batch_journal

    root, git = _notes_case(publishing, plan)
    events, base = publishing[4], git.head_sha()
    if stop == "before_clear":  # the queue says committed; the journal still exists
        clear = order_batch_journal.clear
        calls = []
        monkeypatch.setattr(order_batch_journal, "clear",
                            lambda directory: calls.append(1) or (_die() if len(calls) == 1 else clear(directory)))
        monkeypatch.setattr(order_batch_journal, "recover", lambda *args: None)
    else:  # the journal is gone; only the queue checkpoint remembers the commit
        monkeypatch.setattr(git, "push_base", _die)
    with pytest.raises(SystemExit):
        _flush(publishing)
    monkeypatch.undo()
    monkeypatch.setattr(order, "require_clean_preread", lambda *args: None)
    assert {row["status"] for row in order_batch.pending(root)["notes"]} == {"committed"}
    git.reset_to(base)  # the batch commit is no longer in HEAD's history
    for name in ("docs/ideas.md", "docs/decisions.md", "docs/rules.md"):
        (root / name).write_text(git.saved[name])  # the checkout follows the reset
    for name in [p.relative_to(root).as_posix() for p in (root / "docs/specs").rglob("*")]:
        (root / name).unlink()
    _flush(publishing)
    assert (root / "docs/ideas.md").read_text().count("An idea.") == 1
    assert (root / "docs/decisions.md").read_text().count("D9. A decision.") == 1
    assert git.saved["docs/ideas.md"].count("An idea.") == 1, "the note reaches HEAD again"
    assert f"docs/specs/{plan['id']}.md" in git.saved, "the order's files are published again"
    assert [event[0] for event in events].count("commit") == 2
    state = order_batch.pending(root)
    assert {row["status"] for row in [*state["orders"], *state["notes"]]} == {"published"}


def test_real_git_commit_recorded_hands_over_the_sha_before_head_moves(git_history):
    root = git_history.clone
    repo = GitRepo(root)
    parent = repo.head_sha()
    (root / "batch.txt").write_text("batch\n")
    (root / "other.txt").write_text("someone else's staged work\n")
    git_command(root, "add", "other.txt")
    seen = []

    def record(sha):
        seen.append((sha, repo.head_sha()))

    sha = repo.commit_recorded(["batch.txt"], "Foragent batch x", record)
    assert seen == [(sha, parent)], "the sha is known while HEAD still points at the parent"
    assert repo.head_sha() == sha and repo.is_ancestor(parent, "HEAD")
    assert git_command(root, "show", "--name-only", "--format=", sha).split() == ["batch.txt"]
    status = git_command(root, "status", "--porcelain=v1")
    assert "A  other.txt" in status and "batch.txt" not in status, "others' staging stays; ours is clean"
    (root / "batch.txt").write_text("again\n")

    def dies(sha):
        raise SystemExit(9)

    with pytest.raises(SystemExit):
        repo.commit_recorded(["batch.txt"], "Foragent batch y", dies)
    assert repo.head_sha() == sha, "a death before recording never moves HEAD"


def _aliased_manager(root):
    """A library skill whose alias (its SKILL.md name) differs from its directory name."""
    library = root.parent / f"{root.name}-aliases"
    skill = library / "skills/testing-strategy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: tester\ndescription: Tests\n---\nUse tests.\n")
    (library / "LICENSE").write_text("SPDX-License-Identifier: MIT\nMIT License\n")
    return SkillManager(Config(skill_libraries=[{"name": "local", "repo": "local", "path": str(library)}]),
                        run=lambda args: "a" * 40 + "\n")


@pytest.mark.parametrize("second", ["local:tester", "local:testing-strategy"])
def test_batch_new_lane_with_a_skill_alias_publishes_its_issues(publishing, plan, second):
    from foragent import order_batch

    root, config, tracker, git, events = publishing
    manager = _aliased_manager(root)
    plans = []
    for index, identity in enumerate(["local:tester", second]):
        value = _lane_plan(plan, f"web{index}")
        value["new_lanes"][0]["skills"] = [identity]
        order_batch.defer(value, "next-code-merge", root, config, tracker, repo="owner/repo", manager=manager)
        plans.append(value)
    order_batch.flush("next-code-merge", root, config, tracker, repo="owner/repo", git=git, manager=manager)
    assert load_config(root).lanes["web"]["skills"] == ["testing-strategy"]
    titles = {issue.title for issue in tracker.list_issues()}
    assert {"web0 web", "web1 web"} <= titles, "both orders name the same installed skill"
    assert {row["status"] for row in order_batch.pending(root)["orders"]} == {"published"}


def test_batch_existing_lane_files_are_in_the_manifest_and_unstaged_on_failure(publishing, plan, monkeypatch):
    from foragent import order_batch

    root, config, tracker, git, events = publishing
    git.saved[".foragent.yml"] = (root / ".foragent.yml").read_text()
    value = _lane_plan(plan, "web0")
    # An earlier interrupted publication left the lane configured but not committed.
    from foragent.lanes import add_lane

    add_lane(root, "web", ["web/**"], git=SimpleNamespace(current_branch=lambda: "main",
                                                            ensure_tracked=lambda paths: None,
                                                            commit_paths=lambda paths, title: None), push=False)
    before = _checkout(root)
    config = load_config(root)
    order_batch.defer(value, "next-code-merge", root, config, tracker, repo="owner/repo")
    monkeypatch.setattr(git, "commit_recorded", lambda paths, title, record, contents=None: (git.stage(paths), _die()))
    with pytest.raises(SystemExit):
        order_batch.flush("next-code-merge", root, config, tracker, repo="owner/repo", git=git)
    monkeypatch.undo()
    monkeypatch.setattr(order, "require_clean_preread", lambda *args: None)
    assert git.index == {}, "every committed path is listed, so rollback unstages it"
    assert _checkout(root) == before
    order_batch.flush("next-code-merge", root, load_config(root), tracker, repo="owner/repo", git=git)
    _one_batch_commit(events, ".foragent.yml", ".claude/agents/fa-web.md")


@pytest.mark.parametrize("partial", ["parent-only", "unmapped-child"])
def test_retry_of_a_legacy_partial_order_keeps_it_with_the_legacy_owner(publishing, plan, partial):
    """A pre-team order that stopped early is still the original factory's order."""
    root, config, tracker, git, _ = publishing
    if partial == "parent-only":  # Stopped before its first child: the parent records none.
        tracker.add_issue(1, plan["title"], f"{plan['summary']}\n\n\n\n{order._plan_marker(plan['id'], {}, {})}\n")
    else:  # Stopped after creating the client child, before recording it in the map.
        publish(publishing, plan)
        parent = tracker.get_issue(1)
        body = re.sub(r"map=\{[^\n]*?\}", 'map={"api": 2}', parent.body)
        tracker.update_issue(1, title=parent.title, body=body)
        for number in (1, 2, 3):
            make_legacy(tracker, number)
    tracker.login = "bob"
    report = order.publish(plan, root, replace(config, legacy_owner="Jin"), tracker, repo="owner/repo", git=git)
    assert report.parent == 1, "The legacy parent is kept"
    children = {row["number"] for row in report.rows}
    survivors = set() if partial == "parent-only" else {2, 3}
    assert survivors <= children, "Legacy children found by marker are kept"
    assert all(owners(tracker)[n] == {"by:jin"} for n in children - survivors)
    assert all(owners(tracker)[n] == set() for n in {1} | survivors), "Legacy issues are not relabelled"
    assert all(order.OWNED_MARKER not in tracker.get_issue(n).body for n in {1} | survivors)
    assert all(tracker.get_issue(n).state == "open" for n in {1} | children)



def test_a_parent_whose_owner_label_a_person_removed_is_refused_not_replaced(publishing, plan):
    """A recorded order without its by: label is neither recreated nor given to anyone."""
    root, config, tracker, git, _ = publishing
    publish(publishing, plan)
    tracker.remove_labels(1, ["by:human"])
    tracker.login = "bob"
    before = deepcopy(tracker.issues)
    with pytest.raises(order.PlanError, match="#1 was published for an owner label"):
        order.publish(plan, root, replace(config, legacy_owner="Jin"), tracker, repo="owner/repo", git=git)
    assert tracker.issues == before
    tracker.add_labels(1, ["by:human"])  # A person restores the owner; publication continues.
    assert order.publish(plan, root, config, tracker, repo="owner/repo", git=git).parent == 1
    assert len(tracker.issues) == 3


def test_batch_retry_after_a_push_interruption_never_commits_unrelated_checkout_changes(
        offline_publication, plan, monkeypatch):
    from foragent import order_batch

    root, config, tracker, git, _calls, origin = offline_publication
    order_batch.note(root, "idea", "An idea.", "next-code-merge", "en")
    order_batch.defer(plan, "next-code-merge", root, config, tracker, repo="owner/repo")
    start = git_command(root, "rev-parse", "HEAD")
    tracked = git_command(root, "ls-files").splitlines()[0]  # a base file no order touches
    push = git.push_base
    monkeypatch.setattr(git, "push_base", lambda branch: (_ for _ in ()).throw(GitError("push interrupted")))
    with pytest.raises(GitError, match="push interrupted"):
        order_batch.flush("next-code-merge", root, config, tracker, repo="owner/repo", git=git)
    batch = git_command(root, "rev-parse", "HEAD")
    assert git_command(root, "rev-list", "--count", f"{start}..{batch}") == "1"
    # Someone's unrelated work sits in the checkout when the batch is retried.
    (root / "scratch.txt").write_text("not part of any order\n")
    (root / tracked).write_text((root / tracked).read_text() + "# local edit\n")
    monkeypatch.setattr(git, "push_base", push)
    order_batch.flush("next-code-merge", root, config, tracker, repo="owner/repo", git=git)
    assert git_command(root, "rev-parse", "HEAD") == batch, "the retry pushes the recorded commit only"
    assert git_command(origin, "rev-parse", "refs/heads/main") == batch
    status = git_command(root, "status", "--porcelain=v1", "--untracked-files=all")
    assert "?? scratch.txt" in status and git_command(root, "diff", "--name-only") == tracked
    assert git_command(root, "log", "--all", "--format=%H", "--", "scratch.txt") == ""
    assert "# local edit" not in git_command(root, "show", f"HEAD:{tracked}")
    assert {row["status"] for row in [*order_batch.pending(root)["orders"], *order_batch.pending(root)["notes"]]} \
        == {"published"}


@pytest.mark.parametrize("paths", [[], ["missing-and-untracked.txt"]])
def test_real_git_empty_scopes_stay_empty(git_history, paths):
    root = git_history.clone
    (root / "unrelated.txt").write_text("someone's file\n")
    repo = GitRepo(root)
    head = repo.head_sha()
    assert repo.uncommitted_paths(paths) == [], "no named path means nothing, never the whole checkout"
    recorded = []
    if not paths:
        assert repo.commit_recorded(paths, "Foragent batch x", recorded.append) is None
        assert recorded == [] and repo.head_sha() == head


def test_batch_committed_only_retry_stages_and_commits_nothing(publishing, plan, monkeypatch):
    from foragent import order_batch, order_batch_journal

    root, git, events = _notes_case(publishing, plan)[0], publishing[3], publishing[4]
    push = git.push_base
    monkeypatch.setattr(git, "push_base", _die)
    with pytest.raises(SystemExit):
        _flush(publishing)
    monkeypatch.setattr(git, "push_base", push)
    touched = []
    for name in ("uncommitted_paths", "commit_recorded", "commit_paths", "stage", "ensure_tracked"):
        monkeypatch.setattr(git, name, lambda *args, name=name: touched.append(name) or [])
    monkeypatch.setattr(order_batch_journal, "write", lambda *args, **kwargs: touched.append("journal"))
    _flush(publishing)
    assert touched == [], "a retry of committed work only pushes the recorded commit"
    assert [event[0] for event in events].count("push") == 1
    assert {row["status"] for row in order_batch.pending(root)["notes"]} == {"published"}


def test_batch_killed_before_its_commit_leaves_no_note_for_another_commit_to_pick_up(
        publishing, plan, monkeypatch):
    from foragent import order_batch, order_batch_journal

    root, git = _notes_case(publishing, plan)
    events = publishing[4]
    monkeypatch.setattr(git, "commit_recorded", _die)  # killed before the batch commit exists
    monkeypatch.setattr(order_batch_journal, "recover", lambda *args: None)
    with pytest.raises(SystemExit):
        _flush(publishing)
    monkeypatch.undo()
    monkeypatch.setattr(order, "require_clean_preread", lambda *args: None)
    for name in ("docs/ideas.md", "docs/decisions.md", "docs/rules.md"):
        assert (root / name).read_text() == git.saved[name], "nothing of the batch is in the checkout yet"
    assert not (root / f"docs/specs/{plan['id']}.md").exists()
    # Someone commits the checkout's files with their own edits before the resume.
    for name, extra in (("docs/ideas.md", "Their idea.\n"), ("docs/rules.md", "- Theirs. (D8)\n")):
        git.unrelated_commit(name, (root / name).read_text() + extra)
    _flush(publishing)
    ideas, rules = (root / "docs/ideas.md").read_text(), (root / "docs/rules.md").read_text()
    assert ideas.count("An idea.") == 1 and "Their idea." in ideas
    assert rules.count("- C. (D3)") == 1 and "- A. (D1)" not in rules and "- Theirs. (D8)" in rules
    assert (root / "docs/decisions.md").read_text().count("D9. A decision.") == 1
    assert [event[0] for event in events].count("commit") == 1


def test_batch_landed_recovery_unstages_every_listed_path_lane_files_included(publishing, plan, monkeypatch):
    from foragent import order_batch, order_batch_journal

    root, config, tracker, git, events = publishing
    git.saved[".foragent.yml"] = (root / ".foragent.yml").read_text()
    value = _lane_plan(plan, "web0")
    _batch(publishing, value)
    monkeypatch.setattr(order_batch, "_committed", _die)  # HEAD moved; the checkpoint is not written
    monkeypatch.setattr(order_batch_journal, "recover", lambda *args: None)
    with pytest.raises(SystemExit):
        _flush(publishing)
    monkeypatch.undo()
    monkeypatch.setattr(order, "require_clean_preread", lambda *args: None)
    # A stop right after HEAD moved leaves the earlier index entries: a staged reversal.
    git.index.update({".foragent.yml": "lanes: {api: {paths: ['api/**'], agent: fa-api}}\n",
                      f"docs/specs/{value['id']}.md": None})
    assert order_batch.recover(root, git) is True
    assert git.index == {}, "no reversal of the published batch stays staged"
    _flush(publishing)
    _one_batch_commit(events, ".foragent.yml", ".claude/agents/fa-web.md")


def test_real_git_stop_after_head_moves_leaves_no_staged_reversal(offline_publication, plan, monkeypatch):
    from foragent import order_batch

    root, config, tracker, git, _calls, _origin = offline_publication
    order_batch.note(root, "idea", "An idea.", "next-code-merge", "en")
    order_batch.defer(plan, "next-code-merge", root, config, tracker, repo="owner/repo")
    start = git_command(root, "rev-parse", "HEAD")
    restore = git.restore_staged_paths
    calls = []

    def dies_once(ref, paths):
        calls.append(1)
        if len(calls) == 1:  # the sync inside commit_recorded, right after update-ref
            raise SystemExit(9)
        return restore(ref, paths)

    monkeypatch.setattr(git, "restore_staged_paths", dies_once)
    with pytest.raises(SystemExit):
        order_batch.flush("next-code-merge", root, config, tracker, repo="owner/repo", git=git)
    monkeypatch.setattr(git, "restore_staged_paths", restore)
    order_batch.flush("next-code-merge", root, config, tracker, repo="owner/repo", git=git)
    assert git_command(root, "rev-list", "--count", f"{start}..HEAD") == "1"
    assert git_command(root, "diff", "--cached", "--name-only") == "", "nothing staged after recovery"
    assert (root / "docs/ideas.md").read_text().count("An idea.") == 1
    assert git_command(root, "show", "HEAD:docs/ideas.md").count("An idea.") == 1


@pytest.mark.parametrize("link", ["file", "parent"])
def test_batch_refuses_a_symlinked_note_destination_before_reading_or_committing(publishing, plan, link):
    from foragent import order_batch

    root, git, events = publishing[0], publishing[3], publishing[4]
    (root / ".env").write_text("TOKEN=secret\n")
    if link == "file":
        (root / "docs").mkdir()
        (root / "docs/ideas.md").symlink_to(root / ".env")
    else:  # the parent directory points elsewhere in the repository
        (root / "private").mkdir()
        (root / "private/ideas.md").write_text("TOKEN=secret\n")
        (root / "docs").symlink_to(root / "private", target_is_directory=True)
    order_batch.note(root, "idea", "An idea.", "next-code-merge", "en")
    with pytest.raises(order.PlanError, match="docs"):
        _flush(publishing)
    assert events == [] and git.index == {}, "nothing is read into a commit through a link"
    assert "secret" not in (root / ".foragent/order-batch.json").read_text()
    assert not (root / ".foragent/order-batch-journal.json").exists()


def test_batch_recovery_keeps_every_resource_of_a_lane_whose_configuration_stays(publishing, plan, monkeypatch):
    from foragent import order_batch, order_batch_journal

    root, config, tracker, git, events = publishing
    (root / ".gitignore").write_text("THIRD_PARTY_NOTICES.md\n")
    git.saved.update({name: (root / name).read_text() for name in (".foragent.yml", ".gitignore")})
    manager = _skill_manager(root)
    value = _lane_plan(plan, "web0")
    value["new_lanes"][0]["skills"] = ["local:testing-strategy"]
    order_batch.defer(value, "next-code-merge", root, config, tracker, repo="owner/repo", manager=manager)
    monkeypatch.setattr(git, "commit_recorded", _die)  # killed before the batch commit
    monkeypatch.setattr(order_batch_journal, "recover", lambda *args: None)
    with pytest.raises(SystemExit):
        order_batch.flush("next-code-merge", root, config, tracker, repo="owner/repo", git=git, manager=manager)
    monkeypatch.undo()
    monkeypatch.setattr(order, "require_clean_preread", lambda *args: None)
    # Another process commits only the lane configuration it found in the checkout.
    git.unrelated_commit(".foragent.yml", (root / ".foragent.yml").read_text())
    assert order_batch.recover(root, git) is True
    assert "web" in load_config(root).lanes
    for kept in (".claude/agents/fa-web.md", ".claude/skills/testing-strategy/SKILL.md"):
        assert (root / kept).is_file(), f"a configured lane keeps {kept}"
    assert "/.claude/skills/testing-strategy/" in (root / ".gitignore").read_text()
    order_batch.flush("next-code-merge", root, load_config(root), tracker, repo="owner/repo", git=git,
                      manager=manager)
    assert order_batch.pending(root)["orders"][0]["status"] == "published"
    assert "lane:web" in next(i for i in tracker.list_issues() if i.title == "web0 web").labels
