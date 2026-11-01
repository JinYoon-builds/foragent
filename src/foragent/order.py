"""Deterministic context gathering and publication for the fa-order skill."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sys
from urllib.parse import quote

from foragent import board, capacity, design, order_references, order_reviewed, order_waiting, ownership
from foragent import decisions as decision_links
from foragent.config import Config
from foragent.executors import executor_for
from foragent.git import GitError, GitRepo
from foragent.i18n import text
from foragent.inspect_data import DECISION_MARKER, comment_key, has_marker, latest_flag
from foragent.issue import MARKER, acceptance_paths, body_hash, parse_body
from foragent.kick import kick_tick
from foragent.labels import LABELS, issue_lane, issue_size, lane_order, state_group
from foragent.lanes import add_lane, lane_files, validate_lane
from foragent.order_files import check_destinations, check_write, issue_body, validate_paths
from foragent.paths import repo_path
from foragent.skills import SkillManager
from foragent.state import StateStore
from foragent.ownership import OWNED_MARKER, OwnershipError, effective_orderer, label_description, owner_label, parse_orderer
from foragent.tracker import IdentityError, Issue, LabelSpec, Tracker, TrackerError, pr_issue_number


log = logging.getLogger(__name__)


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


PR_HISTORY = 30


def _pull_requests(tracker: Tracker) -> list[dict]:
    """Open PRs and the latest merged ones, so planning sees what was done and by whom (#848)."""
    merged = sorted(tracker.list_prs(state="merged"), key=lambda pr: (pr.merged_at or "", pr.number))[-PR_HISTORY:]
    return [{"number": pr.number, "title": pr.title, "head": pr.head, "state": state,
             "merged_at": pr.merged_at, "foragent": pr_issue_number(pr.head) is not None}
            for state, prs in (("open", tracker.list_prs(state="open")), ("merged", merged[::-1]))
            for pr in prs]


def context(root: Path, config: Config, tracker: Tracker) -> dict:
    issues = sorted((issue for issue in tracker.list_open_issues()
                     if issue.state == "open" and not issue.is_pull_request), key=lambda issue: issue.number)
    login, identity = None, {}
    try:
        login = ownership.normalize_login(tracker.authenticated_login(), config.language)
    except IdentityError as exc:
        identity = {"identity": {"login": None, "error": text(
            "identity_not_a_person", config.language, login=exc.login, kind=exc.kind)}}
    except (TrackerError, OwnershipError) as exc:
        identity = {"identity": {"login": None, "error": text(
            "init_identity_failed", config.language, error=exc)}}
    feedback, decisions, other_orders, owner_errors = [], [], [], {}
    for issue in issues:
        owner, ours = None, False
        try:
            # Keep legacy questions visible on identity failure without inventing an owner.
            # An unconfirmed owner-label publication is not legacy work.
            legacy = (login is None and ownership.parse_orderer(issue.labels, config.language) is None
                      and OWNED_MARKER not in issue.body)
            if not (legacy and config.legacy_owner is None):
                owner = ownership.effective_orderer(issue, config, login)
            ours = legacy or (login is not None and owner == login)
        except OwnershipError as exc:
            owner_errors[issue.number] = {"owner_error": str(exc)}
        if owner is not None and not ours:
            other_orders.append({"number": issue.number, "title": issue.title, "orderer": owner,
                                 "files": parse_body(issue.body).files})
        if "feedback" in issue.labels:
            about = re.search(r"(?:^|\n)About #(\d+)\.", issue.body)
            feedback.append({"number": issue.number, "title": issue.title, "body": issue.body,
                             "about": int(about[1]) if about else None})
        if ours and "needs-human" in issue.labels:
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
    # D95: .foragent.yml is the lanes' only skills source; agent frontmatter is a projection of it.
    lanes = {name: {"paths": lane.get("paths", []), "agent": lane.get("agent"),
                    "skills": lane.get("skills", [])} for name, lane in config.lanes.items()}
    return {
        **identity,
        "language": config.language, "lanes": lanes, "tiers": config.tiers,
        "interview": _interview_context(config),
        "profile": config.profile,
        "ui": design.ui_context(root),
        "acceptance_dir": config.acceptance_dir, "base_branch": config.base_branch,
        "feedback": feedback, "decisions": decisions, "other_orders": other_orders,
        "open_issues": [{"number": issue.number, "title": issue.title, "labels": sorted(issue.labels),
                         "lane": issue_lane(issue.labels, config.lanes), "size": issue_size(issue.labels),
                         "state_group": state_group(issue, tracker),
                         "waiting": order_waiting.section(issue.body),
                         **owner_errors.get(issue.number, {})} for issue in issues],
        "pull_requests": _pull_requests(tracker),
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


def validate_plan(plan: dict, root: Path, config: Config, *,
                  retained_files: dict[str, str] | None = None) -> dict[str, str | bytes]:
    """Every file the order publishes: text as str, reference images as decoded bytes."""
    _object(plan, {"id", "title", "summary", "spec_markdown", "issues"},
            {"new_lanes", "edits", "reference_files"}, "plan")
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
    references = order_references.validate(plan, root)
    acceptance = repo_path(root, config.acceptance_dir)
    keys, edits = set(), set()
    for kind in ("issues", "edits"):
        for index, item in enumerate(plan.get(kind, [])):
            label = f"{kind}[{index}]"
            required = {"goal", "out_of_scope", "done_when"}
            required |= {"key", "title", "lane", "size"} if kind == "issues" else {"number"}
            _object(item, required, {"checklist", "acceptance_files", "blocked_by", "files", "exclusive", "interactive",
                                     "references", "statements", "decisions"} if kind == "issues"
                    else {"checklist", "acceptance_files", "blocked_by", "files", "exclusive", "references",
                          "statements", "decisions"}, label)
            if "decisions" in item:
                try:
                    decision_links.validate(item["decisions"], f"{label}.decisions")
                except ValueError as exc:
                    raise PlanError(str(exc)) from exc
            if "statements" in item:
                _strings(item["statements"], f"{label}.statements", nonempty=True)
                if any("\n" in line or "\r" in line for line in item["statements"]):
                    raise PlanError(f"{label}.statements items must each be a single line")
            _text(item["goal"], f"{label}.goal")
            _text(item["out_of_scope"], f"{label}.out_of_scope")
            _strings(item["done_when"], f"{label}.done_when", nonempty=True)
            validate_paths(item, root, label)
            order_references.validate_selection(plan, item, label)
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
                _strings(item.get("blocked_by", []), f"{label}.blocked_by")
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
            body = issue_body(item, "owner/repo", config.base_branch,
                              references=order_references.selected(plan, item))
            if parse_body(body).done_when_commands != item["done_when"]:
                raise PlanError(f"{label}.done_when cannot contain Markdown code fences or trailing newlines")
    for kind in ("issues", "edits"):
        for item in plan.get(kind, []):
            for name in acceptance_paths(item["done_when"], config.acceptance_dir):
                if name in files:
                    continue
                if retained_files is not None and name in retained_files:
                    files[name] = _text(retained_files[name], f"retained_files[{name}]", empty=True)
                    continue
                if not repo_path(root, name).is_file():
                    # A statement issue's tests are written in preparation; publication refuses
                    # it until they are reviewed, so a draft may name tests not written yet.
                    if "statements" in item:
                        continue
                    raise PlanError(f"Done when references missing acceptance file: {name}")
                files[name] = repo_path(root, name).read_bytes().decode("utf-8")
    for name, content in references.items():
        if name in files and files[name] != content:
            raise PlanError(f"conflicting file contents: {name}")
        files[name] = content
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
    # An edit is no plan key, so nothing depends on it and it closes no cycle.
    for item in plan.get("edits", []):
        for key in item.get("blocked_by", []):
            if key not in keys:
                raise PlanError(f"unknown blocked_by key: {key}")
    check_destinations(root, files)
    return files



def _plan_marker(identity: str, mapping: dict[str, int], acceptance: dict[str, dict[str, str]]) -> str:
    return (f"<!-- foragent:plan id={identity} map={json.dumps(mapping, sort_keys=True)} "
            f"acceptance={json.dumps(acceptance, sort_keys=True)} -->")


_EDIT_MARKER = "<!-- foragent:edit plan="


def _child_marker(identity: str, key: str) -> str:
    return f"<!-- foragent:plan-child id={identity} key={key} -->"


def _published_body(plan: dict, item: dict, repo: str, config: Config) -> str:
    """The issue body publish writes for a plan child or edit, before any plan-child marker."""
    return issue_body(item, repo, config.base_branch, config.acceptance_dir, order_references.selected(plan, item))


@dataclass
class PublishReport:
    title: str
    parent: int | None
    rows: list[dict]
    paths: list[str]
    dry_run: bool
    # Approved issues outside this order whose locked acceptance bytes it would change (#1107).
    affected: dict[int, list[str]] = field(default_factory=dict)
    language: str = "en"
    waiting_for: str | None = None

    def summary(self) -> str:
        if self.waiting_for is not None:
            return text("order_submission_waiting", self.language, id=self.title, boundary=self.waiting_for)
        lines = [f"{'Plan (dry run)' if self.dry_run else 'Published'}: {self.title}"]
        if self.parent is not None:
            lines.append(f"Parent: #{self.parent}")
        lines.append("Files: " + ", ".join(self.paths))
        if self.affected:
            lines += ["", text("order_locks_heading", self.language), *_owner_lines(self.affected)]
        for lane in sorted({row["lane"] for row in self.rows}, key=lane_order):
            lines += [f"\nlane:{lane}", "| Number | Title | Size | Blocked by | Approved? | Interactive? |", "| --- | --- | --- | --- | --- | --- |"]
            for row in self.rows:
                if row["lane"] == lane:
                    title = row["title"].replace("|", "\\|").replace("\n", " ")
                    blockers = ", ".join(map(str, row["blocked_by"])) or "—"
                    lines.append(f"| {row['number']} | {title} | {row['size']} | {blockers} | {'yes' if row['approved'] else 'no'} | {'yes' if row['interactive'] else 'no'} |")
        return "\n".join(lines)


def _publish_files(plan: dict, root: Path, config: Config, git: GitRepo, files: dict[str, str | bytes],
                   manager: SkillManager | None) -> dict[str, str]:
    """Commit only this order's own paths on the latest base and push them."""
    # Destinations are checked here, after the base refresh, so upstream changes count.
    spec = f"docs/specs/{plan['id']}.md"
    references = set(plan.get("reference_files", {}))
    roots = {name: "docs/specs" if name == spec else "docs/design" if name in references
             else config.acceptance_dir for name in files}
    check_destinations(root, files)
    for name in files:
        check_write(root, name, roots[name])
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
        data = content.encode("utf-8") if isinstance(content, str) else content
        path = repo_path(root, name)
        if not path.exists() or path.read_bytes() != data:
            # Parents are created one real directory at a time, never through a symlink.
            check_write(root, name, roots[name])
            order_references.real_directory(root, PurePosixPath(name).parent.as_posix())
            check_write(root, name, roots[name])
            path.write_bytes(data)
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


def _order_owner(tracker: Tracker, config: Config, parent, children: list) -> str:
    """The person this order's new issues belong to; republication never transfers it.

    An existing order keeps its parent's owner (the effective legacy owner for an
    unlabelled parent). A new order belongs to the authenticated publisher. Every
    existing issue of the order must have at most one owner label.
    """
    language = config.language
    try:
        login = tracker.authenticated_login()
    except IdentityError as exc:
        raise PlanError(text("identity_not_a_person", language, login=exc.login, kind=exc.kind)) from exc
    except TrackerError as exc:
        raise PlanError(text("identity_unavailable", language, error=" ".join(str(exc).split())[:300])) from exc
    try:
        for issue in children:
            parse_orderer(issue.labels, language)
        return effective_orderer(parent, config, login) if parent is not None else parse_orderer(
            [owner_label(login)], language)
    except OwnershipError as exc:
        raise PlanError(str(exc)) from exc


_MARKER_PREFIX = "<!-- foragent:"
# A retired issue keeps its text for the record, but no Foragent marker matches it.
_RETIRED_PREFIX = "<!-- foragent-refused:"


def _labelled(issue) -> bool:
    return any(label.lower().startswith(ownership.PREFIX) for label in issue.labels)


def _retired_body(body: str) -> str:
    """``body`` with its publication markers retired and its owner-label contract kept.

    The plan, child and v1 markers stop matching, so nothing finds or works it as
    order content; ``OWNED_MARKER`` stays, so an unlabelled issue still has no owner
    (``ownership.effective_orderer``) instead of turning into legacy work.
    """
    retired_contract = OWNED_MARKER.replace(_MARKER_PREFIX, _RETIRED_PREFIX)
    return body.replace(_MARKER_PREFIX, _RETIRED_PREFIX).replace(retired_contract, OWNED_MARKER)


def _with_contract(body: str, existing_body: str) -> str:
    """Rewritten ``body`` that keeps the owner-label contract of the issue it replaces."""
    return f"{body}{OWNED_MARKER}\n" if OWNED_MARKER in existing_body and OWNED_MARKER not in body else body


def _retire(tracker: Tracker, config: Config, issue, label: str) -> None:
    """Close an unconfirmed issue and retire its markers, each step on its own.

    Closing comes first: a closed issue is never work. Retiring the markers is
    attempted even when closing fails, and the other way round. Whatever still fails
    is harmless: discovery never reuses an unconfirmed issue (``_unconfirmed``), and
    the next publication of the order retires it again.
    """
    if issue.state == "open":
        try:
            tracker.close_issue(issue.number, text("identity_closed_unlabelled", config.language, label=label))
        except Exception:
            log.warning("could not close unconfirmed issue #%s", issue.number, exc_info=True)
    retired = _retired_body(issue.body)
    if retired != issue.body:
        try:
            tracker.update_issue(issue.number, title=issue.title, body=retired)
        except Exception:
            log.warning("could not retire the markers of unconfirmed issue #%s", issue.number, exc_info=True)


def _unconfirmed(issue, mapped: set[int]) -> bool:
    """Created under the owner-label contract, yet unlabelled and not part of its order.

    Only bodies written by this publisher carry ``OWNED_MARKER``; legacy issues
    (published before owner labels) never do, so they keep being reused and
    belong to ``legacy_owner``. A labelled issue, or one already in its parent's
    map, is a confirmed member of the order.
    """
    return OWNED_MARKER in issue.body and not _labelled(issue) and issue.number not in mapped


def _create_owned(tracker: Tracker, config: Config, owner: str, title: str, body: str, labels: list[str]) -> int:
    """Create an issue carrying ``owner``'s label, or refuse it and stop publication.

    GitHub silently drops labels a creator may not apply, so the label is read back.
    Only an issue observed without its label is retired, and publication stops. When
    the read itself fails, the outcome is unknown and nothing is touched: a labelled
    issue keeps its live markers, so a retry reuses it under its original owner; an
    unlabelled one carries ``OWNED_MARKER`` without the label, so it is nobody's work
    (``ownership.effective_orderer``), discovery never reuses it (``_unconfirmed``),
    and a retry retires it. The same holds when every rollback step fails.
    The issue is created in one request so its body has no edit that would
    postdate an approval added in the same publication.
    """
    label = owner_label(owner)
    number = tracker.create_issue(title, body, [label, *labels])
    confirmed = label in tracker.get_issue(number).labels
    if not confirmed:
        _retire(tracker, config, Issue(number, title, body, frozenset(), "open"), label)
        raise PlanError(text("identity_label_missing", config.language, number=number, label=label))
    return number


def _reconcile_dependencies(tracker: Tracker, number: int, desired: set[int]) -> None:
    """Invalidate approval before changing native edges, including restoring hand removals."""
    existing = {dep.number for dep in tracker.blocked_by(number)}
    if existing != desired:
        # Approval covers the dependency contract; ready must be reconsidered by the gate.
        # Do this before either mutation so an interrupted sync cannot leave stale readiness.
        tracker.remove_labels(number, ["approved", "ready"])
    for blocker in sorted(existing - desired):
        tracker.remove_blocked_by(number, blocker)
    for blocker in sorted(desired - existing):
        tracker.add_blocked_by(number, blocker)


@dataclass
class _Publication:
    files: dict[str, str | bytes]
    keys: set[str]
    selected_keys: set[str]
    selected_numbers: set[int]
    parent: int | None
    mapping: dict[str, int]
    saved_blobs: dict[str, dict[str, str]]
    by_number: dict[int, Issue]
    edit_numbers: set[int]
    unconfirmed: list[Issue]
    rows: list[dict]
    owner: str | None
    reviewed_bases: dict[str, dict]

    @property
    def own(self) -> set[int]:
        # Dropped children keep map entries, but this publication does not update them.
        return {number for key, number in self.mapping.items() if key in self.keys} | self.edit_numbers


def _require_route(plan: dict, config: Config, repo: str, mapping: dict[str, int], by_number: dict) -> None:
    targets = {**mapping, **{f"#{item['number']}": item["number"] for item in plan.get("edits", [])}}
    order_reviewed.require_route_kept(
        plan, config, repo, {key: by_number[number].body for key, number in targets.items() if number in by_number},
        "publish", numbers=targets, listed=by_number.values())


def _publication_checks(plan: dict, root: Path, config: Config, tracker: Tracker, *, repo: str,
            manager: SkillManager | None = None,
            approve: bool = False, approve_keys: list[str] | None = None,
            approve_numbers: list[int] | None = None, dry_run: bool = False,
            check_owner: bool = True, retained_files: dict[str, str] | None = None,
            git: GitRepo | None = None, refreshed: bool = False) -> "_Publication":
    try:
        files = (validate_plan(plan, root, config) if retained_files is None else
                 validate_plan(plan, root, config, retained_files=retained_files))
    except ValueError as exc:
        raise PlanError(str(exc)) from exc
    if dry_run:
        # A dry run only previews: read GitHub without recording any observation locally.
        from foragent.tracker import RecordingTracker

        tracker = tracker._tracker if isinstance(tracker, RecordingTracker) else tracker
    keys = {item["key"] for item in plan["issues"]}
    selected_keys, selected_numbers = set(approve_keys or []), set(approve_numbers or [])
    if selected_keys - keys:
        raise PlanError(f"unknown approve keys: {', '.join(sorted(selected_keys - keys))}")
    all_issues = tracker.list_issues()
    pattern = re.compile(r"<!-- foragent:plan id=" + re.escape(plan["id"])
                         + r" map=(\{[^\n]*?\})(?: acceptance=(\{[^\n]*\}))? -->")
    parents = [(issue, pattern.search(issue.body)) for issue in all_issues if pattern.search(issue.body)]
    # A parent is part of its order once it records a child; until then, like a child
    # missing from the map, an unlabelled owner-contract parent is an unconfirmed creation.
    unconfirmed = [issue for issue, marker in parents if marker[1] == "{}" and _unconfirmed(issue, set())]
    parents = [(issue, marker) for issue, marker in parents if issue not in unconfirmed]
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
    mapped = set(mapping.values())
    for item in plan["issues"]:
        found = [issue for issue in all_issues if _child_marker(plan["id"], item["key"]) in issue.body]
        unconfirmed += [issue for issue in found if _unconfirmed(issue, mapped)]
        matches = [issue.number for issue in found if not _unconfirmed(issue, mapped)]
        if len(matches) > 1:
            raise PlanError(f"duplicate children for key {item['key']}")
        if item["key"] not in mapping and matches:
            mapping[item["key"]] = matches[0]
    # An earlier refusal may have retired an issue's markers but failed to close it.
    retired = (f"{_RETIRED_PREFIX}plan id={plan['id']} map=",
               *(_child_marker(plan["id"], item["key"]).replace(_MARKER_PREFIX, _RETIRED_PREFIX)
                 for item in plan["issues"]))
    unconfirmed += [issue for issue in all_issues
                    if issue.state == "open" and any(marker in issue.body for marker in retired)]
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
                     "lane": issue_lane(issue.labels, config.lanes), "blocked_by": ([mapping.get(key, key) for key in item["blocked_by"]] if "blocked_by" in item
                                    else [dep.number for dep in tracker.blocked_by(issue.number)]),
                     "approved": approve or issue.number in selected_numbers})
    if dry_run:
        return _Publication(files, keys, selected_keys, selected_numbers, parent, mapping, saved_blobs,
                            by_number, edit_numbers, unconfirmed, rows, None, {})
    # Statement issues need the reviewed evidence preparation recorded, approved or not; the
    # tested base is checked again under the publication lock, before anything is written.
    _require_route(plan, config, repo, mapping, by_number)
    # The evidence is checked against ``files``, the bytes read once above and written below.
    reviewed_bases = require_reviewed_tests(
        plan, root, config, repo, order_reviewed.statement_items(plan, by_number, config), files=files) or {}
    # A selected number not mapped yet is a child created now; its number is unknown
    # until creation, so every child still to be created needs a clean preread.
    # Statement issues take the reviewed-tests route instead of preread.
    new_numbers = bool(selected_numbers - edit_numbers - set(mapping.values()))
    require_clean_preread(plan, root, config, [
        *(item for item in plan["issues"]
          if "statements" not in item
          and (approve or item["key"] in selected_keys or mapping.get(item["key"]) in selected_numbers
               or (new_numbers and item["key"] not in mapping))),
        *(edit_view(item, by_number[item["number"]], config) for item in plan.get("edits", [])
          if "statements" not in item and (approve or item["number"] in selected_numbers)),
    ])
    if reviewed_bases:
        git = git or GitRepo(root)
        order_reviewed.require_reviewed_bytes(git, config, order_reviewed.statement_items(plan, by_number, config),
                                              reviewed_bases, files)
        order_reviewed.require_tested_base(git, config, reviewed_bases,
                                           order_reviewed.landing(git, config.base_branch, refreshed=refreshed))
    owner = (_order_owner(tracker, config, by_number.get(parent) if parent is not None else None,
                          [by_number[n] for n in mapping.values()]) if check_owner else None)
    return _Publication(files, keys, selected_keys, selected_numbers, parent, mapping, saved_blobs,
                        by_number, edit_numbers, unconfirmed, rows, owner, reviewed_bases)


def publish(plan: dict, root: Path, config: Config, tracker: Tracker, *, repo: str,
            git: GitRepo | None = None, manager: SkillManager | None = None,
            approve: bool = False, approve_keys: list[str] | None = None,
            approve_numbers: list[int] | None = None, dry_run: bool = False,
            defer: str | None = None, urgent: bool = False) -> PublishReport:
    waiting = defer is not None and not urgent and not plan.get("new_lanes")
    publication = _publication_checks(plan, root, config, tracker, repo=repo, manager=manager,
                                      approve=approve, approve_keys=approve_keys,
                                      approve_numbers=approve_numbers, dry_run=dry_run,
                                      check_owner=not waiting, git=git)
    affected = _affected_owners(publication.by_number.values(), publication.own, publication.files,
                                repo, config, lambda: git or GitRepo(root), fetch=True)
    if dry_run:
        return PublishReport(plan["title"], publication.parent, publication.rows,
                             sorted(publication.files), True, affected=affected, language=config.language)
    if affected:
        raise PlanError(text("order_locks_refused", config.language, owners="\n".join(_owner_lines(affected))))
    if waiting:
        from foragent import order_submissions
        order_submissions.retain(plan, repo, defer, approve=approve, approve_keys=approve_keys,
                                 approve_numbers=approve_numbers, language=config.language,
                                 files=publication.files)
        return PublishReport(plan["title"], publication.parent, publication.rows,
                             sorted(publication.files), False, waiting_for=defer, language=config.language)
    git = git or GitRepo(root)
    if git.current_branch() != config.base_branch:
        raise GitError(f"order publish must run on the base branch {config.base_branch!r}")
    owner = publication.owner
    with git.publish_lock():
        git.update_base(config.base_branch)
        # A preparation that started or finished since the first checks is visible now, and none can
        # start before this publication is pushed: it claims its issues under this lock.
        listed = tracker.list_issues()
        current = {**publication.by_number, **{issue.number: issue for issue in listed}}
        _require_route(plan, config, repo, publication.mapping, current)
        items = order_reviewed.statement_items(plan, current, config)
        reviewed = require_reviewed_tests(plan, root, config, repo, items, files=publication.files) or {}
        if reviewed:
            order_reviewed.require_reviewed_bytes(git, config, items, reviewed, publication.files)
            # Refreshed HEAD is the actual landing commit, including local commits.
            order_reviewed.require_tested_base(git, config, reviewed,
                                               order_reviewed.landing(git, config.base_branch, refreshed=True))
        affected = _affected_owners(listed, publication.own, publication.files,
                                    repo, config, lambda: git, fetch=False)
        if affected:
            raise PlanError(text("order_locks_refused", config.language,
                                 owners="\n".join(_owner_lines(affected))))
        tracker.ensure_labels([LabelSpec(owner_label(owner), ownership.LABEL_COLOR, label_description(owner))])
        published_blobs = _publish_files(plan, root, config, git, publication.files, manager)
    return _publish_issues(plan, root, config, tracker, repo=repo, publication=publication,
                           published_blobs=published_blobs, approve=approve)


def _publish_issues(plan: dict, root: Path, config: Config, tracker: Tracker, *, repo: str,
                    publication: _Publication, published_blobs: dict[str, str],
                    approve: bool = False) -> PublishReport:
    """Apply existing issue/approval controls using files from an already published commit."""
    files, keys = publication.files, publication.keys
    selected_keys, selected_numbers = publication.selected_keys, publication.selected_numbers
    parent, mapping, saved_blobs = publication.parent, publication.mapping, publication.saved_blobs
    by_number, edit_numbers = publication.by_number, publication.edit_numbers
    unconfirmed, rows, owner = publication.unconfirmed, publication.rows, publication.owner
    assert owner is not None
    for issue in unconfirmed:
        _retire(tracker, config, issue, owner_label(owner))
    lane_names = {item["lane"] for item in plan["issues"]} - {"main"}
    tracker.ensure_labels([*LABELS, *(LabelSpec(f"lane:{name}", "ededed", f"Foragent lane {name}.") for name in sorted(lane_names))])
    if any(item.get("interactive", False) for item in plan["issues"]):
        tracker.ensure_labels([LabelSpec("interactive", "d4c5f9", "Work that needs substantial back-and-forth with the user.")])

    recorded_blobs = saved_blobs.copy()

    def acceptance_blobs(item: dict) -> dict[str, str]:
        # Reference files an issue relies on revoke its approval when they change, like its tests.
        referenced = (set(item.get("acceptance_files", {})) | set(acceptance_paths(item["done_when"], config.acceptance_dir))
                      | set(order_references.selected(plan, item)))
        return {name: published_blobs[name] for name in sorted(referenced)}

    def parent_body() -> str:
        children = [tracker.get_issue(n) for n in mapping.values()]
        checklist = "\n".join(f"- [{'x' if issue.state == 'closed' else ' '}] #{issue.number} {issue.title}" for issue in children)
        owned = f"{OWNED_MARKER}\n" if parent_owned else ""
        return f"{plan['summary']}\n\n{checklist}\n\n{_plan_marker(plan['id'], mapping, recorded_blobs)}\n{owned}"

    # An order with no sub-issues (edits or approvals only) is finished once it
    # applies: publish closes its record below, so it never gets a Backlog card.
    childless = not plan["issues"] and not mapping
    parent_owned = parent is None or OWNED_MARKER in by_number[parent].body
    if parent is None:
        parent = _create_owned(tracker, config, owner, plan["title"], parent_body(), [])
        linked: set[int] = set()
    else:
        tracker.update_issue(parent, title=plan["title"], body=parent_body())
        # GitHub rejects re-adding an existing sub-issue, so read the links once.
        linked = {issue.number for issue in tracker.sub_issues(parent)}
    if not childless:
        board.add_issue(tracker, repo, config.board, parent, board_map=config.board_map)
    for item in plan["issues"]:
        number = mapping.get(item["key"])
        existing = tracker.get_issue(number) if number is not None else None
        owned = existing is None or OWNED_MARKER in existing.body
        body = (_published_body(plan, item, repo, config) + "\n" + _child_marker(plan["id"], item["key"]) + "\n"
                + (f"{OWNED_MARKER}\n" if owned else ""))
        # The written body itself records the wait it replaces, so the transition survives a lost comment.
        body = order_waiting.stamp(existing.body if existing else None, body)
        labels = [f"size:{item['size']}", *([f"lane:{item['lane']}"] if item["lane"] != "main" else [])]
        if item.get("interactive", False):
            labels.append("interactive")
        blobs = acceptance_blobs(item)
        body = decision_links.with_marker(body, item.get("decisions"), existing.body if existing else None)
        if number is None:
            number = _create_owned(tracker, config, owner, item["title"], body, labels)
            mapping[item["key"]] = number
            order_waiting.announce(tracker, number, body, config.language)
            recorded_blobs[str(number)] = blobs
            # Persist progress before native edge calls so retries reuse the child.
            tracker.update_issue(parent, title=plan["title"], body=parent_body())
        else:
            if (existing.body != body or saved_blobs.get(str(number), {}) != blobs
                    or ("interactive" in existing.labels) != item.get("interactive", False)):
                tracker.remove_labels(number, ["approved", "ready"])
            tracker.update_issue(number, title=item["title"], body=body)
            # A record follows the fact it describes: announce the wait the body now holds.
            order_waiting.announce(tracker, number, body, config.language)
            tracker.remove_labels(number, [label for label in existing.labels
                                           if (label.startswith(("lane:", "size:")) or label == "interactive")
                                           and label not in labels])
            tracker.add_labels(number, labels)
            recorded_blobs[str(number)] = blobs
        if number not in linked:
            tracker.add_sub_issue(parent, number)
            linked.add(number)
        board.add_issue(tracker, repo, config.board, number, item["lane"], item["size"], config.board_map)
    for item in plan["issues"]:
        number = mapping[item["key"]]
        _reconcile_dependencies(tracker, number, {mapping[key] for key in item.get("blocked_by", [])})
    for item in plan.get("edits", []):
        number = item["number"]
        existing = tracker.get_issue(number)
        # An edit keeps both the owner contract and omitted decision links.
        body = decision_links.with_marker(_with_contract(_published_body(plan, item, repo, config), existing.body),
                                          item.get("decisions"), existing.body)
        body = order_waiting.stamp(existing.body, body)
        # A revision comment identifies this write, including a changed decision marker.
        body = order_waiting.mark_write(existing.body, body)
        blobs = acceptance_blobs(item)
        revision = body_hash(body + json.dumps({
            name: files[name] if isinstance(files[name], str) else hashlib.sha256(files[name]).hexdigest()
            for name in sorted(blobs)}, sort_keys=True)
            # Dependencies are part of what was approved; absent, the existing edges stay.
            + (json.dumps(item["blocked_by"]) if "blocked_by" in item else ""))
        comment = ("<!-- foragent:edited -->\nUpdated by order from your decision; needs approval.\n\n"
                   f"<!-- foragent:edit plan={plan['id']} sha256={revision} -->")
        # Only the latest edit revision is what the issue stands approved for: an older
        # matching revision (A, then B, then A again) does not keep B's approval.
        latest = max((c for c in tracker.list_comments(number) if _EDIT_MARKER in c.body),
                     key=comment_key, default=None)
        current = latest is not None and latest.body == comment
        changed = (existing.body != body or not current or "needs-human" in existing.labels
                   or saved_blobs.get(str(number), {}) != blobs)
        if changed:
            tracker.remove_labels(number, ["needs-human", "approved", "ready"])
            tracker.update_issue(number, title=existing.title, body=body)
        # Records follow the facts they describe: the revision comment covers the body and
        # the dependencies, and the waiting line the body's wait, so each comes after them.
        if "blocked_by" in item:
            _reconcile_dependencies(tracker, number, {mapping[key] for key in item["blocked_by"]})
        order_waiting.announce(tracker, number, body, config.language)
        if changed and not current:
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
    if childless and (parent not in by_number or by_number[parent].state == "open"):
        tracker.close_issue(parent, text("order_record_closed", config.language, count=0))
    for item, row in zip([*plan["issues"], *plan.get("edits", [])], rows):
        row["number"] = mapping[item["key"]] if "key" in item else item["number"]
        if "key" in item or "blocked_by" in item:
            row["blocked_by"] = [mapping[key] for key in item.get("blocked_by", [])]
    for row in rows:
        row["approved"] = "approved" in tracker.get_issue(row["number"]).labels
    return PublishReport(plan["title"], parent, rows, sorted(files), False)


PREREAD_FILE = ".foragent/preread.json"
PREREAD_TIMEOUT_S = 15 * 60


def edit_view(item: dict, issue, config: Config) -> dict:
    """An edit read as the issue its implementer will receive."""
    return {**item, "key": f"#{item['number']}", "title": issue.title,
            "lane": issue_lane(issue.labels, config.lanes), "size": issue_size(issue.labels),
            "interactive": "interactive" in issue.labels, "edit": True}


def preread_prompt(plan: dict, item: dict, root: Path | None = None, reader: Path | None = None) -> str:
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
    # Selected reference text inline; images as absolute paths into the preread snapshot.
    references = order_references.preread_section(plan, item, root, reader) if root is not None else ""
    return (
        "You will implement the issue below later. Do not implement it now and do not change any file.\n"
        "Your lane owns HOW: structure, names, values and internal shapes are yours to decide.\n"
        "Read the issue and the repository read-only, then reply with lines of three kinds only:\n"
        "APPROACH: <one line of your plan> (at most 8 lines).\n"
        "DECIDE: <a reversible detail you will settle yourself and follow>.\n"
        "QUESTION: <an unknown, only when the WHAT is unclear: a wrong guess would fail a locked test, break a\n"
        "contract with another issue, or change behaviour the person notices>.\n"
        "If you have nothing to add, reply with exactly NONE.\n\n"
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
    ) + (f"\n{references}" if references else "")


def preread_target(plan: dict, item: dict, config: Config, root: Path, reader: Path | None = None):
    """The implementer the runner would start for this issue, its prompt and the evidence key.

    Executor, model, lane agent, lane notes and skills come from the same function the
    runner's first implementation attempt uses, so preread cannot differ from it.
    With a reader directory the prompt points images there; the digest always uses the
    reader-free prompt, so each reader's own directory never changes the evidence key.
    """
    from foragent.run_implementation import implementer_setup, lane_config

    setup = implementer_setup(lane_config(config, item["lane"]), item["lane"], item["size"], root)
    prompt = setup.notes + preread_prompt(plan, item, root) + setup.memory_notice
    sent = prompt if reader is None else setup.notes + preread_prompt(plan, item, root, reader) + setup.memory_notice
    # The whole plan is part of the key: any change to any issue needs a new preread.
    digest = hashlib.sha256(json.dumps(
        [setup.executor, setup.model, setup.options, prompt, plan], ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")).hexdigest()
    return setup, sent, digest


def _preread_records(root: Path) -> dict:
    try:
        records = json.loads((root / PREREAD_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise PlanError(f"{PREREAD_FILE} is not valid JSON; delete it and run fa order preread again") from exc
    return records if isinstance(records, dict) else {}


def _record_preread(root: Path, directory: Path, digest: str, record: dict) -> None:
    """Merge one answer into the records under a lock, so concurrent prereads keep every answer.

    The lock covers only re-reading, adding and replacing, never an agent turn. Each write
    uses its own exclusively created temporary file, so no other writer can remove or
    overwrite it before it replaces the records. Nothing here follows a symlink.
    """
    flags = os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    path = directory / "preread.json.lock"
    if path.is_symlink():
        raise PlanError(f"{path} must be a regular file, not a symlink")
    lock = os.open(path, os.O_RDWR | os.O_CREAT | flags, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = _preread_records(root)
        # Re-inserting moves this answer to the end: file order is write order (see lane_proposal).
        records.pop(digest, None)
        records[digest] = record
        while True:
            temporary = directory / f"preread.json.{secrets.token_hex(8)}.tmp"
            try:
                os.close(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | flags, 0o600))
                break
            except FileExistsError:
                continue
        try:
            temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, directory / "preread.json")
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    finally:
        os.close(lock)  # closing releases the lock


def _preread_lines(message: str) -> list[str]:
    if message.strip() == "NONE":
        return []
    lines = (re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", line.strip()) for line in message.splitlines())
    return [line for line in lines if line and line != "NONE"]


_TAG = re.compile(r"(?i)^(APPROACH|DECIDE|QUESTION):\s*")


def _unknowns(message: str) -> list[str]:
    """What blocks approval. Once any line carries a tag, only QUESTION lines do and the
    unprefixed lines are notes; an all-plain reply keeps every line as an unknown."""
    lines = _preread_lines(message)
    if not any(_TAG.match(line) for line in lines):
        return lines
    return _tagged(message, "QUESTION")


def _tagged(message: str, tag: str) -> list[str]:
    tagged = []
    for line in _preread_lines(message):
        match = _TAG.match(line)
        if match and match.group(1).upper() == tag:
            tagged.append(line[match.end():])
    return tagged


def _decisions(message: str) -> list[str]:
    return _tagged(message, "DECIDE")


def _approach(message: str) -> list[str]:
    return _tagged(message, "APPROACH")


_CHILD_LINE = re.compile(r"<!-- foragent:plan-child id=(\S+) key=(\S+) -->")


def _body_digest(body: str) -> str:
    """Every authored byte of an issue body counts; only line endings and outer blank space do not."""
    return hashlib.sha256(body.replace("\r\n", "\n").strip().encode("utf-8")).hexdigest()


def _without_child_marker(body: str) -> tuple[str, re.Match | None]:
    """Split off the lines publish appends after the rendered body, nothing else.

    Those are the plan-child marker (children) and the owner-label contract line
    (``OWNED_MARKER``, last; children and edits of contract issues).
    """
    text = body.replace("\r\n", "\n").rstrip()
    head, _, last = text.rpartition("\n")
    if last == OWNED_MARKER:
        text = head.rstrip()
        head, _, last = text.rpartition("\n")
    marker = _CHILD_LINE.fullmatch(last)
    return (head if marker else text), marker


_ANY_PLAN_ACCEPTANCE = re.compile(r"<!-- foragent:plan id=\S+ map=\{[^\n]*?\} acceptance=(\{[^\n]*\}) -->")


def _under_lock(name: str, locks: set[str]) -> bool:
    """Whether the gate's lock on one of ``locks`` (a file or a directory) covers ``name``."""
    return any(name == lock or name.startswith(lock + "/") for lock in locks)


def _owner_lines(affected: dict[int, list[str]]) -> list[str]:
    return [f"#{number}: {', '.join(paths)}" for number, paths in affected.items()]


def _affected_owners(issues, own: set[int], files: dict[str, str | bytes], repo: str, config: Config,
                     base_git: Callable[[], GitRepo], *, fetch: bool) -> dict[int, list[str]]:
    """Approved owners outside ``own`` affected by the complete prospective push.

    Locks come from Done when (on every clone), parent maps and local snapshots.
    Compare the fetched base with all unpushed changes plus the plan's final bytes;
    publication pushes existing commits too. Fetch before discovering overlap, since
    a stale tracking ref cannot tell us which commits remain unpublished.
    """
    # Every gate, on any clone, locks the Done-when acceptance paths of the issue body
    # (``scheduler_approval._snapshot``), whether or not an order recorded them.
    locked: dict[int, set[str]] = {
        issue.number: set(acceptance_paths(parse_body(issue.body).done_when_commands, config.acceptance_dir))
        for issue in issues if issue.state == "open" and "approved" in issue.labels and issue.number not in own}
    if not locked:
        return {}
    for issue in issues:
        for match in _ANY_PLAN_ACCEPTANCE.finditer(issue.body):
            try:
                acceptance = json.loads(match[1])
            except ValueError:
                continue
            for number, blobs in (acceptance.items() if isinstance(acceptance, dict) else ()):
                if number.isdecimal() and int(number) in locked and isinstance(blobs, dict):
                    locked[int(number)].update(path for path in blobs if isinstance(path, str))
    store = StateStore.for_repo(repo)
    for number, paths in locked.items():
        if (approval := store.load_approval(number)) is not None:
            paths.update(approval.acceptance_blobs)
    if not any(locked.values()):
        return {}
    git = base_git()
    if fetch:
        git.fetch("origin", config.base_branch)
    base = f"origin/{config.base_branch}"
    # committed_paths excludes remote-only history when this clone is behind, and
    # includes deletions. Inspect final HEAD bytes, so restored files are unchanged.
    pending = git.committed_paths(base)
    overlap = {number: covered for number, paths in sorted(locked.items())
               if (covered := sorted(name for name in pending | files.keys() if _under_lock(name, paths)))}
    candidates = sorted({path for paths in overlap.values() for path in paths})
    if not candidates:
        return {}
    current = git.blob_shas(base, candidates)
    proposed = git.blob_shas("HEAD", [path for path in candidates if path not in files])
    for path in candidates:
        if path in files:
            data = files[path].encode("utf-8") if isinstance(files[path], str) else files[path]
            digest = hashlib.sha256 if len(current[path]) == 64 else hashlib.sha1
            proposed[path] = digest(b"blob %d\0" % len(data) + data).hexdigest()
    changed = {path for path in candidates if proposed[path] != current[path]}
    return {number: [path for path in paths if path in changed]
            for number, paths in overlap.items() if changed & set(paths)}


def _blob(data: bytes) -> str:
    """Git's blob hash of a file's content, as stored in approval snapshots."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _binding(plan: dict, item: dict, config: Config, repo: str | None,
             published: dict[str, str | bytes]) -> dict:
    """What the implementer of this item will receive: the exact body and the files it names.

    The files are this issue's acceptance tests and selected references with the contents
    publication writes (``published`` from validate_plan: plan contents from any issue, else
    the repository's). The body names the repository in links publish generates; without the
    repository the body cannot be reproduced, so the record is not bound and never handed over.
    """
    # Decision links are bookkeeping like the plan-child marker; neither side counts them (#1025).
    content = None if repo is None else _body_digest(
        decision_links.MARKER_LINE.sub("", _published_body(plan, item, repo, config)))
    names = (set(item.get("acceptance_files", {})) | set(acceptance_paths(item["done_when"], config.acceptance_dir))
             | set(order_references.selected(plan, item)))
    files = {}
    for name in sorted(names):
        data = published.get(name)
        files[name] = "missing" if data is None else _blob(data.encode("utf-8") if isinstance(data, str) else data)
    return {"content": content, "files": files}


def lane_proposal(root: Path, n: int, body: str, acceptance_blobs: dict[str, str],
                  worktree: Path) -> dict | None:
    """The latest preread proposal recorded for what this run's implementer receives, if any.

    A record counts only when its key names this issue (plan child key in the same order,
    or an edit of #n), the body publish rendered for it equals the approved body byte for byte
    (apart from the trailing lines publish appends), and every file it bound
    (acceptance tests and references) has the same content in the run's worktree and, where
    the approval snapshot names the file, in that snapshot. The preread writer keeps
    records in write order, so the last match is the latest answer; when that answer
    proposed nothing (NONE or questions only), there is no proposal to hand over.
    """
    try:
        records = _preread_records(root)
    except PlanError:
        return None
    text, child = _without_child_marker(decision_links.MARKER_LINE.sub("", body))
    content = _body_digest(text)
    actual: dict[str, str] = {}

    def current(name: str) -> str:
        if name not in actual:
            path = worktree / name
            actual[name] = _blob(path.read_bytes()) if path.is_file() else "missing"
        return actual[name]

    latest = None
    for record in records.values():
        if not isinstance(record, dict) or record.get("content") != content:
            continue
        seen = record.get("files")
        if not isinstance(seen, dict) or any(
                not isinstance(sha, str) or current(name) != sha
                or acceptance_blobs.get(name, sha) != sha for name, sha in seen.items()):
            continue
        if record.get("key") == f"#{n}" or (
                child is not None and record.get("plan") == child[1] and record.get("key") == child[2]):
            latest = record
    if latest is None or not (latest.get("approach") or latest.get("decisions")):
        return None
    return latest


def preread(plan: dict, root: Path, config: Config,
            tracker: Callable[[], Tracker] | None = None, repo: str | None = None) -> list[dict]:
    """Ask each issue's implementer, read-only, for its unknowns and record them by content.

    Edits need the tracker for each existing issue's title and lane; it is opened only then.
    """
    try:
        published = validate_plan(plan, root, config)
    except ValueError as exc:
        raise PlanError(str(exc)) from exc
    # Statement issues are prepared and reviewed instead; preread reads only the legacy issues.
    items = [item for item in plan["issues"] if "statements" not in item]
    edits = [item for item in plan.get("edits", []) if "statements" not in item]
    existing, listed = {}, []
    if edits:
        if tracker is None:
            raise PlanError("prereading edits needs the tracker for the existing issues")
        source = tracker()
        existing = {item["number"]: source.get_issue(item["number"]) for item in edits}
        listed = source.list_issues()
        items += [edit_view(item, existing[item["number"]], config) for item in edits]
    # An issue that took the statement route keeps it: no legacy evidence is made for it.
    order_reviewed.require_route_kept(plan, config, repo or config.repo,
                                      {f"#{n}": issue.body for n, issue in existing.items()}, "preread",
                                      numbers={f"#{n}": n for n in existing}, listed=listed)
    waiting, lanes = _uncreated_lanes(plan, config, items)
    if waiting:
        # A generic reader is not the lane's implementer; record nothing rather than false evidence.
        raise PlanError(f"cannot preread issue(s) {', '.join(waiting)}: new lane(s) {', '.join(lanes)} "
                        f"do not exist yet; {NEW_LANE_STEPS}")
    # Local evidence stays in real directories; a planted symlink must not redirect writes.
    directory = order_references.real_directory(root, ".foragent")
    logs = order_references.real_directory(root, ".foragent/preread-logs")
    ignore = directory / ".gitignore"
    if ignore.is_symlink():
        raise PlanError(f"{ignore} must be a regular file, not a symlink")
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
    store = _workload_store(root, repo or config.repo)
    language = (getattr(config, "language", None) or "en").lower().split("-")[0]
    if len(items) > 1 and _free_slots(store) < 1:
        print(text("preread_turns_wait", language, count=len(items)), file=sys.stderr, flush=True)
    results = []
    for item in items:
        # Each reader gets its own snapshot directory, never the references' destination.
        snapshot = order_references.materialize(plan, item, root)
        try:
            setup, prompt, digest = preread_target(plan, item, config, root, snapshot)
            log = logs / f"{item['key'].lstrip('#')}-{digest[:12]}.log"
            if log.is_symlink():
                log.unlink()
            admitted = False
            try:
                # Each turn is one agent: it holds one shared machine slot only while it runs.
                with capacity.hold_workload(store, config, kind="preread", role=item["key"]):
                    admitted = True
                    result = executor_for(setup.executor).run(
                        worktree=root, prompt=prompt, model=setup.model, timeout_s=PREREAD_TIMEOUT_S,
                        log_path=log, read_only=True, **setup.options)
            except TimeoutError as exc:
                if admitted:
                    raise
                raise PlanError(str(exc)) from exc
        finally:
            order_references.remove(root, snapshot)
        if result.timed_out or result.exit_code != 0 or not result.last_message.strip():
            raise PlanError(f"preread of issue {item['key']} did not finish with an answer; see {log}")
        unknowns = _unknowns(result.last_message)
        decisions = _decisions(result.last_message)
        approach = _approach(result.last_message)
        _record_preread(root, directory, digest, {
            "key": item["key"], "plan": plan["id"], **_binding(plan, item, config, repo or config.repo, published),
            "unknowns": unknowns, "decisions": decisions, "approach": approach,
            "at": datetime.now(UTC).isoformat(timespec="seconds")})
        results.append({"key": item["key"], "unknowns": unknowns}
                       | ({"decisions": decisions} if decisions else {})
                       | ({"approach": approach} if approach else {}))
    return results


def _workload_store(root: Path, repo: str | None) -> StateStore:
    """The state store whose machine slots this planning agent counts in; a repository-less root gets its own."""
    if not repo:
        repo = f"local/{root.name}-{hashlib.sha256(str(root.resolve()).encode('utf-8')).hexdigest()[:12]}"
    store = StateStore.for_repo(repo)
    store.root.mkdir(parents=True, exist_ok=True)  # The machine scan reads its parent; a first run has none.
    return store


def _free_slots(store: StateStore) -> int:
    """Machine slots free right now; only for telling the person why a turn may wait."""
    return capacity.workload_budget(store) - len(capacity.machine_holders(store, datetime.now(UTC), live=True))


def _uncreated_lanes(plan: dict, config: Config, items: list[dict]) -> tuple[list[str], list[str]]:
    """Issues on a lane this order creates: their implementer does not exist before publication."""
    new = {lane["name"] for lane in plan.get("new_lanes", [])} - set(config.lanes)
    keys = [item["key"] for item in items if item["lane"] in new]
    return keys, sorted({item["lane"] for item in items if item["lane"] in new})


NEW_LANE_STEPS = ("publish the order without --approve first so the lane's agent and skills exist, "
                  "then run fa order preread PLAN.json and publish again with --approve")


require_reviewed_tests = order_reviewed.require_reviewed_tests


def require_clean_preread(plan: dict, root: Path, config: Config, items: list[dict]) -> None:
    """Approval needs the latest preread of each issue's current content to have no unknowns."""
    records = _preread_records(root)
    missing, open_items = [], []
    waiting, lanes = _uncreated_lanes(plan, config, items)
    for item in items:
        if item["key"] in waiting:
            continue
        record = records.get(preread_target(plan, item, config, root)[2])
        if not isinstance(record, dict) or not isinstance(record.get("unknowns"), list):
            missing.append(item["key"])
        elif record["unknowns"]:
            open_items.append(f"{item['key']}: " + "; ".join(map(str, record["unknowns"])))
    problems = []
    if waiting:
        problems.append(f"issue(s) {', '.join(waiting)} use new lane(s) {', '.join(lanes)} that do not exist "
                        f"yet, so their implementer has not prereaded them; {NEW_LANE_STEPS}")
    if missing:
        problems.append(f"no preread of the current content for issue(s) {', '.join(missing)}; "
                        "run fa order preread PLAN.json first")
    if open_items:
        problems.append("the implementer listed unknowns; resolve them in the plan, then run "
                        "fa order preread PLAN.json again: " + " | ".join(open_items))
    if problems:
        raise PlanError("cannot approve: " + ". ".join(problems))
