"""Offline GitHub Actions world for pr-ci-related-only (CI capacity B, r1227).

A small Python project is committed to a local Git repository together with this
repository's CI: the committed workflows, then `fa ci workflow` output on top (so a
workflow the generator now writes replaces the copy). One GitHub event is then
simulated: a lane pull request, a merge-train pull request or a push to main.

The simulator evaluates triggers, job and step conditions, matrices, `needs`, step
and job outputs, GITHUB_OUTPUT/GITHUB_ENV and artifacts, and runs every active `run`
step with bash. Nothing reaches GitHub or the network: `uv` is a shim that runs the
command with this checkout's foragent on PYTHONPATH (the trusted `foragent @ git+...`
runner therefore tests the code under test), and `gh` is a fake that answers only
offline questions about the simulated issue and pull request.

What counts is what pytest actually executed: every test function of the project
appends its own file path to one log. No signal is ever sent and no process is
killed; subprocesses run without timeouts.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import textwrap

import pytest
import yaml

from foragent import cli
from foragent.ci_workflow import WORKFLOW
from foragent.order_files import issue_body
from test_local_truth import record_gh
from test_skills_autosync import cli_exit


ROOT = next(parent for parent in Path(__file__).resolve().parents
            if (parent / ".foragent.yml").is_file() and (parent / "src/foragent/ci_shard.py").is_file())
REPOSITORY = "owner/repo"
ISSUE = 7
LANE_PR = 101
TRAIN_PR = 202
LANE_BRANCH = f"foragent/issue-{ISSUE}"
TRAIN_BRANCH = "foragent/batch-merge-1"
EXCLUSIONS_TRAILER = "Foragent-Acceptance-Excluded: []"

DONE_WHEN_FILE = "tests/acceptance/test_issue_done.py"
DONE_WHEN = f"uv run pytest -q {DONE_WHEN_FILE}"
UNIT_ALPHA = "tests/test_alpha.py"
UNIT_BETA = "tests/test_beta.py"
UNIT_GAMMA = "tests/test_gamma.py"
UNIT_DELTA = "tests/nested/test_delta.py"
ACCEPT_ALPHA = "tests/acceptance/test_alpha_accept.py"
ACCEPT_BETA = "tests/acceptance/test_beta_accept.py"
ALL_TESTS = frozenset({UNIT_ALPHA, UNIT_BETA, UNIT_GAMMA, UNIT_DELTA, ACCEPT_ALPHA, ACCEPT_BETA, DONE_WHEN_FILE})


def _test_file(path, imports, check, log):
    return (f"{imports}\n\n\n"
            "def test_recorded():\n"
            f"    with open({str(log)!r}, 'a', encoding='utf-8') as stream:\n"
            f"        stream.write({path + chr(10)!r})\n"
            f"    {check}\n")


def project_files(log):
    return {
        "pyproject.toml": ('[project]\nname = "demo"\nversion = "0.0.0"\n\n'
                           '[tool.pytest.ini_options]\npythonpath = ["src"]\n'
                           'markers = ["local_only: requires machine tools; run locally"]\n'),
        "src/demo/__init__.py": "",
        "src/demo/alpha.py": "def value():\n    return 1\n",
        "src/demo/beta.py": "def value():\n    return 2\n",
        "src/demo/gamma.py": "def value():\n    return 5\n",
        "tests/conftest.py": "# Shared test harness of the demo project.\n",
        UNIT_ALPHA: _test_file(UNIT_ALPHA, "from demo import alpha", "assert alpha.value() >= 1", log),
        UNIT_BETA: _test_file(UNIT_BETA, "from demo import beta", "assert beta.value() == 2", log),
        UNIT_GAMMA: _test_file(UNIT_GAMMA, "from demo import gamma", "assert gamma.value() == 5", log),
        UNIT_DELTA: _test_file(UNIT_DELTA, "import demo.gamma", "assert demo.gamma.value() == 5", log),
        ACCEPT_ALPHA: _test_file(ACCEPT_ALPHA, "from demo import alpha", "assert alpha.value() >= 1", log),
        ACCEPT_BETA: _test_file(ACCEPT_BETA, "from demo import beta", "assert beta.value() == 2", log),
        # The issue's own Done when test imports no project module: only the Done when
        # makes it part of the lane PR's selection, never the changed-file relation.
        DONE_WHEN_FILE: _test_file(DONE_WHEN_FILE, "import json", "assert json.loads('1') == 1", log),
    }


# ---------------------------------------------------------------- expressions

_STRING = re.compile(r"'(?:[^']|'')*'")
_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
_STATUS = re.compile(r"\b(success|failure|always|cancelled)\s*\(")


def _code(text):
    text = re.sub(r"[A-Za-z_]\w*(?:-\w+)+", lambda match: match.group(0).replace("-", "__dash__"), text)
    text = text.replace("&&", " and ").replace("||", " or ")
    text = re.sub(r"!(?!=)", " not ", text)
    return text.replace(".*", "")


def _pythonize(source):
    parts, position = [], 0
    for match in _STRING.finditer(source):
        parts.append(_code(source[position:match.start()]))
        parts.append(repr(match.group(0)[1:-1].replace("''", "'")))
        position = match.end()
    parts.append(_code(source[position:]))
    return "".join(parts).strip()


def truthy(value):
    return value not in (None, False, 0, "", 0.0)


def text_of(value):
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _number(value):
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return 0.0
    try:
        return float(str(value).strip() or 0)
    except ValueError:
        return float("nan")


def _equal(left, right):
    if isinstance(left, str) and isinstance(right, str):
        return left.lower() == right.lower()
    if type(left) is type(right) or left is None or right is None:
        return left == right if not (left is None) ^ (right is None) else False
    return _number(left) == _number(right)


def evaluate(source, context, *, status=True):
    """Evaluate one GitHub expression with GitHub's value semantics."""

    def lookup(container, key):
        key = key.replace("__dash__", "-") if isinstance(key, str) else key
        if isinstance(container, dict):
            if key in container:
                return container[key]
            if isinstance(key, str):
                for name, value in container.items():
                    if isinstance(name, str) and name.lower() == key.lower():
                        return value
            return None
        if isinstance(container, list) and isinstance(key, (int, float)) and 0 <= int(key) < len(container):
            return container[int(key)]
        return None

    functions = {
        "contains": lambda container, item: (any(_equal(value, item) for value in container)
                                             if isinstance(container, list)
                                             else text_of(item).lower() in text_of(container).lower()),
        "startsWith": lambda value, prefix: text_of(value).lower().startswith(text_of(prefix).lower()),
        "endsWith": lambda value, suffix: text_of(value).lower().endswith(text_of(suffix).lower()),
        "format": lambda template, *values: re.sub(
            r"\{(\d+)\}", lambda match: text_of(values[int(match.group(1))]), text_of(template)),
        "join": lambda values, separator=",": (text_of(separator).join(text_of(value) for value in values)
                                               if isinstance(values, list) else text_of(values)),
        "toJSON": lambda value: json.dumps(value, indent=2),
        "fromJSON": lambda value: json.loads(text_of(value)),
        "hashFiles": lambda *patterns: "0" * 64,
        "success": lambda: status,
        "failure": lambda: not status,
        "always": lambda: True,
        "cancelled": lambda: False,
    }

    def visit(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            name = node.id.replace("__dash__", "-")
            if name in ("true", "false", "null"):
                return {"true": True, "false": False, "null": None}[name]
            return context.get(name)
        if isinstance(node, ast.Attribute):
            return lookup(visit(node.value), node.attr)
        if isinstance(node, ast.Subscript):
            return lookup(visit(node.value), visit(node.slice))
        if isinstance(node, ast.BoolOp):
            value = None
            for child in node.values:
                value = visit(child)
                if isinstance(node.op, ast.And) and not truthy(value):
                    return value
                if isinstance(node.op, ast.Or) and truthy(value):
                    return value
            return value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not truthy(visit(node.operand))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -_number(visit(node.operand))
        if isinstance(node, ast.Compare):
            left = visit(node.left)
            for operator, comparator in zip(node.ops, node.comparators):
                right = visit(comparator)
                if isinstance(operator, ast.Eq):
                    result = _equal(left, right)
                elif isinstance(operator, ast.NotEq):
                    result = not _equal(left, right)
                elif isinstance(operator, ast.Lt):
                    result = _number(left) < _number(right)
                elif isinstance(operator, ast.LtE):
                    result = _number(left) <= _number(right)
                elif isinstance(operator, ast.Gt):
                    result = _number(left) > _number(right)
                elif isinstance(operator, ast.GtE):
                    result = _number(left) >= _number(right)
                else:
                    raise ValueError(f"unsupported comparison in {source!r}")
                if not result:
                    return False
                left = right
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id
            if name not in functions:
                raise ValueError(f"unsupported function {name} in {source!r}")
            return functions[name](*(visit(argument) for argument in node.args))
        raise ValueError(f"unsupported expression {source!r}")

    return visit(ast.parse(_pythonize(source), mode="eval").body)


def interpolate(value, context):
    if isinstance(value, str):
        whole = _EXPRESSION.fullmatch(value.strip())
        if whole and value.strip() == value:
            result = evaluate(whole.group(1), context)
            return result if isinstance(result, (dict, list)) else text_of(result)
        return _EXPRESSION.sub(lambda match: text_of(evaluate(match.group(1), context)), value)
    if isinstance(value, dict):
        return {key: interpolate(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [interpolate(item, context) for item in value]
    return value


def condition(value, context, *, status):
    """A job or step `if`: implicitly `success() && (...)` without a status function."""
    if value is None:
        return status
    if isinstance(value, bool):
        return status and value
    source = str(value).strip()
    whole = _EXPRESSION.fullmatch(source)
    if whole:
        source = whole.group(1)
    result = truthy(evaluate(source, context, status=status))
    return result if _STATUS.search(source) else status and result


# ---------------------------------------------------------------- triggers

def _glob(pattern):
    expression = ""
    index = 0
    while index < len(pattern):
        if pattern.startswith("**", index):
            expression += ".*"
            index += 2
        elif pattern[index] == "*":
            expression += "[^/]*"
            index += 1
        elif pattern[index] == "?":
            expression += "[^/]"
            index += 1
        else:
            expression += re.escape(pattern[index])
            index += 1
    return re.compile(expression + r"\Z")


def _filtered(patterns, values):
    """GitHub include/exclude lists with `!` negation, last match wins."""
    selected = set()
    for value in values:
        keep = False
        for pattern in patterns:
            negate = pattern.startswith("!")
            if _glob(pattern[1:] if negate else pattern).match(value):
                keep = not negate
        if keep:
            selected.add(value)
    return selected


def triggered(document, event):
    triggers = document.get("on", document.get(True))
    if isinstance(triggers, str):
        triggers = {triggers: None}
    elif isinstance(triggers, list):
        triggers = {name: None for name in triggers}
    names = [event.name] + (["pull_request_target"] if event.name == "pull_request" else [])
    for name in names:
        if name not in (triggers or {}):
            continue
        options = triggers[name] or {}
        branch = event.base_ref if name.startswith("pull_request") else event.ref_name
        if "branches" in options and not _filtered(options["branches"], [branch]):
            continue
        if "branches-ignore" in options and _filtered(options["branches-ignore"], [branch]):
            continue
        if name.startswith("pull_request") and "types" in options and "synchronize" not in options["types"]:
            continue
        if "paths" in options and not _filtered(options["paths"], event.changed):
            continue
        if "paths-ignore" in options and set(event.changed) <= _filtered(options["paths-ignore"], event.changed):
            continue
        return True
    return False


# ---------------------------------------------------------------- shims

_UV = '''
import os, sys
ROOT = {root!r}
args = sys.argv[1:]
if not args or args[0] != "run":
    sys.exit(0)  # sync/pip: the environment already has every dependency
args = args[1:]
VALUE = {{"--with", "--python", "-p", "--project", "--directory", "--with-requirements", "--extra",
         "--group", "--package", "--env-file", "--index", "--index-url", "--extra-index-url",
         "--with-editable", "--only-group", "--no-group", "--cache-dir"}}
directory = None
index = 0
while index < len(args):
    argument = args[index]
    if argument == "--":
        index += 1
        break
    if not argument.startswith("-"):
        break
    if argument == "--directory":
        directory = args[index + 1]
    if argument in VALUE:
        index += 2
    else:
        index += 1
command = args[index:]
if not command:
    sys.exit("uv shim: no command")
if directory:
    os.chdir(directory)
'''

_EXEC = '''
if command[0] in ("python", "python3") or os.path.basename(command[0]).startswith("python3"):
    command = [sys.executable, *command[1:]]
elif command[0] == "pytest":
    command = [sys.executable, "-m", "pytest", *command[1:]]
elif command[0] in ("fa", "foragent"):
    command = [sys.executable, "-c", "import sys; from foragent.cli import main; sys.exit(main())", *command[1:]]
paths = [os.path.join(ROOT, "src"), os.path.join(os.getcwd(), "src")]
existing = os.environ.get("PYTHONPATH")
os.environ["PYTHONPATH"] = os.pathsep.join(paths + ([existing] if existing else []))
os.execvp(command[0], command)
'''

_DIRECT = '''
import os, sys
ROOT = {root!r}
command = [{name!r}, *sys.argv[1:]]
'''

_GH = '''
import json, re, sys
DATA = json.load(open({data!r}, encoding="utf-8"))
with open({calls!r}, "a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
args = sys.argv[1:]

def option(*names):
    for index, argument in enumerate(args):
        for name in names:
            if argument == name and index + 1 < len(args):
                return args[index + 1]
            if argument.startswith(name + "="):
                return argument.split("=", 1)[1]
    return None

def emit(value):
    jq = option("--jq", "-q")
    fields = option("--json")
    if fields and isinstance(value, dict):
        value = {{key: value.get(key) for key in fields.split(",")}}
    if jq is None:
        print(json.dumps(value))
        return
    jq = jq.strip()
    if jq == ".":
        print(json.dumps(value))
    elif re.fullmatch(r"\\.\\w+", jq) and isinstance(value, dict):
        item = value.get(jq[1:])
        print(item if isinstance(item, str) else json.dumps(item))
    elif re.fullmatch(r"\\.\\[\\]\\.\\w+", jq) and isinstance(value, list):
        for item in value:
            print(item.get(jq[4:]))
    elif jq == ".[]" and isinstance(value, list):
        for item in value:
            print(json.dumps(item))
    else:
        sys.exit(f"fake gh: unsupported --jq {{jq}}")

text = " ".join(args)
issues, pulls = DATA["issues"], DATA["pulls"]
numbers = [argument for argument in args if argument.isdigit()]
if args[:2] == ["issue", "view"] and numbers and numbers[0] in issues:
    emit(issues[numbers[0]])
elif args[:2] == ["pr", "view"] and numbers and numbers[0] in pulls:
    emit(pulls[numbers[0]])
elif args[:2] == ["pr", "diff"] and numbers and numbers[0] in pulls and "--name-only" in args:
    print("\\n".join(file["path"] for file in pulls[numbers[0]]["files"]))
elif args and args[0] == "api":
    match = re.search(r"/(issues|pulls)/(\\d+)(/files|/comments)?(?:\\?.*)?$",
                      next((argument for argument in args[1:] if "/" in argument and not argument.startswith("-")), ""))
    if not match:
        sys.exit("fake gh: offline world has no answer for " + text)
    kind, number, tail = match.groups()
    table = issues if kind == "issues" else pulls
    if number not in table:
        sys.exit("fake gh: unknown number " + number)
    if tail == "/files":
        emit([{{"filename": file["path"], "status": "modified"}} for file in table[number]["files"]])
    elif tail == "/comments":
        emit([])
    else:
        emit(table[number])
else:
    sys.exit("fake gh: offline world has no answer for " + text)
'''


def write_shims(directory, data_file, calls_file):
    directory.mkdir(parents=True, exist_ok=True)
    header = f"#!{sys.executable}\n"
    scripts = {"uv": _UV.format(root=str(ROOT)) + _EXEC,
               "gh": _GH.format(data=str(data_file), calls=str(calls_file))}
    for name in ("python", "python3", "pytest", "fa", "foragent"):
        scripts[name] = _DIRECT.format(root=str(ROOT), name=name) + _EXEC
    for name, body in scripts.items():
        path = directory / name
        path.write_text(header + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)


# ---------------------------------------------------------------- world

@dataclass
class Event:
    name: str
    ref: str
    ref_name: str
    sha: str
    base_ref: str
    changed: list
    payload: dict
    head_ref: str = ""


@dataclass
class Outcome:
    kind: str
    changed: list
    executed: list
    jobs: dict
    log: list = field(default_factory=list)
    gh_calls: list = field(default_factory=list)

    @property
    def files(self):
        return set(self.executed)

    def describe(self):
        lines = [f"{self.kind} CI for changed files {self.changed}",
                 f"executed test files: {sorted(self.files)}",
                 f"job results: {self.jobs}"]
        lines.extend(self.log[-40:])
        return "\n".join(lines)


def _git(root, *args, env):
    return subprocess.run(["git", *args], cwd=root, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


class World:
    """One project repository, its committed CI, and one simulated event."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.project = directory / "project"
        self.home = directory / "home"
        self.temp = directory / "runner-temp"
        self.artifacts = directory / "artifacts"
        self.log_file = directory / "executed.log"
        self.shims = directory / "bin"
        self.data_file = directory / "gh-data.json"
        self.calls_file = directory / "gh-calls.jsonl"
        for path in (self.project, self.home, self.temp, self.artifacts):
            path.mkdir(parents=True, exist_ok=True)
        self.env = {
            "PATH": os.pathsep.join([str(self.shims), os.environ.get("PATH", "/usr/bin:/bin")]),
            "HOME": str(self.home), "GIT_CONFIG_GLOBAL": str(self.home / ".gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "LANG": "C.UTF-8",
            "TMPDIR": str(self.temp), "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_AUTHOR_NAME": "lane", "GIT_AUTHOR_EMAIL": "lane@example.invalid",
            "GIT_COMMITTER_NAME": "lane", "GIT_COMMITTER_EMAIL": "lane@example.invalid",
            "FORAGENT_VIEW": "off", "FORAGENT_ORCH_SPAWN": "off",
        }
        write_shims(self.shims, self.data_file, self.calls_file)
        self._build()

    def git(self, *args):
        return _git(self.project, *args, env=self.env)

    def _build(self):
        for relative, text in project_files(self.log_file).items():
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        config = yaml.safe_load((ROOT / ".foragent.yml").read_text(encoding="utf-8"))
        config.update(base_branch="main", ci_shards=2, checks="ci", test_command="uv run pytest -q")
        (self.project / ".foragent.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
        workflows = self.project / ".github/workflows"
        workflows.mkdir(parents=True, exist_ok=True)
        for source in sorted((ROOT / ".github/workflows").glob("*.y*ml")):
            if source.name != Path(WORKFLOW).name:
                shutil.copyfile(source, workflows / source.name)
        with pytest.MonkeyPatch.context() as patch:
            record_gh(patch, lambda argv: json.dumps({"isPrivate": False}))
            code = cli_exit(cli.main, ["ci", "workflow", "--repo-root", str(self.project)])
        assert code == 0, "`fa ci workflow` must generate the project's CI"
        self.git("init", "-q", "-b", "main")
        self.base = self._snapshot([], "Demo project with its CI")
        self.git("update-ref", "refs/heads/main", self.base)
        # Like an `actions/checkout` with fetch-depth 0: origin and its remote-tracking refs exist,
        # and `git fetch origin` works offline because origin is this repository.
        self.git("remote", "add", "origin", str(self.project))
        self.git("update-ref", "refs/remotes/origin/main", self.base)

    # Plumbing only: the suite's conftest forbids porcelain commit/checkout/reset in tests.
    def _snapshot(self, parents, message):
        self.git("add", "-A")
        tree = self.git("write-tree")
        return self.git("commit-tree", tree, *(item for parent in parents for item in ("-p", parent)),
                        "-m", message)

    def _materialize(self, commit, *, detach):
        self.git("read-tree", "-u", "--reset", commit)
        if detach:
            self.git("update-ref", "--no-deref", "HEAD", commit)

    def _commit_change(self, branch, changes, message):
        self._materialize(self.base, detach=False)
        for relative, change in changes.items():
            path = self.project / relative
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            path.write_text(change(text) if callable(change) else change, encoding="utf-8")
        head = self._snapshot([self.base], f"{message}\n\n{EXCLUSIONS_TRAILER}")
        self.git("update-ref", f"refs/heads/{branch}", head)
        self.git("update-ref", f"refs/remotes/origin/{branch}", head)
        self._materialize(self.base, detach=False)
        changed = self.git("diff", "--name-only", self.base, head).splitlines()
        return head, changed

    def _pr_event(self, number, branch, head, changed, body, title):
        tree = self.git("rev-parse", f"{head}^{{tree}}")
        merge = self.git("commit-tree", tree, "-p", self.base, "-p", head, "-m", f"Merge {head} into {self.base}")
        self.git("update-ref", f"refs/pull/{number}/merge", merge)
        self.git("update-ref", f"refs/pull/{number}/head", head)
        repository = {"full_name": REPOSITORY, "name": "repo", "default_branch": "main",
                      "owner": {"login": "owner"}}
        pull = {"number": number, "title": title, "body": body, "state": "open",
                "head": {"ref": branch, "sha": head, "repo": repository},
                "base": {"ref": "main", "sha": self.base, "repo": repository},
                "changed_files": len(changed)}
        payload = {"action": "synchronize", "number": number, "pull_request": pull,
                   "repository": repository, "before": self.base, "after": head}
        return Event("pull_request", f"refs/pull/{number}/merge", f"{number}/merge", merge, "main",
                     changed, payload, head_ref=branch)

    def lane_pr(self, changes):
        head, changed = self._commit_change(LANE_BRANCH, changes, f"Lane change for #{ISSUE}")
        body = ("Closes #7\n\n## Summary\n\nLane change.\n\n"
                "| Done when command | Result |\n| --- | --- |\n"
                f"| <code>{DONE_WHEN}</code> | pass |\n\n## Review\n\nVERDICT: PASS\n")
        self._serve(head, changed, LANE_PR, LANE_BRANCH, body, f"Lane change (#{ISSUE})")
        return self._pr_event(LANE_PR, LANE_BRANCH, head, changed, body, f"Lane change (#{ISSUE})")

    def train_pr(self, changes):
        head, changed = self._commit_change(TRAIN_BRANCH, changes, f"Merge train with #{LANE_PR}")
        body = f"Merge train tip.\n\nMembers: #{LANE_PR}\n"
        self._serve(head, changed, TRAIN_PR, TRAIN_BRANCH, body, "Merge train 1")
        return self._pr_event(TRAIN_PR, TRAIN_BRANCH, head, changed, body, "Merge train 1")

    def main_push(self, changes):
        head, changed = self._commit_change("pushed", changes, f"Land #{LANE_PR}")
        self.git("update-ref", "refs/heads/main", head)
        self.git("update-ref", "refs/remotes/origin/main", head)
        self.git("update-ref", "-d", "refs/heads/pushed")
        self.git("update-ref", "-d", "refs/remotes/origin/pushed")
        self._materialize(head, detach=False)
        self._serve(head, changed, LANE_PR, LANE_BRANCH, "", "landed")
        repository = {"full_name": REPOSITORY, "name": "repo", "default_branch": "main",
                      "owner": {"login": "owner"}}
        payload = {"ref": "refs/heads/main", "before": self.base, "after": head, "repository": repository,
                   "head_commit": {"id": head}, "commits": [{"id": head}], "created": False, "forced": False}
        return Event("push", "refs/heads/main", "main", head, "main", changed, payload)

    def _serve(self, head, changed, number, branch, body, title):
        item = {"goal": "Lane change.", "statements": ["Demo statement."], "out_of_scope": "Nothing else.",
                "done_when": [DONE_WHEN], "files": ["src/demo/*.py"]}
        issue = issue_body(item, REPOSITORY, "main")
        files = [{"path": path, "additions": 1, "deletions": 0} for path in changed]
        data = {
            "issues": {str(ISSUE): {"number": ISSUE, "title": "Lane change", "body": issue, "state": "OPEN",
                                    "labels": [], "url": f"https://github.com/{REPOSITORY}/issues/{ISSUE}"},
                       str(number): {"number": number, "title": title, "body": body, "state": "OPEN",
                                     "labels": [], "pull_request": {}}},
            "pulls": {str(number): {"number": number, "title": title, "body": body, "state": "OPEN",
                                    "headRefName": branch, "baseRefName": "main", "headRefOid": head,
                                    "baseRefOid": self.base, "files": files,
                                    "head": {"ref": branch, "sha": head}, "base": {"ref": "main", "sha": self.base},
                                    "closingIssuesReferences": [{"number": ISSUE}] if branch == LANE_BRANCH else []}},
        }
        self.data_file.write_text(json.dumps(data), encoding="utf-8")

    # ------------------------------------------------------------ simulation

    def simulate(self, event, kind):
        self.log_file.write_text("", encoding="utf-8")
        self.calls_file.write_text("", encoding="utf-8")
        event_path = self.directory / "event.json"
        event_path.write_text(json.dumps(event.payload), encoding="utf-8")
        outcome = Outcome(kind, list(event.changed), [], {})
        github = {
            "event_name": event.name, "event": event.payload, "sha": event.sha, "ref": event.ref,
            "ref_name": event.ref_name, "head_ref": event.head_ref,
            "base_ref": event.base_ref if event.name == "pull_request" else "",
            "repository": REPOSITORY, "repository_owner": "owner", "token": "offline-token",
            "run_id": "4242", "run_number": "1", "run_attempt": "1", "actor": "lane",
            "workspace": str(self.project), "server_url": "https://github.com",
            "api_url": "https://api.github.com", "action": "run",
        }
        self.github_env = {
            "GITHUB_ACTIONS": "true", "CI": "true", "GITHUB_EVENT_NAME": event.name,
            "GITHUB_EVENT_PATH": str(event_path), "GITHUB_REF": event.ref, "GITHUB_REF_NAME": event.ref_name,
            "GITHUB_SHA": event.sha, "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_REPOSITORY_OWNER": "owner",
            "GITHUB_WORKSPACE": str(self.project), "GITHUB_RUN_ID": "4242", "GITHUB_RUN_NUMBER": "1",
            "GITHUB_RUN_ATTEMPT": "1", "GITHUB_HEAD_REF": event.head_ref,
            "GITHUB_BASE_REF": github["base_ref"], "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_API_URL": "https://api.github.com", "GITHUB_ACTOR": "lane",
            "RUNNER_TEMP": str(self.temp), "RUNNER_OS": "Linux", "GH_TOKEN": "offline-token",
        }
        workflows = self.git("ls-tree", "--name-only", f"{event.sha}:.github/workflows").splitlines()
        for name in workflows:
            if not name.endswith((".yml", ".yaml")):
                continue
            document = yaml.safe_load(self.git("show", f"{event.sha}:.github/workflows/{name}"))
            if not isinstance(document, dict) or not triggered(document, event):
                continue
            self._workflow(name, document, github, event, outcome)
        executed = self.log_file.read_text(encoding="utf-8").splitlines()
        outcome.executed = [line for line in executed if line]
        outcome.gh_calls = [json.loads(line) for line in self.calls_file.read_text().splitlines() if line]
        return outcome

    def _workflow(self, name, document, github, event, outcome, inputs=None, prefix=""):
        jobs = document.get("jobs") or {}
        results, outputs, order = {}, {}, []
        pending = list(jobs)
        while pending:
            ready = [job for job in pending if all(need in results for need in self._needs(jobs[job]))]
            if not ready:
                raise AssertionError(f"{name}: unresolvable job needs {pending}")
            for job_id in ready:
                pending.remove(job_id)
                result, job_outputs = self._job(name, document, job_id, jobs[job_id], github, event, outcome,
                                                {need: {"result": results[need], "outputs": outputs.get(need, {})}
                                                 for need in self._needs(jobs[job_id])}, inputs or {})
                results[job_id], outputs[job_id] = result, job_outputs
                outcome.jobs[f"{prefix}{name}:{job_id}"] = result
                order.append(job_id)
        return results, outputs

    @staticmethod
    def _needs(job):
        needs = job.get("needs") or []
        return [needs] if isinstance(needs, str) else list(needs)

    def _job(self, workflow, document, job_id, job, github, event, outcome, needs, inputs):
        status = all(value["result"] == "success" for value in needs.values())
        context = {"github": github, "needs": needs, "inputs": inputs, "vars": {}, "secrets": {},
                   "runner": {"temp": str(self.temp), "os": "Linux"}, "env": {}, "matrix": {}, "strategy": {}}
        if not condition(job.get("if"), context, status=status):
            return "skipped", {}
        if "uses" in job:
            target = str(job["uses"])
            if not target.startswith("./"):
                outcome.log.append(f"{workflow}:{job_id}: external reusable workflow {target} not simulated")
                return "failure", {}
            called = yaml.safe_load(self.git("show", f"{event.sha}:{target[2:]}"))
            results, _ = self._workflow(target, called, github, event, outcome,
                                        inputs=interpolate(job.get("with") or {}, context),
                                        prefix=f"{workflow}:{job_id}->")
            return ("success" if all(value in ("success", "skipped") for value in results.values())
                    else "failure"), {}
        matrix = (job.get("strategy") or {}).get("matrix")
        combinations = self._matrix(interpolate(matrix, context) if matrix is not None else None)
        results, last_outputs = [], {}
        for values in combinations:
            local = {**context, "matrix": values}
            result, steps = self._steps(workflow, document, job_id, job, local, event, outcome)
            results.append(result)
            last_outputs = interpolate(job.get("outputs") or {}, {**local, "steps": steps})
        if not combinations:
            return "skipped", {}
        return ("success" if all(result == "success" for result in results) else "failure"), last_outputs

    @staticmethod
    def _matrix(matrix):
        if matrix is None:
            return [{}]
        if isinstance(matrix, str):
            matrix = json.loads(matrix)
        include = matrix.get("include") or []
        exclude = matrix.get("exclude") or []
        keys = [key for key in matrix if key not in ("include", "exclude")]
        combos = [dict(zip(keys, values)) for values in itertools.product(*(matrix[key] for key in keys))] if keys else []
        combos = [combo for combo in combos
                  if not any(all(combo.get(key) == value for key, value in rule.items()) for rule in exclude)]
        for extra in include:
            matched = False
            for combo in combos:
                if all(combo.get(key) == value for key, value in extra.items() if key in keys):
                    combo.update({key: value for key, value in extra.items() if key not in keys})
                    matched = True
            if not matched or not keys:
                combos.append(dict(extra))
        return combos

    def _steps(self, workflow, document, job_id, job, context, event, outcome):
        steps, failed = {}, False
        env = {}
        for key, value in {**(document.get("env") or {}), **(job.get("env") or {})}.items():
            env[key] = text_of(interpolate(value, {**context, "env": env}))
        defaults = ((document.get("defaults") or {}).get("run") or {}) | ((job.get("defaults") or {}).get("run") or {})
        for index, step in enumerate(job.get("steps") or []):
            local = {**context, "env": env, "steps": steps}
            label = f"{workflow}:{job_id}{context['matrix'] or ''} step {index} {step.get('name') or step.get('uses') or ''}"
            if not condition(step.get("if"), local, status=not failed):
                outcome.log.append(f"skip  {label}")
                continue
            step_env = dict(env)
            for key, value in (step.get("env") or {}).items():
                step_env[key] = text_of(interpolate(value, {**local, "env": step_env}))
            local = {**local, "env": step_env}
            outputs, ok = {}, True
            if "uses" in step:
                ok, outputs = self._action(str(step["uses"]), interpolate(step.get("with") or {}, local), event)
            elif "run" in step:
                ok, outputs, extra_env = self._run(step, defaults, local, step_env, label)
                env.update(extra_env)
            outcome.log.append(f"{'ok   ' if ok else 'FAIL '} {label}")
            if step.get("id"):
                steps[step["id"]] = {"outputs": outputs, "outcome": "success" if ok else "failure",
                                     "conclusion": "success" if ok else "failure"}
            continue_on_error = truthy(interpolate(step.get("continue-on-error", False), local))
            if not ok and not continue_on_error:
                failed = True
        return ("failure" if failed else "success"), steps

    def _action(self, uses, inputs, event):
        action = uses.split("@", 1)[0]
        if action == "actions/checkout":
            reference = inputs.get("ref") or event.sha
            self._materialize(self.git("rev-parse", f"{text_of(reference)}^{{commit}}"), detach=True)
            self.git("clean", "-q", "-ffdx")
            return True, {}
        if action == "actions/upload-artifact":
            name = text_of(inputs.get("name") or "artifact")
            target = self.artifacts / name
            found = False
            for line in text_of(inputs.get("path")).splitlines():
                line = line.strip()
                if not line:
                    continue
                source = Path(line) if Path(line).is_absolute() else self.project / line
                for match in (sorted(source.parent.glob(source.name)) if any(c in source.name for c in "*?[")
                              else [source] if source.exists() else []):
                    found = True
                    target.mkdir(parents=True, exist_ok=True)
                    if match.is_dir():
                        shutil.copytree(match, target / match.name, dirs_exist_ok=True)
                    else:
                        shutil.copyfile(match, target / match.name)
            return (found or inputs.get("if-no-files-found") != "error"), {}
        if action == "actions/download-artifact":
            destination = Path(text_of(inputs.get("path") or self.project))
            destination = destination if destination.is_absolute() else self.project / destination
            names = [text_of(inputs["name"])] if inputs.get("name") else [
                path.name for path in sorted(self.artifacts.iterdir())
                if fnmatchcase(path.name, text_of(inputs.get("pattern") or "*"))]
            for name in names:
                source = self.artifacts / name
                if not source.is_dir():
                    return False, {}
                merge = inputs.get("name") or truthy(inputs.get("merge-multiple"))
                shutil.copytree(source, destination if merge else destination / name, dirs_exist_ok=True)
            return True, {}
        if action.startswith("actions/cache"):
            return True, {"cache-hit": "false", "cache-matched-key": "", "cache-primary-key": text_of(inputs.get("key"))}
        return True, {}

    def _run(self, step, defaults, context, step_env, label):
        script = interpolate(str(step["run"]), context)
        directory = step.get("working-directory") or defaults.get("working-directory")
        directory = Path(interpolate(directory, context)) if directory else self.project
        directory = directory if directory.is_absolute() else self.project / directory
        output_file = self.temp / "github-output"
        env_file = self.temp / "github-env"
        for path in (output_file, env_file, self.temp / "step-summary"):
            path.write_text("", encoding="utf-8")
        shell = step.get("shell") or defaults.get("shell") or "bash"
        script_file = self.temp / "step-script"
        script_file.write_text(script, encoding="utf-8")
        command = {"bash": ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script_file)],
                   "sh": ["sh", "-e", str(script_file)],
                   "python": [sys.executable, str(script_file)]}.get(shell)
        if command is None:
            raise AssertionError(f"{label}: unsupported shell {shell}")
        environment = {**self.env, **self.github_env, **step_env,
                       "GITHUB_OUTPUT": str(output_file), "GITHUB_ENV": str(env_file),
                       "GITHUB_STEP_SUMMARY": str(self.temp / "step-summary")}
        environment["PATH"] = self.env["PATH"]
        result = subprocess.run(command, cwd=directory, env=environment, capture_output=True, text=True, check=False)
        if result.returncode:
            tail = (result.stdout + result.stderr)[-1500:]
            self._last_failure = f"{label} exited {result.returncode}:\n{tail}"
        return (result.returncode == 0, _read_pairs(output_file), _read_pairs(env_file))


def _read_pairs(path):
    pairs, lines = {}, path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if "<<" in line and ("=" not in line or line.index("<<") < line.index("=")):
            key, marker = line.split("<<", 1)
            value = []
            index += 1
            while index < len(lines) and lines[index] != marker:
                value.append(lines[index])
                index += 1
            pairs[key] = "\n".join(value)
        elif "=" in line:
            key, value = line.split("=", 1)
            pairs[key] = value
        index += 1
    return pairs


# ---------------------------------------------------------------- scenarios

TOUCH_ALPHA = {"src/demo/alpha.py": "def value():\n    return 3\n"}
TOUCH_BETA_TEST = {UNIT_BETA: lambda text: text + "\n\n# Lane edit to its own test file.\n"}
TOUCH_CI_WORKFLOW = {".github/workflows/ci.yml": lambda text: text + "\n# Lane edit to the CI workflow.\n"}
TOUCH_HARNESS = {"tests/conftest.py": lambda text: text + "# Lane edit to the shared test harness.\n"}


class Scenarios:
    """Simulate each (kind, change) once per test module; every run gets its own world."""

    def __init__(self, factory):
        self.factory = factory
        self.cache = {}

    def run(self, kind, changes, label):
        key = (kind, label)
        if key not in self.cache:
            world = World(self.factory.mktemp(f"{kind}-{label}"))
            event = {"lane": world.lane_pr, "train": world.train_pr, "push": world.main_push}[kind](changes)
            outcome = world.simulate(event, kind)
            failure = getattr(world, "_last_failure", None)
            if failure:
                outcome.log.append(failure)
            self.cache[key] = outcome
        return self.cache[key]


def assert_succeeded(outcome):
    ran = {name: result for name, result in outcome.jobs.items() if result != "skipped"}
    assert ran, f"No CI job ran at all.\n{outcome.describe()}"
    assert all(result == "success" for result in ran.values()), (
        f"Every CI job that runs must succeed on this clean change.\n{outcome.describe()}")


def assert_full_suite(outcome):
    assert_succeeded(outcome)
    missing = sorted(ALL_TESTS - outcome.files)
    assert not missing, (f"{outcome.kind} CI must run the full suite (every unit and acceptance test file); "
                         f"never executed: {missing}\n{outcome.describe()}")
