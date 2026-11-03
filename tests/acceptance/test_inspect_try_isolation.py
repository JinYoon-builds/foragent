"""Acceptance test for inspect-try-isolation. Locked by Foragent; do not edit.

`fa inspect prepare` runs a merged PR's "Try it" commands. Those commands must
not read or write the person's real Foragent state or config: on 2026-11-03 a
prepared try card ran `fa orch ask` and left real open questions in the inbox.
"""

from datetime import timedelta
import json
from pathlib import Path
import shlex
import sys

import pytest

from foragent.config import Config, load_global_config
from foragent.git import GitRepo
from foragent.inspect_data import InspectContext, collect
from foragent.inspect_runtime import prepare
from foragent.state import StateStore
from foragent.tracker import FakeTracker

from conftest import BASE_TIME


REPO = "owner/repo"
# The `fa` entry point, spelled so the scrubbed try environment still finds it.
FA = shlex.join([sys.executable, "-c", "import sys; from foragent.cli import main; sys.exit(main(sys.argv[1:]))"])


@pytest.fixture
def ctx(git_history):
    # The merged project carries its own Foragent config, as a real project does.
    config = f"repo: {REPO}\nnotify: none\nskill_libraries: []\n"
    git_history.update({".foragent.yml": config})
    (git_history.clone / ".foragent.yml").write_text(config, encoding="utf-8")
    git = GitRepo(git_history.clone)
    git.fetch("origin", "main")
    now = BASE_TIME + timedelta(hours=1)
    # The person's real state store: the one every `fa` command of this project uses.
    store = StateStore.for_repo(REPO, now=lambda: now)
    return InspectContext(FakeTracker(now=lambda: now), store, REPO, Config(), lambda: now,
                          git_history.clone, git)


def merge_try_card(ctx, *commands, number=1):
    body = "## Try it\n\nkind: cli\n```sh\n" + "\n".join(commands) + "\n```\n## Expect\nIt runs.\n"
    pr = ctx.tracker.create_pr(head=f"foragent/issue-{number}", base="main", title="Try", body=body)
    ctx.tracker.merge_pr(pr, expected_head_sha=ctx.tracker.pr_head_sha(pr))
    return pr


def real_questions(capsys, ctx):
    """Open questions as the person's own `fa orch questions` sees them."""
    from foragent.cli import main
    capsys.readouterr()
    assert main(["orch", "questions", "--json", "--repo-root", str(ctx.repo_root)]) == 0
    return json.loads(capsys.readouterr().out)


def prepared_result(ctx, number=1):
    item = next(item for item in collect(ctx).items if item.number == number)
    assert item.prepared is not None, f"#{number} must be prepared"
    return item.prepared.results


def test_try_command_that_asks_a_question_leaves_the_real_inbox_unchanged(ctx, capsys):
    merge_try_card(ctx, f'{FA} orch ask --kind merge --question "PR #50을 머지할까요?" '
                        '--option 머지 --option 보류 --recommend 1')
    merge_try_card(ctx, f'{FA} orch ask --kind order --question "주문 카드를 추가할까요?" '
                        '--option 추가 --option 다음에', number=2)
    assert real_questions(capsys, ctx) == []

    assert sorted(prepare(ctx)) == [1, 2]

    assert real_questions(capsys, ctx) == [], (
        "a try command's `fa orch ask` must not create questions in the person's real inbox"
    )
    for number, question in ((1, "PR #50을 머지할까요?"), (2, "주문 카드를 추가할까요?")):
        [result] = prepared_result(ctx, number)
        assert result.exit_code == 0 and not result.timed_out, (
            f"the try command must still run to completion: {result.stderr}"
        )
        assert question in result.output, "the try command's output must still be captured for the person"


def test_try_command_does_not_see_the_real_state(ctx, capsys):
    from foragent.cli import main
    assert main(["orch", "ask", "--question", "Real question for Jin", "--option", "yes",
                 "--option", "no", "--repo-root", str(ctx.repo_root)]) == 0
    before = real_questions(capsys, ctx)
    assert [row["question"] for row in before] == ["Real question for Jin"]
    merge_try_card(ctx, f"{FA} orch questions --json")

    assert prepare(ctx) == [1]

    [result] = prepared_result(ctx, 1)
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.output) == [], (
        "a try command must get its own empty state, not the person's real open questions"
    )
    assert real_questions(capsys, ctx) == before, "the real question must stay exactly as it was"


