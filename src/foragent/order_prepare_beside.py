"""Lock corrections beside an open issue (Jin 11-08, r1210).

A lock-only edit of an open issue may carry corrected versions of locked acceptance files that are
not in the issue's own Done when (``order_prepare_locks.beside``): a locked helper the issue's tests
import, or a closed issue's locked test the issue's change contradicts. No writer or reviewer runs.
The publication check proves the correction instead:

- on the issue branch, the corrected files pass and the previous ones fail (``proof``): the issue's
  Done when proves a corrected helper, and a corrected test file proves itself;
- on main, every test file that is a corrected file or imports one runs with the previous and the
  corrected bytes (``main_tests``); a test green before and not after means the correction would
  turn main red (``turned_red``).

A correction that keeps main green lands on main ahead of the issue. One that would turn main red
lands on the issue branch, in the same PR as the implementation (``land_on_branch``); without an
open issue PR there is nothing to land with, so ``fa order publish`` refuses it
(``require_issue_branch``). Only the merge owner's publication queue proves and routes them, so a
direct publication (no merge owner) refuses them too.

Main-only preflight must not judge these files: the collection and importer check
(``order_collect.check``) would refuse a correction that turns a main-green test red, which is
exactly the one that belongs in the issue's PR. ``collected_files`` leaves them out of it; the
publication check above runs them completely on the branch and on main instead.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import shlex
from tempfile import TemporaryDirectory

from foragent import order_importers, order_prepare_locks
from foragent.git import GitError, GitRepo
from foragent.issue import acceptance_paths


def branch_of(number: int) -> str:
    return f"foragent/issue-{number}"


def is_test(name: str) -> bool:
    base = PurePosixPath(name).name
    return base.endswith(".py") and (base.startswith("test_") or base.endswith("_test.py"))


def existing(git: GitRepo, config):
    """Whether a file exists on the base (origin's branch when fetched, else the checkout's HEAD)."""
    def check(name: str) -> bool:
        for ref in (f"origin/{config.base_branch}", "HEAD"):
            try:
                return git.file_at(ref, name) is not None
            except GitError:
                continue
        return False
    return check


def _beside(plan: dict, by_number: dict, config, git: GitRepo) -> dict[int, list[str]]:
    from foragent.order import edit_view

    found, sides = existing(git, config), {}
    for edit in plan.get("edits", []):
        issue = by_number.get(edit["number"])
        side = order_prepare_locks.beside(edit_view(edit, issue, config, found)) if issue is not None else []
        if side:
            sides[edit["number"]] = side
    return sides


def collected_files(plan: dict, files: dict, by_number: dict, config, git: GitRepo) -> dict:
    """The files the collection and importer check judges on main: all but beside corrections."""
    side = {name for names in _beside(plan, by_number, config, git).values() for name in names}
    return {name: data for name, data in files.items() if name not in side}


def require_issue_branch(plan: dict, by_number: dict, config, git: GitRepo, tracker) -> None:
    """Refuse a beside correction outside the publication queue, or of an issue with no open PR."""
    from foragent.order import PlanError

    for number, side in _beside(plan, by_number, config, git).items():
        if not config.merge_owner:
            raise PlanError(
                f"#{number}: corrected locked files outside its Done when ({', '.join(side)}) are proved on "
                "the issue branch and on main by the merge owner's publication queue; turn on merge_owner "
                "to publish them.")
        branch = branch_of(number)
        try:
            git.fetch("origin", branch)
            git.ref_sha(f"origin/{branch}")
            pr = tracker.find_pr(branch)
        except GitError:
            pr = None
        if pr is None:
            raise PlanError(
                f"#{number}: corrected locked files outside its Done when "
                f"({', '.join(side)}) are proved on and may "
                f"land with the issue's implementation, but {branch} has no open pull request; "
                "they cannot be published on their own.")


def command_for(done_when: list[str], acceptance_dir: str, name: str) -> str:
    """A command that runs one test file the way the issue's Done when runs its locked tests."""
    for command in done_when:
        for path in acceptance_paths([command], acceptance_dir):
            if path in command:
                return command[:command.index(path)] + shlex.quote(name)
    return f"uv run pytest -q {shlex.quote(name)}"


def proof(item: dict, written: dict, done_when: list[str], acceptance_dir: str) -> tuple[list[str], set[str]]:
    """The commands that prove the correction on the issue branch, and the test files they must prove."""
    tests = [name for name in order_prepare_locks.beside(item) if is_test(name)]
    commands, files = [], set()
    if set(written) - set(tests):  # The issue's own locks or a helper: its Done when proves them.
        commands += list(done_when)
        files |= set(acceptance_paths(done_when, acceptance_dir))
    for name in tests:
        commands.append(command_for(done_when, acceptance_dir, name))
        files.add(name)
    return commands, files


def main_tests(git: GitRepo, base: str, written: dict, done_when: list[str], acceptance_dir: str) -> list[str]:
    """One command per test file on main that is a corrected file or imports one."""
    names = {name for name in written if is_test(name)}
    names |= {name for name in order_importers.importers(git, base, sorted(written), set()) if is_test(name)}
    return [command_for(done_when, acceptance_dir, name) for name in sorted(names)]


def _green(measured: list[dict]) -> set[str] | None:
    nodes = set()
    for run in measured:
        if (run.get("timed_out") or run.get("exit_code") not in {0, 1}
                or run.get("collected_node_ids") is None or run.get("nonpassing_node_ids") is None):
            return None
        nodes |= set(run["collected_node_ids"]) - set(run["nonpassing_node_ids"])
    return nodes


def turned_red(before: list[dict], after: list[dict]) -> tuple[list[str] | None, str | None]:
    """Tests green on main with the previous files and not with the corrected ones."""
    green_before, green_after = _green(before), _green(after)
    if green_before is None or green_after is None:
        output = "\n".join(run.get("output") or "" for run in [*before, *after]).strip()
        return None, output or "Could not establish which tests are green on main."
    return sorted(green_before - green_after), None


def _data(content) -> bytes:
    return content.encode("utf-8") if isinstance(content, str) else content


def land_on_branch(git: GitRepo, scratch: Path, branch: str, head: str, files: dict, title: str) -> str:
    """Commit ``files`` on the issue branch at ``head`` once; a moved branch refuses the push."""
    if all((current := git.file_at(head, name)) is not None and _data(current) == _data(content)
           for name, content in files.items()):
        return head
    with TemporaryDirectory(prefix="lock-correction-", dir=scratch) as directory:
        root = Path(directory) / "tree"
        git.add_detached_worktree(root, head)
        try:
            tree = git.at(root)
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(_data(content))
            tree.ensure_tracked(sorted(files))
            tree.commit_paths(sorted(files), title)
            sha = tree.head_sha()
            tree._call(["push", "origin", f"{sha}:refs/heads/{branch}"])
            return sha
        finally:
            git.remove_worktree(root)


def branch_landings(prepared) -> list[tuple[dict, str, str, list[str]]]:
    """(row, branch, recorded issue head, files) for every checked correction that lands on its branch."""
    found = []
    for row, _publication in prepared:
        for check in row.get("red_checks", {}).values():
            if check.get("lands_on") == "branch" and check.get("issue_head"):
                head = check["issue_head"]
                found.append((row, head["branch"], head["head"], list(check.get("branch_files") or [])))
    return found
