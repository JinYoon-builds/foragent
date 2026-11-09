"""Shared real-Git setup for restore-approved-locks: a saved WIP holding an older lock."""

from dataclasses import replace

from conftest import git_command
from foragent.issue import body_hash

BRANCH = "foragent/issue-1"
LOCK = "tests/acceptance/test_widget_lock.py"
OTHER = "tests/acceptance/test_other_lock.py"
LOCKS = (LOCK, OTHER)
ORIGINAL = "# lock as first published\n"
WIP_COPY = "# lock approved when the WIP was saved\n"
CORRECTED_1 = "# owner correction one\n"
APPROVED = "# owner correction two (approved now)\n"
OWN_SOURCE = {"src/widget.py": "VALUE = 'saved implementation'\n"}


def configure(case):
    case.ctx.config = replace(case.ctx.config, review="fast", max_retries=0, skill_libraries=[],
                              test_command="true", checks="local", approvers=["human"])


def wip_commit(case, parent, files):
    """A single-parent issue commit built with plumbing (no git commit)."""
    stream = [f"commit refs/heads/{BRANCH}\n", "committer Tests <t@example.com> 1792483200 +0000\n",
              f"data 10\nIssue work\nfrom {parent}\n"]
    for path, content in files.items():
        stream.append(f"M 100644 inline {path}\ndata {len(content.encode())}\n{content}\n")
    git_command(case.history.origin, "fast-import", "--quiet", input="".join(stream) + "\ndone\n")
    return git_command(case.history.origin, "rev-parse", BRANCH)


def approve(case):
    body = ("<!-- foragent:v1 -->\n## Goal\nWork.\n## Out of scope\nNone.\n## Done when\n```sh\n"
            f"test -f {LOCK}\n```\n```sh\ntest -f {OTHER}\n```\n")
    case.ctx.git.fetch("origin", "main")
    case.tracker.update_issue(1, title="Work", body=body)
    case.store.save_approval(1, body=body, body_sha=body_hash(body),
                             acceptance_blobs=case.ctx.git.blob_shas("origin/main", list(LOCKS)),
                             approved_by="human", approved_at=case.now[0].isoformat())


def place(case, head, saved_at):
    """Keep the saved branch only locally, or only on origin."""
    if saved_at == "local":
        case.ctx.git.fetch("origin", BRANCH)
        git_command(case.history.clone, "update-ref", f"refs/heads/{BRANCH}", head)
        git_command(case.history.origin, "update-ref", "-d", f"refs/heads/{BRANCH}")
