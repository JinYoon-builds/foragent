"""Deterministic context gathering and publication for the fa-order skill."""

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
from urllib.parse import quote

from foragent import board, design
from foragent.config import Config
from foragent.git import GitError, GitRepo
from foragent.inspect_data import DECISION_MARKER, comment_key, has_marker, latest_flag
from foragent.issue import MARKER, acceptance_paths, body_hash, parse_body
from foragent.kick import kick_tick
from foragent.labels import LABELS, issue_lane, issue_size, lane_order, state_group
from foragent.lanes import add_lane, lane_files, validate_lane
from foragent.paths import repo_path
from foragent.skills import SkillManager
from foragent.tracker import LabelSpec, Tracker


class PlanError(ValueError):
    """An order plan is malformed or cannot be applied."""


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
            _object(item, required, {"checklist", "acceptance_files", "blocked_by", "files"} if kind == "issues"
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
            lines += [f"\nlane:{lane}", "| Number | Title | Size | Blocked by | Approved? |", "| --- | --- | --- | --- | --- |"]
            for row in self.rows:
                if row["lane"] == lane:
                    title = row["title"].replace("|", "\\|").replace("\n", " ")
                    blockers = ", ".join(map(str, row["blocked_by"])) or "—"
                    lines.append(f"| {row['number']} | {title} | {row['size']} | {blockers} | {'yes' if row['approved'] else 'no'} |")
        return "\n".join(lines)


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
                     "lane": item["lane"], "blocked_by": [mapping.get(key, key) for key in item.get("blocked_by", [])],
                     "approved": approve or item["key"] in selected_keys or number in selected_numbers})
    for item in plan.get("edits", []):
        issue = by_number[item["number"]]
        rows.append({"number": issue.number, "title": issue.title, "size": issue_size(issue.labels),
                     "lane": issue_lane(issue.labels, config.lanes), "blocked_by": [dep.number for dep in tracker.blocked_by(issue.number)],
                     "approved": approve or issue.number in selected_numbers})
    if dry_run:
        return PublishReport(plan["title"], parent, rows, sorted(files), True)
    git = git or GitRepo(root)
    if git.current_branch() != config.base_branch:
        raise GitError(f"order publish must run on the base branch {config.base_branch!r}")
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
    lane_names = {item["lane"] for item in plan["issues"]} - {"main"}
    tracker.ensure_labels([*LABELS, *(LabelSpec(f"lane:{name}", "ededed", f"Foragent lane {name}.") for name in sorted(lane_names))])

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
            if existing.body != body or saved_blobs.get(str(number), {}) != blobs:
                tracker.remove_labels(number, ["approved", "ready"])
            tracker.update_issue(number, title=item["title"], body=body)
            tracker.remove_labels(number, [label for label in existing.labels if label.startswith(("lane:", "size:")) and label not in labels])
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
