"""Every cache the generated acceptance workflow saves is restored under the same path.

actions/cache versions an entry by its path input as well as its key, so a
restore with the right key but another path string misses on GitHub. The
offline simulator matches by key only, so this check guards the path half.
"""

import re

import pytest
import yaml

from foragent.ci_workflow import workflow_text
from foragent.config import Config

SHA = re.compile(r"\$\{\{\s*github\.(?:sha|event\.pull_request\.base\.sha)\s*\}\}")
SAVE = {"actions/cache/save", "actions/cache"}
RESTORE = {"actions/cache/restore", "actions/cache"}


def cache_steps(document, actions):
    """(key, path) per matrix instance; both commit expressions name the pushed base."""
    pairs = []
    for job in document["jobs"].values():
        shards = (job.get("strategy") or {}).get("matrix", {}).get("shard") or [None]
        for step in job.get("steps", []):
            if step.get("uses", "").split("@", 1)[0] not in actions:
                continue
            for shard in shards:
                values = []
                for name in ("key", "path"):
                    value = str(step["with"][name])
                    if shard is not None:
                        value = re.sub(r"\$\{\{\s*matrix\.shard\s*\}\}", str(shard), value)
                    values.append(SHA.sub("SHA", value))
                pairs.append(tuple(values))
    return pairs


@pytest.mark.parametrize("shards", [1, 2, 6])
def test_each_saved_cache_is_restored_with_the_identical_path(shards):
    document = yaml.safe_load(workflow_text(Config(checks="ci", ci_shards=shards)))
    saved, restored = cache_steps(document, SAVE), cache_steps(document, RESTORE)
    assert saved and restored
    saved_paths = dict(saved)
    for key, path in restored:
        assert key in saved_paths, f"restore of {key} has no matching save"
        assert saved_paths[key] == path, f"{key}: saved at {saved_paths[key]}, restored from {path}"
    restored_keys = {key for key, _ in restored}
    assert all(key in restored_keys for key, _ in saved), "every saved cache is read by some restore"
