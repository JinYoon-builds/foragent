"""Generate the opt-in acceptance workflow and explain init's checks choice."""

import json
from pathlib import Path
import shlex
import subprocess
import sys

import yaml

from foragent import gh
from foragent.config import load_config
from foragent.git import GitError, GitRepo

WORKFLOW = ".github/workflows/foragent-acceptance.yml"
LEGACY_PUBLISH_WORKFLOW = ".github/workflows/foragent-acceptance-publish.yml"
EXCLUSIONS = ".foragent/acceptance-exclusions.json"
EXCLUSIONS_TRAILER = "Foragent-Acceptance-Excluded"
TRUSTED_RUNNER = "uv run --no-project --python 3.12 --with pytest --with 'foragent @ git+https://github.com/JinYoon-builds/foragent' python -m "


def acceptance_command(config, root):
    from foragent.order import detect_test_commands
    from foragent.run_checks import _pytest_command

    command = config.test_command or next(iter(detect_test_commands(root)), None)
    return _pytest_command(command) if command else None


def measurement_scope(config, root, excluded):
    return {"acceptance_dir": config.acceptance_dir, "command": acceptance_command(config, root),
            "marker": "not local_only", "excluded": list(excluded), "shard_count": config.ci_shards}


def read_exclusions(git, base_ref):
    value = git.latest_trailer(EXCLUSIONS_TRAILER, base_ref)
    if value is None:
        return None
    return parse_exclusions(value)


def parse_exclusions(value):
    excluded = json.loads(value)
    if (not isinstance(excluded, list) or any(not isinstance(path, str) for path in excluded)
            or excluded != sorted(set(excluded))):
        raise ValueError("invalid acceptance exclusions")
    return excluded


def repository_private(repo, run=None):
    try:
        if run is None:
            output = gh.run(["repo", "view", repo, "--json", "isPrivate"],
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

    if command is None or _pytest_command(command) is None:
        # Visibility only matters for CI mode, which needs pytest; skip the GitHub call.
        print("checks: local (CI acceptance requires pytest; using local checks for this test command); "
              "change checks in .foragent.yml.")
        return "local"
    private = repository_private(repo, run)
    mode = "ci" if private is False else "local"
    if private is False:
        reason = "public repository; GitHub-hosted Actions minutes are free"
    elif private is True:
        reason = "private repository; avoid paid Actions minutes"
    else:
        reason = "could not read repository visibility; using local checks"
    line = f"checks: {mode} ({reason}); change checks in .foragent.yml."
    if private is True:
        line += " A self-hosted runner avoids measurement minutes; set ci_runs_on: [self-hosted, linux] there."
    print(("Note: " if private is None else "") + line)
    return mode


def workflow_text(config):
    base = config.base_branch.replace("'", "''")
    cache_file = "${{ runner.temp }}/acceptance-base.json"
    base_key = "foragent-acceptance-base-v1-${{ github.event.pull_request.base.sha }}"
    producer_probe = TRUSTED_RUNNER.removesuffix("-m ") + "-c " + shlex.quote(
        'import sys; from foragent import ci_shard; sys.exit(not callable(getattr(ci_shard, "produce", None)))')
    job = {
        "if": ("github.event_name == 'pull_request' && "
               "github.event.pull_request.head.repo.full_name == github.repository && "
               "(startsWith(github.event.pull_request.head.ref, 'foragent/issue-') || "
               "startsWith(github.event.pull_request.head.ref, 'foragent/group-')) && "
               f"github.event.pull_request.base.ref == '{base}'"),
        "runs-on": config.ci_runs_on,
        "permissions": {"contents": "read", "actions": "read"},
        "strategy": {"fail-fast": False, "matrix": {"shard": list(range(config.ci_shards))}},
        "outputs": {"comparison": "${{ steps.compare.outputs.comparison }}"},
        "steps": [
            {"uses": "actions/checkout@v4", "with": {"ref": "${{ github.event.pull_request.head.sha }}", "fetch-depth": 0,
                                                     "persist-credentials": False}},
            {"uses": "astral-sh/setup-uv@v6"},
            {"name": "Restore trusted base node results", "id": "base-cache", "uses": "actions/cache/restore@v4",
             "with": {"path": cache_file, "key": base_key}},
            {"name": "Verify base cache branch origin", "working-directory": "${{ runner.temp }}",
             "env": {"GH_TOKEN": "${{ github.token }}", "FA_BASE_CACHE_FILE": cache_file,
                     "FA_BASE_CACHE_KEY": base_key,
                     "FA_BASE_CACHE_MATCHED_KEY": "${{ steps.base-cache.outputs.cache-matched-key }}",
                     "FA_BASE_CACHE_REF": f"refs/heads/{config.base_branch}"},
             # The introducing PR still installs the trusted tool from before
             # cache support. Never pass an unverified restore to comparison.
             "run": (f"if ! {TRUSTED_RUNNER}foragent.ci_base_cache; then\n"
                     '  rm -f -- "$FA_BASE_CACHE_FILE"\n'
                     '  echo "Trusted base cache verification unavailable; measuring selected base nodes."\n'
                     "fi\n")},
            {"name": "Compare acceptance passing sets", "id": "compare",
             "working-directory": "${{ runner.temp }}",
             "env": {"FA_REPOSITORY": "${{ github.repository }}", "FA_CHECKOUT": "${{ github.workspace }}",
                     "FA_BASE_SHA": "${{ github.event.pull_request.base.sha }}",
                     "FA_HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
                     "FA_SHARD": "${{ matrix.shard }}", "FA_SHARDS": str(config.ci_shards),
                     "FA_BASE_CACHE_FILE": cache_file,
                     "FA_REPORT_FILE": "${{ runner.temp }}/shard-${{ matrix.shard }}.json"},
             "run": TRUSTED_RUNNER + "foragent.ci_shard"},
            {"uses": "actions/upload-artifact@v4", "if": "steps.compare.outputs.comparison != ''",
             "with": {"name": "acceptance-${{ matrix.shard }}",
             "path": "${{ runner.temp }}/shard-${{ matrix.shard }}.json", "if-no-files-found": "error"}},
        ],
    }
    producer = {
        "if": f"github.event_name == 'push' && github.ref == 'refs/heads/{base}'",
        "runs-on": config.ci_runs_on,
        "permissions": {"contents": "read"},
        "steps": [
            {"uses": "actions/checkout@v4", "with": {"ref": "${{ github.sha }}", "fetch-depth": 0,
                                                      "persist-credentials": False}},
            {"uses": "astral-sh/setup-uv@v6"},
            {"name": "Measure full portable base selection", "id": "produce",
             "working-directory": "${{ runner.temp }}",
             "env": {"FA_REPOSITORY": "${{ github.repository }}", "FA_CHECKOUT": "${{ github.workspace }}",
                     "FA_BASE_SHA": "${{ github.sha }}", "FA_HEAD_SHA": "${{ github.sha }}",
                     "FA_BASE_CACHE_MODE": "produce", "FA_BASE_CACHE_FILE": cache_file},
             # Legacy ci_shard has no producer mode. Clear any old file before
             # probing, and only publish a fresh successful production result.
             "run": ('rm -f -- "$FA_BASE_CACHE_FILE"\n'
                     f"if {producer_probe}; then\n"
                     f"  {TRUSTED_RUNNER}foragent.ci_shard\n"
                     '  if [ -s "$FA_BASE_CACHE_FILE" ]; then\n'
                     '    echo "cache=true" >> "$GITHUB_OUTPUT"\n'
                     "  fi\n"
                     "else\n"
                     '  echo "Trusted tool has no base cache producer; skipping reusable base results."\n'
                     "fi\n")},
            {"name": "Save trusted base node results", "uses": "actions/cache/save@v4",
             "if": "steps.produce.outputs.cache == 'true'",
             "with": {"path": cache_file, "key": "foragent-acceptance-base-v1-${{ github.sha }}"}},
        ],
    }
    return "# Generated by fa ci workflow; edit .foragent.yml, then regenerate.\n" + yaml.safe_dump({
        "name": "foragent-acceptance", "on": {"pull_request": None, "push": {"branches": [config.base_branch]}},
        "permissions": {"contents": "read"}, "jobs": {"acceptance": job, "acceptance-base": producer},
    }, sort_keys=False)


def obsolete_workflow_paths(root: Path) -> list[str]:
    path = root / LEGACY_PUBLISH_WORKFLOW
    if path.is_file() and path.read_text(encoding="utf-8").startswith("# Generated by fa ci workflow"):
        return [LEGACY_PUBLISH_WORKFLOW]
    return []


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
    if not missing_only or not path.exists():
        path.write_text(workflow_text(config), encoding="utf-8")
    for obsolete in obsolete_workflow_paths(root):
        (root / obsolete).unlink()
        print(f"Removed: {obsolete}")
    return path
