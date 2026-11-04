"""A fake lane agent chooses the lesson's owner; FA only appends and deduplicates.

No script is asked to infer lesson meaning from prose. The command's lane,
source issue and delivery id are the public append contract between lanes.
"""

import json

from conftest import git_command
from foragent.runner import run_issue
from test_orch_review_handoff import bind_cli, make_case
from test_one_entrance_seat import invoke
from test_skills_autosync import isolated_home  # noqa: F401


def test_cross_lane_run_appends_the_selected_lesson_once_with_source(
    git_history, tmp_path, monkeypatch, capsys, isolated_home,
):
    lanes = {"api": {"paths": ["api/**"], "agent": "fa-api"},
             "view": {"paths": ["view/**"], "agent": "fa-view"}}
    folder = ".claude/agent-memory/fa-view"
    curated = "# View lessons\nKeep curated history.\n"
    pending = "- [id:earlier] Keep this earlier pending lesson.\n"
    git_history.update({".foragent.yml": "repo: owner/repo\nskill_libraries: []\n",
                        f"{folder}/MEMORY.md": curated, f"{folder}/MEMORY.new.md": pending,
                        "view/schema.txt": "old\n", "api/service.txt": "old\n"})
    git_command(git_history.clone, "fetch", "-q", "origin")
    git_command(git_history.clone, "merge", "-q", "--ff-only", "origin/main")
    case = make_case(git_history, tmp_path, monkeypatch, lanes=lanes, skill_libraries=[], test_command="true")
    case.tracker.add_labels(1, ["lane:api"])
    bind_cli(case, monkeypatch)
    lesson = "View schemas need a locale when the API supplies a customer name."
    deliveries = []

    def run(kwargs):
        case.implement(kwargs)
        for path in ("view/schema.txt", "api/service.txt"):
            (kwargs["worktree"] / path).write_text("updated\n")
        for _ in range(2):  # Retry the same append delivery, not two independently learned lessons.
            result = invoke(capsys, "lanes", "lesson", "--lane", "view", "--issue", "1",
                            "--id", "issue-1-view-locale", "--text", lesson,
                            "--repo-root", kwargs["worktree"])
            deliveries.append(result)
        return "Implemented; the view lesson was sent through FA."

    case.implementer.steps = [run]
    outcome = run_issue(1, case.ctx)
    assert len(deliveries) == 2, "The real run must reach the append boundary: " + case.store.log_path(1).read_text()
    assert all(result.code == 0 for result in deliveries), "The run needs an FA append tool for the owning lane: " + deliveries[0].text
    assert outcome == "merged"
    saved = git_command(case.root, "show", f"refs/heads/foragent/issue-1:{folder}/MEMORY.new.md")
    assert pending.strip() in saved and saved.count(lesson) == 1
    delivered = next(line for line in saved.splitlines() if lesson in line)
    assert "#1" in delivered and "view" in delivered, "The lesson must name its source issue and owning lane"
    assert git_command(case.root, "show", f"refs/heads/foragent/issue-1:{folder}/MEMORY.md") == curated.rstrip("\n")
    # Test evidence: the lesson came from a run that really changed both owners' files.
    changed = git_command(case.root, "diff", "--name-only", "origin/main...refs/heads/foragent/issue-1").splitlines()
    assert {"view/schema.txt", "api/service.txt"} <= set(changed), json.dumps(changed)
