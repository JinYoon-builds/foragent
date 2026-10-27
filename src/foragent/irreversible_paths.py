"""Deploy-on-merge workflows and migration directories wait for a person at first touch.

`detect` reads repository files only. `register` runs before a gate merge decision: when the
PR touches a detected path that was never announced, it appends the glob to `always_human`
through `config.add_always_human` (the key's only writer), records one event whose `say`
line is the notice, and makes this run's config hold the merge even if the write failed.
The orchestrator commits the changed `.foragent.yml` in its next batched commit.
"""

from __future__ import annotations

from dataclasses import replace
from glob import escape
import json
import os
from pathlib import Path
import re
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

import yaml

from foragent import config as config_module
from foragent.orch_events import append_event
from foragent.policy import touched

if TYPE_CHECKING:
    from foragent.git import GitRepo
    from foragent.runner import RunContext

EVENT_KIND = "irreversible-path"
DEPLOY_WORDS = ("npm publish", "gh release", "twine upload", "docker push", "kubectl apply",
                "terraform apply", "fly deploy", "vercel --prod", "wrangler deploy", "deploy")
NAME_WORDS = ("deploy", "release", "publish")
MIGRATION_DIRS = ("migrations", "alembic/versions", "prisma/migrations", "db/migrate")
_NOT_MIGRATIONS = ("docs", "tests")
_RUN_WORDS = re.compile("|".join(rf"(?<![\w-]){re.escape(word)}(?![\w-])" for word in DEPLOY_WORDS), re.I)
# Action names split words on `-`, `_`, `/` and `.`: `release-action` is the word release, `deployment` is not deploy.
_ACTION_WORDS = re.compile("|".join(rf"(?<![a-z0-9]){word}(?![a-z0-9])" for word in (*NAME_WORDS, *DEPLOY_WORDS)), re.I)


def detect(root: Path | str, *, base_branch: str) -> list[str]:
    """Return sorted path globs from files on disk: deploy workflow files and `<migration dir>/**`."""
    root = Path(root)
    files = []
    for directory, names, found in os.walk(root):
        names[:] = [name for name in names if name != ".git"]
        files += [(Path(directory) / name).relative_to(root).as_posix() for name in found]
    return _detect(files, lambda path: _read(root / path), base_branch)


def detect_at(git: GitRepo, ref: str, *, base_branch: str) -> list[str]:
    """`detect` over the files tracked at a Git ref."""
    return _detect(git.tree_files(ref), lambda path: git.file_at(ref, path), base_branch)


def register(ctx: RunContext, n: int, git: GitRepo, changed_paths: list[str]) -> None:
    """Announce and hold detected paths this PR touches for the first time.

    Reads the PR head (the worktree) and the configured base, so a PR that adds or removes
    a deploy workflow or a migration directory is caught either way.
    """
    base = ctx.config.base_branch
    if hasattr(git, "tree_files"):
        detected = set(detect_at(git, "HEAD", base_branch=base)) | set(
            detect_at(git, f"origin/{base}", base_branch=base))
    else:  # Test doubles without Git trees: the worktree and the project checkout on disk.
        detected = set(detect(git.worktree_root(), base_branch=base)) | set(detect(ctx.repo_root, base_branch=base))
    candidates = [glob for glob in sorted(detected)
                  if glob not in ctx.config.always_human and touched(changed_paths, [glob])]
    if not candidates:
        return
    # One lock around read-announcements, write and announce: concurrent first touches announce once.
    with ctx.store._state_lock():
        announced = _announced(ctx)
        for glob in candidates:
            if glob in announced:
                # Announced before: follow the file as it is now (another run's write, or a person's removal).
                try:
                    current = config_module.load_config(ctx.repo_root).always_human
                except (config_module.ConfigError, OSError):
                    current = []
                if glob in current:
                    ctx.config = replace(ctx.config, always_human=[*ctx.config.always_human, glob])
                continue
            try:
                config_module.add_always_human(ctx.repo_root, glob)
                written = "added it to always_human in .foragent.yml; commit that file in the next batched commit"
            except (config_module.ConfigError, OSError) as exc:
                written = f"could not write .foragent.yml ({exc}); this merge is held anyway"
            ctx.config = replace(ctx.config, always_human=[*ctx.config.always_human, glob])
            append_event(ctx.store, ctx.now(), n, EVENT_KIND,
                         f"PR touches {glob}, a deploy or migration path; {written}.",
                         say=f"Merges touching {glob} now wait for a person; say so to change it.",
                         locked=True, path=glob)


def _announced(ctx: RunContext) -> set[str]:
    path = ctx.store.root / "orch-events.jsonl"
    if not path.exists():
        return set()
    paths = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("kind") == EVENT_KIND and isinstance(event.get("path"), str):
            paths.add(event["path"])
    return paths


def _detect(files: Iterable[str], read: Callable[[str], str | None], base_branch: str) -> list[str]:
    found = set()
    for path in files:
        parts = path.split("/")
        for end in range(1, len(parts)):
            if parts[end - 1] in _NOT_MIGRATIONS:
                break  # Anything beneath a docs/ or tests/ directory, at any depth, is not a migration root.
            directory = "/".join(parts[:end])
            if any(directory == name or directory.endswith("/" + name) for name in MIGRATION_DIRS):
                found.add(f"{escape(directory)}/**")
                break
        if len(parts) == 3 and parts[:2] == [".github", "workflows"] and path.endswith((".yml", ".yaml")):
            if _deploy_workflow(read(path), base_branch):
                found.add(escape(path))
    return sorted(found)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _deploy_workflow(text: str | None, base_branch: str) -> bool:
    if text is None:
        return False
    try:
        workflow = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    return isinstance(workflow, dict) and _pushes_to(workflow, base_branch) and _deploys(workflow)


def _pushes_to(workflow: dict, base_branch: str) -> bool:
    # YAML 1.1 reads an unquoted `on` key as True.
    trigger = workflow.get("on", workflow.get(True))
    if trigger == "push":
        return True
    if isinstance(trigger, list):
        return "push" in trigger
    if not isinstance(trigger, dict) or "push" not in trigger:
        return False
    push = trigger["push"]
    if not isinstance(push, dict):
        return True
    branches, ignored = push.get("branches"), push.get("branches-ignore")
    if branches is not None:
        return _branch_matches(base_branch, _list(branches))
    if ignored is not None:
        return not _branch_matches(base_branch, _list(ignored))
    return not ("tags" in push or "tags-ignore" in push)


def _branch_matches(branch: str, patterns: list) -> bool:
    """GitHub filter patterns, in order: a later `!pattern` un-matches, a later pattern matches again."""
    matched = False
    for pattern in map(str, patterns):
        negated = pattern.startswith("!")
        if _filter_regex(pattern[1:] if negated else pattern).fullmatch(branch):
            matched = not negated
    return matched


def _filter_regex(pattern: str) -> re.Pattern:
    """GitHub's filter pattern syntax: `*` stays within one `/` segment, `**` crosses segments,
    `?` and `+` make the preceding character optional or repeatable, `[...]` is a set, `\\` escapes."""
    atoms: list[str] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "\\" and i + 1 < len(pattern):
            atoms.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if pattern.startswith("**", i):
            atoms.append(".*")
            i += 2
            continue
        if char == "*":
            atoms.append("[^/]*")
        elif char in "?+" and atoms:
            atoms[-1] = f"(?:{atoms[-1]}){char}"
        elif char == "[" and (close := pattern.find("]", i + 1)) > i + 1:
            atoms.append("[" + pattern[i + 1:close].replace("\\", "\\\\") + "]")
            i = close + 1
            continue
        else:
            atoms.append(re.escape(char))
        i += 1
    return re.compile("".join(atoms))


def _deploys(workflow: dict) -> bool:
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        return False
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        name = f"{job_id} {job.get('name', '')}".lower()
        if any(word in name for word in NAME_WORDS) or _production(job.get("environment")):
            return True
        if isinstance(job.get("uses"), str) and _uses_deploys(job["uses"]):
            return True
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            if isinstance(step.get("run"), str) and _RUN_WORDS.search(step["run"]):
                return True
            if isinstance(step.get("uses"), str) and _uses_deploys(step["uses"]):
                return True
    return False


def _uses_deploys(uses: str) -> bool:
    return bool(_ACTION_WORDS.search(uses.split("@", 1)[0]))


def _production(environment: Any) -> bool:
    if isinstance(environment, dict):
        environment = environment.get("name")
    if not isinstance(environment, str):
        return False
    return "${{" in environment or environment.strip().lower().startswith("prod")


def _list(value: Any) -> list:
    return value if isinstance(value, list) else [value]
