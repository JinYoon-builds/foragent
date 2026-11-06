"""Per-node base results and the restore transport's trusted-branch boundary.

Record metadata cannot establish origin. The workflow verifies GitHub's cache
inventory before passing a restored file to the credential-free comparison.
"""

import json
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def cache_scope(scope):
    return {key: scope[key] for key in ("acceptance_dir", "command", "marker")}


def read_results(path, repository, base_sha, scope):
    if not path:
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
                or value["schema_version"] != 1 or value.get("repository") != repository
                or value.get("base_sha") != base_sha or value.get("scope") != cache_scope(scope)):
            return {}
        results = value.get("results")
        if not isinstance(results, dict) or any(
                not isinstance(node, str) or not node or not isinstance(result, str)
                or result not in {"passed", "nonpassing"} for node, result in results.items()):
            return {}
        return results
    except (OSError, ValueError):
        return {}


def verify_restored_origin():
    """Discard ambiguous/PR-scoped restores, including when the API is unavailable."""
    path = Path(os.environ["FA_BASE_CACHE_FILE"])
    if not path.exists():
        return
    trusted = False
    try:
        key = os.environ["FA_BASE_CACHE_KEY"]
        if os.environ.get("FA_BASE_CACHE_MATCHED_KEY") == key:
            entries = []
            page = 1
            while True:
                query = urlencode({"key": key, "per_page": 100, "page": page})
                url = (f"{os.environ.get('GITHUB_API_URL', 'https://api.github.com')}/repos/"
                       f"{os.environ['GITHUB_REPOSITORY']}/actions/caches?{query}")
                request = Request(url, headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}",
                                               "Accept": "application/vnd.github+json",
                                               "X-GitHub-Api-Version": "2022-11-28"})
                with urlopen(request, timeout=30) as response:
                    rows = json.load(response)["actions_caches"]
                entries.extend(row for row in rows if row["key"] == key)
                if len(rows) < 100:
                    break
                page += 1
            ref = os.environ["FA_BASE_CACHE_REF"]
            trusted = (any(row["ref"] == ref for row in entries)
                       and not any(row["ref"].startswith("refs/pull/") for row in entries))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        trusted = False
    if not trusted:
        path.unlink(missing_ok=True)
        print("Base cache origin unavailable or untrusted; measuring selected base nodes.")


if __name__ == "__main__":
    verify_restored_origin()
