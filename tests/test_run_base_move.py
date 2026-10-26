"""Boundary cases for classifying a moved base (#376)."""

from pathlib import Path
import os
import subprocess
from types import SimpleNamespace

import pytest

from foragent.git import GitRepo
from foragent.run_baseline import AcceptanceBaseline
from foragent.run_base_move import BaseMove, base_move_is_neutral, capture_move, config_move_is_neutral, moved_paths


def git(root: Path, *args: str, env: dict | None = None) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True,
                          env=None if env is None else {**os.environ, **env}).stdout.strip()


def commit(root: Path, branch: str, files: dict[str, str | None], *, parents: tuple[str, ...] = ()) -> str:
    """Add one commit to branch with plumbing only; a guard forbids porcelain commits."""
    index = {"GIT_INDEX_FILE": str(root.parent / f"index-{branch}")}
    tip = git(root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}") if branch_exists(root, branch) else None
    if tip:
        git(root, "read-tree", tip, env=index)
    else:
        git(root, "read-tree", "--empty", env=index)
    for name, text in files.items():
        if text is None:
            git(root, "update-index", "--force-remove", name, env=index)
            continue
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=root, input=text,
                              check=True, capture_output=True, text=True).stdout.strip()
        git(root, "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}", env=index)
    tree = git(root, "write-tree", env=index)
    parent_args = [arg for parent in ((tip,) if tip else ()) + parents for arg in ("-p", parent)]
    sha = git(root, "commit-tree", tree, *parent_args, "-m", "change")
    git(root, "update-ref", f"refs/heads/{branch}", sha)
    return sha


def branch_exists(root: Path, branch: str) -> bool:
    return subprocess.run(["git", "rev-parse", "--verify", "-q", f"refs/heads/{branch}"], cwd=root,
                          capture_output=True).returncode == 0


def move(root: Path, files: dict[str, str | None]) -> str:
    return commit(root, "main", files)


def on_feature(root: Path) -> None:
    git(root, "symbolic-ref", "HEAD", "refs/heads/feature")
    git(root, "read-tree", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    base = move(root, {"src/a.py": "A = 0\n", "docs/a.md": "a\n", "tests/acceptance/test_other.py": "x\n"})
    git(root, "update-ref", "refs/heads/feature", base)
    on_feature(root)
    return root


OTHER = "tests/acceptance/test_other.py"


def attempt(root: Path, owners: dict[int, list[str]], *, merged=(), n: int = 1, skipped=(OTHER,), own_locked=()):
    approvals = {number: SimpleNamespace(number=number, acceptance_blobs={name: "sha" for name in names})
                 for number, names in owners.items()}
    state = root.parent / "state"
    (state / "approvals").mkdir(parents=True, exist_ok=True)
    for number in approvals:
        (state / "approvals" / f"{number}.json").write_text("{}")
    store = SimpleNamespace(
        root=state, load_approval=approvals.get,
        read_history=lambda: [SimpleNamespace(number=number, outcome="merged") for number in merged],
    )
    baseline = AcceptanceBaseline("pytest", (), excluded=tuple(skipped))
    approval = SimpleNamespace(acceptance_blobs={name: "sha" for name in own_locked})
    tracker = SimpleNamespace(list_issues=lambda state: [
        SimpleNamespace(number=number, labels=frozenset(), body="", state="open") for number in approvals])
    ctx = SimpleNamespace(store=store, tracker=tracker, config=SimpleNamespace(acceptance_dir="tests/acceptance"))
    return SimpleNamespace(n=n, ctx=ctx, git=GitRepo(root), baseline=baseline,
                           approval=approval)


def neutral(work, target: str) -> bool:
    return base_move_is_neutral(work, capture_move(work.git, target))


def test_every_intervening_commit_counts_even_when_reverted(repo):
    move(repo, {"src/a.py": "A = 1\n"})
    target = move(repo, {"src/a.py": "A = 0\n", "docs/b.md": "b\n"})
    assert git(repo, "diff", "--name-only", "feature", target) == "docs/b.md"
    assert moved_paths(GitRepo(repo), capture_move(GitRepo(repo), target)) == {"src/a.py", "docs/b.md"}
    assert not neutral(attempt(repo, {}), target)


def test_docs_only_move_is_neutral(repo):
    move(repo, {"docs/b.md": "b\n"})
    target = move(repo, {"docs/specs/c.md": "c\n", "docs/a.md": None})
    assert neutral(attempt(repo, {}), target)


def test_rename_out_of_docs_is_result_affecting(repo):
    target = move(repo, {"docs/a.md": None, "src/a.md": "a\n"})
    assert moved_paths(GitRepo(repo), capture_move(GitRepo(repo), target)) == {"docs/a.md", "src/a.md"}
    assert not neutral(attempt(repo, {}), target)


def test_merge_commit_on_base_is_inspected_against_each_parent(repo):
    git(repo, "update-ref", "refs/heads/side", git(repo, "rev-parse", "main"))
    side = commit(repo, "side", {"src/a.py": "A = 2\n"})
    move(repo, {"docs/b.md": "b\n"})
    target = commit(repo, "main", {"src/a.py": "A = 2\n"}, parents=(side,))
    assert "src/a.py" in moved_paths(GitRepo(repo), capture_move(GitRepo(repo), target))
    assert not neutral(attempt(repo, {}), target)


@pytest.mark.parametrize(("owners", "merged", "expected"), [
    pytest.param({2: ["tests/acceptance/test_other.py"]}, (), True, id="other-open-owner"),
    pytest.param({2: ["tests/acceptance/test_other.py"]}, (2,), False, id="owner-merged"),
    pytest.param({1: ["tests/acceptance/test_other.py"]}, (), False, id="own-issue"),
    pytest.param({1: ["tests/acceptance/test_other.py"], 2: ["tests/acceptance/test_other.py"]}, (), False,
                 id="shared-with-own-issue"),
    pytest.param({}, (), False, id="no-owner"),
])
def test_locked_file_is_neutral_only_for_another_unmerged_owner(repo, owners, merged, expected):
    target = move(repo, {"tests/acceptance/test_other.py": "y\n", "docs/c.md": "c\n"})
    assert neutral(attempt(repo, owners, merged=merged), target) is expected


def test_docs_this_branch_changed_are_result_affecting(repo):
    commit(repo, "feature", {"docs/a.md": "feature\n"})
    on_feature(repo)
    other = move(repo, {"docs/b.md": "b\n"})
    target = move(repo, {"docs/a.md": "a\nmain\n"})
    assert neutral(attempt(repo, {}), other)
    assert not neutral(attempt(repo, {}), target)


def test_locked_file_the_baseline_ran_is_result_affecting_even_if_now_owned(repo):
    """An issue approved after verification does not undo what the baseline already ran."""
    target = move(repo, {OTHER: "y\n"})
    owners = {2: [OTHER]}
    assert neutral(attempt(repo, owners), target)
    assert not neutral(attempt(repo, owners, skipped=()), target)
    no_baseline = attempt(repo, owners)
    no_baseline.baseline = None
    assert not neutral(no_baseline, target)


# Each input of the decision is pinned to the move that will be merged. A
# mismatch in any one of them must force re-verification (#376).


def test_range_is_pinned_when_the_target_ref_moves_after_capture(repo):
    """Input 1: the fixed head_sha..target_sha range, not a ref read again later."""
    docs = move(repo, {"docs/b.md": "b\n"})
    work = attempt(repo, {})
    pinned = capture_move(work.git, "main")
    assert pinned == BaseMove(git(repo, "rev-parse", "feature"), docs)
    move(repo, {"src/a.py": "A = 1\n"})
    assert moved_paths(work.git, pinned) == {"docs/b.md"}
    assert base_move_is_neutral(work, pinned)
    assert not neutral(work, "main"), "A range that includes the later src commit re-verifies"


def test_range_start_is_the_captured_head(repo):
    """The range starts at the captured head, so commits already merged are not judged again."""
    move(repo, {"src/a.py": "A = 1\n"})
    target = move(repo, {"docs/b.md": "b\n"})
    work = attempt(repo, {})
    assert not neutral(work, target)
    src_tip = git(repo, "rev-parse", f"{target}~1")
    assert base_move_is_neutral(work, BaseMove(src_tip, target))
    assert not base_move_is_neutral(work, BaseMove(git(repo, "rev-parse", "feature"), target))


@pytest.mark.parametrize("name", [
    "\ndocs/code.py", " docs/code.py", "\tdocs/code.py", "docs\n/code.py",
], ids=["leading-newline", "leading-space", "leading-tab", "newline-inside-prefix"])
def test_exact_nul_delimited_paths_outside_docs_reverify(repo, name):
    """Input 2: filenames are compared byte for byte; nothing but NUL separates them."""
    target = move(repo, {name: "code\n"})
    assert moved_paths(GitRepo(repo), capture_move(GitRepo(repo), target)) == {name}
    assert not neutral(attempt(repo, {}), target)


def test_exact_paths_inside_docs_keep_their_whitespace(repo):
    target = move(repo, {"docs/trailing newline\n": "x\n", "docs/ space.md": "y\n"})
    assert moved_paths(GitRepo(repo), capture_move(GitRepo(repo), target)) == {
        "docs/trailing newline\n", "docs/ space.md",
    }
    assert neutral(attempt(repo, {}), target)


def test_excluded_path_must_match_the_moved_path_exactly(repo):
    target = move(repo, {OTHER + "\n": "y\n"})
    assert not neutral(attempt(repo, {2: [OTHER]}), target)


def test_own_locked_files_reverify_even_when_the_baseline_skipped_them(repo):
    """Input 3: this run's exclusion set minus its own Done when files."""
    target = move(repo, {OTHER: "y\n"})
    assert neutral(attempt(repo, {2: [OTHER]}), target)
    assert not neutral(attempt(repo, {2: [OTHER]}, own_locked=(OTHER,)), target)


BASE_CONFIG = "repo: owner/repo\nstall_minutes: 30\n"


@pytest.mark.parametrize(("before", "after", "expected"), [
    pytest.param(BASE_CONFIG, "repo: owner/repo\nstall_minutes: 60\n", True, id="neutral-key"),
    pytest.param(BASE_CONFIG, BASE_CONFIG + "run_hours: always\n", True, id="neutral-key-added"),
    pytest.param(BASE_CONFIG, BASE_CONFIG + "test_command: pytest\n", False, id="test-command-added"),
    pytest.param(BASE_CONFIG, BASE_CONFIG + "ci_required: true\n", False, id="ci-prefix"),
    pytest.param(BASE_CONFIG, BASE_CONFIG + "review: careful\n", False, id="review"),
    pytest.param(BASE_CONFIG, BASE_CONFIG + "executor: codex\n", False, id="executor-prefix"),
    pytest.param(BASE_CONFIG, BASE_CONFIG + "stall_minutes: [\n", False, id="unparsable"),
    pytest.param(BASE_CONFIG, "repo: owner/repo\nstall_minutes: -1\n", False, id="invalid-config"),
    pytest.param(BASE_CONFIG, BASE_CONFIG + "no_such_key: 1\n", False, id="unknown-key"),
    pytest.param(BASE_CONFIG, None, False, id="removed"),
    pytest.param(None, BASE_CONFIG, False, id="absent-at-head"),
])
def test_config_move_compares_head_and_target_keys(repo, before, after, expected):
    if before is not None:
        commit(repo, "main", {".foragent.yml": before})
        git(repo, "update-ref", "refs/heads/feature", git(repo, "rev-parse", "main"))
        on_feature(repo)
    target = move(repo, {".foragent.yml": after})
    work = attempt(repo, {})
    assert config_move_is_neutral(work.git, capture_move(work.git, target)) is expected
    assert neutral(work, target) is expected


NEW_LOCKED = "tests/acceptance/test_new_order.py"


def test_locked_file_absent_from_the_baseline_commit_stays_neutral_on_a_second_move(repo):
    """A first neutral move brings it into HEAD; the baseline still never ran it (#514)."""
    baseline_sha = git(repo, "rev-parse", "feature")
    first = move(repo, {NEW_LOCKED: "x\n"})
    work = attempt(repo, {2: [NEW_LOCKED]}, skipped=())
    work.baseline = AcceptanceBaseline("pytest", (), excluded=(), base_sha=baseline_sha)
    assert neutral(work, first)
    # The neutral move is merged into the verified branch; the baseline is unchanged.
    git(repo, "update-ref", "refs/heads/feature",
        git(repo, "commit-tree", f"{first}^{{tree}}", "-p", baseline_sha, "-p", first, "-m", "merge"))
    on_feature(repo)
    second = move(repo, {NEW_LOCKED: "y\n"})
    assert git(repo, "cat-file", "-t", f"feature:{NEW_LOCKED}") == "blob"
    assert neutral(work, second)
    ran = attempt(repo, {2: [NEW_LOCKED]}, skipped=())
    ran.baseline = AcceptanceBaseline("pytest", (), excluded=(), base_sha=first)
    assert not neutral(ran, second), "A file present where the baseline ran stays result-affecting"


@pytest.mark.parametrize("directory", ["tests/acceptance", "./tests/acceptance", "./tests/acceptance/"])
@pytest.mark.parametrize("named", [NEW_LOCKED, "./" + NEW_LOCKED])
def test_dot_slash_acceptance_dir_and_owner_paths_match_git_paths(repo, directory, named):
    target = move(repo, {NEW_LOCKED: "x\n"})
    work = attempt(repo, {2: [named]}, skipped=())
    work.ctx.config.acceptance_dir = directory
    assert neutral(work, target)


def commit_bytes(root: Path, files: dict[str, bytes]) -> str:
    """Like move(), with raw blob bytes; names may hold surrogate-escaped bytes."""
    index = {"GIT_INDEX_FILE": str(root.parent / "index-main")}
    git(root, "read-tree", git(root, "rev-parse", "main"), env=index)
    for name, data in files.items():
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=root, input=data,
                              check=True, capture_output=True).stdout.decode().strip()
        git(root, "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}", env=index)
    tree = git(root, "write-tree", env=index)
    sha = git(root, "commit-tree", tree, "-p", git(root, "rev-parse", "main"), "-m", "bytes")
    git(root, "update-ref", "refs/heads/main", sha)
    return sha


@pytest.mark.parametrize("data", [
    BASE_CONFIG.encode() + b"stall_minutes: 60\n# caf\xe9\n",
    b"\xff\xfe" + BASE_CONFIG.encode(),
], ids=["latin1-comment", "invalid-start-bytes"])
def test_undecodable_config_bytes_are_unparsable(repo, data):
    commit(repo, "main", {".foragent.yml": BASE_CONFIG})
    git(repo, "update-ref", "refs/heads/feature", git(repo, "rev-parse", "main"))
    on_feature(repo)
    target = commit_bytes(repo, {".foragent.yml": data})
    work = attempt(repo, {})
    assert not config_move_is_neutral(work.git, capture_move(work.git, target))
    assert not neutral(work, target)


def test_undecodable_path_is_result_affecting_even_under_docs(repo):
    name = "docs/caf\udce9.md"
    target = commit_bytes(repo, {name: b"x\n"})
    work = attempt(repo, {})
    assert moved_paths(work.git, capture_move(work.git, target)) == {name}
    assert not neutral(work, target)
