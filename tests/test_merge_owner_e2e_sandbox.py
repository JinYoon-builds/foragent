"""Real scheduler/runner/GhTracker with local Git and an executable JSON GitHub.

Only external boundaries are substituted: models, Git commit/ref transport,
machine sensors, launchd and the detached owner launcher. No publication, comparison,
queue, approval, repair or completion function is replaced. Workers are forked
processes that exit naturally. Forbidden Git commands use fast-import/fetch.
"""

from contextlib import contextmanager
from datetime import timedelta
import fcntl
import hashlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET

import pytest

from conftest import ACCOUNT_HOME, BASE_TIME, REAL_HOME, git_command
from foragent import capacity, merge_owner, orch_queue, runner, scheduler
from foragent.ci_workflow import WORKFLOW, measurement_scope, read_exclusions
from foragent.config import load_config
from foragent.executors import ExecResult
from foragent.git import GitRepo
from foragent.run_previous import retry_marker
from foragent.state import StateStore
from foragent.tracker import GhTracker


# Deliberately has no network client or generic command proxy. Unknown argv is
# an error, so added GhTracker dependencies cannot silently bypass this service.
FAKE_GH = r'''
import fcntl, io, json, os, pathlib, subprocess, sys, urllib.parse, zipfile
home = pathlib.Path.home().resolve()
root = pathlib.Path(os.environ['FA_SANDBOX']).resolve()
assert home == root / 'home'
assert str(home) not in json.loads(os.environ['FA_REAL_HOMES'])
assert pathlib.Path(os.environ['XDG_DATA_HOME']).resolve().is_relative_to(home)
path = root / 'github.json'
args = sys.argv[1:]
def git(*argv, input=None):
    assert not {'commit', 'push', 'reset', 'checkout', 'stash'}.intersection(argv)
    return subprocess.run(['git', *argv], cwd=root / 'origin.git', input=input,
                          check=True, capture_output=True, text=True).stdout.strip()
def flag(name):
    return args[args.index(name) + 1]
def pr_row(item):
    return {**item, 'head': {**item['head'], 'sha': git('rev-parse', item['head']['ref'])}}
with (root / 'github.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    state = json.loads(path.read_text())
    state['calls'].append(args)
    result, binary, failure = None, None, None
    if args[:2] == ['api', 'graphql']:
        assert 'lastEditedAt' in flag('-f')
        result = {'data': {'repository': {'issue': {'lastEditedAt': None}}}}
    elif args[0] == 'api':
        endpoint = next(a for a in args[1:] if a.startswith('repos/'))
        parsed = urllib.parse.urlsplit(endpoint)
        route = parsed.path.removeprefix('repos/sandbox/repo').strip('/').split('/')
        query = urllib.parse.parse_qs(parsed.query)
        method = flag('-X') if '-X' in args else 'GET'
        if route == ['']:
            result = {'full_name': 'sandbox/repo', 'default_branch': 'main'}
        elif route[0] == 'issues':
            if len(route) == 1:
                result = [i for i in state['issues'].values()
                          if query.get('state', ['all'])[0] in ('all', i['state'])]
            else:
                issue = state['issues'].get(route[1]) or state['prs'][route[1]]
                if len(route) == 2:
                    result = issue
                elif route[2] == 'events':
                    result = state['events'][route[1]]
                elif route[2] == 'parent':
                    failure = 'HTTP 404'
                elif route[2] in ('sub_issues', 'dependencies'):
                    result = []
                elif route[2] == 'comments':
                    comments = state['comments'].setdefault(route[1], [])
                    if method == 'POST':
                        comments.append({'id': 1 + sum(map(len, state['comments'].values())),
                                         'user': {'login': 'sandbox'}, 'created_at': state['now'],
                                         **json.load(sys.stdin)})
                        result = comments[-1]
                    else:
                        result = comments
                elif route[2] == 'labels':
                    labels = {i['name'] for i in issue['labels']}
                    if method == 'POST':
                        labels.update(a.split('=', 1)[1] for a in args if a.startswith('labels[]='))
                    elif method == 'DELETE':
                        labels.discard(urllib.parse.unquote(route[3]))
                    else:
                        raise AssertionError(args)
                    issue['labels'] = [{'name': name} for name in sorted(labels)]
                    result = issue['labels']
        elif route[0] == 'pulls':
            if len(route) == 1:
                result = [pr_row(p) for p in state['prs'].values()
                          if p['state'] == query.get('state', ['open'])[0]
                          and ('head' not in query or p['head']['ref'] == query['head'][0].split(':', 1)[1])]
            else:
                pr = pr_row(state['prs'][route[1]])
                if len(route) == 2:
                    result = pr
                elif route[2] == 'files':
                    result = [{'filename': p} for p in git('diff', '--name-only',
                        pr['base']['ref'] + '...' + pr['head']['sha']).splitlines()]
        elif route[0] == 'commits' and route[2] == 'check-runs':
            result = []
        elif route[:2] == ['actions', 'runs']:
            if len(route) == 2:
                row = state['ci'].get(query['head_sha'][0])
                result = [row['run']] if row else []
            elif route[3] == 'artifacts':
                result = [r['artifact'] for r in state['ci'].values() if str(r['run']['id']) == route[2]]
        elif route[:2] == ['actions', 'artifacts'] and route[3] == 'zip':
            row = next(r for r in state['ci'].values() if str(r['artifact']['id']) == route[2])
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w') as archive:
                archive.writestr('shard-0.json', json.dumps(row['report']))
            binary = buffer.getvalue()
    elif args[:2] == ['pr', 'create']:
        number = str(100 + len(state['prs']))
        state['prs'][number] = {'number': int(number), 'head': {'ref': flag('--head')},
            'base': {'ref': flag('--base')}, 'title': flag('--title'), 'body': sys.stdin.read(),
            'merged_at': None, 'state': 'open', 'html_url': 'https://github.com/sandbox/repo/pull/' + number}
        result = state['prs'][number]['html_url']
    elif args[:2] == ['pr', 'checks']:
        result = [{'bucket': 'pass', 'name': 'units'}]
    elif args[:2] == ['pr', 'view']:
        result = {'title': state['prs'][args[2]]['title']}
    elif args[:2] in (['pr', 'close'], ['pr', 'edit'], ['pr', 'merge']):
        pr = state['prs'][args[2]]
        assert '--delete-branch' not in args
        if args[1] == 'close':
            assert pr['merged_at'] is None
            pr['state'] = 'closed'
            state['repairs'].append(pr['number'])
        elif args[1] == 'edit':
            pr.update(base={'ref': flag('--base')}, title=flag('--title'), body=sys.stdin.read())
        else:
            assert pr['state'] == 'open' and pr['merged_at'] is None
            head = git('rev-parse', pr['head']['ref'])
            assert head == flag('--match-head-commit')
            parent = git('rev-parse', 'main')
            tree = git('merge-tree', '--write-tree', parent, head).splitlines()[0]
            subject = flag('--subject')
            stream = ['commit refs/heads/main\n', 'committer Sandbox <test@example.com> 1792486800 +0000\n',
                      f'data {len(subject.encode())}\n{subject}\n', f'from {parent}\n', 'deleteall\n']
            for entry in git('ls-tree', '-r', '-z', tree).split('\0'):
                if entry:
                    metadata, name = entry.split('\t', 1)
                    mode, _, blob = metadata.split()
                    stream.append(f'M {mode} {blob} {json.dumps(name)}\n')
            git('fast-import', '--quiet', input=''.join(stream) + '\ndone\n')
            pr.update(state='closed', merged_at=state['now'])
            state['merges'].append({'pr': pr['number'], 'subject': subject, 'sha': git('rev-parse', 'main')})
            if state.pop('lose_merge_response', False):
                failure = 'HTTP 502 after successful merge'
        result = ''
    elif args[:2] == ['issue', 'close']:
        state['issues'][args[2]]['state'] = 'closed'
        state['closures'].append(int(args[2]))
        result = ''
    path.write_text(json.dumps(state))
    if failure:
        sys.stderr.write(failure)
        sys.exit(1)
    assert result is not None or binary is not None, args
    if binary is not None:
        sys.stdout.buffer.write(binary)
    else:
        print(result if isinstance(result, str) else json.dumps(result))
'''


@contextmanager
def service_file(root):
    with (root / "github.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / "github.json"
        state = json.loads(path.read_text())
        yield state
        path.write_text(json.dumps(state))


class Sandbox:
    def __init__(self, root, monkeypatch):
        root = root.resolve()
        self.root, self.monkeypatch = root, monkeypatch
        self.home, self.origin, self.repo = root / "home", root / "origin.git", root / "repo"
        self.home.mkdir()
        monkeypatch.setenv("HOME", str(self.home))
        for key, suffix in (("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"),
                            ("XDG_STATE_HOME", ".local/state"), ("XDG_CACHE_HOME", ".cache")):
            monkeypatch.setenv(key, str(self.home / suffix))
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(self.home / ".gitconfig"))
        monkeypatch.setenv("FA_SANDBOX", str(root))
        monkeypatch.setenv("FA_REAL_HOMES", json.dumps([str(p.resolve()) for p in (REAL_HOME, ACCOUNT_HOME)]))
        scratch = root / "tmp"
        scratch.mkdir()
        # Scope library-created worktrees and command buffers to this sandbox,
        # independently of where pytest allocated tmp_path or the user's TMPDIR.
        monkeypatch.setattr(tempfile, "tempdir", str(scratch))
        bindir = root / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text(f"#!{sys.executable}\n" + FAKE_GH)
        gh.chmod(0o755)
        monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
        self.clock = BASE_TIME + timedelta(hours=1)
        self.workers, self.owner_requests = [], []
        self.assert_isolated()
        git_command(root, "init", "--bare", "--initial-branch=main", str(self.origin))
        # Real settings loader and committed settings; no Config(**fake_shape).
        settings = {
            "repo": "sandbox/repo", "review": "fast", "merge_owner": True, "checks": "ci",
            "checks_fallback": None, "base_branch": "main", "ci_shards": 1,
            "executor": "claude", "reviewer": "codex", "approvers": ["human"],
            "max_parallel": 1, "test_slots": 1, "merge_group_max": 1, "max_retries": 1,
            "skill_libraries": [], "test_command": f"{shlex.quote(sys.executable)} -m pytest -q",
        }
        files = {".foragent.yml": json.dumps(settings), "src/value.py": "VALUE = 0\n",
                 "pytest.ini": "[pytest]\nmarkers =\n    local_only: machine checks\n", WORKFLOW:
                 "name: measurement\non: [pull_request]\njobs: {}\n"}
        for n in (1, 2, 4, 5):
            files[f"tests/acceptance/test_own_{n}.py"] = (
                "from pathlib import Path\ndef test_own():\n"
                f"    assert Path('src/member_{n}.py').exists()\n")
        self.main_update(files, "Seed")
        git_command(root, "clone", str(self.origin), str(self.repo))
        self.config = load_config(self.repo)
        assert self.config.merge_owner and self.config.checks == "ci"
        self.state_root = StateStore.for_repo("sandbox/repo").root
        assert self.state_root.is_relative_to(self.home)
        issues, events = {}, {}
        for n in (1, 2, 4, 5):
            body = ("## Goal\nDeliver member.\n## Files\n" + f"- src/member_{n}.py\n"
                    "## Done when\n```sh\n" + settings["test_command"]
                    + f" tests/acceptance/test_own_{n}.py\n```\n")
            issues[str(n)] = {"number": n, "title": f"Member {n}", "body": body,
                              "state": "open", "labels": [{"name": "approved"}]}
            events[str(n)] = [{"label": {"name": "approved"}, "event": "labeled",
                               "actor": {"login": "human"}, "created_at": self.clock.isoformat()}]
        (root / "github.json").write_text(json.dumps({"issues": issues, "events": events, "comments": {},
            "prs": {}, "ci": {}, "calls": [], "merges": [], "repairs": [], "closures": [],
            "now": self.clock.isoformat(), "lose_merge_response": True}))
        # Sensors and launchers are external resources. All policy stays real.
        monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.0))
        monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.0))
        monkeypatch.setattr(scheduler.launchd, "refresh_from_tick", lambda *args: None)
        monkeypatch.setattr(merge_owner, "start", lambda ctx, report:
                            self.owner_requests.append(ctx) if merge_owner.admissions(ctx.store) else None)
        # Fail if a real system service/model/remote command ever escapes.
        popen = subprocess.Popen

        def local_only(argv, *args, **kwargs):
            self.assert_isolated()
            assert isinstance(argv, (list, tuple)), argv
            assert Path(argv[0]).name in {"git", "gh", Path(sys.executable).name, "sh"}, argv
            if Path(argv[0]).name == "gh":
                assert Path(shutil.which("gh")).resolve() == gh
            if Path(argv[0]).name == "git":
                assert not {"commit", "push", "reset", "checkout", "stash"}.intersection(argv), argv
                assert not any("://" in arg or arg.startswith("git@") for arg in argv), argv
            if kwargs.get("cwd") is None:
                kwargs["cwd"] = self.repo
            cwd = Path(kwargs["cwd"]).resolve()
            assert cwd.is_relative_to(root), cwd
            return popen(argv, *args, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", local_only)

    def assert_isolated(self):
        assert Path.home().resolve() == self.home
        assert self.home not in {REAL_HOME.resolve(), ACCOUNT_HOME.resolve()}
        assert self.home == self.root / "home"
        assert Path(os.environ["FA_SANDBOX"]).resolve() == self.root
        for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
            assert Path(os.environ[name]).resolve().is_relative_to(self.home), name
        assert Path(tempfile.gettempdir()).resolve().is_relative_to(self.root)

    def main_update(self, files, title):
        before = subprocess.run(["git", "rev-parse", "--verify", "main"], cwd=self.origin,
                                text=True, capture_output=True)
        stream = ["commit refs/heads/main\n", "committer Sandbox <test@example.com> 1792483200 +0000\n",
                  f"data {len(title.encode())}\n{title}\n"]
        if before.returncode == 0:
            stream.append(f"from {before.stdout.strip()}\n")
        for path, content in files.items():
            stream.append(f"M 100644 inline {path}\ndata {len(content.encode())}\n{content}\n")
        git_command(self.origin, "fast-import", "--quiet", input="".join(stream) + "\ndone\n")

    def snapshot(self, root, title):
        parent = git_command(root, "rev-parse", "HEAD")
        symbolic = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=root,
                                  text=True, capture_output=True)
        ref = symbolic.stdout.strip() if symbolic.returncode == 0 else "refs/foragent/sandbox-snapshot"
        stream = [f"commit {ref}\n", "committer Sandbox <test@example.com> 1792486800 +0000\n",
                  f"data {len(title.encode())}\n{title}\n", f"from {parent}\n"]
        merge = root / git_command(root, "rev-parse", "--git-path", "MERGE_HEAD")
        if merge.exists():
            stream.extend(f"merge {sha}\n" for sha in merge.read_text().splitlines())
        stream.append("deleteall\n")
        for entry in git_command(root, "ls-files", "--stage", "-z").split("\0"):
            if entry:
                metadata, path = entry.split("\t", 1)
                mode, blob, stage = metadata.split()
                assert stage == "0"
                stream.append(f"M {mode} {blob} {json.dumps(path)}\n")
        git_command(root, "fast-import", "--quiet", "--force", input="".join(stream) + "\ndone\n")
        if symbolic.returncode:
            git_command(root, "update-ref", "--no-deref", "HEAD", git_command(root, "rev-parse", ref))
        if merge.exists():
            git_command(root, "merge", "--quit")

    def git_transport(self, argv):
        root, args = self.repo, list(argv)
        while args[:1] == ["-C"]:
            root, args = Path(args[1]), args[2:]
        if "commit" in args:
            self.snapshot(root, args[-1])
            return ""
        if args[:1] == ["push"]:
            source, destination = args[-1].split(":", 1) if ":" in args[-1] else (args[-1], args[-1])
            branch = destination.removeprefix("refs/heads/")
            for arg in args:
                if arg.startswith("--force-with-lease="):
                    assert (GitRepo(self.repo).remote_branch_sha(branch) or "") == arg.rsplit(":", 1)[1]
            git_command(self.origin, "fetch", str(root), f"+{source}:refs/heads/{branch}")
            self.publish_measurement(root, git_command(root, "rev-parse", source))
            return ""
        if "merge" in args and "--no-edit" in args:
            result = git_command(root, *args, "--no-commit")
            if (root / git_command(root, "rev-parse", "--git-path", "MERGE_HEAD")).exists():
                self.snapshot(root, "Offline base refresh")
            return result
        return git_command(root, *args)

    def measure(self, sha, excluded):
        key = hashlib.sha256(json.dumps([sha, excluded]).encode()).hexdigest()
        directory = self.root / "measurements" / key
        cached = directory / "suite.json"
        if cached.exists():
            return json.loads(cached.read_text())
        directory.mkdir(parents=True)
        data = subprocess.run(["git", "archive", sha], cwd=self.origin, check=True, capture_output=True).stdout
        with tarfile.open(fileobj=io.BytesIO(data)) as bundle:
            bundle.extractall(directory, filter="data")
        xml = directory / "result.xml"
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "-o", "addopts=", "-o", "junit_family=legacy", "--rootdir=.", f"--junitxml={xml}",
            "tests/acceptance", *[f"--ignore={p}" for p in excluded]], cwd=directory,
            capture_output=True, text=True, env={**os.environ, "PYTEST_ADDOPTS": "", "PYTHONPATH": ""})
        assert result.returncode in (0, 1, 5), result.stdout + result.stderr
        collected, passing = [], []
        for node in ET.parse(xml).iter("testcase"):
            identity = f"{node.get('file')}::{node.get('name')}"
            collected.append(identity)
            if not any(node.find(tag) is not None for tag in ("failure", "error", "skipped")):
                passing.append(identity)
        suite = {"complete": True, "collected_ids": sorted(collected), "passing_ids": sorted(passing)}
        cached.write_text(json.dumps(suite))
        return suite

    def publish_measurement(self, root, head):
        base = git_command(self.origin, "rev-parse", "main")
        excluded = read_exclusions(GitRepo(root, self.git_transport), base) or []
        # The newest published trailer between base and HEAD pins exclusions.
        base_suite, head_suite = self.measure(base, excluded), self.measure(head, excluded)
        with service_file(self.root) as state:
            if head in state["ci"]:
                return
            run_id = 1000 + len(state["ci"])
            state["ci"][head] = {
                "run": {"id": run_id, "head_sha": head, "status": "completed", "conclusion": "success",
                    "event": "pull_request", "path": WORKFLOW, "repository": {"full_name": "sandbox/repo"},
                    "head_repository": {"full_name": "sandbox/repo"}},
                "artifact": {"id": run_id * 10, "name": "acceptance-0", "expired": False,
                             "workflow_run": {"id": run_id, "head_sha": head}},
                "report": {"schema_version": 1, "repository": "sandbox/repo", "base_sha": base,
                    "head_sha": head, "excluded": excluded, "shard_count": 1, "shard_index": 0,
                    "duration_seconds": 0.01, "scope": measurement_scope(self.config, self.repo, excluded),
                    "base": base_suite, "head": head_suite}}

    def launch_worker(self, n):
        def work():
            self.assert_isolated()
            repair = retry_marker(self.state_root, n).exists()

            class Model:
                def __init__(self, name):
                    self.name = name

                def run(executor_self, **kwargs):
                    if executor_self.name == "claude":
                        (kwargs["worktree"] / f"src/member_{n}.py").write_text(
                            f"VALUE = {50 if repair and n == 5 else n}\n")
                    return ExecResult(0, False, "VERDICT: PASS", kwargs["log_path"])

            ctx = runner.RunContext("sandbox/repo", self.repo, GhTracker("sandbox/repo"),
                StateStore(self.state_root, now=lambda: self.clock), self.config,
                GitRepo(self.repo, self.git_transport), now=lambda: self.clock,
                executor_factory=Model, sleep=lambda seconds: (_ for _ in ()).throw(AssertionError("Worker polled")),
                poll_hook=lambda execute, poll: execute())
            outcome = runner.run_issue(n, ctx)
            (self.root / f"worker-{n}-{self.clock.timestamp()}.json").write_text(json.dumps({"outcome": outcome}))
            assert outcome == "merge-queued", ctx.store.log_path(n).read_text()

        process = multiprocessing.get_context("fork").Process(target=work)
        process.start()
        self.workers.append(process)
        process.join(60)
        assert not process.is_alive(), "Sandbox worker did not exit naturally; no signals are allowed"
        assert process.exitcode == 0, StateStore(self.state_root).log_path(n).read_text()
        assert git_command(self.origin, "rev-parse", f"foragent/issue-{n}")
        return None

    def tick(self, *, start_runs=False):
        self.assert_isolated()
        waiting = StateStore(self.state_root).github_wait_until(self.clock)
        if waiting is not None:
            self.clock = waiting
        self.clock += timedelta(seconds=1)
        with service_file(self.root) as state:
            state["now"] = self.clock.isoformat()
        # Discard contexts, trackers, snapshots and stores between every tick.
        ctx = scheduler.TickContext("sandbox/repo", self.repo, GhTracker("sandbox/repo"),
            StateStore(self.state_root, now=lambda: self.clock), load_config(self.repo),
            GitRepo(self.repo, self.git_transport), now=lambda: self.clock, launcher=self.launch_worker,
            start_runs=start_runs, baseline_launcher=lambda *args: None, committed_config=True)
        report = scheduler.tick(ctx)
        while self.owner_requests:
            owner = self.owner_requests.pop(0)
            with owner.store.tick_lock() as acquired:
                assert acquired, "Owner must run after scheduler.tick releases its lock"
            lock = owner.store.lock_acceptance_baseline("merge-owner", blocking=False)
            assert lock is not None
            with lock:
                merge_owner.tick(owner, report)
        assert not ctx.store.active_runs()
        assert all(not p.is_alive() and p.exitcode == 0 for p in self.workers)
        return ctx.store


def test_four_approved_issues_merge_after_restart_and_lost_test_repair(tmp_path, monkeypatch):
    sandbox = Sandbox(tmp_path, monkeypatch)
    store = sandbox.tick(start_runs=True)
    assert len(sandbox.workers) == 4
    assert len(merge_owner.admissions(store)) == 4
    assert not store.active_runs()
    assert not json.loads((tmp_path / "github.json").read_text())["merges"]
    baseline = git_command(sandbox.origin, "rev-parse", "main")
    orch_queue.set_global(store, [2, 4, 1, 5])
    # A new passing main test is lost only on #5's otherwise green CI head.
    sandbox.main_update({"tests/acceptance/test_new_main.py":
        "from pathlib import Path\nimport runpy\ndef test_new_main():\n"
        "    path = Path('src/member_5.py')\n"
        "    assert not path.exists() or runpy.run_path(str(path))['VALUE'] == 50\n"}, "New contract")
    saw_repair, repaired_pr = False, None
    for _ in range(20):
        store = sandbox.tick()
        state = json.loads((tmp_path / "github.json").read_text())
        if retry_marker(store.root, 5).exists():
            assert not saw_repair, "Repair should produce exactly one replacement worker"
            saw_repair = True
            repaired_pr = state["repairs"][0]
            assert state["prs"][str(repaired_pr)]["state"] == "closed"
            assert state["issues"]["5"]["state"] == "open"
            assert "ready" in {label["name"] for label in state["issues"]["5"]["labels"]}
            assert len(state["merges"]) == 3
            comments = "\n".join(c["body"] for c in state["comments"]["5"])
            assert "Acceptance baseline regression" in comments and "test_new_main" in comments
            sandbox.tick(start_runs=True)
        if len(state["merges"]) == 4 and not merge_owner.admissions(store):
            break
    else:
        raise AssertionError("Sandbox did not drain: " + store.log_path(5).read_text())
    assert saw_repair and len(sandbox.workers) == 5
    assert [row["subject"] for row in state["merges"]] == [f"Member {n} (#{n})" for n in (2, 4, 1, 5)]
    commits = git_command(sandbox.origin, "log", "--first-parent", "--reverse", "--format=%H %s",
                          f"{baseline}..main").splitlines()
    assert [row.split(" ", 1)[1] for row in commits] == ["New contract", *[r["subject"] for r in state["merges"]]]
    assert [row.split()[0] for row in commits[1:]] == [r["sha"] for r in state["merges"]]
    assert len(state["prs"]) == 5 and state["repairs"] == [repaired_pr]
    assert sorted(state["closures"]) == [1, 2, 4, 5]
    assert all(issue["state"] == "closed" for issue in state["issues"].values())
    assert len(store.owner_merge_facts()) == 4
    turns = [json.loads(line) for line in (store.root / "merge-queue.jsonl").read_text().splitlines()]
    assert [r["n"] for r in turns if r["event"] == "turn" and r["result"] == "merged"] == [2, 4, 1, 5]
    before = state["merges"], state["closures"], state["repairs"], len(state["prs"])
    sandbox.tick()
    sandbox.tick()
    after = json.loads((tmp_path / "github.json").read_text())
    assert (after["merges"], after["closures"], after["repairs"], len(after["prs"])) == before
    assert all(args[0] in {"api", "pr", "issue"} for args in after["calls"])
    sandbox.assert_isolated()


@pytest.mark.parametrize("exists,lease,allowed", [
    (False, "empty", True),
    (True, "matching", True),
    (False, "matching", False),
    (True, "empty", False),
    (True, "wrong", False),
], ids=["create", "replace", "missing-with-sha", "existing-with-empty", "wrong-sha"])
def test_sandbox_push_enforces_creation_and_replacement_leases(tmp_path, monkeypatch, exists, lease, allowed):
    sandbox = Sandbox(tmp_path, monkeypatch)
    old = git_command(sandbox.origin, "rev-parse", "main")
    branch = "lease-check"
    if exists:
        git_command(sandbox.origin, "update-ref", f"refs/heads/{branch}", old)
    sandbox.main_update({"src/new.py": "VALUE = 7\n"}, "Next head")
    git_command(sandbox.repo, "fetch", "origin", "main:refs/heads/upload")
    new = git_command(sandbox.repo, "rev-parse", "upload")
    assert old != new
    expected = {"empty": "", "matching": old, "wrong": new}[lease]
    measurements = []
    monkeypatch.setattr(sandbox, "publish_measurement", lambda root, sha: measurements.append((root, sha)))
    argv = ["push", f"--force-with-lease=refs/heads/{branch}:{expected}",
            "origin", f"upload:refs/heads/{branch}"]
    if allowed:
        sandbox.git_transport(argv)
    else:
        with pytest.raises(AssertionError):
            sandbox.git_transport(argv)
    current = git_command(sandbox.origin, "ls-remote", "--heads", ".", f"refs/heads/{branch}")
    actual = current.split()[0] if current else None
    assert actual == (new if allowed else old if exists else None)
    assert measurements == ([(sandbox.repo, new)] if allowed else [])


@pytest.mark.parametrize(("exists", "lease", "accepted"), [
    (False, "absent", True),
    (False, "old", False),
    (True, "absent", False),
    (True, "old", True),
    (True, "new", False),
])
def test_sandbox_push_lease_matches_existing_or_absent_remote_ref(tmp_path, monkeypatch, exists, lease, accepted):
    sandbox = Sandbox(tmp_path, monkeypatch)
    branch = "foragent/issue-1"
    old = git_command(sandbox.origin, "rev-parse", "main")
    if exists:
        git_command(sandbox.origin, "update-ref", f"refs/heads/{branch}", old)
    sandbox.main_update({"src/changed.py": "VALUE = 99\n"}, "New local head")
    new = git_command(sandbox.origin, "rev-parse", "main")
    git_command(sandbox.repo, "fetch", "origin", "main")
    git_command(sandbox.repo, "update-ref", "refs/heads/main", new)
    expected = {"absent": "", "old": old, "new": new}[lease]
    args = ["push", f"--force-with-lease=refs/heads/{branch}:{expected}", "origin",
            f"HEAD:refs/heads/{branch}"]
    if accepted:
        sandbox.git_transport(args)
    else:
        with pytest.raises(AssertionError):
            sandbox.git_transport(args)
    assert GitRepo(sandbox.repo).remote_branch_sha(branch) == (new if accepted else old if exists else None)
    ci = json.loads((tmp_path / "github.json").read_text())["ci"]
    assert list(ci) == ([new] if accepted else [])
