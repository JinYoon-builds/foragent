"""Deterministic context gathering and publication for the fa-order skill."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from urllib.parse import quote

from foragent import board, design
from foragent.config import Config
from foragent.executors import executor_for
from foragent.git import GitError, GitRepo
from foragent.inspect_data import DECISION_MARKER, comment_key, has_marker, latest_flag
from foragent.issue import MARKER, acceptance_paths, body_hash, parse_body
from foragent.kick import kick_tick
from foragent.labels import LABELS, issue_lane, issue_size, lane_order, state_group
from foragent.lanes import add_lane, lane_files, validate_lane
from foragent.paths import repo_path
from foragent.skills import SkillManager
from foragent.state import StateStore
from foragent.tracker import LabelSpec, Tracker


class PlanError(ValueError):
    """An order plan is malformed or cannot be applied."""


PROGRESS_SLOTS = ("goal", "out_of_scope", "done_when", "lane", "skills")
PROGRESS_STEPS = ("start", "ledger", "chunk", "cell", "tests", "check", "approval", "publish", "end")


def progress(
    store: StateStore, step: str, *, title: str | None = None, add: str | None = None,
    chunk: str | None = None, slot: str | None = None, ask: str | None = None,
    filled: bool = False, done: int | None = None, total: int | None = None,
    issue: int | None = None,
) -> dict | None:
    """Record preparation facts, without GitHub, executors or terminal actions.

    Cells are keyed by chunk and slot. A question reopens its cell; marking it
    filled resolves the question. Replayed additions and fills are idempotent.
    """
    if step not in PROGRESS_STEPS:
        raise PlanError(f"unknown order progress step: {step}")

    def update(state):
        if step == "end":
            return None
        if step == "start":
            _text(title, "title")
            return {"title": title, "step": step, "ledger": [], "chunks": [],
                    "cells": {}, "tests": {"done": 0, "total": 0},
                    "published": [], "cells_left": 0}
        if state is None:
            raise PlanError("no order in preparation; run fa order progress start --title TITLE")
        if step in {"ledger", "chunk"}:
            _text(add, "add")
            collection = state["ledger" if step == "ledger" else "chunks"]
            if add not in collection:
                collection.append(add)
                if step == "chunk":
                    state["cells"][add] = {name: {"filled": False, "ask": None}
                                           for name in PROGRESS_SLOTS}
        elif step == "cell":
            _text(chunk, "chunk")
            if chunk not in state["chunks"]:
                raise PlanError(f"unknown chunk: {chunk}; add it with fa order progress chunk --add")
            if slot not in PROGRESS_SLOTS:
                raise PlanError(f"unknown cell slot: {slot}; choose from {', '.join(PROGRESS_SLOTS)}")
            if filled == (ask is not None):
                raise PlanError("cell requires either --ask or --filled")
            if ask is not None:
                _text(ask, "ask")
            state["cells"][chunk][slot] = {"filled": filled, "ask": ask}
        elif step == "tests":
            if type(done) is not int or type(total) is not int or not 0 <= done <= total:
                raise PlanError("tests requires integer counts with 0 <= done <= total")
            state["tests"] = {"done": done, "total": total}
        elif step == "publish":
            if type(issue) is not int or issue <= 0:
                raise PlanError("issue must be a positive integer")
            if issue not in state["published"]:
                state["published"].append(issue)
        state["step"] = step
        state["cells_left"] = sum(not cell["filled"] for cells in state["cells"].values()
                                  for cell in cells.values())
        return state

    return store.update_order_progress(update)


def read_plan(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PlanError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)


def _installed_ouroboros_version() -> str | None:
    path = Path.home() / ".claude/plugins/installed_plugins.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return None
    for key, entries in plugins.items():
        if key.startswith("ouroboros@"):
            if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
                return None
            version = entries[0].get("version")
            return version if isinstance(version, str) else None
    return None


def _interview_context(config: Config) -> dict:
    installed = _installed_ouroboros_version()
    engine, reason = config.interview, None
    if config.interview == "ouroboros":
        if installed is None:
            engine, reason = "native", "Ouroboros not installed"
        elif config.ouroboros_version is not None and config.ouroboros_version != installed:
            engine = "native"
            reason = f"Ouroboros version mismatch: installed {installed}, pinned {config.ouroboros_version}"
    return {"requested": config.interview, "engine": engine, "installed_version": installed,
            "pinned_version": config.ouroboros_version, "reason": reason}


def detect_test_commands(root: Path) -> list[str]:
    """Detect full-suite commands for both order context and pre-merge checks."""
    commands = []
    if (root / "pyproject.toml").is_file():
        commands.append("uv run pytest")
    package = root / "package.json"
    if package.is_file():
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
            if isinstance(data.get("scripts"), dict) and data["scripts"].get("test"):
                pnpm = (root / "pnpm-lock.yaml").is_file() or str(data.get("packageManager", "")).startswith("pnpm@")
                commands.append("pnpm test" if pnpm else "npm test")
        except (ValueError, AttributeError):
            pass
    if (root / "go.mod").is_file():
        commands.append("go test ./...")
    return commands


def context(root: Path, config: Config, tracker: Tracker) -> dict:
    issues = sorted(tracker.list_open_issues(), key=lambda issue: issue.number)
    feedback, decisions = [], []
    for issue in issues:
        if "feedback" in issue.labels:
            about = re.search(r"(?:^|\n)About #(\d+)\.", issue.body)
            feedback.append({"number": issue.number, "title": issue.title, "body": issue.body,
                             "about": int(about[1]) if about else None})
        if "needs-human" in issue.labels:
            comments = tracker.list_comments(issue.number)
            flag = latest_flag(comments)
            if flag and flag[1] in {"spec-question", "no-machine-check"}:
                answer = max((comment for comment in comments if has_marker(comment, DECISION_MARKER)),
                             key=comment_key, default=None)
                content = flag[0].body.partition("\n")[2]
                summary, _, details = content.partition("\n")
                decisions.append({
                    "number": issue.number, "title": issue.title, "body": issue.body,
                    "reason": flag[1], "question": details.strip() or summary.strip().strip("*"),
                    "answer": answer.body.split("\n", 1)[-1].strip() if answer else None,
                })
    specs = []
    for path in sorted((root / "docs/specs").glob("*.md")):
        heading = next((re.sub(r"\s+#+\s*$", "", match[1]).strip() for line in path.read_text(encoding="utf-8").splitlines()
                        if (match := re.match(r"^#{1,6}\s+(.+)", line))), None)
        specs.append({"path": path.relative_to(root).as_posix(), "heading": heading})
    ignored = {".git", ".venv", "node_modules", ".pytest_cache", "__pycache__"}
    tree, languages = [], set()
    extensions = {".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript",
                  ".tsx": "TypeScript", ".go": "Go", ".rs": "Rust", ".swift": "Swift",
                  ".java": "Java", ".rb": "Ruby", ".sh": "Shell"}
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in ignored and not (Path(parent) / name).is_symlink())
        relative = Path(parent).relative_to(root)
        depth = 0 if relative == Path(".") else len(relative.parts)
        if depth < 2:
            tree.extend((relative / name).as_posix() + "/" for name in dirs)
            tree.extend((relative / name).as_posix() for name in sorted(files) if name not in ignored)
        languages.update(extensions[Path(name).suffix] for name in files if Path(name).suffix in extensions)
    commands = detect_test_commands(root)
    if (root / "pyproject.toml").is_file():
        languages.add("Python")
    if (root / "package.json").is_file():
        languages.add("JavaScript")
    if (root / "go.mod").is_file():
        languages.add("Go")
    lanes = {name: {"paths": lane.get("paths", []), "agent": lane.get("agent"),
                    "skills": lane.get("skills", [])} for name, lane in config.lanes.items()}
    # Existing lane definitions can keep their installed skills in agent frontmatter.
    for name, lane in lanes.items():
        if lane["agent"] and not lane["skills"]:
            path = root / ".claude/agents" / f"{lane['agent']}.md"
            if path.is_file():
                import yaml
                match = re.match(r"\A---\s*\n(.*?)\n---", path.read_text(encoding="utf-8"), re.S)
                if match:
                    meta = yaml.safe_load(match[1])
                    lane["skills"] = meta.get("skills", []) if isinstance(meta, dict) else []
    return {
        "language": config.language, "lanes": lanes, "tiers": config.tiers,
        "interview": _interview_context(config),
        "profile": config.profile,
        "ui": design.ui_context(root),
        "acceptance_dir": config.acceptance_dir, "base_branch": config.base_branch,
        "feedback": feedback, "decisions": decisions,
        "open_issues": [{"number": issue.number, "title": issue.title, "labels": sorted(issue.labels),
                         "lane": issue_lane(issue.labels, config.lanes), "size": issue_size(issue.labels),
                         "state_group": state_group(issue, tracker)} for issue in issues],
        "specs": specs, "repo": {"tree": sorted(tree), "languages": sorted(languages),
                                 "test_commands": commands, "has_workflows": (root / ".github/workflows").is_dir()},
    }


def _object(value, required: set[str], optional: set[str], label: str) -> dict:
    if not isinstance(value, dict):
        raise PlanError(f"{label} must be an object")
    missing, extra = required - value.keys(), value.keys() - required - optional
    if missing:
        raise PlanError(f"{label}: missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise PlanError(f"{label}: unknown fields: {', '.join(sorted(extra))}")
    return value


def _text(value, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise PlanError(f"{label} must be a {'string' if empty else 'nonempty string'}")
    return value


def _strings(value, label: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise PlanError(f"{label} must be a {'nonempty ' if nonempty else ''}list of strings")
    for item in value:
        _text(item, label)
    return value


def validate_plan(plan: dict, root: Path, config: Config) -> dict[str, str]:
    _object(plan, {"id", "title", "summary", "spec_markdown", "issues"}, {"new_lanes", "edits"}, "plan")
    for key in ("id", "title", "summary", "spec_markdown"):
        _text(plan[key], f"plan.{key}")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", plan["id"]):
        raise PlanError("plan.id must contain only letters, numbers, hyphens, and underscores")
    for key in ("issues", "edits", "new_lanes"):
        if not isinstance(plan.get(key, []), list):
            raise PlanError(f"plan.{key} must be an array")
    lanes = set(config.lanes) | {"main"}
    for index, lane in enumerate(plan.get("new_lanes", [])):
        label = f"new_lanes[{index}]"
        _object(lane, {"name", "paths", "skills"}, set(), label)
        if not isinstance(lane["name"], str) or not re.fullmatch(r"[a-z0-9-]+", lane["name"]):
            raise PlanError(f"{label}.name is invalid")
        if lane["name"] in lanes - set(config.lanes):
            raise PlanError(f"duplicate or implicit lane: {lane['name']}")
        lanes.add(lane["name"])
        _strings(lane["paths"], f"{label}.paths", nonempty=True)
        _strings(lane["skills"], f"{label}.skills")
    names = [lane["name"] for lane in plan.get("new_lanes", [])]
    if len(set(names)) != len(names):
        raise PlanError("duplicate new_lanes names")
    files = {f"docs/specs/{plan['id']}.md": plan["spec_markdown"]}
    repo_path(root, next(iter(files)))
    acceptance = repo_path(root, config.acceptance_dir)
    keys, edits = set(), set()
    for kind in ("issues", "edits"):
        for index, item in enumerate(plan.get(kind, [])):
            label = f"{kind}[{index}]"
            required = {"goal", "out_of_scope", "done_when"}
            required |= {"key", "title", "lane", "size"} if kind == "issues" else {"number"}
            _object(item, required, {"checklist", "acceptance_files", "blocked_by", "files", "interactive"} if kind == "issues"
                    else {"checklist", "acceptance_files", "files"}, label)
            _text(item["goal"], f"{label}.goal")
            _text(item["out_of_scope"], f"{label}.out_of_scope")
            _strings(item["done_when"], f"{label}.done_when", nonempty=True)
            for name in _strings(item.get("files", []), f"{label}.files"):
                if name.splitlines() != [name]:
                    raise PlanError(f"{label}.files items must each be a single line")
                try:
                    repo_path(root, name)
                except ValueError as exc:
                    raise PlanError(f"{label}.files: {exc}") from exc
            _strings(item.get("checklist", []), f"{label}.checklist")
            if any("\n" in line or "\r" in line for line in item.get("checklist", [])):
                raise PlanError(f"{label}.checklist items must each be a single line")
            if kind == "issues":
                if type(item.get("interactive", False)) is not bool:
                    raise PlanError(f"{label}.interactive must be a boolean")
                _text(item["key"], f"{label}.key")
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", item["key"]):
                    raise PlanError(f"{label}.key must be a simple identifier")
                if item["key"] in keys:
                    raise PlanError(f"duplicate issue key: {item['key']}")
                keys.add(item["key"])
                _text(item["title"], f"{label}.title")
                if not isinstance(item["lane"], str) or item["lane"] not in lanes:
                    raise PlanError(f"{label}: unknown lane: {item['lane']}")
                if item["size"] not in ("s", "m", "l"):
                    raise PlanError(f"{label}.size must be s, m, or l")
                _strings(item.get("blocked_by", []), f"{label}.blocked_by")
            else:
                if type(item["number"]) is not int or item["number"] <= 0 or item["number"] in edits:
                    raise PlanError(f"{label}.number must be a unique positive integer")
                edits.add(item["number"])
            acceptance_files = item.get("acceptance_files", {})
            if not isinstance(acceptance_files, dict):
                raise PlanError(f"{label}.acceptance_files must be an object")
            for name, content in acceptance_files.items():
                path = repo_path(root, name)
                if not path.resolve().is_relative_to(acceptance.resolve()) or path == acceptance:
                    raise PlanError(f"acceptance file must be under {config.acceptance_dir}: {name}")
                if PurePosixPath(name).as_posix() != name or name.startswith("./"):
                    raise PlanError(f"acceptance file path must be normalized: {name}")
                _text(content, f"{label}.acceptance_files[{name}]", empty=True)
                if name in files and files[name] != content:
                    raise PlanError(f"conflicting file contents: {name}")
                files[name] = content
            body = issue_body(item, "owner/repo", config.base_branch)
            if parse_body(body).done_when_commands != item["done_when"]:
                raise PlanError(f"{label}.done_when cannot contain Markdown code fences or trailing newlines")
    for kind in ("issues", "edits"):
        for item in plan.get(kind, []):
            for name in acceptance_paths(item["done_when"], config.acceptance_dir):
                if name not in files and not repo_path(root, name).is_file():
                    raise PlanError(f"Done when references missing acceptance file: {name}")
                if name not in files:
                    files[name] = repo_path(root, name).read_text(encoding="utf-8")
    dependencies = {item["key"]: item.get("blocked_by", []) for item in plan["issues"]}
    visiting, visited = set(), set()

    def visit(key):
        if key not in keys:
            raise PlanError(f"unknown blocked_by key: {key}")
        if key in visiting:
            raise PlanError(f"blocked_by cycle at {key}")
        if key in visited:
            return
        visiting.add(key)
        for blocker in dependencies[key]:
            visit(blocker)
        visiting.remove(key)
        visited.add(key)

    for key in dependencies:
        visit(key)
    return files


def issue_body(item: dict, repo: str, base: str, acceptance_dir: str = "tests/acceptance/") -> str:
    lines = [MARKER, "## Goal", item["goal"], "", "## Out of scope", item["out_of_scope"]]
    if item.get("files"):
        lines.extend(["", "## Files", *(f"- {path}" for path in item["files"])])
    lines.extend(["", "## Done when"])
    for command in item["done_when"]:
        lines.extend(["```sh", command, "```"])
    lines.extend(f"- [ ] {text}" for text in item.get("checklist", []))
    for path in sorted(set(item.get("acceptance_files", {})) | set(acceptance_paths(item["done_when"], acceptance_dir))):
        lines.append(f"[{path}](https://github.com/{repo}/blob/{quote(base, safe='')}/{quote(path)})")
    return "\n".join(lines) + "\n"


def _plan_marker(identity: str, mapping: dict[str, int], acceptance: dict[str, dict[str, str]]) -> str:
    return (f"<!-- foragent:plan id={identity} map={json.dumps(mapping, sort_keys=True)} "
            f"acceptance={json.dumps(acceptance, sort_keys=True)} -->")


def _child_marker(identity: str, key: str) -> str:
    return f"<!-- foragent:plan-child id={identity} key={key} -->"


@dataclass
class PublishReport:
    title: str
    parent: int | None
    rows: list[dict]
    paths: list[str]
    dry_run: bool

    def summary(self) -> str:
        lines = [f"{'Plan (dry run)' if self.dry_run else 'Published'}: {self.title}"]
        if self.parent is not None:
            lines.append(f"Parent: #{self.parent}")
        lines.append("Files: " + ", ".join(self.paths))
        for lane in sorted({row["lane"] for row in self.rows}, key=lane_order):
            lines += [f"\nlane:{lane}", "| Number | Title | Size | Blocked by | Approved? | Interactive? |", "| --- | --- | --- | --- | --- | --- |"]
            for row in self.rows:
                if row["lane"] == lane:
                    title = row["title"].replace("|", "\\|").replace("\n", " ")
                    blockers = ", ".join(map(str, row["blocked_by"])) or "—"
                    lines.append(f"| {row['number']} | {title} | {row['size']} | {blockers} | {'yes' if row['approved'] else 'no'} | {'yes' if row['interactive'] else 'no'} |")
        return "\n".join(lines)


def _publish_files(plan: dict, root: Path, config: Config, git: GitRepo, files: dict[str, str],
                   manager: SkillManager | None) -> dict[str, str]:
    """Commit only this order's own paths on the latest base and push them."""
    for lane in plan.get("new_lanes", []):
        if lane["name"] not in config.lanes:
            add_lane(root, lane["name"], lane["paths"], skills=lane["skills"], git=git, manager=manager, push=False)
        else:
            # Recover a lane whose files were written before an interrupted commit.
            paths = lane_files(root, lane["name"], config.lanes[lane["name"]].get("skills", []))
            if not repo_path(root, paths[1]).is_file():
                raise PlanError(f"existing lane agent is missing: {paths[1]}")
            git.ensure_tracked(paths)
            pending = git.uncommitted_paths(paths)
            if pending:
                git.commit_paths(pending, f"Foragent lane {lane['name']}")
    for name, content in files.items():
        path = repo_path(root, name)
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    git.ensure_tracked(list(files))
    changed = git.uncommitted_paths(list(files))
    if changed:
        git.commit_paths(changed, f"Foragent order {plan['id']}: acceptance tests and spec")
    published_blobs = git.blob_shas(git.head_sha(), files)
    missing = [name for name in files if published_blobs.get(name, "missing") == "missing"]
    if missing:
        raise PlanError(f"published commit is missing files: {', '.join(sorted(missing))}")
    # Also retry a previously failed push when the files already match HEAD.
    git.push_base(config.base_branch)
    return published_blobs


def publish(plan: dict, root: Path, config: Config, tracker: Tracker, *, repo: str,
            git: GitRepo | None = None, manager: SkillManager | None = None,
            approve: bool = False, approve_keys: list[str] | None = None,
            approve_numbers: list[int] | None = None, dry_run: bool = False) -> PublishReport:
    try:
        files = validate_plan(plan, root, config)
    except ValueError as exc:
        raise PlanError(str(exc)) from exc
    keys = {item["key"] for item in plan["issues"]}
    selected_keys, selected_numbers = set(approve_keys or []), set(approve_numbers or [])
    if selected_keys - keys:
        raise PlanError(f"unknown approve keys: {', '.join(sorted(selected_keys - keys))}")
    all_issues = tracker.list_issues()
    pattern = re.compile(r"<!-- foragent:plan id=" + re.escape(plan["id"])
                         + r" map=(\{[^\n]*?\})(?: acceptance=(\{[^\n]*\}))? -->")
    parents = [(issue, pattern.search(issue.body)) for issue in all_issues if pattern.search(issue.body)]
    if len(parents) > 1:
        raise PlanError(f"multiple parents for plan {plan['id']}")
    parent, mapping, saved_blobs = None, {}, {}
    if parents:
        issue, marker = parents[0]
        parent = issue.number
        try:
            mapping = json.loads(marker[1])
            if not isinstance(mapping, dict) or any(not isinstance(k, str) or type(n) is not int or n <= 0 for k, n in mapping.items()):
                raise ValueError("invalid issue map")
            if len(set(mapping.values())) != len(mapping):
                raise ValueError("duplicate issue numbers in map")
            saved_blobs = json.loads(marker[2]) if marker[2] else {}
            if not isinstance(saved_blobs, dict) or any(
                not number.isdecimal() or int(number) <= 0 or not isinstance(blobs, dict)
                or any(not isinstance(sha, str) for sha in blobs.values())
                for number, blobs in saved_blobs.items()
            ):
                raise ValueError("invalid acceptance blob map")
        except ValueError as exc:
            raise PlanError(f"invalid plan map on parent #{parent}") from exc
    for item in plan["issues"]:
        matches = [issue.number for issue in all_issues if _child_marker(plan["id"], item["key"]) in issue.body]
        if len(matches) > 1:
            raise PlanError(f"duplicate children for key {item['key']}")
        if item["key"] not in mapping and matches:
            mapping[item["key"]] = matches[0]
    by_number = {issue.number: issue for issue in all_issues}
    edit_numbers = {item["number"] for item in plan.get("edits", [])}
    if edit_numbers & set(mapping.values()):
        raise PlanError("a plan child cannot also appear in edits")
    for number in edit_numbers | set(mapping.values()):
        if number not in by_number or number == parent:
            raise PlanError(f"issue #{number} does not exist or is the plan parent")
    unknown_numbers = selected_numbers - edit_numbers - {mapping[key] for key in keys if key in mapping}
    if unknown_numbers & by_number.keys() or (unknown_numbers and keys <= mapping.keys()):
        raise PlanError("approve numbers must refer to this plan's existing children or edits")
    for lane in plan.get("new_lanes", []):
        if lane["name"] in config.lanes:
            existing = config.lanes[lane["name"]]
            if (existing.get("paths", []) != lane["paths"] or existing.get("agent") != f"fa-{lane['name']}"
                    or existing.get("skills", []) != [skill.split(":", 1)[-1].rsplit("/", 1)[-1] for skill in lane["skills"]]):
                raise PlanError(f"existing lane differs from plan: {lane['name']}")
        else:
            validate_lane(root, config, lane["name"], lane["paths"], lane["skills"], manager=manager)
    rows = []
    for item in plan["issues"]:
        number = mapping.get(item["key"])
        rows.append({"number": number or item["key"], "title": item["title"], "size": item["size"],
                     "interactive": item.get("interactive", False),
                     "lane": item["lane"], "blocked_by": [mapping.get(key, key) for key in item.get("blocked_by", [])],
                     "approved": approve or item["key"] in selected_keys or number in selected_numbers})
    for item in plan.get("edits", []):
        issue = by_number[item["number"]]
        rows.append({"number": issue.number, "title": issue.title, "size": issue_size(issue.labels),
                     "interactive": "interactive" in issue.labels,
                     "lane": issue_lane(issue.labels, config.lanes), "blocked_by": [dep.number for dep in tracker.blocked_by(issue.number)],
                     "approved": approve or issue.number in selected_numbers})
    if dry_run:
        return PublishReport(plan["title"], parent, rows, sorted(files), True)
    require_clean_preread(plan, root, config, [
        *(item for item in plan["issues"]
          if approve or item["key"] in selected_keys or mapping.get(item["key"]) in selected_numbers),
        *(edit_view(item, by_number[item["number"]], config) for item in plan.get("edits", [])
          if approve or item["number"] in selected_numbers),
    ])
    git = git or GitRepo(root)
    if git.current_branch() != config.base_branch:
        raise GitError(f"order publish must run on the base branch {config.base_branch!r}")
    with git.publish_lock():
        git.update_base(config.base_branch)
        published_blobs = _publish_files(plan, root, config, git, files, manager)
    lane_names = {item["lane"] for item in plan["issues"]} - {"main"}
    tracker.ensure_labels([*LABELS, *(LabelSpec(f"lane:{name}", "ededed", f"Foragent lane {name}.") for name in sorted(lane_names))])
    if any(item.get("interactive", False) for item in plan["issues"]):
        tracker.ensure_labels([LabelSpec("interactive", "d4c5f9", "Work that needs substantial back-and-forth with the user.")])

    recorded_blobs = saved_blobs.copy()

    def acceptance_blobs(item: dict) -> dict[str, str]:
        referenced = set(item.get("acceptance_files", {})) | set(acceptance_paths(item["done_when"], config.acceptance_dir))
        return {name: published_blobs[name] for name in sorted(referenced)}

    def parent_body() -> str:
        children = [tracker.get_issue(n) for n in mapping.values()]
        checklist = "\n".join(f"- [{'x' if issue.state == 'closed' else ' '}] #{issue.number} {issue.title}" for issue in children)
        return f"{plan['summary']}\n\n{checklist}\n\n{_plan_marker(plan['id'], mapping, recorded_blobs)}\n"

    if parent is None:
        parent = tracker.create_issue(plan["title"], parent_body(), [])
    else:
        tracker.update_issue(parent, title=plan["title"], body=parent_body())
    board.add_issue(tracker, repo, config.board, parent)
    for item in plan["issues"]:
        body = issue_body(item, repo, config.base_branch, config.acceptance_dir) + "\n" + _child_marker(plan["id"], item["key"]) + "\n"
        labels = [f"size:{item['size']}", *([f"lane:{item['lane']}"] if item["lane"] != "main" else [])]
        if item.get("interactive", False):
            labels.append("interactive")
        number = mapping.get(item["key"])
        blobs = acceptance_blobs(item)
        if number is None:
            number = tracker.create_issue(item["title"], body, labels)
            mapping[item["key"]] = number
            recorded_blobs[str(number)] = blobs
            # Persist progress before native edge calls so retries reuse the child.
            tracker.update_issue(parent, title=plan["title"], body=parent_body())
        else:
            existing = tracker.get_issue(number)
            if (existing.body != body or saved_blobs.get(str(number), {}) != blobs
                    or ("interactive" in existing.labels) != item.get("interactive", False)):
                tracker.remove_labels(number, ["approved", "ready"])
            tracker.update_issue(number, title=item["title"], body=body)
            tracker.remove_labels(number, [label for label in existing.labels
                                           if (label.startswith(("lane:", "size:")) or label == "interactive")
                                           and label not in labels])
            tracker.add_labels(number, labels)
            recorded_blobs[str(number)] = blobs
        tracker.add_sub_issue(parent, number)
        board.add_issue(tracker, repo, config.board, number, item["lane"], item["size"])
    for item in plan["issues"]:
        number = mapping[item["key"]]
        desired = {mapping[key] for key in item.get("blocked_by", [])}
        existing = {dep.number for dep in tracker.blocked_by(number)}
        for blocker in sorted(existing - desired):
            tracker.remove_blocked_by(number, blocker)
        for blocker in sorted(desired - existing):
            tracker.add_blocked_by(number, blocker)
    for item in plan.get("edits", []):
        number = item["number"]
        existing = tracker.get_issue(number)
        body = issue_body(item, repo, config.base_branch, config.acceptance_dir)
        blobs = acceptance_blobs(item)
        referenced = set(blobs)
        revision = body_hash(body + json.dumps({name: files[name] for name in sorted(referenced)}, sort_keys=True))
        comment = ("<!-- foragent:edited -->\nUpdated by order from your decision; needs approval.\n\n"
                   f"<!-- foragent:edit plan={plan['id']} sha256={revision} -->")
        prior = any(c.body == comment for c in tracker.list_comments(number))
        if existing.body != body or not prior or "needs-human" in existing.labels or saved_blobs.get(str(number), {}) != blobs:
            tracker.remove_labels(number, ["needs-human", "approved", "ready"])
            tracker.update_issue(number, title=existing.title, body=body)
            if not prior:
                tracker.comment(number, comment)
        recorded_blobs[str(number)] = blobs
    tracker.update_issue(parent, title=plan["title"], body=parent_body())
    if selected_numbers - edit_numbers - {mapping[key] for key in keys}:
        raise PlanError("approve numbers must refer to this plan's children or edits")
    approved_numbers = (set(mapping[key] for key in keys) | edit_numbers) if approve else selected_numbers | {mapping[key] for key in selected_keys}
    for number in sorted(approved_numbers):
        tracker.add_labels(number, ["approved"])
        board.sync_status(board.ProjectContext(tracker, repo, config), number, "Approved")
    if approved_numbers:
        kick_tick(root)
    for item, row in zip(plan["issues"], rows):
        row["number"] = mapping[item["key"]]
        row["blocked_by"] = [mapping[key] for key in item.get("blocked_by", [])]
    for row in rows:
        row["approved"] = "approved" in tracker.get_issue(row["number"]).labels
    return PublishReport(plan["title"], parent, rows, sorted(files), False)


PREREAD_FILE = ".foragent/preread.json"
PREREAD_TIMEOUT_S = 15 * 60


def edit_view(item: dict, issue, config: Config) -> dict:
    """An edit read as the issue its implementer will receive."""
    return {**item, "key": f"#{item['number']}", "title": issue.title,
            "lane": issue_lane(issue.labels, config.lanes), "interactive": "interactive" in issue.labels,
            "edit": True}


def preread_prompt(plan: dict, item: dict) -> str:
    """What the implementer would receive, asked read-only for its unknowns."""
    commands = "\n".join(item.get("done_when", []))
    tests = "".join(f"\n### {path}\n```\n{content}\n```\n"
                    for path, content in item.get("acceptance_files", {}).items()) or "\n(none)\n"
    files = "\n".join(f"- {path}" for path in item.get("files", [])) or "(not listed)"
    checklist = "\n".join(f"- {line}" for line in item.get("checklist", [])) or "(none)"
    titles = {issue["key"]: issue["title"] for issue in plan["issues"]}
    blocked = "\n".join(f"- {key}: {titles.get(key, '')}" for key in item.get("blocked_by", [])) or "(none)"
    lanes = json.dumps(plan.get("new_lanes", []), ensure_ascii=False, sort_keys=True)
    heading = (f"# Edit of existing issue {item['key']}: {item['title']}" if item.get("edit")
               else f"# Issue {item['key']}: {item['title']}")
    return (
        "You will implement the issue below later. Do not implement it now and do not change any file.\n"
        "Read the issue and the repository read-only, then list every unknown: a question whose answer\n"
        "is not in the issue text or the code and that you would otherwise have to guess.\n"
        "Reply with one unknown per line and nothing else.\n"
        "If there are no unknowns, reply with exactly NONE.\n\n"
        f"# Order: {plan['title']}\n{plan.get('summary', '')}\n\n"
        f"## Spec\n{plan.get('spec_markdown', '')}\n\n"
        f"New lanes in this order: {lanes}\n\n"
        f"{heading}\n"
        f"Lane: {item['lane']}\n"
        f"Interactive (needs back-and-forth with the user): {'yes' if item.get('interactive') else 'no'}\n\n"
        f"## Goal\n{item['goal']}\n\n"
        f"## Out of scope\n{item['out_of_scope']}\n\n"
        f"## Blocked by\n{blocked}\n\n"
        f"## Files\n{files}\n\n"
        f"## Done when\n```sh\n{commands}\n```\n\n"
        f"## Checklist\n{checklist}\n\n"
        f"## Acceptance tests (locked){tests}"
    )


def preread_target(plan: dict, item: dict, config: Config) -> tuple[str, str | None, str, str]:
    """The implementer the runner would use for this issue, its prompt and the evidence key."""
    lane = config.lanes.get(item["lane"], {})
    name = lane.get("executor") or config.executor
    # Same choice as the runner's per-lane executor configuration.
    model = lane.get("model") if lane.get("model") is not None else config.executor_model
    prompt = preread_prompt(plan, item)
    digest = hashlib.sha256(json.dumps([name, model, prompt], ensure_ascii=False).encode("utf-8")).hexdigest()
    return name, model, prompt, digest


def _preread_records(root: Path) -> dict:
    try:
        records = json.loads((root / PREREAD_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise PlanError(f"{PREREAD_FILE} is not valid JSON; delete it and run fa order preread again") from exc
    return records if isinstance(records, dict) else {}


def _unknowns(message: str) -> list[str]:
    if message.strip() == "NONE":
        return []
    lines = (re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", line.strip()) for line in message.splitlines())
    return [line for line in lines if line and line != "NONE"]


def preread(plan: dict, root: Path, config: Config,
            tracker: Callable[[], Tracker] | None = None) -> list[dict]:
    """Ask each issue's implementer, read-only, for its unknowns and record them by content.

    Edits need the tracker for each existing issue's title and lane; it is opened only then.
    """
    try:
        validate_plan(plan, root, config)
    except ValueError as exc:
        raise PlanError(str(exc)) from exc
    items = list(plan["issues"])
    if plan.get("edits"):
        if tracker is None:
            raise PlanError("prereading edits needs the tracker for the existing issues")
        source = tracker()
        items += [edit_view(item, source.get_issue(item["number"]), config) for item in plan["edits"]]
    directory = root / ".foragent"
    logs = directory / "preread-logs"
    logs.mkdir(parents=True, exist_ok=True)
    ignore = directory / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
    results = []
    for item in items:
        name, model, prompt, digest = preread_target(plan, item, config)
        log = logs / f"{item['key'].lstrip('#')}-{digest[:12]}.log"
        result = executor_for(name).run(worktree=root, prompt=prompt, model=model,
                                        timeout_s=PREREAD_TIMEOUT_S, log_path=log, read_only=True)
        if result.timed_out or result.exit_code != 0 or not result.last_message.strip():
            raise PlanError(f"preread of issue {item['key']} did not finish with an answer; see {log}")
        unknowns = _unknowns(result.last_message)
        records = _preread_records(root)
        records[digest] = {"key": item["key"], "unknowns": unknowns,
                           "at": datetime.now(UTC).isoformat(timespec="seconds")}
        temporary = root / (PREREAD_FILE + ".tmp")
        temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, root / PREREAD_FILE)
        results.append({"key": item["key"], "unknowns": unknowns})
    return results


def require_clean_preread(plan: dict, root: Path, config: Config, items: list[dict]) -> None:
    """Approval needs the latest preread of each issue's current content to have no unknowns."""
    records = _preread_records(root)
    missing, open_items = [], []
    for item in items:
        record = records.get(preread_target(plan, item, config)[3])
        if not isinstance(record, dict) or not isinstance(record.get("unknowns"), list):
            missing.append(item["key"])
        elif record["unknowns"]:
            open_items.append(f"{item['key']}: " + "; ".join(map(str, record["unknowns"])))
    problems = []
    if missing:
        problems.append(f"no preread of the current content for issue(s) {', '.join(missing)}; "
                        "run fa order preread PLAN.json first")
    if open_items:
        problems.append("the implementer listed unknowns; resolve them in the plan, then run "
                        "fa order preread PLAN.json again: " + " | ".join(open_items))
    if problems:
        raise PlanError("cannot approve: " + ". ".join(problems))
