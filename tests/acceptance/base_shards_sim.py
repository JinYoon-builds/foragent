"""Offline simulator for one push run of a generated GitHub Actions workflow.

Test helper only (not a production decision rule). It runs every job that a
push to the base branch activates, in `needs` order, on a local runner:

- each job instance (one per matrix combination) gets its own workspace
  checkout and its own runner.temp, so data crosses jobs only through the
  artifact and cache stores below, as on GitHub;
- `run` steps execute in bash with the GitHub default environment; the
  trusted `uv run ... python -m foragent...` installation is replaced by the
  local interpreter (no network);
- actions/upload-artifact, actions/download-artifact, actions/cache(/save,
  /restore) use in-memory stores with GitHub v4 semantics; checkout and
  setup actions are local no-ops or clones.

`fault` lets a test make one job instance crash (no steps run) or lose /
replace everything it published after it finished successfully.
"""

from fnmatch import fnmatchcase
import glob
import itertools
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

import yaml

import foragent


class SimError(AssertionError):
    """The simulator cannot model a workflow construct."""


# ----------------------------------------------------------------------------
# GitHub expression evaluation
# ----------------------------------------------------------------------------

TOKEN = re.compile(r"""\s*(?:
    (?P<number>-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|0x[0-9a-fA-F]+)
  | (?P<string>'(?:[^']|'')*')
  | (?P<op>==|!=|<=|>=|&&|\|\||[<>!()\[\].,*])
  | (?P<name>[A-Za-z_][A-Za-z0-9_-]*)
)""", re.VERBOSE)


def tokenize(source):
    tokens, position = [], 0
    source = source.strip()
    while position < len(source):
        match = TOKEN.match(source, position)
        if not match or match.end() == position:
            raise SimError(f"cannot parse expression {source!r} at {position}")
        position = match.end()
        kind = match.lastgroup
        tokens.append((kind, match.group(kind)))
        while position < len(source) and source[position].isspace():
            position += 1
    return tokens


def truthy(value):
    if isinstance(value, float) and math.isnan(value):
        return False
    return value not in (False, 0, "", None)


def number(value):
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        try:
            return float(int(text, 16)) if text.lower().startswith("0x") else float(text)
        except ValueError:
            return math.nan
    return math.nan


def equal(left, right):
    if isinstance(left, str) and isinstance(right, str):
        return left.lower() == right.lower()
    if type(left) is type(right) and not isinstance(left, (int, float)):
        return left == right
    if isinstance(left, (dict, list)) or isinstance(right, (dict, list)):
        return left is right
    return number(left) == number(right)


def text(value):
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2)
    return str(value)


def lookup(parent, key):
    if isinstance(parent, list):
        if key == "*":
            return parent
        if isinstance(key, (int, float)) and not isinstance(key, bool) and 0 <= int(key) < len(parent):
            return parent[int(key)]
        if isinstance(key, str):
            return [lookup(item, key) for item in parent]
        return None
    if isinstance(parent, dict):
        if key == "*":
            return list(parent.values())
        if isinstance(key, str):
            if key in parent:
                return parent[key]
            for name, value in parent.items():
                if isinstance(name, str) and name.lower() == key.lower():
                    return value
    return None


class Parser:
    def __init__(self, source, context, functions):
        self.tokens = tokenize(source)
        self.index = 0
        self.context = context
        self.functions = functions

    def peek(self, value=None):
        if self.index >= len(self.tokens):
            return None
        token = self.tokens[self.index]
        return token if value is None or token[1] == value else None

    def take(self, value=None):
        token = self.peek(value)
        if token is None:
            raise SimError(f"expected {value!r} in expression")
        self.index += 1
        return token

    def parse(self):
        value = self.disjunction()
        if self.index != len(self.tokens):
            raise SimError(f"trailing expression tokens {self.tokens[self.index:]}")
        return value

    def disjunction(self):
        value = self.conjunction()
        while self.peek("||"):
            self.take()
            right = self.conjunction()
            value = value if truthy(value) else right
        return value

    def conjunction(self):
        value = self.equality()
        while self.peek("&&"):
            self.take()
            right = self.equality()
            value = right if truthy(value) else value
        return value

    def equality(self):
        value = self.comparison()
        while self.peek("==") or self.peek("!="):
            operator = self.take()[1]
            right = self.comparison()
            value = equal(value, right) if operator == "==" else not equal(value, right)
        return value

    def comparison(self):
        value = self.unary()
        while any(self.peek(op) for op in ("<", ">", "<=", ">=")):
            operator = self.take()[1]
            right = self.unary()
            if isinstance(value, str) and isinstance(right, str):
                left_value, right_value = value.lower(), right.lower()
            else:
                left_value, right_value = number(value), number(right)
            value = {"<": left_value < right_value, ">": left_value > right_value,
                     "<=": left_value <= right_value, ">=": left_value >= right_value}[operator]
        return value

    def unary(self):
        if self.peek("!"):
            self.take()
            return not truthy(self.unary())
        return self.postfix()

    def postfix(self):
        value = self.primary()
        while True:
            if self.peek("."):
                self.take()
                if self.peek("*"):
                    self.take()
                    value = lookup(value, "*")
                else:
                    value = lookup(value, self.take()[1])
            elif self.peek("["):
                self.take()
                key = self.disjunction()
                self.take("]")
                value = lookup(value, key)
            else:
                return value

    def primary(self):
        kind, value = self.take()
        if kind == "number":
            return number(value)
        if kind == "string":
            return value[1:-1].replace("''", "'")
        if value == "(":
            inner = self.disjunction()
            self.take(")")
            return inner
        if kind == "name":
            if self.peek("("):
                self.take()
                args = []
                while not self.peek(")"):
                    args.append(self.disjunction())
                    if self.peek(","):
                        self.take()
                self.take(")")
                return self.call(value, args)
            lowered = value.lower()
            if lowered in {"true", "false", "null"}:
                return {"true": True, "false": False, "null": None}[lowered]
            return lookup(self.context, value)
        raise SimError(f"unexpected token {value!r}")

    def call(self, name, args):
        lowered = name.lower()
        if lowered in self.functions:
            return self.functions[lowered](*args)
        if lowered == "contains":
            container, item = args
            if isinstance(container, list):
                return any(equal(entry, item) for entry in container)
            return text(item).lower() in text(container).lower()
        if lowered == "startswith":
            return text(args[0]).lower().startswith(text(args[1]).lower())
        if lowered == "endswith":
            return text(args[0]).lower().endswith(text(args[1]).lower())
        if lowered == "format":
            template, *values = args
            return re.sub(r"\{(\d+)\}", lambda match: text(values[int(match.group(1))]),
                          text(template).replace("{{", "\0").replace("}}", "\1")).replace("\0", "{").replace("\1", "}")
        if lowered == "join":
            values = args[0] if isinstance(args[0], list) else [args[0]]
            return (text(args[1]) if len(args) > 1 else ",").join(text(item) for item in values)
        if lowered == "tojson":
            return json.dumps(args[0], indent=2)
        if lowered == "fromjson":
            return json.loads(text(args[0]))
        raise SimError(f"unsupported expression function {name}()")


STATUS = re.compile(r"\b(success|failure|always|cancelled)\s*\(", re.IGNORECASE)
EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)


def evaluate(source, context, functions=None):
    return Parser(source, context, functions or {}).parse()


def render(value, context):
    if not isinstance(value, str):
        return value
    return EXPRESSION.sub(lambda match: text(evaluate(match.group(1), context)), value)


def deep_render(value, context):
    if isinstance(value, str):
        stripped = value.strip()
        whole = EXPRESSION.fullmatch(stripped)
        if whole:
            return evaluate(whole.group(1), context)
        return render(value, context)
    if isinstance(value, list):
        return [deep_render(item, context) for item in value]
    if isinstance(value, dict):
        return {key: deep_render(item, context) for key, item in value.items()}
    return value


def condition(source, context, functions):
    """Evaluate a job or step `if`, adding GitHub's implicit success()."""
    if source is None:
        source = "success()"
    if isinstance(source, bool):
        source = "true" if source else "false"
    source = str(source).strip()
    whole = EXPRESSION.fullmatch(source)
    if whole:
        source = whole.group(1).strip()
    if not STATUS.search(source):
        source = f"success() && ({source})"
    return truthy(evaluate(source, context, functions))


# ----------------------------------------------------------------------------
# Local runner
# ----------------------------------------------------------------------------

UV_SHIM = """#!/usr/bin/env python3
# Replace a network installation by the local interpreter; keep the command.
import os, sys
args = sys.argv[1:]
if not args or args[0] != "run":
    sys.exit("simulated uv only supports `uv run`, got: %r" % (args,))
args = args[1:]
VALUED = {"--python", "-p", "--with", "--with-editable", "--with-requirements", "--from", "--project",
          "--directory", "--env-file", "--extra", "--group", "--index", "--index-url",
          "--extra-index-url", "--package", "--only-group", "--no-group"}
while args and args[0].startswith("-"):
    option = args.pop(0)
    if option == "--":
        break
    if option in VALUED and "=" not in option:
        args.pop(0)
if not args:
    sys.exit("simulated uv run without a command")
command, rest = args[0], args[1:]
python = @PYTHON@
# The trusted tool is the foragent under test, never another installation.
os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, [@SOURCE@, os.environ.get("PYTHONPATH")]))
if command in {"python", "python3", "python3.12"}:
    os.execv(python, [python, *rest])
script = os.path.join(os.path.dirname(python), command)
if os.path.isfile(script):
    os.execv(script, [script, *rest])
os.execvp(command, [command, *rest])
"""

PYTHON_SHIM = "#!/bin/sh\nPYTHONPATH=%(source)s${PYTHONPATH:+:$PYTHONPATH} exec %(python)s \"$@\"\n"
GH_SHIM = "#!/bin/sh\necho 'simulated runner has no GitHub API: gh '\"$*\" >&2\nexit 1\n"
NOOP_ACTIONS = {"astral-sh/setup-uv", "actions/setup-python"}


def matrix_combinations(strategy):
    matrix = strategy.get("matrix")
    if matrix is None:
        return [None]
    if not isinstance(matrix, dict):
        raise SimError(f"unsupported matrix {matrix!r}")
    include = matrix.get("include") or []
    exclude = matrix.get("exclude") or []
    axes = {key: value for key, value in matrix.items() if key not in {"include", "exclude"}}
    for key, value in axes.items():
        if not isinstance(value, list):
            raise SimError(f"unsupported matrix axis {key}={value!r}")
    combos = [dict(zip(axes, values)) for values in itertools.product(*axes.values())] if axes else []
    combos = [combo for combo in combos
              if not any(all(equal(combo.get(k), v) for k, v in rule.items()) for rule in exclude)]
    for extra in include:
        matched = False
        for combo in combos:
            if all(equal(combo[k], v) for k, v in extra.items() if k in axes):
                if not any(k in combo and k not in axes and not equal(combo[k], v) for k, v in extra.items()):
                    combo.update({k: v for k, v in extra.items() if k not in axes})
                    matched = True
        if not matched:
            combos.append(dict(extra))
    return combos or [None]


def instance_label(job_id, combo):
    return job_id if combo is None else f"{job_id} {json.dumps(combo, sort_keys=True)}"


def needs_of(job):
    value = job.get("needs") or []
    return [value] if isinstance(value, str) else list(value)


def job_order(jobs):
    order, seen = [], set()

    def visit(name, path=()):
        if name in seen:
            return
        if name in path:
            raise SimError(f"needs cycle at {name}")
        for dependency in needs_of(jobs[name]):
            if dependency not in jobs:
                raise SimError(f"job {name} needs unknown job {dependency}")
            visit(dependency, (*path, name))
        seen.add(name)
        order.append(name)

    for name in jobs:
        visit(name)
    return order


def transitive_needs(jobs, name):
    result, pending = set(), list(needs_of(jobs[name]))
    while pending:
        current = pending.pop()
        if current not in result:
            result.add(current)
            pending.extend(needs_of(jobs[current]))
    return result


def parse_outputs(path):
    outputs = {}
    if not path.exists():
        return outputs
    lines = path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if "<<" in line and ("=" not in line or line.index("<<") < line.index("=")):
            key, delimiter = line.split("<<", 1)
            value = []
            while index < len(lines) and lines[index] != delimiter:
                value.append(lines[index])
                index += 1
            index += 1
            outputs[key] = "\n".join(value)
        elif "=" in line:
            key, value = line.split("=", 1)
            outputs[key] = value
    return outputs


def lines_of(value):
    return [line.strip() for line in text(value).splitlines() if line.strip()]


class PushRun:
    """Simulate one push of `sha` to `branch` of a local repository."""

    def __init__(self, document, origin, sha, workdir, *, branch="main", repository="owner/repo",
                 fault=None, extra_env=None):
        self.document = document
        self.origin = Path(origin)
        self.sha = sha
        self.branch = branch
        self.repository = repository
        self.workdir = Path(workdir)
        self.fault = fault or {}
        self.extra_env = dict(extra_env or {})
        self.artifacts = {}     # name -> {"files": {relative: bytes}, "owner": label}
        self.caches = {}        # key -> {"files": [bytes...], "owner": label}
        self.cache_saves = []   # every attempted save: {"key", "owner", "job", "paths"}
        self.instances = []     # ordered records of every job instance
        self.job_results = {}
        self.job_outputs = {}
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.bin = self.workdir / "bin"
        self.bin.mkdir(exist_ok=True)
        source = str(Path(foragent.__file__).resolve().parents[1])
        self._shim("uv", UV_SHIM.replace("@PYTHON@", repr(sys.executable)).replace("@SOURCE@", repr(source)))
        for name in ("python", "python3"):
            self._shim(name, PYTHON_SHIM % {"python": shlex.quote(sys.executable), "source": shlex.quote(source)})
        self._shim("gh", GH_SHIM)
        self.event = {"ref": f"refs/heads/{branch}", "before": "0" * 40, "after": sha,
                      "head_commit": {"id": sha},
                      "repository": {"full_name": repository, "default_branch": branch,
                                     "name": repository.split("/")[1],
                                     "owner": {"login": repository.split("/")[0]}}}
        self.event_path = self.workdir / "event.json"
        self.event_path.write_text(json.dumps(self.event), encoding="utf-8")

    def _shim(self, name, body):
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    # -- triggers ---------------------------------------------------------
    def triggered(self):
        triggers = self.document.get("on", self.document.get(True))
        if isinstance(triggers, str):
            triggers = {triggers: None}
        elif isinstance(triggers, list):
            triggers = {name: None for name in triggers}
        if "push" not in (triggers or {}):
            return False
        options = triggers["push"] or {}
        if "branches" in options and not any(fnmatchcase(self.branch, p) for p in options["branches"]):
            return False
        return not any(fnmatchcase(self.branch, p) for p in options.get("branches-ignore", []))

    def github(self, workspace=None):
        return {"event_name": "push", "event": self.event, "sha": self.sha,
                "ref": f"refs/heads/{self.branch}", "ref_name": self.branch, "ref_type": "branch",
                "repository": self.repository, "repository_owner": self.repository.split("/")[0],
                "run_id": "4242", "run_number": "7", "run_attempt": "1", "actor": "pusher",
                "workflow": self.document.get("name", ""), "head_ref": "", "base_ref": "",
                "server_url": "https://github.com", "api_url": "https://api.github.com",
                "token": "simulated-token", "workspace": str(workspace or "")}

    # -- run --------------------------------------------------------------
    def run(self):
        if not self.triggered():
            return self
        jobs = self.document.get("jobs", {})
        for name in job_order(jobs):
            self.run_job(name, jobs[name])
        return self

    def needs_context(self, job):
        return {dependency: {"result": self.job_results.get(dependency, "skipped"),
                             "outputs": self.job_outputs.get(dependency, {})}
                for dependency in needs_of(job)}

    def run_job(self, name, job):
        needs = self.needs_context(job)
        base = {"github": self.github(), "needs": needs, "vars": {}, "secrets": {}, "inputs": {},
                "env": dict(self.document.get("env") or {})}
        all_needs_ok = all(value["result"] == "success" for value in needs.values())
        functions = {"success": lambda: all_needs_ok, "failure": lambda: any(
            value["result"] == "failure" for value in needs.values()),
                     "always": lambda: True, "cancelled": lambda: False}
        if not condition(job.get("if"), base, functions):
            self.job_results[name] = "skipped"
            self.job_outputs[name] = {}
            return
        strategy = deep_render(job.get("strategy") or {}, base)
        combos = matrix_combinations(strategy)
        results, outputs = [], {}
        for index, combo in enumerate(combos):
            result, values = self.run_instance(name, job, combo, index, len(combos), needs, strategy)
            results.append(result)
            outputs.update({key: value for key, value in values.items() if value != ""})
        self.job_results[name] = ("failure" if "failure" in results else
                                  "cancelled" if "cancelled" in results else "success")
        self.job_outputs[name] = outputs

    def run_instance(self, name, job, combo, index, total, needs, strategy):
        label = instance_label(name, combo)
        root = self.workdir / "runners" / f"{len(self.instances):02d}-{re.sub(r'[^A-Za-z0-9]+', '-', label)}"
        workspace, temp = root / "work" / "repo", root / "temp"
        workspace.mkdir(parents=True)
        temp.mkdir()
        record = {"job": name, "matrix": combo, "label": label, "workspace": workspace, "temp": temp,
                  "result": None, "steps": [], "published": []}
        self.instances.append(record)
        fault = self.fault.get(len(self.instances) - 1)
        if fault == "crash":
            record["result"] = "failure"
            return "failure", {}
        context = {"github": self.github(workspace), "needs": needs, "matrix": combo or {},
                   "strategy": {"fail-fast": strategy.get("fail-fast", True), "job-index": index,
                                "job-total": total, "max-parallel": total},
                   "runner": {"temp": str(temp), "os": "Linux", "arch": "X64", "name": "sim",
                              "tool_cache": str(root / "tool")},
                   "job": {"status": "success"}, "steps": {}, "vars": {}, "secrets": {}, "inputs": {}}
        env = {}
        for layer in (self.document.get("env") or {}, job.get("env") or {}):
            env.update({key: text(deep_render(value, {**context, "env": env})) for key, value in layer.items()})
        failed = False
        post = []
        defaults = ((job.get("defaults") or {}).get("run") or {})
        for step_index, step in enumerate(job.get("steps", [])):
            step_env = dict(env)
            local = {**context, "env": step_env}
            for key, value in (step.get("env") or {}).items():
                step_env[key] = text(deep_render(value, {**context, "env": step_env}))
            functions = {"success": lambda: not failed, "failure": lambda: failed,
                         "always": lambda: True, "cancelled": lambda: False}
            step_id = step.get("id", f"__step{step_index}")
            if not condition(step.get("if"), local, functions):
                context["steps"][step_id] = {"outputs": {}, "outcome": "skipped", "conclusion": "skipped"}
                record["steps"].append({"id": step_id, "outcome": "skipped"})
                continue
            inputs = {key: text(deep_render(value, local)) for key, value in (step.get("with") or {}).items()}
            if "run" in step:
                ok, outputs, exported = self.run_shell(step, step_env, local, workspace, temp, defaults)
                env.update(exported)
            else:
                ok, outputs, later = self.run_action(step, inputs, record, workspace)
                if later:
                    post.append(later)
            outcome = "success" if ok else "failure"
            continue_on_error = truthy(deep_render(step.get("continue-on-error", False), local))
            conclusion = "success" if ok or continue_on_error else "failure"
            context["steps"][step_id] = {"outputs": outputs, "outcome": outcome, "conclusion": conclusion}
            record["steps"].append({"id": step_id, "outcome": outcome, "uses": step.get("uses"),
                                    "run": step.get("run")})
            failed |= conclusion == "failure"
            context["job"]["status"] = "failure" if failed else "success"
        for later in reversed(post):
            later(failed)
        result = "failure" if failed else "success"
        record["result"] = result
        values = {key: text(deep_render(value, {**context, "env": env}))
                  for key, value in (job.get("outputs") or {}).items()}
        if fault == "lose-publications":
            self.forget(label)
        elif isinstance(fault, tuple) and fault[0] == "replace-reports-with":
            self.replace_publications(label, fault[1])
        return result, values

    def run_shell(self, step, step_env, context, workspace, temp, defaults):
        output = temp / f"_output_{len(os.listdir(temp))}"
        github_env = temp / f"_env_{len(os.listdir(temp))}"
        output.touch()
        github_env.touch()
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("GITHUB_", "FA_", "RUNNER_", "PYTEST_"))
                       and key not in {"VIRTUAL_ENV", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT"}}
        environment.update(self.extra_env)
        environment.update({
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"], "CI": "true", "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": str(self.event_path), "GITHUB_SHA": self.sha,
            "GITHUB_REF": f"refs/heads/{self.branch}", "GITHUB_REF_NAME": self.branch,
            "GITHUB_REPOSITORY": self.repository, "GITHUB_RUN_ID": "4242", "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_WORKSPACE": str(workspace), "GITHUB_OUTPUT": str(output), "GITHUB_ENV": str(github_env),
            "GITHUB_STEP_SUMMARY": str(temp / "_summary"), "GITHUB_API_URL": "https://api.github.com",
            "RUNNER_TEMP": str(temp), "HOME": os.environ.get("HOME", str(temp))})
        environment.update(step_env)
        script = render(step["run"], context)
        directory = step.get("working-directory") or defaults.get("working-directory")
        cwd = Path(render(directory, context)) if directory else workspace
        if not cwd.is_absolute():
            cwd = workspace / cwd
        shell = step.get("shell") or defaults.get("shell") or "bash"
        if shell not in {"bash", "sh"}:
            raise SimError(f"unsupported shell {shell}")
        argv = (["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script] if shell == "bash"
                else ["sh", "-e", "-c", script])
        result = subprocess.run(argv, cwd=cwd, env=environment, capture_output=True, text=True)
        self.log(f"$ [{step.get('name', step.get('id', 'run'))}] exit {result.returncode}\n"
                 f"{result.stdout}{result.stderr}")
        exported = parse_outputs(github_env)
        return result.returncode == 0, parse_outputs(output), exported

    def log(self, message):
        with (self.workdir / "run.log").open("a", encoding="utf-8") as stream:
            stream.write(message + "\n")

    # -- actions ----------------------------------------------------------
    def run_action(self, step, inputs, record, workspace):
        uses = step.get("uses", "")
        action = uses.split("@", 1)[0]
        if action == "actions/checkout":
            return self.checkout(inputs, workspace), {}, None
        if action in NOOP_ACTIONS:
            return True, {}, None
        if action == "actions/upload-artifact":
            return self.upload(inputs, record, workspace), {}, None
        if action == "actions/download-artifact":
            return self.download(inputs, workspace), {}, None
        if action == "actions/cache/save":
            return self.save(inputs, record, workspace), {}, None
        if action == "actions/cache/restore":
            ok, outputs = self.restore(inputs, workspace)
            return ok, outputs, None
        if action == "actions/cache":
            ok, outputs = self.restore(inputs, workspace)
            hit = outputs.get("cache-hit") == "true"
            return ok, outputs, (None if hit else lambda failed: failed or self.save(inputs, record, workspace))
        raise SimError(f"simulator does not model action {uses}")

    def checkout(self, inputs, workspace):
        target = workspace / inputs["path"] if inputs.get("path") else workspace
        ref = inputs.get("ref") or self.sha
        if target.exists() and any(target.iterdir()):
            shutil.rmtree(target)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(target)], check=True, capture_output=True)
        subprocess.run(["git", "switch", "-q", "--detach", ref], cwd=target, check=True, capture_output=True)
        return True

    @staticmethod
    def resolve(value, workspace):
        path = Path(os.path.expanduser(value))
        return path if path.is_absolute() else workspace / path

    def upload(self, inputs, record, workspace):
        name = inputs.get("name") or "artifact"
        patterns = lines_of(inputs.get("path", ""))
        include = [self.resolve(p, workspace) for p in patterns if not p.startswith("!")]
        exclude = [str(self.resolve(p[1:], workspace)) for p in patterns if p.startswith("!")]
        files, roots = [], []
        for pattern in include:
            raw = str(pattern)
            if glob.has_magic(raw):
                prefix = raw[:min(raw.index(c) for c in "*?[" if c in raw)]
                roots.append(Path(prefix) if prefix.endswith(os.sep) else Path(prefix).parent)
                matched = [Path(m) for m in glob.glob(raw, recursive=True)]
            else:
                roots.append(pattern if pattern.is_dir() else pattern.parent)
                matched = [pattern] if pattern.exists() else []
            for item in matched:
                files.extend(sorted(p for p in item.rglob("*") if p.is_file()) if item.is_dir()
                             else [item] if item.is_file() else [])
        files = [f for f in dict.fromkeys(files) if not any(fnmatchcase(str(f), e) for e in exclude)]
        if not files:
            behaviour = inputs.get("if-no-files-found") or "warn"
            self.log(f"upload-artifact {name}: no files ({behaviour})")
            return behaviour != "error"
        if name in self.artifacts and not truthy(inputs.get("overwrite") == "true"):
            self.log(f"upload-artifact {name}: conflict, artifact already exists")
            return False
        common = Path(os.path.commonpath([str(r) for r in roots])) if roots else workspace
        stored = {str(f.relative_to(common)) if f.is_relative_to(common) else f.name: f.read_bytes() for f in files}
        self.artifacts[name] = {"files": stored, "owner": record["label"]}
        record["published"].append(("artifact", name))
        return True

    def download(self, inputs, workspace):
        destination = self.resolve(inputs["path"], workspace) if inputs.get("path") else workspace
        name = inputs.get("name")
        if name:
            if name not in self.artifacts:
                self.log(f"download-artifact: {name} not found")
                return False
            selected, merge = [name], True
        else:
            pattern = inputs.get("pattern")
            selected = sorted(n for n in self.artifacts if not pattern or fnmatchcase(n, pattern))
            merge = inputs.get("merge-multiple", "false").lower() == "true"
        for artifact in selected:
            target = destination if merge else destination / artifact
            for relative, content in self.artifacts[artifact]["files"].items():
                path = target / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        destination.mkdir(parents=True, exist_ok=True)
        return True

    def save(self, inputs, record, workspace):
        key = inputs.get("key", "")
        paths = [self.resolve(p, workspace) for p in lines_of(inputs.get("path", ""))]
        self.cache_saves.append({"key": key, "owner": record["label"], "job": record["job"],
                                 "paths": [str(p) for p in paths]})
        if not key or any(not p.is_file() for p in paths):
            self.log(f"cache save {key}: path missing")
            return False
        if key in self.caches:
            return True  # GitHub keeps the first entry and warns.
        self.caches[key] = {"files": [p.read_bytes() for p in paths], "owner": record["label"]}
        record["published"].append(("cache", key))
        return True

    def restore(self, inputs, workspace):
        key = inputs.get("key", "")
        paths = [self.resolve(p, workspace) for p in lines_of(inputs.get("path", ""))]
        candidates = [key] if key in self.caches else []
        if not candidates:
            for prefix in lines_of(inputs.get("restore-keys", "")):
                candidates = sorted(k for k in self.caches if k.startswith(prefix))[:1]
                if candidates:
                    break
        outputs = {"cache-primary-key": key, "cache-hit": "false", "cache-matched-key": ""}
        if candidates:
            entry = self.caches[candidates[0]]
            for path, content in zip(paths, entry["files"]):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            outputs.update({"cache-matched-key": candidates[0], "cache-hit": text(candidates[0] == key)})
        return True, outputs

    # -- faults -----------------------------------------------------------
    def forget(self, label):
        for kind, name in [item for record in self.instances if record["label"] == label
                           for item in record["published"]]:
            (self.artifacts if kind == "artifact" else self.caches).pop(name, None)

    def replace_publications(self, label, source_label):
        """The instance's published reports (artifacts or caches, whichever transport the
        workflow uses) are replaced by a copy of another instance's reports of the same kind."""
        replaced = False
        for store, merge in ((self.artifacts, True), (self.caches, False)):
            mine = sorted(name for name, value in store.items() if value["owner"] == label)
            theirs = sorted(name for name, value in store.items() if value["owner"] == source_label)
            if not mine or not theirs:
                continue
            if merge:
                donor = {}
                for name in theirs:
                    donor.update(store[name]["files"])
                for name in mine:
                    store[name] = {"files": dict(donor), "owner": label}
            else:
                for position, name in enumerate(mine):
                    source = store[theirs[min(position, len(theirs) - 1)]]
                    store[name] = {"files": list(source["files"]), "owner": label}
            replaced = True
        if not replaced:
            raise SimError(f"cannot replace reports of {label} with {source_label}: "
                           "neither published an artifact or cache")

def load(text_or_path):
    value = Path(text_or_path).read_text(encoding="utf-8") if isinstance(text_or_path, Path) else text_or_path
    return yaml.safe_load(value)
