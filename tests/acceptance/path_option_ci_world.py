"""Lane PR world for #2772 promise p4 (r1320 round 1): path-valued pytest options.

Same offline GitHub Actions world as pr_ci_world.py, but the lane PR's check table also
holds a related-check row, as the gate writes it: the related unit test command with
path-valued pytest options such as `--junitxml <report>` or `-o cache_dir=<dir>`.

Each option value is either a path that does not exist in the checkout (a report or cache
outside the repository, or a repository-relative report that is not written yet) or an
existing directory such as `.`. Read as a test selector, the first kind is a missing test
path (today: fall back to the full suite, or pytest fails on a missing file) and the second
kind selects every test.
"""

from __future__ import annotations

from foragent.run_checks import pytest_command

from pr_ci_world import DONE_WHEN, ISSUE, LANE_BRANCH, LANE_PR, UNIT_ALPHA, World


def _cell(command):
    # Same escaping as the gate's PR check table (run_pr_body.pr_body).
    command = command.replace("&", "&amp;").replace("<", "&lt;").replace("|", "&#124;")
    return command.replace("`", "&#96;").replace("\n", "<br>")


def related_rows(temp):
    """label -> related-check command for UNIT_ALPHA, each with path-valued options."""
    unit = pytest_command("uv run pytest -q")
    return {
        # The gate's own related-check form (run_checks.run_related_check).
        "generated-junitxml": (f"{unit} --junitxml {temp}/runs/issue-{ISSUE}-related.xml "
                               f"-o junit_family=legacy {UNIT_ALPHA}"),
        # The statement's examples, written by hand.
        "junitxml-and-cache-dir": (f"uv run pytest -q --junitxml {temp}/x.xml -o cache_dir={temp}/c "
                                   f"{UNIT_ALPHA}"),
        # Values that exist (`.`) or are repository-relative but not present.
        "rootdir-basetemp-relative-report": (f"uv run pytest -q --rootdir . --basetemp {temp}/base "
                                             f"--junitxml reports/related.xml {UNIT_ALPHA}"),
    }


class PathOptionWorld(World):
    """A World whose lane PR table lists one related-check command beside the Done when."""

    def __init__(self, directory, label):
        self.label = label
        super().__init__(directory)

    def lane_pr(self, changes):
        head, changed = self._commit_change(LANE_BRANCH, changes, f"Lane change for #{ISSUE}")
        related = related_rows(self.temp)[self.label]
        body = ("Closes #7\n\n## Summary\n\nLane change.\n\n"
                "| Done when command | Result |\n| --- | --- |\n"
                f"| <code>{_cell(related)}</code> | pass |\n"
                f"| <code>{_cell(DONE_WHEN)}</code> | pass |\n\n## Review\n\nVERDICT: PASS\n")
        title = f"Lane change (#{ISSUE})"
        self._serve(head, changed, LANE_PR, LANE_BRANCH, body, title)
        return self._pr_event(LANE_PR, LANE_BRANCH, head, changed, body, title)


class PathOptionScenarios:
    """Simulate one lane PR per related-check form, once per test module."""

    def __init__(self, factory):
        self.factory = factory
        self.cache = {}

    def run(self, label, changes):
        if label not in self.cache:
            world = PathOptionWorld(self.factory.mktemp(f"lane-{label}"), label)
            outcome = world.simulate(world.lane_pr(changes), "lane")
            outcome.log.insert(0, f"related-check row: {related_rows(world.temp)[label]}")
            failure = getattr(world, "_last_failure", None)
            if failure:
                outcome.log.append(failure)
            self.cache[label] = outcome
        return self.cache[label]


LABELS = ("generated-junitxml", "junitxml-and-cache-dir", "rootdir-basetemp-relative-report")
