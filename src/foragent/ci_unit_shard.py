"""Split the unit test files into CI shards and run one shard with pytest."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


def select(paths: list[str], shard: int, shards: int) -> list[str]:
    """Return this shard's files: sorted round-robin, so shards are disjoint and stable."""
    if shards < 1 or not 0 <= shard < shards:
        raise ValueError(f"shard must be in 0..{shards - 1}, got {shard} of {shards}")
    return sorted(paths)[shard::shards]


def unit_files(root: Path) -> list[str]:
    """Unit test files: tests/**/test_*.py outside tests/acceptance."""
    tests = root / "tests"
    acceptance = tests / "acceptance"
    return sorted(
        path.relative_to(root).as_posix()
        for path in tests.rglob("test_*.py")
        if path.is_file() and not path.is_relative_to(acceptance)
    )


def selected_unit_files(root: Path) -> list[str]:
    """Every unit file, or on a lane PR only its related ones (related_ci)."""
    from foragent.config import ConfigError, load_config
    from foragent.related_ci import event_selection

    files = unit_files(root)
    try:
        acceptance_dir = load_config(root, global_path=root / ".foragent/no-global-config").acceptance_dir
    except ConfigError:
        acceptance_dir = "tests/acceptance/"
    selection = event_selection(root, acceptance_dir)
    return files if selection is None else [path for path in files if path in set(selection.unit)]


def empty_report(report_file: str, tested_commit: str) -> None:
    """An empty shard still reports its exact commit, so every shard's evidence exists."""
    suites = ET.Element("testsuites")
    suite = ET.SubElement(suites, "testsuite", name="pytest", tests="0", errors="0", failures="0", skipped="0")
    ET.SubElement(ET.SubElement(suite, "properties"), "property", name="foragent_tested_commit", value=tested_commit)
    ET.ElementTree(suites).write(report_file, encoding="utf-8", xml_declaration=True)


def main() -> int:
    shard = int(os.environ["FA_SHARD"])
    shards = int(os.environ["FA_SHARDS"])
    files = select(selected_unit_files(Path.cwd()), shard, shards)
    report_file = os.environ.get("FA_REPORT_FILE")
    if not files:
        print(f"shard {shard}/{shards}: no unit test files")
        if report_file:
            empty_report(report_file, subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip())
        return 0
    command = [sys.executable, "-m", "pytest", "-q", "-m", "not local_only", *files]
    if report_file:
        # Capture the checkout before pytest runs, independently of Actions metadata.
        tested_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        Path(report_file).unlink(missing_ok=True)
        # Legacy JUnit preserves exact file paths for pytest node-id selectors.
        command.extend([f"--junitxml={report_file}", "-o", "junit_family=legacy", "--junit-prefix="])
    result = subprocess.run(command, check=False)
    if report_file and Path(report_file).is_file():
        report = ET.parse(report_file)
        for suite in report.iter("testsuite"):
            properties = suite.find("properties")
            if properties is None:
                properties = ET.SubElement(suite, "properties")
            ET.SubElement(properties, "property", name="foragent_tested_commit", value=tested_commit)
        report.write(report_file, encoding="utf-8", xml_declaration=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
