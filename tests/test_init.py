"""Offline setup tests: Git mutations forbidden for this run are recorded only."""

from dataclasses import dataclass, field
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from foragent.config import ConfigError, load_config
from foragent.git import GitError, GitRepo
from foragent.init import init_repo
from foragent.issue import parse_body
from foragent.labels import LABELS
from foragent.tracker import FakeTracker, TrackerError

from conftest import GitHistory, git_command


PATHS = [".foragent.yml", ".github/ISSUE_TEMPLATE/foragent-task.md", "tests/acceptance/.gitkeep"]
EXPECTED_CONFIG = (
    "# Configuration: Foragent docs/gate.md (section 9), docs/contract.md (section 4).\n"
    "review: batch\napprovers:\n- human\nchecks: local\n"
)
EXPECTED_TEMPLATE = """---
name: Foragent task
about: Define a task for Foragent with executable acceptance checks.
---
<!-- foragent:v1 -->
## Goal

## Out of scope

## Done when
```sh
```
Commands must run in a CI-free shell; acceptance tests live in tests/acceptance/.
"""


@dataclass
class Setup:
    root: Path
    tracker: FakeTracker = field(default_factory=FakeTracker)
    git_calls: list = field(default_factory=list)
    gh_calls: list = field(default_factory=list)
    installs: list = field(default_factory=list)
    remote: str | None = "git@github.com:owner/repo.git"
    identity: dict = field(default_factory=lambda: {"user.name": "Test Human", "user.email": "human@example.com"})
    commit_fails: bool = False
    push_fails: bool = False
    private: bool | None = True

    def run_git(self, args):
        self.git_calls.append(args)
        root = self.root
        if args[:1] == ["-C"]:
            root, args = Path(args[1]), args[2:]
        if args == ["remote", "get-url", "origin"] and self.remote is not None:
            return self.remote
        if args[:2] == ["config", "--get"]:
            if args[2] not in self.identity:
                raise subprocess.CalledProcessError(1, ["git", *args], stderr="")
            return self.identity[args[2]]
        if "commit" in args:
            if self.commit_fails:
                raise subprocess.CalledProcessError(1, ["git", *args], stderr="pre-commit hook failed")
            # Import only the requested setup paths, retaining unrelated index entries.
            title = args[args.index("-m") + 1]
            parent = git_command(root, "rev-parse", "HEAD")
            branch = git_command(root, "symbolic-ref", "HEAD")
            stream = [
                f"commit {branch}\n", "committer Offline Tests <tests@example.com> 1792483200 +0000\n",
                f"data {len(title.encode())}\n{title}\n", f"from {parent}\n",
            ]
            for setup_path in args[args.index("--") + 1:]:
                if setup_path == ":!CLAUDE.local.md":
                    continue
                entry = git_command(root, "ls-files", "--stage", "--", setup_path)
                if entry:
                    metadata, filename = entry.split("\t", 1)
                    mode, sha, stage = metadata.split()
                    assert stage == "0"
                    stream.append(f"M {mode} {sha} {json.dumps(filename)}\n")
                else:
                    stream.append(f"D {json.dumps(setup_path)}\n")
            stream.append("\ndone\n")
            git_command(root, "fast-import", "--quiet", input="".join(stream))
            return ""
        if args[0] == "push":
            if self.push_fails:
                raise subprocess.CalledProcessError(1, ["git", *args], stderr="push rejected")
            source, destination = args[-1].split(":")
            origin = root.parent / "origin.git"
            git_command(origin, "fetch", str(root), f"{source}:{destination}")
            git_command(root, "update-ref", destination.replace("refs/heads/", "refs/remotes/origin/", 1),
                        git_command(root, "rev-parse", source))
            return ""
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout

    def run_gh(self, args, input=None):
        self.gh_calls.append((args, input))
        if args == ["auth", "status"]:
            return ""
        if args[:2] == ["repo", "view"] and args[-2:] == ["--json", "isPrivate"]:
            return json.dumps({"isPrivate": self.private})
        if args == ["api", "user", "--jq", ".login"]:
            return "human\n"
        if args[:3] == ["api", "-X", "POST"]:
            return "{}"
        raise AssertionError(args)

    def install(self, root):
        self.installs.append(root)
        return "dev.foragent.owner.repo"

    def init(self, **kwargs):
        # These legacy setup checks focus on files/labels. Board defaults and
        # lifecycle are exercised separately below and in locked acceptance tests.
        kwargs.setdefault("no_board", True)
        return init_repo(
            self.root, git=GitRepo(self.root, self.run_git), gh_run=self.run_gh,
            tracker=self.tracker, install_launchd=self.install, **kwargs,
        )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    # Isolate global config reads as well as all writes from the real home.
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    run = subprocess.run

    def offline_tools(argv, *args, **kwargs):
        if argv == ["codex", "login", "status"]:
            return subprocess.CompletedProcess(argv, 0, stdout="Logged in using ChatGPT\n", stderr="")
        if argv == ["claude", "auth", "status"]:
            return subprocess.CompletedProcess(argv, 0, stdout='{"loggedIn": true}', stderr="")
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", offline_tools)
    origin, root = tmp_path / "origin.git", tmp_path / "project"
    git_command(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    GitHistory(origin, root).update({"README.md": "Original\n"})
    git_command(tmp_path, "clone", str(origin), str(root))
    return Setup(root)


def test_init_creates_exact_files_labels_and_commits_only_setup_paths(setup):
    report = setup.init()
    assert report.created == PATHS and report.existed == []
    assert (setup.root / PATHS[0]).read_text() == EXPECTED_CONFIG
    assert (setup.root / PATHS[1]).read_text() == EXPECTED_TEMPLATE
    assert (setup.root / PATHS[2]).read_bytes() == b""
    body = EXPECTED_TEMPLATE.split("---\n", 2)[2]
    parsed = parse_body(body)
    assert parsed.version == "v1" and parsed.done_when_commands == [""]
    config = load_config(setup.root)
    assert config.approvers == ["human"] and config.review == "batch"
    assert config.executor == "codex" and config.acceptance_dir == "tests/acceptance/"
    assert config.run_hours == "always"
    assert list(setup.tracker.labels.values()) == LABELS
    assert ["add", "-f", "--", *PATHS] in setup.git_calls
    assert [
        "-c", "user.name=Test Human", "-c", "user.email=human@example.com",
        "commit", "--only", "-m", "Set up Foragent", "--", *PATHS, ":!CLAUDE.local.md",
    ] in setup.git_calls
    assert ["push", "origin", "refs/heads/main:refs/heads/main"] in setup.git_calls
    assert report.committed and report.pushed and setup.installs == [setup.root]
    assert "Created: .foragent.yml" in report.summary()


@pytest.mark.parametrize("error", [FileNotFoundError("codex"), subprocess.TimeoutExpired("codex", 10)])
def test_claude_default_when_codex_is_missing_or_probe_times_out(setup, monkeypatch, error):
    run = subprocess.run

    def probe(argv, *args, **kwargs):
        if argv == ["codex", "login", "status"]:
            raise error
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", probe)
    report = setup.init(dry_run=True)
    assert report.created == PATHS and not (setup.root / PATHS[0]).exists()
    setup.init()
    config = load_config(setup.root)
    assert config.executor == config.reviewer == "claude"


@pytest.mark.parametrize("status", ['{"loggedIn": false}', 'not JSON', '[]'])
def test_successful_exit_without_claude_login_is_not_authenticated(setup, monkeypatch, status):
    run = subprocess.run

    def probe(argv, *args, **kwargs):
        if argv == ["codex", "login", "status"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Not logged in")
        if argv == ["claude", "auth", "status"]:
            return subprocess.CompletedProcess(argv, 0, stdout=status, stderr="")
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", probe)
    with pytest.raises(ConfigError, match="No logged-in coding tool"):
        setup.init()
    assert not (setup.root / PATHS[0]).exists() and not setup.tracker.labels


def test_existing_config_does_not_probe_tools_or_change_worker_and_reviewer(setup, monkeypatch):
    original = "# Keep my choices\nexecutor: claude\nreviewer: codex\n"
    (setup.root / PATHS[0]).write_text(original)
    run = subprocess.run

    def no_probes(argv, *args, **kwargs):
        assert argv[0] not in {"codex", "claude"}, "Existing choices do not need tool detection"
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", no_probes)
    setup.init()
    assert (setup.root / PATHS[0]).read_text() == original + "checks: local\n"


def test_board_default_is_persisted_and_idempotent(setup):
    report = setup.init(no_board=False)
    project_id = load_config(setup.root).board
    assert project_id and project_id in report.summary()
    original = (setup.root / ".foragent.yml").read_bytes()
    setup.git_calls.clear()
    report = setup.init(no_board=False)
    assert report.board_id == project_id and not report.committed and not report.pushed
    assert (setup.root / ".foragent.yml").read_bytes() == original


@pytest.mark.parametrize("existing", ["# Keep me\nreview: fast\n", "review: fast\nboard: null\n"])
def test_init_adds_board_to_existing_config_and_commits_it(setup, existing):
    setup.init()
    path = setup.root / ".foragent.yml"
    path.write_text(existing)
    report = setup.init(no_board=False)
    assert load_config(setup.root).review == "fast" and load_config(setup.root).board
    assert path.read_text().count("board:") == 1
    if existing.startswith("#"):
        assert path.read_text().startswith(existing)
    assert report.added_keys == ["checks", "board"] and report.committed and report.pushed


def test_board_opt_out_and_dry_run_do_not_call_graphql(setup):
    setup.tracker.graphql = lambda *a: pytest.fail("must not call Projects API")
    setup.init(no_board=True)
    assert load_config(setup.root).board is None
    report = setup.init(no_board=False, dry_run=True)
    assert report.board_planned and report.commit_planned and report.push_planned
    assert load_config(setup.root).board is None


def test_board_failure_does_not_stop_init(setup, caplog):
    def unavailable(*args):
        raise OSError("missing project scope")

    setup.tracker.graphql = unavailable
    assert setup.init(no_board=False).committed
    assert load_config(setup.root).board is None
    assert "Could not configure board" in caplog.text


def test_existing_board_mapping_is_chosen_once_and_saved_locally(setup, monkeypatch):
    from foragent import board

    project_id = setup.tracker.graphql("""mutation ForagentCreateBoard($input: CreateProjectV2Input!) {
      createProjectV2(input: $input) { projectV2 { id } }
    }""", {"input": {"ownerId": "O_owner", "title": "Foragent: repo"}})["data"]["createProjectV2"]["projectV2"]["id"]
    original = board.Project(setup.tracker, "owner/repo", project_id).fields()["Status"]
    answers = iter(["1", "1", "2", "2", "3"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    setup.init(no_board=False)
    config = load_config(setup.root)
    assert config.board == project_id
    assert config.board_map == {"Backlog": "Todo", "Up next": "Todo", "In progress": "In Progress",
                                "Your turn": "In Progress", "Done": "Done"}
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("mapping must only be asked once"))
    assert not setup.init(no_board=False).committed
    assert board.Project(setup.tracker, "owner/repo", project_id).fields()["Status"] == original


def test_existing_board_with_foragent_columns_saves_default_mapping_once(setup, monkeypatch):
    from foragent import board

    project_id = setup.tracker.graphql("""mutation ForagentCreateBoard($input: CreateProjectV2Input!) {
      createProjectV2(input: $input) { projectV2 { id } }
    }""", {"input": {"ownerId": "O_owner", "title": "Foragent: repo"}})["data"]["createProjectV2"]["projectV2"]["id"]
    board.Project(setup.tracker, "owner/repo", project_id).ensure_fields({"Status": board.COLUMNS}, new_project=True)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("Foragent's own columns have one mapping"))
    report = setup.init(no_board=False)
    config = load_config(setup.root)
    assert config.board == project_id and config.board_map == board.DEFAULT_MAP
    assert "board_map" in (setup.root / ".foragent.yml").read_text() and report.committed
    assert not setup.init(no_board=False).committed


@pytest.mark.parametrize("value", ["Backlog=Todo", "Backlog=Todo,Backlog=Doing", "Unknown=Todo", "Backlog="])
def test_invalid_board_map_fails_before_external_calls(setup, value):
    with pytest.raises(ConfigError, match="--board-map"):
        setup.init(no_board=False, board_map=value)
    assert not setup.git_calls and not setup.gh_calls


def test_second_init_is_idempotent_and_preserves_config_and_mtimes(setup):
    setup.init(executor="codex", run_hours="always")
    before = {path: ((setup.root / path).read_bytes(), (setup.root / path).stat().st_mtime_ns) for path in PATHS}
    setup.git_calls.clear()
    setup.gh_calls.clear()
    report = setup.init(review="careful", executor="claude", approvers=["other"], run_hours="22-08")
    assert report.created == []
    assert report.existed == [PATHS[0], PATHS[1], "tests/acceptance/"]
    assert not report.committed and not report.pushed
    assert not any("commit" in args or args[0] == "push" for args in setup.git_calls)
    assert setup.gh_calls == [(["auth", "status"], None)]
    assert len(setup.installs) == 2
    assert before == {path: ((setup.root / path).read_bytes(), (setup.root / path).stat().st_mtime_ns) for path in PATHS}
    assert "Already exists: .foragent.yml" in report.summary()


def test_existing_partial_config_and_template_remain_byte_for_byte(setup):
    config = "# Keep this comment\nreview: careful\nexecutor: claude\napprovers: [original]\n"
    (setup.root / PATHS[0]).write_text(config)
    template = setup.root / PATHS[1]
    template.parent.mkdir(parents=True)
    template.write_text("Existing template\n")
    report = setup.init(review="fast", approvers=["new"])
    assert (setup.root / PATHS[0]).read_text() == config + "checks: local\n"
    assert template.read_text() == "Existing template\n"
    assert report.created == [PATHS[2]]
    assert ["add", "-f", "--", PATHS[2], *PATHS[:2]] in setup.git_calls


def test_existing_acceptance_directory_gets_no_placeholder(setup):
    (setup.root / "tests/acceptance").mkdir(parents=True)
    assert setup.init().created == PATHS[:2]
    assert not (setup.root / PATHS[2]).exists()


def test_only_nondefault_keys_and_explicit_approvers_are_written(setup):
    setup.init(review="fast", executor="claude", run_hours="22-08", approvers=["alice", "bob"])
    assert yaml.safe_load((setup.root / PATHS[0]).read_text()) == {
        "review": "fast", "executor": "claude", "approvers": ["alice", "bob"], "run_hours": "22-08",
        "reviewer": "claude", "checks": "local",
    }
    assert setup.gh_calls == [(["auth", "status"], None), (["repo", "view", "owner/repo", "--json", "isPrivate"], None)]


@pytest.mark.parametrize("language", ["ko", "en", "ja"])
def test_init_writes_explicit_language(setup, language):
    setup.init(language=language)
    assert yaml.safe_load((setup.root / PATHS[0]).read_text())["language"] == language
    assert load_config(setup.root).language == language


@pytest.mark.parametrize("key,value", [
    ("language", "ko"), ("review", "fast"), ("executor", "claude"),
    ("approvers", ["new-human"]), ("run_hours", "22-08"),
])
def test_existing_config_adds_only_missing_explicit_keys(setup, key, value):
    config = "# Preserve this comment\nbase_branch: main\n"
    (setup.root / PATHS[0]).write_text(config)
    report = setup.init(**{key: value})
    assert (setup.root / PATHS[0]).read_text().startswith(config)
    assert yaml.safe_load((setup.root / PATHS[0]).read_text()) == {"base_branch": "main", key: value, "checks": "local"}
    assert report.added_keys == [key, "checks"]
    assert f"Added config key: {key}" in report.summary()
    setup.git_calls.clear()
    report = setup.init(**{key: value})
    assert not report.added_keys and not report.committed


@pytest.mark.parametrize("text", [
    "", "{}\n", "{review: fast}\n", "---\nreview: fast\n...\n", "  review: fast\n",
    "review: fast\n... # end\n# keep valid YAML\n", "&config\n  review: fast\n",
    "null\n", "~\n", "NULL\n", "# Empty configuration\n", "---\nnull\n...\n",
])
def test_init_adds_missing_key_to_empty_flow_or_explicit_yaml_document(setup, text):
    (setup.root / PATHS[0]).write_text(text)
    setup.init(language="ko")
    assert load_config(setup.root).language == "ko"
    if "review" in text:
        assert load_config(setup.root).review == "fast"


def test_init_null_configuration_dry_run_and_multiple_missing_keys(setup):
    path = setup.root / PATHS[0]
    path.write_text("~\n")
    report = setup.init(language="ko", executor="claude", dry_run=True)
    assert report.added_keys == ["executor", "language", "checks"]
    assert path.read_text() == "~\n"
    setup.init(language="ko", executor="claude")
    assert yaml.safe_load(path.read_text()) == {"executor": "claude", "language": "ko", "checks": "local"}
    assert not setup.init(language="ko", executor="claude").added_keys


def test_existing_language_is_never_overwritten_and_missing_values_override_global(setup, monkeypatch):
    global_path = setup.root.parent / "global/config.yml"
    global_path.parent.mkdir()
    global_path.write_text("language: ja\nexecutor: claude\n")
    monkeypatch.setattr(Path, "home", lambda: global_path.parent.parent)
    # Use the standard global configuration location with a scoped fake HOME.
    global_config = Path.home() / ".config/foragent/config.yml"
    global_config.parent.mkdir(parents=True)
    global_config.write_text(global_path.read_text())
    (setup.root / PATHS[0]).write_text("language: ko\n")
    report = setup.init(language="en", executor="codex")
    assert load_config(setup.root).language == "ko"
    assert load_config(setup.root).executor == "codex"
    assert report.added_keys == ["executor", "checks"]


def test_init_dry_run_reports_missing_keys_without_writing(setup):
    (setup.root / PATHS[0]).write_text("review: fast\n")
    report = setup.init(language="ko", dry_run=True)
    assert (setup.root / PATHS[0]).read_text() == "review: fast\n"
    assert report.added_keys == ["language", "checks"]
    assert "Would add config key: language" in report.summary()
    assert not report.committed and report.commit_planned


def test_invalid_missing_option_on_existing_config_fails_before_writes(setup):
    (setup.root / PATHS[0]).write_text("review: fast\n")
    with pytest.raises(ConfigError, match="language"):
        setup.init(language="not a tag")
    assert (setup.root / PATHS[0]).read_text() == "review: fast\n"
    assert not setup.tracker.labels


def test_dirty_worktree_and_unrelated_staged_changes_are_preserved(setup):
    readme = setup.root / "README.md"
    readme.write_text("Staged user change\n")
    git_command(setup.root, "add", "README.md")
    staged = git_command(setup.root, "show", ":README.md")
    readme.write_text("Unstaged user change\n")
    (setup.root / "untracked.txt").write_text("Keep me\n")
    setup.init()
    assert git_command(setup.root, "show", ":README.md") == staged
    assert readme.read_text() == "Unstaged user change\n"
    assert (setup.root / "untracked.txt").read_text() == "Keep me\n"
    commit = next(args for args in setup.git_calls if "commit" in args)
    assert "--only" in commit and commit[commit.index("--") + 1:] == [*PATHS, ":!CLAUDE.local.md"]
    assert [args for args in setup.git_calls if args[0] == "add"] == [["add", "-f", "--", *PATHS]]


def test_no_push_still_commits_and_no_launchd_skips_install(setup):
    report = setup.init(no_push=True, no_launchd=True)
    assert report.committed and not report.pushed and report.launchd_label is None
    assert not any(args[0] == "push" for args in setup.git_calls)
    assert setup.installs == []


@pytest.mark.parametrize("initialized", [False, True])
@pytest.mark.parametrize("skip", [False, True])
def test_dry_run_leaves_repo_labels_and_launchd_unchanged(setup, initialized, skip):
    if initialized:
        setup.init()
    setup.git_calls.clear()
    setup.tracker.labels.clear()
    setup.installs.clear()
    before = {path.relative_to(setup.root): path.read_bytes() for path in setup.root.rglob("*") if path.is_file()}
    report = setup.init(dry_run=True, no_push=skip, no_launchd=skip)
    after = {path.relative_to(setup.root): path.read_bytes() for path in setup.root.rglob("*") if path.is_file()}
    assert before == after and setup.tracker.labels == {} and setup.installs == []
    assert not any("commit" in args or args[0] in {"add", "push"} for args in setup.git_calls)
    assert not report.committed and not report.pushed
    assert report.summary().startswith("Foragent setup plan:")
    assert ("Would install" in report.summary()) == (not skip)
    assert ("Would push" in report.summary()) == (not initialized and not skip)


@pytest.mark.parametrize("identity,expected", [
    ({}, ["-c", "user.name=foragent", "-c", "user.email=foragent@localhost"]),
    ({"user.name": "Human"}, ["-c", "user.name=Human", "-c", "user.email=foragent@localhost"]),
])
def test_missing_git_identity_uses_fallback(setup, identity, expected):
    setup.identity = identity
    setup.init()
    assert next(args for args in setup.git_calls if "commit" in args)[:4] == expected


def test_not_a_git_worktree_fails_before_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    with pytest.raises(GitError, match="git work tree"):
        init_repo(tmp_path, gh_run=lambda *a, **kw: pytest.fail("must not call gh"))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("remote", [None, "git@gitlab.com:owner/repo.git", "https://github.com.evil/owner/repo"])
def test_origin_must_be_github(setup, remote):
    setup.remote = remote
    with pytest.raises(GitError, match="origin remote on github.com"):
        setup.init()
    assert not (setup.root / PATHS[0]).exists() and setup.gh_calls == []


def test_missing_origin_has_clear_error(setup):
    setup.remote = None
    git_command(setup.root, "remote", "remove", "origin")
    with pytest.raises(GitError, match="origin remote on github.com"):
        setup.init()


@pytest.mark.parametrize("error", [
    subprocess.CalledProcessError(1, ["gh", "auth", "status"], stderr="not logged in"),
    FileNotFoundError("gh not installed"),
])
def test_auth_precondition_error_changes_nothing(setup, error):
    def fail(*args, **kwargs):
        raise error

    setup.run_gh = fail
    with pytest.raises(TrackerError, match="gh auth status failed"):
        setup.init()
    assert not (setup.root / PATHS[0]).exists() and setup.tracker.labels == {}


def test_empty_authenticated_login_fails(setup):
    setup.run_gh = lambda *args, **kwargs: ""
    with pytest.raises(TrackerError, match="empty authenticated login"):
        setup.init()
    assert setup.tracker.labels == {}


@pytest.mark.parametrize("options", [
    {"run_hours": "24-08"}, {"run_hours": "bad"}, {"review": "unknown"}, {"executor": "unknown"},
])
def test_invalid_settings_fail_without_writes(setup, options):
    with pytest.raises(ConfigError):
        setup.init(**options)
    assert not (setup.root / PATHS[0]).exists() and setup.tracker.labels == {}


def test_requires_base_branch_without_switching_branches(setup):
    git_command(setup.root, "symbolic-ref", "HEAD", "refs/heads/feature")
    with pytest.raises(GitError, match="base branch 'main'"):
        setup.init()
    assert not (setup.root / PATHS[0]).exists() and setup.tracker.labels == {}


def test_existing_config_selects_base_branch(setup):
    git_command(setup.root, "branch", "trunk")
    git_command(setup.root, "symbolic-ref", "HEAD", "refs/heads/trunk")
    (setup.root / PATHS[0]).write_text("base_branch: trunk\n")
    setup.init()
    assert ["push", "origin", "refs/heads/trunk:refs/heads/trunk"] in setup.git_calls


def test_default_gh_tracker_uses_injected_runner_for_labels(setup):
    init_repo(
        setup.root, git=GitRepo(setup.root, setup.run_git), gh_run=setup.run_gh,
        no_launchd=True, no_push=True,
        no_board=True,
    )
    mutations = [(args, body) for args, body in setup.gh_calls if args[:3] == ["api", "-X", "POST"]]
    assert len(mutations) == len(LABELS)
    assert all(args[3] == "repos/owner/repo/labels" for args, body in mutations)


def test_rerun_recovers_after_commit_hook_failure(setup):
    before = git_command(setup.root, "rev-parse", "HEAD")
    setup.commit_fails = True
    with pytest.raises(GitError, match="pre-commit hook failed"):
        setup.init()
    assert git_command(setup.root, "rev-parse", "HEAD") == before
    assert set(git_command(setup.root, "diff", "--cached", "--name-only").splitlines()) == set(PATHS)
    setup.commit_fails = False
    setup.git_calls.clear()
    report = setup.init()
    assert report.created == [] and report.committed and report.pushed
    assert "Committed: Set up Foragent" in report.summary()
    assert git_command(setup.root, "log", "-1", "--format=%s") == "Set up Foragent"
    assert git_command(setup.root, "status", "--porcelain") == ""


def test_init_push_ignores_origin_push_destination_override(setup):
    origin = setup.root.parent / "origin.git"
    original = git_command(origin, "rev-parse", "main")
    git_command(origin, "branch", "release", "main")
    git_command(setup.root, "config", "remote.origin.push", "refs/heads/main:refs/heads/release")
    assert setup.init().pushed
    assert ["push", "origin", "refs/heads/main:refs/heads/main"] in setup.git_calls
    assert git_command(origin, "rev-parse", "release") == original
    assert git_command(origin, "rev-parse", "main") == git_command(setup.root, "rev-parse", "HEAD")


def test_rerun_recovers_rejected_push_without_another_commit(setup):
    setup.push_fails = True
    with pytest.raises(GitError, match="push rejected"):
        setup.init()
    setup_sha = git_command(setup.root, "rev-parse", "HEAD")
    setup.push_fails = False
    setup.git_calls.clear()
    report = setup.init()
    assert report.created == [] and not report.committed and report.pushed
    assert "Committed:" not in report.summary() and "Pushed to origin." in report.summary()
    assert not any("commit" in args for args in setup.git_calls)
    assert git_command(setup.root, "rev-parse", "origin/main") == setup_sha


@pytest.mark.parametrize("staged", [False, True])
def test_rerun_commits_existing_setup_changes_and_preserves_other_staged_paths(setup, staged):
    setup.init()
    template = setup.root / PATHS[1]
    template.write_text(template.read_text() + "Extra guidance\n")
    if staged:
        git_command(setup.root, "add", "--", PATHS[1])
    (setup.root / "README.md").write_text("User change\n")
    git_command(setup.root, "add", "--", "README.md")
    setup.git_calls.clear()
    report = setup.init()
    assert not report.created and report.committed and report.pushed
    commit = next(args for args in setup.git_calls if "commit" in args)
    assert commit[commit.index("--") + 1:] == [PATHS[1], ":!CLAUDE.local.md"]
    assert git_command(setup.root, "diff", "--cached", "--name-only") == "README.md"
    assert git_command(setup.root, "show", "HEAD:README.md") == "Original"


@pytest.mark.parametrize("no_push", [False, True])
def test_recovery_dry_run_plans_existing_commit_and_push_without_writes(setup, no_push):
    setup.init(no_push=True)
    (setup.root / PATHS[0]).write_text(EXPECTED_CONFIG + "executor: claude\n")
    setup.git_calls.clear()
    report = setup.init(dry_run=True, no_push=no_push)
    assert not report.created and report.commit_planned and report.push_planned == (not no_push)
    assert "Would commit: Set up Foragent" in report.summary()
    assert not report.committed and not report.pushed
    assert not any("commit" in args or args[0] in {"add", "push"} for args in setup.git_calls)


def test_push_recovery_detects_setup_among_multiple_unpushed_commits(setup):
    setup.init(no_push=True)
    (setup.root / "README.md").write_text("Follow-up\n")
    GitRepo(setup.root, setup.run_git).commit_paths(["README.md"], "Follow-up work")
    report = setup.init()
    assert report.pushed and not report.committed
    assert git_command(setup.root, "rev-parse", "HEAD") == git_command(setup.root, "rev-parse", "origin/main")


def test_no_push_for_unrelated_commits_ahead_of_origin(setup):
    setup.init()
    (setup.root / "README.md").write_text("Other work\n")
    GitRepo(setup.root, setup.run_git).commit_paths(["README.md"], "Other work")
    setup.git_calls.clear()
    report = setup.init()
    assert not report.committed and not report.pushed
    assert not any(args[0] == "push" for args in setup.git_calls)


def test_no_push_recovery_when_base_has_diverged(setup):
    setup.init(no_push=True)
    origin = setup.root.parent / "origin.git"
    GitHistory(origin, setup.root, git_command(origin, "rev-parse", "main")).update({"remote.txt": "Remote work\n"})
    GitRepo(setup.root).fetch("origin", "main")
    assert not setup.init().pushed


def test_push_recovery_when_remote_branch_does_not_exist_yet(setup):
    setup.init(no_push=True)
    git_command(setup.root, "update-ref", "-d", "refs/remotes/origin/main")
    assert setup.init().pushed


def test_explicit_default_options_override_global_config(setup):
    global_path = Path.home() / ".config/foragent/config.yml"
    global_path.parent.mkdir(parents=True)
    global_path.write_text("executor: claude\nrun_hours: 22-08\nreview: careful\napprovers: [global]\n")
    setup.init(executor="codex", run_hours="always", review="batch", approvers=["human"])
    assert yaml.safe_load((setup.root / PATHS[0]).read_text()) == {
        "executor": "codex", "run_hours": "always", "review": "batch", "approvers": ["human"],
        "checks": "local",
    }
    config = load_config(setup.root)
    assert (config.executor, config.run_hours, config.review, config.approvers) == (
        "codex", "always", "batch", ["human"],
    )


@pytest.mark.parametrize("dry_run", [False, True])
def test_nested_repo_root_is_normalized_and_reported(setup, dry_run):
    nested = setup.root / "src"
    nested.mkdir()
    report = init_repo(
        nested, git=GitRepo(nested, setup.run_git), gh_run=setup.run_gh,
        tracker=setup.tracker, install_launchd=setup.install, dry_run=dry_run,
    )
    assert report.normalized_root == setup.root
    assert f"Using repository root: {setup.root}" in report.summary()
    assert list(nested.iterdir()) == []
    assert (setup.root / PATHS[0]).exists() == (not dry_run)
    assert setup.installs == ([] if dry_run else [setup.root])


def test_recovery_still_requires_base_branch_for_pending_paths(setup):
    setup.init()
    (setup.root / PATHS[0]).write_text(EXPECTED_CONFIG + "executor: claude\n")
    git_command(setup.root, "branch", "feature")
    git_command(setup.root, "symbolic-ref", "HEAD", "refs/heads/feature")
    setup.git_calls.clear()
    with pytest.raises(GitError, match="base branch 'main'"):
        setup.init()
    assert not any("commit" in args or args[0] == "push" for args in setup.git_calls)


def test_init_ci_commits_workflow_together_with_checks_choice(setup, monkeypatch):
    from foragent.ci_workflow import WORKFLOW

    setup.private = False
    (setup.root / '.foragent.yml').write_text('test_command: python -m pytest\n')
    monkeypatch.setattr('builtins.input', lambda prompt: pytest.fail('checks must not prompt'))
    report = setup.init()
    assert WORKFLOW in report.created
    assert yaml.safe_load((setup.root / '.foragent.yml').read_text())['checks'] == 'ci'
    commit = next(args for args in setup.git_calls if 'commit' in args)
    assert '.foragent.yml' in commit and WORKFLOW in commit
    assert git_command(setup.root, 'show', f'HEAD:{WORKFLOW}') == (setup.root / WORKFLOW).read_text().strip()


@pytest.mark.parametrize('existing', [
    '.github/workflows/foragent-acceptance.yml',
    '.github/workflows/foragent-acceptance-publish.yml',
])
def test_ci_init_creates_missing_workflow_without_rewriting_existing(setup, existing):
    from foragent.ci_workflow import PUBLISH_WORKFLOW, WORKFLOW

    history = GitHistory(setup.root.parent / 'origin.git', setup.root,
                         git_command(setup.root, 'rev-parse', 'HEAD'))
    original = 'name: Customized workflow\non: [workflow_dispatch]\n'
    history.update({'.foragent.yml': 'checks: ci\ntest_command: python -m pytest\n', existing: original})
    git_command(setup.root, 'fetch', 'origin')
    git_command(setup.root, 'merge', '--ff-only', 'origin/main')
    missing = PUBLISH_WORKFLOW if existing == WORKFLOW else WORKFLOW

    report = setup.init()

    assert missing in report.created and existing in report.existed
    assert (setup.root / existing).read_text() == original
    commit = next(args for args in setup.git_calls if 'commit' in args)
    assert missing in commit and existing not in commit
    assert git_command(setup.root, 'status', '--porcelain') == ''
    assert not setup.init().committed


def test_public_init_without_a_detected_test_command_chooses_local(setup):
    from foragent.ci_workflow import WORKFLOW

    setup.private = False
    setup.init()
    assert yaml.safe_load((setup.root / '.foragent.yml').read_text())['checks'] == 'local'
    assert not (setup.root / WORKFLOW).exists()


def test_local_init_leaves_existing_acceptance_workflow_uncommitted(setup):
    from foragent.ci_workflow import WORKFLOW

    setup.init()
    path = setup.root / WORKFLOW
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('name: user workflow\n')
    report = setup.init()
    assert not report.committed
    assert path.read_text() == 'name: user workflow\n'
    assert git_command(setup.root, 'ls-files', '--', WORKFLOW) == ''


@pytest.mark.parametrize('existing', [False, True])
def test_public_non_pytest_project_selects_local_with_one_line_note(setup, capsys, existing):
    from foragent.ci_workflow import WORKFLOW

    setup.private = False
    (setup.root / 'package.json').write_text('{"scripts":{"test":"node test.js"}}')
    if existing:
        (setup.root / '.foragent.yml').write_text('test_command: npm test\n')
    setup.init()
    assert load_config(setup.root).checks == 'local'
    assert not (setup.root / WORKFLOW).exists()
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and 'pytest' in lines[0] and 'checks: local' in lines[0]


@pytest.mark.parametrize('private,mode,reason', [(False, 'ci', 'free'), (True, 'local', 'minutes'),
                                               (None, 'local', 'visibility')])
def test_missing_checks_in_existing_config_is_selected_without_input_and_kept(
    setup, monkeypatch, capsys, private, mode, reason,
):
    from foragent.ci_workflow import WORKFLOW

    setup.private = private
    path = setup.root / '.foragent.yml'
    original = '# Preserve project settings\nreview: fast\ntest_command: python -m pytest\n'
    path.write_text(original)
    monkeypatch.setattr('builtins.input', lambda prompt: pytest.fail('checks must not prompt'))
    report = setup.init()
    assert path.read_text().startswith(original)
    assert yaml.safe_load(path.read_text())['checks'] == mode
    assert report.added_keys == ['checks']
    assert (setup.root / WORKFLOW).exists() is (mode == 'ci')
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and f'checks: {mode}' in lines[0]
    assert reason in lines[0] and '.foragent.yml' in lines[0]
    if private is True:
        assert 'self-hosted' in lines[0] and 'ci_runs_on' in lines[0]
    if private is None:
        assert 'Note:' in lines[0]
    before = path.read_bytes()
    setup.private = not private
    setup.gh_calls.clear()
    assert not setup.init().committed
    assert path.read_bytes() == before
    assert setup.gh_calls == [(['auth', 'status'], None)]
