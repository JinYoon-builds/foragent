"""Acceptance test for irreversible-paths. Locked by Foragent; do not edit."""

from datetime import UTC, datetime
from importlib import import_module, util
from pathlib import Path
from types import SimpleNamespace
import json
import shutil
import socket
import subprocess
import sys

import pytest
import yaml

from foragent.config import load_config
from foragent.orch_inbox import questions
from foragent.state import StateStore


NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)
BASE = "b" * 40
HEAD = "a" * 40
DEPLOY = ".github/workflows/deploy.yml"
MIGRATIONS = ("migrations", "alembic/versions", "prisma/migrations", "db/migrate")
BODY = "<!-- foragent:v1 -->\n## Goal\nUpdate the project.\n## Out of scope\nOther work.\n## Done when\n```sh\ntrue\n```\n"


@pytest.fixture(autouse=True)
def offline_irreversible(monkeypatch):
    from foragent import executors, notify, runner

    def forbidden(*args, **kwargs):
        raise AssertionError("Irreversible-path acceptance tests must use fakes, never external processes or network")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(executors.ClaudeExecutor, "run", forbidden)
    monkeypatch.setattr(executors.CodexExecutor, "run", forbidden)
    monkeypatch.setattr("builtins.input", forbidden)
    monkeypatch.setattr(notify, "send", lambda *args, **kwargs: True)
    # Fake only the shell execution, leaving verification and merge policy real.
    monkeypatch.setattr(runner, "run_logged", lambda *args, **kwargs: (0, False, None))


def _workflow(*, base="main", trigger="push", job=None):
    return yaml.safe_dump({
        "name": "Application", "on": {trigger: {"branches": [base]}},
        "jobs": {"app": {"runs-on": "ubuntu-latest", **(job or {"steps": [{"run": "deploy application"}]})}},
    }, sort_keys=False).replace("'on':", "on:", 1)  # Real GitHub YAML uses the unquoted `on` key.


def _write(root, files):
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def _detect(root, base="main"):
    # The spec leaves the new public detector's name open; this is its locked contract.
    module = "foragent.irreversible_paths"
    assert util.find_spec(module) is not None, "Public irreversible_paths.detect must discover repository path globs"
    detect = getattr(import_module(module), "detect", None)
    assert callable(detect), "Public irreversible_paths.detect must discover repository path globs"
    paths = detect(root, base_branch=base)
    assert isinstance(paths, list) and all(isinstance(path, str) for path in paths), "Detector must return path globs"
    return paths


def _events(store):
    path = store.root / "orch-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class _Git:
    """Repository facts and disposable file copies; no Git command is executed."""

    def __init__(self, root, changed=(), *, source=None, base="main"):
        self.root, self.changed = Path(root), list(changed)
        self.source, self.base = Path(source or root), base
        self.publication_paths = set()

    def check_worktree(self):
        pass

    def worktree_root(self):
        return self.root.resolve()

    def remote_slug(self):
        return "owner/repo"

    def current_branch(self):
        return self.base

    def uncommitted_paths(self, paths):
        return []

    def commit_paths(self, *args):
        pass  # Init's injected fake: no subprocess or real commit.

    def fetch(self, *args):
        pass

    def ref_sha(self, ref):
        return HEAD if "foragent/issue-" in ref or ref == "HEAD" else BASE

    def head_sha(self):
        return HEAD

    def blob_sha(self, ref, path):
        return None  # This fixture has no risk map, acceptance snapshot or exclusions file.

    def blob_shas(self, ref, paths):
        assert not paths, "This fixture's approved acceptance snapshot is empty"
        return {}

    def changed_paths(self, ref):
        return list(self.changed)

    def add_detached_worktree(self, path, ref):
        shutil.copytree(self.source, path, dirs_exist_ok=True)

    def at(self, root):
        return _Git(root, self.changed, source=self.source, base=self.base)

    def remove_worktree(self, path):
        pass  # pytest owns every disposable directory in this fixture.

    def is_ancestor(self, base, head):
        return base == BASE and head == HEAD

    def stage_changes(self):
        pass

    def has_staged_changes(self):
        return False

    def check_publication(self, base):
        pass


def _case(tmp_path, files=None, *, base="main"):
    from foragent.runner import RunContext
    from foragent.tracker import FakeTracker

    root = tmp_path / "repo"
    _write(root, files or {})
    (root / ".foragent.yml").write_text(
        "# Keep the project's existing settings and this comment.\n"
        + yaml.safe_dump({"review": "fast", "reviewer": "none", "checks": "local", "notify": "none",
                          "skill_libraries": [], "always_human": ["private/**"], "base_branch": base}, sort_keys=False)
    )
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    tracker = FakeTracker(now=lambda: NOW, strict=True)
    git = _Git(root, base=base)
    ctx = RunContext("owner/repo", root, tracker, store, load_config(root), git, now=lambda: NOW)
    return SimpleNamespace(root=root, store=store, tracker=tracker, git=git, ctx=ctx)


def _merge(case, number, paths):
    from foragent.issue import body_hash
    from foragent.runner import review_only

    case.tracker.add_issue(number, f"Update {number}", BODY, labels=["approved"])
    case.store.save_approval(number, body=BODY, body_sha=body_hash(BODY), acceptance_blobs={},
                             approved_by="human", approved_at=NOW.isoformat())
    pr = case.tracker.create_pr(head=f"foragent/issue-{number}", base=case.ctx.config.base_branch,
                                title=f"Update {number} (#{number})", body="Verified changes")
    case.tracker.set_pr_head_sha(pr, HEAD)
    case.tracker.set_pr_files(pr, paths)
    case.tracker.set_pr_checks(pr, "pass")
    case.git.changed = list(paths)
    return review_only(number, case.ctx, verdict_head=HEAD, repair_ci=False), pr


def _path_events(store, path):
    # Review/run events are separate; count only the path's new policy announcement.
    return [event for event in _events(store)
            if path in event.get("summary", "") and "person" in event.get("say", event["summary"]).lower()]


def test_public_detector_finds_deploy_publish_release_and_production_jobs(tmp_path):
    jobs = [
        {"steps": [{"run": "./deploy application"}]},
        {"steps": [{"run": "npm publish"}]},
        {"steps": [{"uses": "example/release-action@v1"}]},
        {"environment": "production", "steps": [{"run": "./ship"}]},
        {"environment": {"name": "production"}, "steps": [{"run": "./ship"}]},
    ]
    for index, job in enumerate(jobs):
        root = tmp_path / str(index)
        path = DEPLOY if index % 2 == 0 else ".github/workflows/publish.yaml"
        _write(root, {path: _workflow(job=job)})
        before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        assert path in _detect(root), "A deployment triggered by a push to the base branch must be detected"
        assert {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    for index, trigger in enumerate(("push", "[push, workflow_dispatch]", "{push: {}}")):
        root = tmp_path / f"unfiltered-{index}"
        workflow = "name: Application\non: " + trigger + "\njobs:\n  app:\n    steps:\n      - run: ./deploy\n"
        _write(root, {DEPLOY: workflow})
        assert DEPLOY in _detect(root), "Unfiltered push workflows also deploy when the base branch receives a merge"


def test_public_detector_finds_all_migration_layouts_and_nested_files(tmp_path):
    for index, directory in enumerate(MIGRATIONS):
        root = tmp_path / str(index)
        _write(root, {f"{directory}/20261027/nested/upgrade.sql": "ALTER TABLE example ADD value int;\n"})
        assert f"{directory}/**" in _detect(root), "Migration protection must cover the directory recursively"


def test_public_detector_ignores_workflows_without_a_base_push_deployment(tmp_path):
    files = {
        ".github/workflows/pr.yml": _workflow(trigger="pull_request"),
        ".github/workflows/other-branch.yml": _workflow(base="develop"),
        ".github/workflows/tests.yml": _workflow(job={"steps": [{"run": "python -m pytest"}]}),
        ".github/workflows/manual.yml": _workflow(trigger="workflow_dispatch"),
        "docs/migrations/example.sql": "Documentation, not a migration directory.\n",
    }
    _write(tmp_path, files)
    assert _detect(tmp_path) == [], "Only base-push deployment workflows and actual migration roots need protection"


def test_public_detector_honors_the_configured_base_branch(tmp_path):
    _write(tmp_path, {DEPLOY: _workflow(base="release")})
    assert _detect(tmp_path, "main") == [], "A push to another branch is not a deploy-on-merge for main"
    assert DEPLOY in _detect(tmp_path, "release"), "The configured base branch must determine deploy-on-merge"


@pytest.mark.parametrize("path,glob,content", [
    pytest.param(DEPLOY, DEPLOY, _workflow(), id="deploy-workflow"),
    *[pytest.param(f"{directory}/20261027/upgrade.sql", f"{directory}/**", "ALTER TABLE example ADD value int;\n",
                   id=directory.replace("/", "-")) for directory in MIGRATIONS],
])
def test_first_merge_registers_and_holds_then_reuses_policy_after_restart(
    tmp_path, monkeypatch, capsys, path, glob, content,
):
    from foragent import config as config_module
    from foragent.reasons import Reason

    case = _case(tmp_path, {path: content})
    before = yaml.safe_load((case.root / ".foragent.yml").read_text())
    writes = []
    writer = getattr(config_module, "add_always_human", None)
    if callable(writer):
        def tracked_writer(*args, **kwargs):
            writes.append((args, kwargs))
            return writer(*args, **kwargs)
        # Patch already imported public aliases too; do not constrain the caller's import style.
        for name, module in tuple(sys.modules.items()):
            if name.startswith("foragent.") and module is not None:
                for binding, value in tuple(vars(module).items()):
                    if value is writer:
                        monkeypatch.setattr(module, binding, tracked_writer)

    outcome, pr = _merge(case, 1, [path])
    assert outcome == "held", "The first merge touching an irreversible path must wait for a person"
    assert case.tracker.merged_prs == [], "The safety default must apply before any merge write"
    assert case.tracker.get_issue(pr).state == "open"
    assert case.store.load_flag(1).reason == Reason.ALWAYS_HUMAN_PATH
    assert callable(writer) and len(writes) == 1, "Register the path once through config.add_always_human"
    written = load_config(case.root)
    assert written.always_human == ["private/**", glob], "Append the detected glob without losing existing policy"
    after = yaml.safe_load((case.root / ".foragent.yml").read_text())
    assert {key: value for key, value in after.items() if key != "always_human"} == {
        key: value for key, value in before.items() if key != "always_human"
    }, "The config writer must preserve unrelated settings"
    assert (case.root / ".foragent.yml").read_text().startswith("# Keep the project's existing settings and this comment.\n")
    events = _path_events(case.store, glob)
    assert len(events) == 1, "Record one event announcing the new human-review path"
    captured = capsys.readouterr()
    log = case.store.log_path(1).read_text()
    rendered = [line for line in (captured.out + captured.err + log).splitlines()
                if glob in line and "person" in line.lower() and "wait" in line.lower()]
    notice = events[0].get("say") or (rendered[0] if len(rendered) == 1 else "")
    assert glob in notice and "wait" in notice.lower() and "person" in notice.lower() and "change" in notice.lower(), (
        "Give one notice line naming the path, the human wait and how to change it"
    )
    assert len(notice.splitlines()) == 1 and len(rendered) <= 1, "Do not repeat the notice across output channels"
    assert questions(case.store, include_closed=True) == [], "The safe default needs a notice, not a question"

    # Simulate another process and another PR, touching a different file in the migration tree.
    case.store = StateStore(case.store.root, now=lambda: NOW)
    case.ctx.store, case.ctx.config = case.store, load_config(case.root)
    later = path if glob == path else glob.removesuffix("/**") + "/later/nested/change.sql"
    outcome, later_pr = _merge(case, 3, [later])
    assert outcome == "held" and case.tracker.merged_prs == [], "Later PRs must follow the persisted always_human glob"
    assert case.tracker.get_issue(later_pr).state == "open"
    assert case.store.load_flag(3).reason == Reason.ALWAYS_HUMAN_PATH
    assert len(writes) == 1 and _path_events(case.store, glob) == events, "Restarting must not repeat the write or event"
    captured = capsys.readouterr()
    later_lines = captured.out + captured.err + case.store.log_path(3).read_text()
    assert notice not in later_lines, "Later holds must not repeat the first-use notice"
    assert questions(case.store, include_closed=True) == []


def test_pr_outside_detected_paths_merges_without_registering_them(tmp_path):
    case = _case(tmp_path, {DEPLOY: _workflow(), "migrations/001.sql": "SELECT 1;\n", "src/app.py": "value = 1\n"})
    before = (case.root / ".foragent.yml").read_bytes()
    outcome, pr = _merge(case, 1, ["src/app.py"])
    assert outcome == "merged" and case.tracker.merged_prs == [pr], "Unrelated PRs must keep the existing merge behavior"
    assert (case.root / ".foragent.yml").read_bytes() == before, "Register a path only when a PR touches it"
    assert not _path_events(case.store, DEPLOY) and not _path_events(case.store, "migrations/**")
    assert questions(case.store, include_closed=True) == []
    assert {DEPLOY, "migrations/**"} <= set(_detect(case.root))


def test_repository_without_detected_paths_merges_with_no_policy_change(tmp_path):
    case = _case(tmp_path, {"src/app.py": "value = 1\n"})
    before = (case.root / ".foragent.yml").read_bytes()
    outcome, pr = _merge(case, 1, ["src/app.py"])
    assert outcome == "merged" and case.tracker.merged_prs == [pr]
    assert (case.root / ".foragent.yml").read_bytes() == before
    assert not [event for event in _events(case.store) if event.get("say")], "There must be no irreversible-path notice"
    assert questions(case.store, include_closed=True) == []
    assert _detect(case.root) == [], "A repository without irreversible paths has no new default to install"


def test_init_asks_nothing_and_defers_registration_until_a_touching_merge(tmp_path, monkeypatch):
    from foragent import cli
    from foragent.init import init_repo

    case = _case(tmp_path, {DEPLOY: _workflow(), "migrations/001.sql": "SELECT 1;\n"})
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, *args, **kwargs: case.store))

    def gh_run(args, input=None):
        assert args == ["auth", "status"], "Init must use only the injected GitHub fake"
        return ""

    # Existing statusline/reporting choices are unrelated to irreversible-path consent.
    global_path = Path.home() / ".config/foragent/config.yml"
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text("upstream_reports: false\n")
    monkeypatch.setattr(cli, "init_repo", lambda selected_root, **kwargs: init_repo(
        selected_root, git=case.git, gh_run=gh_run, tracker=case.tracker, **kwargs,
    ))
    monkeypatch.setattr(cli, "_open_lane_view", lambda root: None)
    before = (case.root / ".foragent.yml").read_bytes()
    assert cli.main(["init", "--repo-root", str(case.root), "--no-push", "--no-launchd", "--no-board",
                     "--statusline", "no", "--starter", "no"]) == 0, "Offline fa init must succeed without irreversible-path questions"
    assert questions(case.store, include_closed=True) == [], "Init must not ask about future irreversible choices"
    assert (case.root / ".foragent.yml").read_bytes() == before, "Init must defer path registration until a touching merge"
    assert _events(case.store) == [], "Init must not announce a merge policy that has not been needed"
    assert {DEPLOY, "migrations/**"} <= set(_detect(case.root))
