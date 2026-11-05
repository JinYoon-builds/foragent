"""Refresh the acceptance shard times from a pytest JUnit file.

Usage: python3 scripts/shard_times.py <junit.xml> [scripts/shard_times.json]

Acceptance CI balances its shards by these per-file seconds, read only from the
exact base commit (src/foragent/ci_shard.py). Run the acceptance suite with
`-o junit_family=legacy --junitxml=<file>` (or reuse a run's acceptance baseline
JUnit), then commit the refreshed file. Files without a time still run, in a
shard chosen by a hash of their path.
"""

import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ElementTree


def file_times(junit):
    totals = {}
    for case in ElementTree.parse(junit).iter("testcase"):
        path = case.get("file")
        if not path:
            parts = case.get("classname", "").split(".")
            while parts and parts[-1][:1].isupper():  # drop test class names
                parts.pop()
            path = "/".join(parts) + ".py" if parts else None
        if path:
            totals[path] = totals.get(path, 0.0) + float(case.get("time") or 0)
    return {path: round(seconds, 1) for path, seconds in sorted(totals.items())}


def main(argv):
    if len(argv) not in (2, 3):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    target = Path(argv[2] if len(argv) == 3 else Path(__file__).with_name("shard_times.json"))
    times = file_times(argv[1])
    target.write_text(json.dumps(times, indent=0, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{target}: {len(times)} files, {sum(times.values()) / 60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
