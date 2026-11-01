"""Offline setup tests: Git mutations forbidden for this run are recorded only."""

from dataclasses import dataclass, field
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from foragent.config import ConfigError, load_config
from foragent.git import GitError, GitRepo
from foragent.i18n import text
from foragent.init import init_repo
from foragent import role_agents, roles
from foragent.issue import parse_body
from foragent.labels import LABELS
from foragent.tracker import FakeTracker, TrackerError

from conftest import GitHistory, git_command


PATHS = [".foragent.yml", ".github/ISSUE_TEMPLATE/foragent-task.md", "tests/acceptance/.gitkeep"]
# The orchestrator and architect agents, memory and skill copies follow the setup files.
ROLE_PATHS = list(role_agents.missing_files(Path("/nonexistent-foragent-project"))[0])
ALL_PATHS = [*PATHS, *ROLE_PATHS]
# Init also installs the pending lane-memory union line (#1000); it is published, not "created".
PUBLISHED_PATHS = [*ALL_PATHS, ".gitattributes"]
def without_roles(text):
    """Settings text without the roles block init writes (#769); each default role must be in it."""
    lines, kept, inside = text.splitlines(keepends=True), [], False
    for line in lines:
        if line.rstrip("\n") == "roles:":
            inside = True
            continue
        if inside and line.startswith((" ", "-")):
            continue
        inside = False
        kept.append(line)
    # Only for configs init wrote: each must carry every default role.
    assert set(roles.DEFAULT_ROLES) <= (yaml.safe_load(text) or {}).get("roles", {}).keys()
    return "".join(kept)


def fresh_config_values(path):
    """A freshly initialized config without its roles block, which must hold every default role."""
    return yaml.safe_load(without_roles(path.read_text()))


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
        if args == ["api", "user"]:
            return json.dumps({"login": "Human", "type": "User"})
        if args[:3] == ["api", "--paginate", "repos/owner/repo/issues?state=all&per_page=100"]:
            return "[]"
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
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):  # A real key would change detection.
        monkeypatch.delenv(name, raising=False)
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
    assert report.created == ALL_PATHS and report.existed == []
    assert without_roles((setup.root / PATHS[0]).read_text()) == EXPECTED_CONFIG
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
    assert ["add", "-f", "--", *PUBLISHED_PATHS] in setup.git_calls
    assert [
        "-c", "user.name=Test Human", "-c", "user.email=human@example.com",
        "commit", "--only", "-m", "Set up Foragent", "--", *PUBLISHED_PATHS, ":!CLAUDE.local.md",
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
    assert report.created == ALL_PATHS and not (setup.root / PATHS[0]).exists()
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


def logged_out_claude(monkeypatch, installed=True):
    run = subprocess.run

    def probe(argv, *args, **kwargs):
        if argv == ["codex", "login", "status"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Not logged in")
        if argv == ["claude", "auth", "status"]:
            return subprocess.CompletedProcess(argv, 1, stdout='{"loggedIn": false}', stderr="")
        if argv == ["claude", "--version"]:
            if not installed:
                raise FileNotFoundError("claude")
            return subprocess.CompletedProcess(argv, 0, stdout="2.0.0 (Claude Code)\n", stderr="")
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", probe)


@pytest.mark.parametrize("token,hinted", [(None, True), ("offline-token", False)])
def test_logged_out_claude_on_an_api_key_or_token_is_available(setup, monkeypatch, token, hinted):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-key")
    if token:
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", token)
    logged_out_claude(monkeypatch)
    summary = setup.init(language="ko").summary()
    config = load_config(setup.root)
    assert config.executor == config.reviewer == "claude"
    saved = (setup.root / PATHS[0]).read_text()
    assert "offline" not in saved and "spend_cap" not in saved
    hints = [line for line in summary.splitlines() if "spend_cap" in line]
    assert hints == ([text("init_api_key_hint", "ko")] if hinted else [])
    assert "spend_cap" not in setup.init().summary(), "re-init does not probe tools or repeat the hint"


def test_api_key_without_installed_claude_is_not_available(setup, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-key")
    logged_out_claude(monkeypatch, installed=False)
    with pytest.raises(ConfigError, match="No logged-in coding tool"):
        setup.init()


def test_logged_in_claude_with_an_api_key_prints_no_hint(setup, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-key")
    assert "spend_cap" not in setup.init().summary()


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
    assert report.created == [PATHS[2], *ROLE_PATHS]
    assert ["add", "-f", "--", PATHS[2], *ROLE_PATHS, PATHS[0], ".gitattributes", PATHS[1]] in setup.git_calls


def test_existing_acceptance_directory_gets_no_placeholder(setup):
    (setup.root / "tests/acceptance").mkdir(parents=True)
    assert setup.init().created == [*PATHS[:2], *ROLE_PATHS]
    assert not (setup.root / PATHS[2]).exists()


def test_only_nondefault_keys_and_explicit_approvers_are_written(setup):
    setup.init(review="fast", executor="claude", run_hours="22-08", approvers=["alice", "bob"])
    assert fresh_config_values(setup.root / PATHS[0]) == {
        "review": "fast", "executor": "claude", "approvers": ["alice", "bob"], "run_hours": "22-08",
        "reviewer": "claude", "checks": "local",
    }
    # No pytest command here, so the repository's visibility is not needed (#419).
    assert setup.gh_calls == [(["auth", "status"], None)]


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
    assert "--only" in commit and commit[commit.index("--") + 1:] == [*PUBLISHED_PATHS, ":!CLAUDE.local.md"]
    assert [args for args in setup.git_calls if args[0] == "add"] == [["add", "-f", "--", *PUBLISHED_PATHS]]


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
    with pytest.raises(GitError, match="origin remote on github.com") as raised:
        setup.init()
    assert not (setup.root / PATHS[0]).exists() and setup.gh_calls == []
    if remote is not None:
        assert "git remote set-url origin https://github.com/<owner>/<repo>.git" in str(raised.value)
        assert "git remote add origin" not in str(raised.value)


def test_missing_origin_has_clear_error(setup):
    setup.remote = None
    git_command(setup.root, "remote", "remove", "origin")
    with pytest.raises(GitError, match="origin remote on github.com") as raised:
        setup.init()
    assert "git remote add origin https://github.com/<owner>/<repo>.git" in str(raised.value)
    assert "set-url" not in str(raised.value)


def test_preflight_fix_follows_requested_language(setup):
    setup.remote = None
    git_command(setup.root, "remote", "remove", "origin")
    with pytest.raises(GitError, match="origin 원격을 추가하세요") as raised:
        setup.init(language="ko")
    assert "git remote add origin https://github.com/<owner>/<repo>.git" in str(raised.value)


@pytest.mark.parametrize("error,fix,other", [
    (subprocess.CalledProcessError(1, ["gh", "auth", "status"], stderr="not logged in"),
     "gh auth login", "brew install gh"),
    (FileNotFoundError("gh not installed"), "brew install gh", "gh auth login"),
])
def test_auth_precondition_error_changes_nothing(setup, error, fix, other):
    def fail(*args, **kwargs):
        raise error

    setup.run_gh = fail
    with pytest.raises(TrackerError, match="gh auth status failed") as raised:
        setup.init()
    assert not (setup.root / PATHS[0]).exists() and setup.tracker.labels == {}
    assert fix in str(raised.value) and other not in str(raised.value)


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
    assert set(git_command(setup.root, "diff", "--cached", "--name-only").splitlines()) == set(PUBLISHED_PATHS)
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
    assert fresh_config_values(setup.root / PATHS[0]) == {
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
    from foragent.ci_workflow import WORKFLOW

    history = GitHistory(setup.root.parent / 'origin.git', setup.root,
                         git_command(setup.root, 'rev-parse', 'HEAD'))
    original = 'name: Customized workflow\non: [workflow_dispatch]\n'
    history.update({'.foragent.yml': 'checks: ci\ntest_command: python -m pytest\n', existing: original})
    git_command(setup.root, 'fetch', 'origin')
    git_command(setup.root, 'merge', '--ff-only', 'origin/main')
    missing = existing != WORKFLOW

    report = setup.init()

    assert (WORKFLOW in report.created) is missing
    assert '.github/workflows/foragent-acceptance-publish.yml' not in report.created
    assert (setup.root / existing).read_text() == original
    commits = [args for args in setup.git_calls if 'commit' in args]
    assert any(WORKFLOW in args for args in commits) is missing
    assert all(existing not in args for args in commits)
    assert git_command(setup.root, 'status', '--porcelain') == ''
    assert not setup.init().committed


@pytest.mark.parametrize('mode', ['ci', 'local'])
@pytest.mark.parametrize('generated', [True, False])
@pytest.mark.parametrize('tracked', [True, False])
def test_init_rerun_removes_only_generated_publisher_and_commits_deletion(setup, generated, mode, tracked):
    from foragent.ci_workflow import WORKFLOW

    setup.init()
    legacy = '.github/workflows/foragent-acceptance-publish.yml'
    original = ('# Generated by fa ci workflow.\n' if generated else '# Team workflow\n') + (
        'name: foragent-acceptance-publish\njobs:\n  publish:\n'
        '    steps:\n      - run: python -m foragent.ci_publish\n')
    history = GitHistory(setup.root.parent / 'origin.git', setup.root,
                         git_command(setup.root, 'rev-parse', 'HEAD'))
    history.update({'.foragent.yml': f'checks: {mode}\ntest_command: python -m pytest\n',
                    WORKFLOW: 'name: customized measurement\n', **({legacy: original} if tracked else {})})
    git_command(setup.root, 'fetch', 'origin')
    git_command(setup.root, 'merge', '--ff-only', 'origin/main')
    if not tracked:
        (setup.root / legacy).write_text(original)
    setup.git_calls.clear()
    plan = setup.init(dry_run=True)
    assert (setup.root / legacy).read_text() == original
    assert plan.removed == ([legacy] if generated else [])
    assert ('Would remove: ' + legacy in plan.summary()) is generated
    report = setup.init()
    assert report.removed == ([legacy] if generated else [])
    assert (setup.root / legacy).exists() is not generated
    if generated:
        assert 'Removed: ' + legacy in report.summary()
        assert any(legacy in args for args in setup.git_calls if 'commit' in args) is tracked
        assert git_command(setup.root, 'ls-tree', 'HEAD', '--', legacy) == ''
    else:
        assert (setup.root / legacy).read_text() == original
        assert not report.committed
    assert not setup.init().committed


def test_init_includes_staged_publisher_deletion_when_generated_file_is_recreated(setup):
    setup.init()
    legacy = '.github/workflows/foragent-acceptance-publish.yml'
    original = '# Generated by fa ci workflow.\nname: foragent-acceptance-publish\n'
    history = GitHistory(setup.root.parent / 'origin.git', setup.root,
                         git_command(setup.root, 'rev-parse', 'HEAD'))
    history.update({legacy: original})
    git_command(setup.root, 'fetch', 'origin')
    git_command(setup.root, 'merge', '--ff-only', 'origin/main')
    git_command(setup.root, 'rm', '--', legacy)
    (setup.root / legacy).parent.mkdir(parents=True, exist_ok=True)
    (setup.root / legacy).write_text(original)
    setup.git_calls.clear()

    assert setup.init(dry_run=True).commit_planned
    assert (setup.root / legacy).read_text() == original
    report = setup.init()

    assert report.removed == [legacy] and report.committed
    assert any(legacy in args for args in setup.git_calls if 'commit' in args)
    assert not (setup.root / legacy).exists()
    assert git_command(setup.root, 'ls-tree', 'HEAD', '--', legacy) == ''
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


def test_role_agent_with_wrong_frontmatter_is_kept_with_one_warning(setup, capsys):
    agent = setup.root / ".claude/agents/fa-architect.md"
    agent.parent.mkdir(parents=True)
    agent.write_text("No frontmatter here\n")
    skill = setup.root / ".claude/skills/fa-orchestrator/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Project's own orchestrator skill\n")
    report = setup.init()
    assert agent.read_text() == "No frontmatter here\n"
    assert skill.read_text() == "Project's own orchestrator skill\n"
    assert ".claude/agents/fa-architect.md" not in report.created
    assert not any(path.startswith(".claude/skills/fa-orchestrator/") for path in report.created)
    assert (setup.root / ".claude/agents/fa-orchestrator.md").read_text() == role_agents.agent_text("fa-orchestrator")
    assert (setup.root / ".claude/agent-memory/fa-architect/MEMORY.md").read_text() == "# fa-architect memory\n"
    warnings = [line for line in capsys.readouterr().err.splitlines() if "fa-architect.md" in line]
    assert len(warnings) == 1 and warnings[0].startswith("Warning:")


def test_dry_run_lists_role_files_and_creates_none(setup):
    report = setup.init(dry_run=True)
    assert "Would create: .claude/agents/fa-orchestrator.md" in report.summary()
    assert "Would create: .claude/agent-memory/fa-architect/MEMORY.md" in report.summary()
    assert not (setup.root / ".claude").exists()


def test_package_data_covers_every_file_of_both_role_skills():
    import tomllib
    root = Path(__file__).resolve().parents[1]
    data_files = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["setuptools"]["data-files"]
    # fa-docs ships with the docs-role issue; until then init lists it without copying.
    for role in [role for role in role_agents.ROLES if role != "fa-docs" or (root / "skills" / role).is_dir()]:
        source = root / "skills" / role
        files = [path.relative_to(root) for path in source.rglob("*") if path.is_file()]
        assert files, f"{role} skill is missing"
        for path in files:
            destination = (Path("share/foragent") / path.parent).as_posix()
            assert any(path.match(pattern) and path.parent == Path(pattern).parent
                       for pattern in data_files.get(destination, [])), f"{path} is not packaged"


def test_reinit_keeps_role_entries_and_adds_missing_defaults_from_agent_skills(setup):
    path = setup.root / PATHS[0]
    custom = {"agent": "fa-orchestrator", "skills": ["kept-skill"], "memory": ".claude/agent-memory/fa-orchestrator/",
              "signals": [], "idle_days": 3}
    path.write_text("# Keep this comment\nreview: fast\nroles:\n  orchestrator: "
                    + yaml.safe_dump(custom, default_flow_style=True))
    agent = setup.root / ".claude/agents/fa-architect.md"
    agent.parent.mkdir(parents=True)
    agent.write_text("---\nname: fa-architect\nmemory: project\nskills: [own-architect]\n---\nBody\n")
    report = setup.init()
    text = path.read_text()
    assert text.startswith("# Keep this comment\nreview: fast\n") and report.committed
    source = yaml.safe_load(text)["roles"]
    assert source["orchestrator"] == custom
    assert source["architect"]["skills"] == ["own-architect"] and source["architect"]["created_at"]
    assert source["docs"]["skills"] == ["fa-docs"] and source["docs"]["scope"] == ["docs/**", "README*"]
    assert agent.read_text().endswith("Body\n")
    assert not setup.init().committed


def test_reinit_writes_missing_default_agent_from_its_roles_entry(setup):
    setup.init()
    path = setup.root / PATHS[0]
    source = yaml.safe_load(path.read_text())["roles"]
    source["architect"]["skills"] = ["project-architect"]
    path.write_text(path.read_text().replace("    - fa-architect\n", "    - project-architect\n", 1))
    assert yaml.safe_load(path.read_text())["roles"] == source
    (setup.root / ".claude/agents/fa-architect.md").unlink()
    setup.init()
    agent = (setup.root / ".claude/agents/fa-architect.md").read_text()
    assert yaml.safe_load(agent.split("---\n")[1])["skills"] == ["project-architect"]
    assert yaml.safe_load(path.read_text())["roles"] == source
    assert roles.role_drift(setup.root, load_config(setup.root)) == []


def recording_commit(commits):
    """A commit_paths stand-in: record the paths and commit the index with plumbing (real commits are guarded)."""
    def commit(self, paths, title):
        commits.append(sorted(paths))
        tree = git_command(self.root, "write-tree")
        head = git_command(self.root, "rev-parse", "HEAD")
        new = git_command(self.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", tree, "-p", head,
                          "-m", title)
        git_command(self.root, "update-ref", "HEAD", new)
    return commit


def test_role_add_uses_an_installed_skill_copy_and_records_why_without_github(setup, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    setup.init()
    commits = []
    monkeypatch.setattr(GitRepo, "commit_paths", recording_commit(commits))
    monkeypatch.setattr(GitRepo, "ensure_tracked", lambda self, paths: None)
    installed = setup.root / ".claude/skills/house-style"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("---\nname: house-style\ndescription: Installed copy\n---\nBody\n")
    monkeypatch.setattr(GitRepo, "remote_slug", lambda self: (_ for _ in ()).throw(GitError("local origin")))
    roles.add_role(setup.root, "style", skills=["house-style"], scope=[], signals=["kinds:hot_overlap_files"],
                   why="Keep the house style")
    entry = yaml.safe_load((setup.root / PATHS[0]).read_text())["roles"]["style"]
    assert entry["skills"] == ["house-style"]
    assert commits == [[".claude/agent-memory/fa-style/MEMORY.md", ".claude/agents/fa-style.md", ".foragent.yml"]]
    assert entry["signals"] == [{"paths": [], "kinds": ["hot_overlap_files"]}]
    events = list((tmp_path / "home").rglob("orch-events.jsonl"))
    assert len(events) == 1
    rows = [json.loads(line) for line in events[0].read_text().splitlines()]
    assert [(row["kind"], row["why"]) for row in rows] == [("role-added", "Keep the house style")]
    with pytest.raises(Exception, match="unknown skill|not found|no skill"):
        roles.add_role(setup.root, "other", skills=["missing-skill"], scope=[], signals=[])


def test_role_set_keeps_an_empty_body_and_records_why_when_nothing_changes(setup, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    setup.init()
    commits = []
    monkeypatch.setattr(GitRepo, "commit_paths", recording_commit(commits))
    monkeypatch.setattr(GitRepo, "ensure_tracked", lambda self, paths: None)
    monkeypatch.setattr(GitRepo, "remote_slug", lambda self: "owner/repo")
    agent = setup.root / ".claude/agents/fa-architect.md"
    agent.write_text("---\nname: fa-architect\nmemory: project\nskills:\n- fa-architect\n---\n")
    roles.set_role(setup.root, "architect", scope=["src/**"], why="Own the source tree")
    assert agent.read_text().endswith("\n---\n") and "description: Foragent architect role" in agent.read_text(), (
        "Regenerate the frontmatter; an empty body stays empty")
    assert commits == [[".claude/agents/fa-architect.md", ".foragent.yml"]]
    roles.set_role(setup.root, "architect", scope=["src/**"], why="Same settings, new evidence")
    assert len(commits) == 1, "Nothing changed, so nothing is committed"
    events, = (tmp_path / "home").rglob("orch-events.jsonl")
    rows = [json.loads(line) for line in events.read_text().splitlines()]
    assert [(row["kind"], row["why"]) for row in rows] == [
        ("role-set", "Own the source tree"), ("role-set", "Same settings, new evidence")]


def test_role_set_recreates_missing_memory_and_doctor_reports_it(setup, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    setup.init()
    commits = []
    monkeypatch.setattr(GitRepo, "commit_paths", recording_commit(commits))
    monkeypatch.setattr(GitRepo, "ensure_tracked", lambda self, paths: None)
    monkeypatch.setattr(GitRepo, "remote_slug", lambda self: "owner/repo")
    memory = setup.root / ".claude/agent-memory/fa-docs/MEMORY.md"
    memory.unlink()
    memory.parent.rmdir()
    drift = roles.role_drift(setup.root, load_config(setup.root))
    assert len(drift) == 1 and "docs" in drift[0] and "memory" in drift[0]
    roles.set_role(setup.root, "docs", why="Repair the docs memory")
    assert memory.read_text() == "# fa-docs memory\n\n## Proposals\n\n## Lessons\n"
    # The recreated memory equals HEAD's, so only the regenerated agent frontmatter is committed.
    assert commits == [[".claude/agents/fa-docs.md"]]
    assert roles.role_drift(setup.root, load_config(setup.root)) == []
    memory.write_text("")
    roles.set_role(setup.root, "docs", why="Again")
    assert memory.read_text() == "", "An existing memory, even empty, is kept"
    assert commits[1:] == [[".claude/agent-memory/fa-docs/MEMORY.md"]], "The hand edit is committed with the role"


@pytest.mark.parametrize("command", ["add", "set", "remove"])
def test_role_commands_commit_files_deleted_by_hand_before_them(tmp_path, monkeypatch, command):
    _role_command_after_hand_deletion(tmp_path, monkeypatch, command, staged=False)


@pytest.mark.parametrize("command", ["add", "set", "remove"])
def test_role_commands_commit_deletions_already_staged_with_git_rm(tmp_path, monkeypatch, command):
    """A `git rm` deletion is gone from both the worktree and the index; only HEAD still has the path."""
    _role_command_after_hand_deletion(tmp_path, monkeypatch, command, staged=True)


def _role_command_after_hand_deletion(tmp_path, monkeypatch, command, *, staged):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    git_command(tmp_path, "init", "-q", "-b", "main", str(root))
    git_command(root, "remote", "add", "origin", "git@github.com:owner/repo.git")
    owned = [".foragent.yml", ".claude/agents/fa-security.md", ".claude/agent-memory/fa-security"]
    files = {".foragent.yml": "review: fast  # keep\n",
             ".claude/agent-memory/fa-security/old.md": "Stale lesson\n"}
    if command != "add":
        entry = {"agent": "fa-security", "skills": [], "memory": ".claude/agent-memory/fa-security/",
                 "signals": []}
        files[".foragent.yml"] += yaml.safe_dump({"roles": {"security": entry}})
        files[".claude/agents/fa-security.md"] = "---\nname: fa-security\nmemory: project\nskills: []\n---\nBody\n"
        files[".claude/agent-memory/fa-security/MEMORY.md"] = "# fa-security memory\n"
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    git_command(root, "add", "-A")
    base = git_command(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit-tree",
                       git_command(root, "write-tree"), "-m", "base")
    git_command(root, "update-ref", "refs/heads/main", base)
    commits = []
    monkeypatch.setattr(GitRepo, "commit_paths", recording_commit(commits))
    deleted = [".claude/agent-memory/fa-security/old.md"]  # deleted by hand before the command
    if command == "set":
        deleted.append(".claude/agent-memory/fa-security/MEMORY.md")
    if command == "remove":
        deleted += [".claude/agent-memory/fa-security", ".claude/agents/fa-security.md"] if staged else [
            ".claude/agent-memory/fa-security/MEMORY.md"]
    for path in deleted:
        if staged:
            git_command(root, "rm", "-rq", "--", path)
        elif (root / path).exists():
            (root / path).unlink()
    {"add": lambda: roles.add_role(root, "security", skills=[], scope=[], signals=[]),
     "set": lambda: roles.set_role(root, "security", scope=["src/**"]),
     "remove": lambda: roles.remove_role(root, "security")}[command]()
    # The index now holds the owned paths' whole state; the one commit carries exactly what differs from HEAD.
    assert git_command(root, "diff", "--name-only", "--", *owned) == "", "Nothing owned is left unstaged"
    assert git_command(root, "ls-files", "--others", "--", *owned) == "", "Nothing owned is left untracked"
    staged = sorted(git_command(root, "diff", "--name-only", "HEAD~1", "HEAD", "--", *owned).splitlines())
    assert git_command(root, "status", "--porcelain", "--", *owned) == "", "Nothing owned is left uncommitted"
    commit, = commits
    assert [path for path in commit if path in staged] == staged, "One commit with every owned change"
    derived = tuple(f".claude/{kind}/fa-{name}" for name in roles.DEFAULT_ROLES
                    for kind in ("agents", "agent-memory", "skills"))
    assert all(path in staged or path.startswith(derived) for path in commit), (
        "Anything else in the commit is a default role the command added to the source")
    assert ".claude/agent-memory/fa-security/old.md" in staged, "Commit the deletion made by hand"
    memory = ".claude/agent-memory/fa-security/MEMORY.md"
    # set recreates MEMORY.md with HEAD's bytes, so only add and remove change it.
    assert (memory in staged) is (command != "set")
    assert git_command(root, "ls-files", "--", memory) == ("" if command == "remove" else memory)
    if command == "remove":
        assert ".claude/agents/fa-security.md" in staged
        assert git_command(root, "ls-tree", "-r", "--name-only", "HEAD", "--", *owned[1:]) == "", (
            "Removal leaves no agent or memory file in HEAD")
    assert git_command(root, "show", "HEAD:.foragent.yml").startswith("review: fast  # keep\n")


def test_role_commands_keep_every_byte_outside_the_roles_block(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    git_command(tmp_path, "init", "-q", "-b", "main", str(root))
    git_command(root, "remote", "add", "origin", "git@github.com:owner/repo.git")
    head, tail = "# Team settings\nreview: fast  # agreed 10-27\n\n", "# keep the tail\nlanes: {}  # none\n"
    orchestrator = {"agent": "fa-orchestrator", "skills": [], "memory": ".claude/agent-memory/fa-orchestrator/",
                    "signals": []}
    (root / ".foragent.yml").write_text(head + "roles:  # roles note\n  orchestrator:\n    agent: fa-orchestrator\n"
                                        "    skills: []\n    memory: .claude/agent-memory/fa-orchestrator/\n"
                                        "    signals: []\n" + tail)
    assert yaml.safe_load((root / ".foragent.yml").read_text())["roles"] == {"orchestrator": orchestrator}
    git_command(root, "add", "-A")
    base = git_command(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit-tree",
                       git_command(root, "write-tree"), "-m", "base")
    git_command(root, "update-ref", "refs/heads/main", base)
    monkeypatch.setattr(GitRepo, "commit_paths", recording_commit([]))
    steps = [lambda: roles.add_role(root, "security", skills=[], scope=["src/**"], signals=["paths:src/**"]),
             lambda: roles.set_role(root, "security", signals=["kinds:hot_overlap_files"]),
             lambda: roles.remove_role(root, "security")]
    for step in steps:
        step()
        text = (root / ".foragent.yml").read_text()
        assert text.startswith(head) and text.endswith(tail), "Bytes outside the roles block stay identical"
        assert text[len(head):len(text) - len(tail)].startswith("roles:\n")
    final = yaml.safe_load(text)["roles"]
    assert final["orchestrator"] == orchestrator and set(final) == set(roles.DEFAULT_ROLES), (
        "The first role command fills in the missing default entries; remove leaves them")
    assert "dropped 1 comment" in capsys.readouterr().err, "Say once that the block's comment was dropped"


@pytest.mark.parametrize("original", [
    "# keep\r\nreview: fast\r\nchecks: local\r\nroles:\r\n  orchestrator:\r\n    agent: fa-orchestrator\r\n"
    "    skills: [fa-orchestrator]\r\n    memory: .claude/agent-memory/fa-orchestrator/\r\n    signals: []\r\n"
    "# tail\r\n",
    "# keep\nreview: fast\nchecks: local\n\n\n",
], ids=["crlf-missing-defaults", "trailing-blank-lines-no-roles"])
def test_init_adds_roles_without_changing_bytes_outside_the_block(setup, original):
    path = setup.root / PATHS[0]
    path.write_bytes(original.encode())
    setup.init()
    written = path.read_bytes().decode()
    if "roles:" in original:
        source = yaml.safe_load(written)["roles"]
        assert set(roles.DEFAULT_ROLES) <= source.keys()
        head, tail = original[:original.index("roles:")], original[original.index("# tail"):]
        assert written.startswith(head) and written.endswith(tail), "Bytes outside the roles block stay identical"
        assert "\n" not in written.replace("\r\n", ""), "A CRLF file keeps CRLF line ends"
        assert source["orchestrator"]["skills"] == ["fa-orchestrator"]
    else:
        assert written == original, "Re-init leaves a config without a roles key byte-identical"


def test_init_leaves_a_flow_style_config_unchanged_and_warns(setup, capsys):
    """The role agents derive from the roles source, so init refuses instead of creating them without it."""
    path = setup.root / PATHS[0]
    path.write_bytes(b"{review: fast, checks: local, roles: {}}  # one line\n")
    with pytest.raises(ConfigError, match="roles"):
        setup.init()
    assert path.read_bytes() == b"{review: fast, checks: local, roles: {}}  # one line\n"
    assert not (setup.root / ".claude/agents").exists(), "No agent without its roles source"


def test_edit_roles_puts_the_block_inside_a_document_with_an_explicit_end():
    text = "review: fast\nchecks: local\n...\n# after the document\n"
    written = roles.edit_roles(text, {"docs": roles.default_entry("docs")})
    assert written.startswith("review: fast\nchecks: local\nroles:\n")
    assert written.endswith("...\n# after the document\n")
    assert set(yaml.safe_load(written)["roles"]) == {"docs"}


def test_init_copies_the_skills_the_roles_entry_lists(setup):
    setup.init()
    path = setup.root / PATHS[0]
    text = path.read_text()
    source = yaml.safe_load(text)["roles"]
    source["architect"]["skills"] = ["audit"]
    path.write_text(roles.edit_roles(text, source))
    for relative in (".claude/agents/fa-architect.md", ".claude/skills/fa-architect/SKILL.md"):
        (setup.root / relative).unlink()
    product = setup.root / "skills/audit"
    product.mkdir(parents=True)
    (product / "SKILL.md").write_text("---\nname: audit\ndescription: Audit\n---\nAudit the code.\n")
    report = setup.init()
    assert (setup.root / ".claude/skills/audit/SKILL.md").read_text() == (product / "SKILL.md").read_text()
    assert ".claude/skills/audit/SKILL.md" in report.created
    assert not (setup.root / ".claude/skills/fa-architect/SKILL.md").exists(), "Copy the listed skill, not the role's"
    assert roles.role_drift(setup.root, load_config(setup.root)) == []


PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe"


def test_init_copies_role_skill_files_as_bytes(setup):
    setup.init()
    path = setup.root / PATHS[0]
    text = path.read_text()
    source = yaml.safe_load(text)["roles"]
    source["architect"]["skills"] = ["audit"]
    path.write_text(roles.edit_roles(text, source))
    product = setup.root / "skills/audit"
    (product / "assets").mkdir(parents=True)
    (product / "SKILL.md").write_bytes(b"---\r\nname: audit\r\ndescription: Audit\r\n---\r\nCRLF body\r\n")
    (product / "assets/diagram.png").write_bytes(PNG)
    setup.init()
    copy = setup.root / ".claude/skills/audit"
    assert (copy / "assets/diagram.png").read_bytes() == PNG, "Binary skill files are copied byte for byte"
    assert (copy / "SKILL.md").read_bytes() == (product / "SKILL.md").read_bytes(), "CRLF text stays CRLF"
    assert set(role_agents.setup_paths(setup.root, {"fa-architect": ["audit"]})) >= {
        ".claude/skills/audit/SKILL.md", ".claude/skills/audit/assets/diagram.png"}, "Recovery compares bytes too"


def test_role_set_keeps_crlf_agent_body_bytes(setup, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    setup.init()
    monkeypatch.setattr(GitRepo, "commit_paths", recording_commit([]))
    monkeypatch.setattr(GitRepo, "remote_slug", lambda self: "owner/repo")
    agent = setup.root / ".claude/agents/fa-architect.md"
    body = b"Line one\r\nLine two\r\n\r\nLast line without end"
    agent.write_bytes(b"---\r\nname: fa-architect\r\nmemory: project\r\nskills:\r\n- fa-architect\r\n---\r\n" + body)
    roles.set_role(setup.root, "architect", scope=["src/**"])
    written = agent.read_bytes()
    assert written.endswith(b"---\r\n" + body), "The body keeps its exact bytes, CRLF included"
    assert b"description: Foragent architect role\r\n" in written and b"\n" not in written.replace(b"\r\n", b"")


def test_init_reports_dropped_roles_comments_once_with_board_settings(setup, capsys):
    setup.init()
    path = setup.root / PATHS[0]
    path.write_text("review: fast\nroles:  # team roles\n  orchestrator:\n    agent: fa-orchestrator\n"
                    "    skills: [fa-orchestrator]\n    memory: .claude/agent-memory/fa-orchestrator/\n"
                    "    signals: []\n")
    capsys.readouterr()
    report = setup.init(no_board=False)
    assert "board" in report.added_keys and "roles" in report.added_keys
    notices = [line for line in capsys.readouterr().err.splitlines() if "dropped" in line]
    assert notices == ["Note: rewrote the roles block of .foragent.yml and dropped 1 comment(s) inside it."]


@pytest.mark.parametrize("command", ["add", "set", "remove"])
def test_role_commands_derive_the_default_roles_they_add_to_a_legacy_config(tmp_path, monkeypatch, command):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    git_command(tmp_path, "init", "-q", "-b", "main", str(root))
    git_command(root, "remote", "add", "origin", "git@github.com:owner/repo.git")
    files = {".foragent.yml": "review: fast\n"}  # a config from before roles existed
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    git_command(root, "add", "-A")
    base = git_command(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit-tree",
                       git_command(root, "write-tree"), "-m", "base")
    git_command(root, "update-ref", "refs/heads/main", base)
    commits = []
    monkeypatch.setattr(GitRepo, "commit_paths", recording_commit(commits))
    if command == "add":
        roles.add_role(root, "security", skills=[], scope=[], signals=[])
    else:
        # set and remove need a security entry; the first command writes the block with the defaults.
        roles.add_role(root, "audit", skills=[], scope=[], signals=[])
        commits.clear()
        roles.set_role(root, "audit", scope=["src/**"]) if command == "set" else roles.remove_role(root, "audit")
    declared = yaml.safe_load((root / ".foragent.yml").read_text())["roles"]
    assert set(roles.DEFAULT_ROLES) <= declared.keys()
    assert roles.role_drift(root, load_config(root)) == [], "Every declared role has its agent and memory"
    head = git_command(root, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    for name in roles.DEFAULT_ROLES:
        assert f".claude/agents/fa-{name}.md" in head and f".claude/agent-memory/fa-{name}/MEMORY.md" in head
    assert git_command(root, "status", "--porcelain", "--", ".claude", ".foragent.yml") == ""


def _fa_allow_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    (root / ".claude").mkdir(parents=True)
    # Existing settings use the direct CLI offer; new projects use the skill's question.
    (root / ".claude/settings.local.json").write_text("{}\n", encoding="utf-8")
    return root


def test_fa_allow_existing_rule_counts_as_accepted_without_asking(tmp_path, monkeypatch):
    from foragent.init import fa_allow_record_path, offer_fa_allow

    root = _fa_allow_env(tmp_path, monkeypatch)
    settings = root / ".claude/settings.json"
    settings.write_text('{"permissions": {"allow": ["Bash(fa:*)"]}}\n', encoding="utf-8")
    before = settings.read_bytes()

    def never(prompt):
        raise AssertionError("must not ask")

    assert offer_fa_allow(root, never) == "present"
    assert (root / ".claude/settings.local.json").read_text() == "{}\n"
    assert settings.read_bytes() == before
    assert json.loads(fa_allow_record_path().read_text()) == {str(root.resolve()): "accepted"}
    assert offer_fa_allow(root, never) == "kept"


def test_fa_allow_without_terminal_remembers_nothing(tmp_path, monkeypatch):
    from foragent.init import fa_allow_record_path, offer_fa_allow

    root = _fa_allow_env(tmp_path, monkeypatch)

    def no_terminal(prompt):
        raise EOFError

    assert offer_fa_allow(root, no_terminal) == "unanswered"
    assert not fa_allow_record_path().exists()
    assert offer_fa_allow(root, lambda prompt: "y") == "added"
    local = json.loads((root / ".claude/settings.local.json").read_text())
    assert local == {"permissions": {"allow": ["Bash(fa:*)"]}}


def test_fa_allow_unreadable_local_settings_is_left_alone(tmp_path, monkeypatch):
    from foragent.init import fa_allow_record_path, offer_fa_allow

    root = _fa_allow_env(tmp_path, monkeypatch)
    local = root / ".claude/settings.local.json"
    local.write_text("[not an object", encoding="utf-8")
    assert offer_fa_allow(root, lambda prompt: "yes") == "unreadable"
    assert local.read_text() == "[not an object"
    assert not fa_allow_record_path().exists()


def test_fa_allow_keeps_settings_edited_while_the_prompt_waits(tmp_path, monkeypatch):
    from foragent.init import offer_fa_allow

    root = _fa_allow_env(tmp_path, monkeypatch)
    local = root / ".claude/settings.local.json"
    local.write_text('{"permissions": {"allow": ["Read(a/**)"]}}\n', encoding="utf-8")

    def edit_then_accept(prompt):
        local.write_text('{"permissions": {"allow": ["Read(a/**)"], "deny": ["Read(.env)"]}}\n',
                         encoding="utf-8")
        return "yes"

    assert offer_fa_allow(root, edit_then_accept) == "added"
    assert json.loads(local.read_text()) == {
        "permissions": {"allow": ["Read(a/**)", "Bash(fa:*)"], "deny": ["Read(.env)"]}}


def test_fa_allow_keeps_other_projects_answers_recorded_while_the_prompt_waits(tmp_path, monkeypatch):
    from foragent.init import fa_allow_record_path, offer_fa_allow

    root = _fa_allow_env(tmp_path, monkeypatch)
    other = tmp_path / "other"
    (other / ".claude").mkdir(parents=True)
    (other / ".claude/settings.json").write_text("{}\n", encoding="utf-8")

    def other_declines_first(prompt):
        assert offer_fa_allow(other, lambda _prompt: "no") == "declined"
        return "no"

    assert offer_fa_allow(root, other_declines_first) == "declined"
    assert json.loads(fa_allow_record_path().read_text()) == {
        str(other.resolve()): "declined", str(root.resolve()): "declined"}
    assert offer_fa_allow(other, lambda prompt: pytest.fail("asked again")) == "kept"


def test_fa_allow_never_overwrites_an_unreadable_record(tmp_path, monkeypatch):
    from foragent.init import fa_allow_record_path, offer_fa_allow

    root = _fa_allow_env(tmp_path, monkeypatch)
    record = fa_allow_record_path()
    record.parent.mkdir(parents=True)
    record.write_text("{broken", encoding="utf-8")
    with pytest.raises(OSError):
        offer_fa_allow(root, lambda prompt: "no")
    assert record.read_text() == "{broken"


def test_fa_allow_concurrent_init_of_the_same_project_does_not_ask_twice(tmp_path, monkeypatch):
    from foragent.init import fa_allow_record_path, offer_fa_allow

    root = _fa_allow_env(tmp_path, monkeypatch)
    nested = []

    def second_init_runs_while_first_waits(prompt):
        nested.append(offer_fa_allow(root, lambda _prompt: pytest.fail("asked twice")))
        return "no"

    assert offer_fa_allow(root, second_init_runs_while_first_waits) == "declined"
    assert nested == ["busy"]
    assert json.loads(fa_allow_record_path().read_text()) == {str(root.resolve()): "declined"}
    assert offer_fa_allow(root, lambda prompt: pytest.fail("asked again")) == "kept"


def test_fa_allow_rule_added_by_hand_during_the_prompt_is_not_added_twice(tmp_path, monkeypatch):
    from foragent.init import offer_fa_allow

    root = _fa_allow_env(tmp_path, monkeypatch)
    local = root / ".claude/settings.local.json"

    def add_by_hand_then_accept(prompt):
        local.write_text('{"permissions": {"allow": ["Bash(fa:*)"]}}\n', encoding="utf-8")
        return "yes"

    assert offer_fa_allow(root, add_by_hand_then_accept) == "present"
    assert json.loads(local.read_text()) == {"permissions": {"allow": ["Bash(fa:*)"]}}


def test_fa_allow_asks_nothing_in_a_project_without_claude_settings(tmp_path, monkeypatch):
    from foragent.init import fa_allow_record_path, offer_fa_allow

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    (root / ".claude/agents").mkdir(parents=True)
    for _ in range(2):
        assert offer_fa_allow(root, lambda prompt: pytest.fail("asked")) == "skipped"
    assert sorted(path.name for path in (root / ".claude").iterdir()) == ["agents"]
    assert not fa_allow_record_path().parent.exists(), "a skipped offer must not create state or locks"


@pytest.mark.parametrize("answer", ["yes", "no"])
def test_fa_allow_explicit_answer_works_without_existing_claude_settings(tmp_path, monkeypatch, answer):
    from foragent.init import fa_allow_record_path, offer_fa_allow

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    assert offer_fa_allow(root, lambda prompt: answer, require_settings=False) == (
        "added" if answer == "yes" else "declined")
    if answer == "yes":
        assert json.loads((root / ".claude/settings.local.json").read_text()) == {
            "permissions": {"allow": ["Bash(fa:*)"]}}
    else:
        assert not (root / ".claude").exists()
    assert json.loads(fa_allow_record_path().read_text()) == {
        str(root.resolve()): "accepted" if answer == "yes" else "declined"}
    assert offer_fa_allow(root, lambda prompt: pytest.fail("asked again")) == "kept"


@pytest.mark.parametrize("answer", [None, "yes", "no"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_init_new_project_reports_undecided_until_person_answers(setup, monkeypatch, capsys, answer, dry_run):
    from foragent import cli
    from foragent.commands import init as command
    from foragent.init import fa_allow_record_path

    monkeypatch.setattr(cli, "init_repo", lambda root, **kwargs: setup.init(**kwargs))
    monkeypatch.setattr(cli, "_offer_statusline", lambda answer: None)
    monkeypatch.setattr(command, "_offer_upstream_reports", lambda root: None)
    monkeypatch.setattr(command, "_offer_starter", lambda *args: None)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("new project must use the skill's question"))
    argv = ["init", "--repo-root", str(setup.root), "--no-push", "--no-launchd", "--no-board"]
    if answer is not None:
        argv += ["--fa-allow", answer]
    if dry_run:
        argv += ["--dry-run"]
    note = "FA commands permission: not yet decided — /fa setup asks, or run `fa init --fa-allow yes|no`."
    local = setup.root / ".claude/settings.local.json"
    for _ in range(2):
        assert cli.main(argv) == 0
        output = capsys.readouterr().out
        assert (note in output) == (answer is None and not dry_run)
        if dry_run or answer is None:
            assert not local.exists()
            assert not fa_allow_record_path().parent.exists()
        else:
            assert json.loads(fa_allow_record_path().read_text()) == {
                str(setup.root.resolve()): "accepted" if answer == "yes" else "declined"}
            if answer == "yes":
                assert json.loads(local.read_text()) == {"permissions": {"allow": ["Bash(fa:*)"]}}
            else:
                assert not local.exists()
            assert "not yet decided" not in setup.init(no_push=True, no_launchd=True).summary()


def test_init_existing_claude_settings_leave_the_offer_to_cli(setup):
    local = setup.root / ".claude/settings.local.json"
    local.parent.mkdir()
    local.write_text("{}\n")
    assert "not yet decided" not in setup.init(no_push=True, no_launchd=True).summary()


@pytest.mark.parametrize("answer", ["yes", "no"])
@pytest.mark.parametrize("root_kind", ["relative", "symlink"])
def test_fa_allow_summary_reuses_decision_for_git_root_alias(setup, tmp_path, monkeypatch, answer, root_kind):
    from foragent.init import fa_allow_record_path, offer_fa_allow

    root = setup.root.resolve()
    monkeypatch.chdir(tmp_path)
    if root_kind == "relative":
        alias = Path("project")
    else:
        alias = tmp_path / "project-link"
        alias.symlink_to(root, target_is_directory=True)

    def never(prompt):
        pytest.fail("the same project's decision must not be requested again")

    def respond(prompt):
        # Alias and canonical paths must share the lock while the first question waits.
        assert offer_fa_allow(root, never, require_settings=False) == "busy"
        return answer

    assert offer_fa_allow(alias, respond, require_settings=False) == ("added" if answer == "yes" else "declined")
    expected = {str(root): "accepted" if answer == "yes" else "declined"}
    assert json.loads(fa_allow_record_path().read_text()) == expected
    local = root / ".claude/settings.local.json"
    # Remembering consent still matters if the person later removes the settings file.
    if answer == "yes":
        local.unlink()

    # The default adapter resolves its root; injected adapters can return an alias.
    monkeypatch.setattr(GitRepo, "worktree_root", lambda self: alias)
    report = init_repo(alias, git=GitRepo(alias, setup.run_git), gh_run=setup.run_gh,
                       tracker=setup.tracker, no_push=True, no_launchd=True, no_board=True)
    assert "FA commands permission: not yet decided" not in report.summary()
    assert not local.exists(), "re-init must not reinstall a removed grant"
    assert offer_fa_allow(alias, never) == offer_fa_allow(root, never) == "kept"
    assert json.loads(fa_allow_record_path().read_text()) == expected
