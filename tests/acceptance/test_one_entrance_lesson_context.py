"""Give the lane agent the other owners and append tool; let it judge the lesson."""

from foragent.runner import run_issue
from test_orch_review_handoff import make_case
from test_skills_autosync import isolated_home  # noqa: F401


def test_run_can_see_other_lane_paths_and_the_fa_lesson_tool(
    git_history, tmp_path, monkeypatch, isolated_home,
):
    lanes = {"api": {"paths": ["api/**"], "agent": "fa-api"},
             "view": {"paths": ["view/**"], "agent": "fa-view"}}
    case = make_case(git_history, tmp_path, monkeypatch, lanes=lanes, skill_libraries=[], test_command="true")
    case.tracker.add_labels(1, ["lane:api"])
    assert run_issue(1, case.ctx) == "merged"
    prompt = case.implementer.calls[0]["prompt"]
    assert "view/**" in prompt, "The agent cannot identify another lane's area without the lane map"
    assert "fa lanes lesson" in prompt and "MEMORY.new.md" in prompt, "Give the agent the FA append path, not a prose classifier"
