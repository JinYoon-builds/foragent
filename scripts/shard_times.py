"""Refresh the acceptance shard times from a pytest JUnit file.

Usage: python3 scripts/shard_times.py <junit.xml> [scripts/shard_times.json]

Acceptance CI balances its shards by these per-file seconds, read only from the
exact base commit (src/foragent/ci_shard.py). Run the acceptance suite with
`-o junit_family=legacy --junitxml=<file>` (or reuse a run's acceptance baseline
JUnit), then commit the refreshed file. The merge owner also rewrites it on main when
more than 10% of acceptance files are untimed (src/foragent/ci_pytest_times.py).
Files without a time still run, in a shard chosen by a hash of their path.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from foragent.ci_pytest_times import file_times, render  # noqa: E402


def main(argv):
    if len(argv) not in (2, 3):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    target = Path(argv[2] if len(argv) == 3 else Path(__file__).with_name("shard_times.json"))
    times = file_times(argv[1])
    target.write_text(render(times), encoding="utf-8")
    print(f"{target}: {len(times)} files, {sum(times.values()) / 60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
