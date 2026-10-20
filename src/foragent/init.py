"""Idempotent repository setup; all external commands can be injected."""

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
import subprocess

import yaml

from foragent import launchd
from foragent.config import DEFAULTS, ConfigError, _read_yaml, _validate, load_config
from foragent.git import GitError, GitRepo
from foragent.labels import LABELS
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

    def summary(self) -> str:
        lines = ["Foragent setup plan:" if self.dry_run else "Foragent setup:"]
        if self.normalized_root is not None:
            lines.append(f"Using repository root: {self.normalized_root}")
        lines.append("Would ensure labels." if self.dry_run else "Labels ensured.")
        lines.extend(f"{'Would create' if self.dry_run else 'Created'}: {path}" for path in self.created)
        lines.extend(f"Already exists: {path}" for path in self.existed)
        lines.extend(f"{'Would add' if self.dry_run else 'Added'} config key: {key}" for key in self.added_keys)
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
    return subprocess.run(["gh", *args], input=input, check=True, capture_output=True, text=True).stdout


def _gh_call(run: Callable[..., str], args: list[str]) -> str:
    try:
        return run(args, input=None).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        message = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise TrackerError(f"gh {' '.join(args)} failed: {message or exc}") from exc


def init_repo(
    repo_root: Path, *, approvers: list[str] | None = None, review: str | None = None,
    executor: str | None = None, run_hours: str | None = None, language: str | None = None,
    no_push: bool = False,
    no_launchd: bool = False, dry_run: bool = False, git: GitRepo | None = None,
    gh_run: Callable[..., str] | None = None, tracker: Tracker | None = None,
    install_launchd: Callable[[Path], str] | None = None,
) -> InitReport:
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
        if added:
            _validate({**asdict(config), **added})
            original = config_path.read_text(encoding="utf-8")
            node = yaml.compose(original)
            closed = any(isinstance(event, yaml.events.DocumentEndEvent) and event.explicit
                         for event in yaml.parse(original))
            if (not isinstance(node, yaml.MappingNode) or node.flow_style or closed
                    or (node.value and node.value[0][0].start_mark.column)):
                updated_config = yaml.safe_dump({**local, **added}, sort_keys=False)
            else:
                updated_config = original.rstrip("\n") + "\n" + yaml.safe_dump(added, sort_keys=False)
    else:
        if approvers is None:
            login = _gh_call(run, ["api", "user", "--jq", ".login"])
            if not login:
                raise TrackerError("gh api user returned an empty authenticated login")
            approvers = [login]
        options = {"review": review, "executor": executor, "run_hours": run_hours}
        if language is not None:
            options["language"] = language
        values = {key: DEFAULTS[key] if value is None else value for key, value in options.items()}
        values["approvers"] = approvers
        _validate({**DEFAULTS, **values})
        minimal = {key: value for key, value in values.items()
                   if key in {"review", "approvers"} or options[key] is not None or value != DEFAULTS[key]}
        contents = {".foragent.yml": CONFIG_COMMENT + yaml.safe_dump(minimal, sort_keys=False), **contents}
    if not (root / "tests/acceptance").exists():
        contents["tests/acceptance/.gitkeep"] = ""

    created = [path for path in contents if not (root / path).exists()]
    existed = [path for path in contents if (root / path).exists()]
    if config_path.exists():
        existed.insert(0, ".foragent.yml")
    if (root / "tests/acceptance").exists():
        existed.append("tests/acceptance/")
    pending = list(dict.fromkeys([
        *created, *([".foragent.yml"] if added else []), *git.uncommitted_paths(SETUP_PATHS),
    ]))
    if pending and git.current_branch() != base_branch:
        raise GitError(f"init must run on the base branch {base_branch!r}; switch to it and re-run")
    push_planned = not no_push and (bool(pending) or git.has_unpushed_setup(base_branch))

    if dry_run:
        return InitReport(created, existed, True, push_planned=push_planned,
                          commit_planned=bool(pending), launchd_planned=not no_launchd,
                          normalized_root=normalized_root, added_keys=list(added))

    (tracker or GhTracker(repo, run=run)).ensure_labels(LABELS)
    for path in created:
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(contents[path])
    if updated_config is not None:
        config_path.write_text(updated_config, encoding="utf-8")
    if pending:
        git.commit_paths(pending, "Set up Foragent")
    if push_planned:
        git.push_base(base_branch)
    label = None if no_launchd else (install_launchd or launchd.install)(root)
    return InitReport(created, existed, False, bool(pending), push_planned, label,
                      normalized_root=normalized_root, added_keys=list(added))
