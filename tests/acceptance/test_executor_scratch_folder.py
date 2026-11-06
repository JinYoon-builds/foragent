"""The implementer is told where scratch files go, and that place is never committed (architect r832).

#2172's executor made .tmp-2172 (27,506 files, 159 MB) at the worktree root because no place
for scratch files was named; staging takes every untracked file except Foragent's own
artifact folder, so the next check would have pushed it into the PR. A scratch diff
(train-scope-proposal.patch) reached main the same way (#1970).
"""

import subprocess

from foragent.git import GitRepo
from foragent.issue import parse_body
from foragent.runner import build_prompt


BODY = ("<!-- foragent:v1 -->\n## Goal\nDo it.\n## Out of scope\nNothing.\n## Done when\n"
        "```sh\ntrue\n```\n")


def test_prompt_names_a_never_committed_scratch_folder_and_staging_skips_it(tmp_path):
    for language in ("en", "ko"):
        flat = " ".join(build_prompt(1, parse_body(BODY), "tests/acceptance/", language=language).split())
        assert ".foragent/tmp/" in flat, f"Name .foragent/tmp/ as the place for scratch files ({language})"
        assert "TMPDIR" in flat, f"Tell helpers to use it as TMPDIR ({language})"

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@e"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "kept.txt").write_text("work\n")
    (repo / ".foragent" / "tmp" / "pytest-of-x").mkdir(parents=True)
    (repo / ".foragent" / "tmp" / "pytest-of-x" / "log.txt").write_text("scratch\n")
    GitRepo(repo).stage_changes()
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=repo, check=True,
                            capture_output=True, text=True).stdout.split()
    assert staged == ["kept.txt"], f"Scratch under .foragent/tmp/ must never be staged: {staged}"
