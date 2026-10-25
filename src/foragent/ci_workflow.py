"""Generate the opt-in acceptance workflow and explain init's checks choice."""

import json
from pathlib import Path
import subprocess
import sys

import yaml

from foragent.config import load_config
from foragent.git import GitError, GitRepo

WORKFLOW = ".github/workflows/foragent-acceptance.yml"
PUBLISH_WORKFLOW = ".github/workflows/foragent-acceptance-publish.yml"
EXCLUSIONS = ".foragent/acceptance-exclusions.json"
TRUSTED_RUNNER = "uv run --no-project --python 3.12 --with pytest --with 'foragent @ git+https://github.com/JinYoon-builds/foragent' python -m "


def acceptance_command(config, root):
    from foragent.order import detect_test_commands
    from foragent.run_checks import _pytest_command

    command = config.test_command or next(iter(detect_test_commands(root)), None)
    return _pytest_command(command) if command else None


def measurement_scope(config, root, excluded):
    return {"acceptance_dir": config.acceptance_dir, "command": acceptance_command(config, root),
            "marker": "not local_only", "excluded": list(excluded), "shard_count": config.ci_shards}


def producer_run_id(check, repo):
    """Use GitHub's suite/run relationship, never the check author's URL."""
    if not isinstance(check, dict) or not isinstance(check.get("app"), dict) or check["app"].get("slug") != "github-actions":
        return None
    suite = check.get("check_suite")
    run = check.get("workflow_run")
    if not isinstance(suite, dict) or type(suite.get("id")) is not int or not isinstance(run, dict):
        return None
    repository = run.get("repository") or {}
    if (suite["id"] <= 0 or run.get("check_suite_id") != suite["id"]
            or repository.get("full_name") != repo
            or not repository.get("default_branch")
            or run.get("head_branch") != repository["default_branch"]
            or run.get("path", "").split("@", 1)[0] != PUBLISH_WORKFLOW
            or run.get("event") != "workflow_run"):
        return None
    return run["id"] if type(run.get("id")) is int and run["id"] > 0 else None


def repository_private(repo, run=None):
    try:
        if run is None:
            output = subprocess.run(["gh", "repo", "view", repo, "--json", "isPrivate"],
                                    check=True, capture_output=True, text=True).stdout
        else:
            output = run(["repo", "view", repo, "--json", "isPrivate"], input=None)
        data = json.loads(output)
        private = data.get("isPrivate") if isinstance(data, dict) else None
        return private if type(private) is bool else None
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def initial_checks(repo, run, *, command=None):
    from foragent.run_checks import _pytest_command

    private = repository_private(repo, run)
    mode = "ci" if private is False else "local"
    if command is None or _pytest_command(command) is None:
        mode = "local"
        reason = "CI acceptance requires pytest; using local checks for this test command"
    elif private is False:
        reason = "public repository; GitHub-hosted Actions minutes are free"
    elif private is True:
        reason = "private repository; avoid paid Actions minutes"
    else:
        reason = "could not read repository visibility; using local checks"
    line = f"checks: {mode} ({reason}); change checks in .foragent.yml."
    if private is True:
        line += (" A self-hosted runner avoids measurement minutes; set ci_runs_on: [self-hosted, linux] there."
                 " Publishing uses a fresh GitHub-hosted runner.")
    print(("Note: " if private is None else "") + line)
    return mode


def workflow_text(config):
    base = config.base_branch.replace("'", "''")
    job = {
        "if": ("github.event.pull_request.head.repo.full_name == github.repository && "
               "startsWith(github.event.pull_request.head.ref, 'foragent/issue-') && "
               f"github.event.pull_request.base.ref == '{base}'"),
        "runs-on": config.ci_runs_on,
        "permissions": {"contents": "read"},
        "strategy": {"fail-fast": False, "matrix": {"shard": list(range(config.ci_shards))}},
        "outputs": {"comparison": "${{ steps.compare.outputs.comparison }}"},
        "steps": [
            {"uses": "actions/checkout@v4", "with": {"ref": "${{ github.event.pull_request.head.sha }}", "fetch-depth": 0,
                                                     "persist-credentials": False}},
            {"uses": "astral-sh/setup-uv@v6"},
            {"name": "Compare acceptance passing sets", "id": "compare",
             "working-directory": "${{ runner.temp }}",
             "env": {"FA_REPOSITORY": "${{ github.repository }}", "FA_CHECKOUT": "${{ github.workspace }}",
                     "FA_BASE_SHA": "${{ github.event.pull_request.base.sha }}",
                     "FA_HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
                     "FA_SHARD": "${{ matrix.shard }}", "FA_SHARDS": str(config.ci_shards),
                     "FA_REPORT_FILE": "${{ runner.temp }}/shard-${{ matrix.shard }}.json"},
             "run": TRUSTED_RUNNER + "foragent.ci_shard"},
            {"uses": "actions/upload-artifact@v4", "if": "steps.compare.outputs.comparison != ''",
             "with": {"name": "acceptance-${{ matrix.shard }}",
             "path": "${{ runner.temp }}/shard-${{ matrix.shard }}.json", "if-no-files-found": "error"}},
        ],
    }
    return "# Generated by fa ci workflow; edit .foragent.yml, then regenerate.\n" + yaml.safe_dump({
        "name": "foragent-acceptance", "on": ["pull_request"],
        # Every measurement job explicitly overrides these permissions to read
        # only. Publication executes in the default branch's separate workflow.
        "permissions": {"contents": "read", "checks": "write"}, "jobs": {"acceptance": job},
    }, sort_keys=False)


def publisher_workflow_text(config):
    # workflow_run executes the default branch's workflow on a fresh hosted
    # runner. It never checks out or imports the measured PR or its dependencies.
    return "# Generated by fa ci workflow.\n" + yaml.safe_dump({
        "name": "foragent-acceptance-publish",
        "on": {"workflow_run": {"workflows": ["foragent-acceptance"], "types": ["completed"]}},
        "permissions": {"actions": "read", "checks": "write"},
        "jobs": {"publish": {
            "if": ("github.event.workflow_run.conclusion == 'success' && "
                   "github.event.workflow_run.event == 'pull_request' && "
                   "github.event.workflow_run.head_repository.full_name == github.repository"),
            "runs-on": "ubuntu-latest",
            "steps": [
                {"uses": "astral-sh/setup-uv@v6"},
                {"uses": "actions/download-artifact@v4", "with": {
                    "github-token": "${{ github.token }}", "run-id": "${{ github.event.workflow_run.id }}",
                    "pattern": "acceptance-*", "merge-multiple": True,
                    "path": "${{ runner.temp }}/acceptance-reports"}},
                {"name": "Publish acceptance checks", "working-directory": "${{ runner.temp }}",
                 "env": {"GH_TOKEN": "${{ github.token }}", "FA_REPOSITORY": "${{ github.repository }}",
                         "FA_SHARDS": str(config.ci_shards),
                         "FA_REPORT_DIR": "${{ runner.temp }}/acceptance-reports"},
                 "run": TRUSTED_RUNNER + "foragent.ci_publish"},
            ],
        }},
    }, sort_keys=False)


def write_workflow(root: Path, config=None, *, repo=None, run=None, warn=True, missing_only=False):
    config = config or load_config(root)
    if warn:
        try:
            private = repository_private(repo or config.repo or GitRepo(root).remote_slug(), run)
        except GitError:
            private = None
        labels = [config.ci_runs_on] if isinstance(config.ci_runs_on, str) else config.ci_runs_on
        if private is None:
            print("Note: could not check repository visibility.", file=sys.stderr)
        elif private is False and "self-hosted" in labels:
            print("Warning: GitHub advises against self-hosted runners on public repositories; "
                  "use self-hosted runners only for private repositories.", file=sys.stderr)
    path = root / WORKFLOW
    path.parent.mkdir(parents=True, exist_ok=True)
    for destination, contents in ((path, workflow_text(config)),
                                  (root / PUBLISH_WORKFLOW, publisher_workflow_text(config))):
        if not missing_only or not destination.exists():
            destination.write_text(contents, encoding="utf-8")
    return path
