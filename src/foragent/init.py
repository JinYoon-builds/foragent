"""Idempotent repository setup; all external commands can be injected."""

from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
import json
import logging
from pathlib import Path
import subprocess

import yaml

from foragent import board, gh, launchd
from foragent.ci_workflow import LEGACY_PUBLISH_WORKFLOW, WORKFLOW, initial_checks, obsolete_workflow_paths, workflow_text, write_workflow
from foragent.config import DEFAULTS, Config, ConfigError, _read_yaml, _validate, load_config
from foragent.git import GitError, GitRepo
from foragent.labels import LABELS
from foragent.order import detect_test_commands
from foragent.tracker import GhTracker, Tracker, TrackerError


CONFIG_COMMENT = "# Configuration: Foragent docs/gate.md (section 9), docs/contract.md (section 4).\n"
ISSUE_TEMPLATE = """---
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
SETUP_PATHS = [".foragent.yml", ".github/ISSUE_TEMPLATE/foragent-task.md", "tests/acceptance/.gitkeep"]


def _parse_board_map(value: str) -> dict[str, str]:
    mapping = {}
    for pair in value.split(","):
        group, separator, column = pair.partition("=")
        group, column = group.strip(), column.strip()
        if not separator or group not in board.DEFAULT_MAP or not column or group in mapping:
            raise ConfigError("--board-map requires Group=Column pairs for the five board groups")
        mapping[group] = column
    if mapping.keys() != board.DEFAULT_MAP.keys():
        raise ConfigError("--board-map must include Backlog, Up next, In progress, Your turn, Done")
    return mapping


def _choose_board_map(columns: list[str]) -> dict[str, str]:
    print("Map Foragent groups to existing team columns (saved in .foragent.yml):")
    for number, column in enumerate(columns, 1):
        print(f"  {number}. {column}")
    mapping = {}
    for group in board.DEFAULT_MAP:
        while True:
            try:
                answer = input(f"{group} — column name or number: ").strip()
            except (EOFError, OSError) as exc:
                raise ConfigError("Existing board needs a mapping; retry init with --board-map Group=Column,...") from exc
            if answer.isdecimal() and 1 <= int(answer) <= len(columns):
                answer = columns[int(answer) - 1]
            if answer in columns:
                mapping[group] = answer
                break
            print("Choose one of the listed columns.")
    return mapping


@dataclass(frozen=True)
class InitReport:
    created: list[str]
    existed: list[str]
    dry_run: bool
    committed: bool = False
    pushed: bool = False
    launchd_label: str | None = None
    launchd_planned: bool = False
    push_planned: bool = False
    commit_planned: bool = False
    normalized_root: Path | None = None
    added_keys: list[str] = field(default_factory=list)
    board_id: str | None = None
    board_planned: bool = False
    removed: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = ["Foragent setup plan:" if self.dry_run else "Foragent setup:"]
        if self.normalized_root is not None:
            lines.append(f"Using repository root: {self.normalized_root}")
        lines.append("Would ensure labels." if self.dry_run else "Labels ensured.")
        lines.extend(f"{'Would create' if self.dry_run else 'Created'}: {path}" for path in self.created)
        lines.extend(f"{'Would remove' if self.dry_run else 'Removed'}: {path}" for path in self.removed)
        lines.extend(f"Already exists: {path}" for path in self.existed)
        lines.extend(f"{'Would add' if self.dry_run else 'Added'} config key: {key}" for key in self.added_keys)
        if self.board_planned:
            lines.append("Would find or create the GitHub Projects board.")
        elif self.board_id:
            lines.append(f"GitHub Projects board: {self.board_id}")
        if self.committed or self.commit_planned:
            lines.append("Would commit: Set up Foragent" if self.dry_run else "Committed: Set up Foragent")
        if self.pushed or self.push_planned:
            lines.append("Would push to origin." if self.dry_run else "Pushed to origin.")
        if self.launchd_planned:
            lines.append("Would install launchd job.")
        elif self.launchd_label:
            lines.append(f"launchd installed: {self.launchd_label}")
        return "\n".join(lines)


def _run_gh(args: list[str], input: str | None = None) -> str:
    return gh.run(args, input=input, check=True, capture_output=True, text=True).stdout


def _gh_call(run: Callable[..., str], args: list[str]) -> str:
    try:
        return run(args, input=None).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        message = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise TrackerError(f"gh {' '.join(args)} failed: {message or exc}") from exc


def _add_config(path: Path, local: dict, added: dict) -> str:
    original = path.read_text(encoding="utf-8")
    node = yaml.compose(original)
    closed = any(isinstance(event, yaml.events.DocumentEndEvent) and event.explicit
                 for event in yaml.parse(original))
    if (added.keys() & local.keys() or not isinstance(node, yaml.MappingNode) or node.flow_style or closed
            or (node.value and node.value[0][0].start_mark.column)):
        return yaml.safe_dump({**local, **added}, sort_keys=False)
    return original.rstrip("\n") + "\n" + yaml.safe_dump(added, sort_keys=False)


def _logged_in(tool: str) -> bool:
    command = ["codex", "login", "status"] if tool == "codex" else ["claude", "auth", "status"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    if tool == "codex":
        return True
    try:
        status = json.loads(result.stdout)
    except ValueError:
        return False
    return isinstance(status, dict) and status.get("loggedIn") is True


def init_repo(
    repo_root: Path, *, approvers: list[str] | None = None, review: str | None = None,
    executor: str | None = None, run_hours: str | None = None, language: str | None = None,
    no_push: bool = False,
    no_launchd: bool = False, no_board: bool = False, dry_run: bool = False, git: GitRepo | None = None,
    board_map: str | None = None, claude_quota: str | None = None,
    gh_run: Callable[..., str] | None = None, tracker: Tracker | None = None,
    install_launchd: Callable[[Path], str] | None = None,
) -> InitReport:
    requested_map = _parse_board_map(board_map) if board_map is not None else None
    root = Path(repo_root).resolve()
    git = git or GitRepo(root)
    git.check_worktree()
    top_level = git.worktree_root()
    normalized_root = top_level if top_level != root else None
    if normalized_root is not None:
        root = top_level
        git = git.at(root)
    try:
        repo = git.remote_slug()
    except GitError as exc:
        raise GitError(f"init requires an origin remote on github.com: {exc}") from exc
    run = gh_run or _run_gh
    _gh_call(run, ["auth", "status"])

    config_path = root / ".foragent.yml"
    contents = {".github/ISSUE_TEMPLATE/foragent-task.md": ISSUE_TEMPLATE}
    base_branch = DEFAULTS["base_branch"]
    added = {}
    updated_config = None
    if config_path.exists():
        config = load_config(root)
        base_branch = config.base_branch
        local = _read_yaml(config_path)
        explicit = {"approvers": approvers, "review": review, "executor": executor,
                    "run_hours": run_hours, "language": language}
        added = {key: value for key, value in explicit.items() if value is not None and key not in local}
        if claude_quota is not None:  # Sets the mode and keeps the block's other keys.
            block = local.get("claude_quota")
            added["claude_quota"] = {**(block if isinstance(block, dict) else {}), "mode": claude_quota}
        if "checks" not in local:
            added["checks"] = initial_checks(repo, run, command=config.test_command or next(iter(detect_test_commands(root)), None))
        if added:
            full = {**added, **({"claude_quota": {**config.claude_quota, **added["claude_quota"]}}
                                if "claude_quota" in added else {})}
            _validate({**asdict(config), **full})
            config = replace(config, **full)
            updated_config = _add_config(config_path, local, added)
    else:
        codex_available = _logged_in("codex")
        claude_available = _logged_in("claude")
        if not codex_available and not claude_available:
            raise ConfigError("No logged-in coding tool found; log in to Claude Code or Codex and retry.")
        if executor is None and not codex_available:
            executor = "claude"
        if approvers is None:
            login = _gh_call(run, ["api", "user", "--jq", ".login"])
            if not login:
                raise TrackerError("gh api user returned an empty authenticated login")
            approvers = [login]
        options = {"review": review, "executor": executor, "run_hours": run_hours}
        if claude_available and executor == "claude":
            # Same-tool review still starts a separate Claude process/session.
            options["reviewer"] = "claude"
        if language is not None:
            options["language"] = language
        values = {key: DEFAULTS[key] if value is None else value for key, value in options.items()}
        values["approvers"] = approvers
        values["checks"] = initial_checks(repo, run, command=next(iter(detect_test_commands(root)), None))
        _validate({**DEFAULTS, **values})
        config = Config(**{**DEFAULTS, **values})
        minimal = {key: value for key, value in values.items()
                   if key in {"review", "approvers", "checks"} or options[key] is not None or value != DEFAULTS[key]}
        if claude_quota is not None:
            minimal["claude_quota"] = {"mode": claude_quota}
            _validate({**asdict(config), "claude_quota": {**config.claude_quota, "mode": claude_quota}})
            config = replace(config, claude_quota={**config.claude_quota, "mode": claude_quota})
        contents = {".foragent.yml": CONFIG_COMMENT + yaml.safe_dump(minimal, sort_keys=False), **contents}
    if config.checks == "ci":
        contents[WORKFLOW] = workflow_text(config)
    if not (root / "tests/acceptance").exists():
        contents["tests/acceptance/.gitkeep"] = ""

    created = [path for path in contents if not (root / path).exists()]
    existed = [path for path in contents if (root / path).exists()]
    if config_path.exists():
        existed.insert(0, ".foragent.yml")
    if (root / "tests/acceptance").exists():
        existed.append("tests/acceptance/")
    removed = obsolete_workflow_paths(root)
    # A staged deletion is absent from the index but still belongs to HEAD.
    tracked_removals = set()
    if removed:
        for command in (["ls-files", "--cached", "-z", "--", *removed],
                        ["ls-tree", "-r", "--name-only", "-z", "HEAD", "--", *removed]):
            tracked_removals.update(git._call(command).split("\0"))
    tracked_removals = [path for path in removed if path in tracked_removals]
    # Also recover a deletion left uncommitted by an interrupted setup.
    legacy_paths = [LEGACY_PUBLISH_WORKFLOW] if not (root / LEGACY_PUBLISH_WORKFLOW).exists() else []
    pending = list(dict.fromkeys([
        *created, *tracked_removals, *([".foragent.yml"] if added else []),
        *git.uncommitted_paths([*SETUP_PATHS, *legacy_paths, *([WORKFLOW] if config.checks == "ci" else [])]),
    ]))
    board_planned = not no_board
    if (pending or (board_planned and not config.board)) and git.current_branch() != base_branch:
        raise GitError(f"init must run on the base branch {base_branch!r}; switch to it and re-run")
    push_planned = not no_push and (bool(pending) or git.has_unpushed_setup(base_branch))

    if dry_run:
        board_change = board_planned and not config.board
        return InitReport(created, existed, True, push_planned=push_planned or (board_change and not no_push),
                          commit_planned=bool(pending) or board_change, launchd_planned=not no_launchd,
                          normalized_root=normalized_root, added_keys=list(added), board_planned=board_planned,
                          removed=removed)

    # GhTracker counts its own attempts; its default runner must not count them again.
    tracker = tracker or GhTracker(repo, run=gh_run)
    tracker.ensure_labels(LABELS)
    board_id = config.board
    if not no_board:
        created_boards: set[str] = set()
        board_id = board.ensure(tracker, repo, config.lanes, config.board, created=created_boards)
        board_changes = {}
        if board_id and board_id != config.board:
            board_changes["board"] = board_id
        if board_id and (requested_map is not None or config.board_map is None):
            try:
                status = board.Project(tracker, repo, board_id).fields().get("Status", {})
                columns = [option["name"] for option in status.get("options", [])]
            except Exception:
                logging.getLogger(__name__).warning("Could not read board columns for %s", repo, exc_info=True)
            else:
                mapping = requested_map
                if mapping is None and columns == list(board.COLUMNS):
                    # Foragent's own five columns have exactly one mapping. A board
                    # created now or already saved uses it by default; an existing
                    # board adopted now records it without asking, once.
                    if board_id != config.board and board_id not in created_boards:
                        mapping = dict(board.DEFAULT_MAP)
                elif mapping is None and columns:
                    mapping = _choose_board_map(columns)
                if mapping is not None:
                    missing = set(mapping.values()) - set(columns)
                    if missing:
                        raise ConfigError(f"board_map refers to unavailable columns: {', '.join(sorted(missing))}")
                    if mapping != config.board_map:
                        board_changes["board_map"] = mapping
        if board_changes:
            if config_path.exists():
                added.update(board_changes)
                updated_config = _add_config(config_path, local, added)
            else:
                contents[".foragent.yml"] += yaml.safe_dump(board_changes, sort_keys=False, allow_unicode=True)
            pending = list(dict.fromkeys([*pending, ".foragent.yml"]))
            push_planned = not no_push
    for path in created:
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if path != WORKFLOW:
            with destination.open("x", encoding="utf-8") as stream:
                stream.write(contents[path])
    for path in removed:
        (root / path).unlink()
    if WORKFLOW in created:
        write_workflow(root, config, repo=repo, run=run, missing_only=True)
    if updated_config is not None:
        config_path.write_text(updated_config, encoding="utf-8")
    if pending:
        git.commit_paths(pending, "Set up Foragent")
    if push_planned:
        git.push_base(base_branch)
    label = None if no_launchd else (install_launchd or launchd.install)(root)
    return InitReport(created, existed, False, bool(pending), push_planned, label,
                      normalized_root=normalized_root, added_keys=list(added), board_id=board_id, removed=removed)
