"""The lane dream worker (#189): one executor turn that tidies the lane's memory."""

from __future__ import annotations

from contextlib import contextmanager
import difflib
from datetime import UTC
import fcntl
import os
from pathlib import Path
import subprocess
import tempfile
from typing import TYPE_CHECKING, Any

from foragent.orch_dream import _runs_since, last_dream_at, log_path, media_root, memory_path, record_done, worktree_path

if TYPE_CHECKING:
    from foragent.config import Config
    from foragent.runner import RunContext
    from foragent.state import StateStore

GIT_IDENTITY = ["-c", "user.name=foragent", "-c", "user.email=foragent@localhost"]


@contextmanager
def _media_lock(target: Path):
    """Serialize every repository's dream on one lane's media file (the bundle is machine-wide)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.with_name(f".{target.name}.lock").open("a", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None


def _media_bundle() -> dict[Path, str]:
    """The whole media bundle as the prompt shows it; each file is read once, under its lock."""
    root = media_root()
    bundle = {}
    for path in sorted(root.glob("*.md")) if root.is_dir() else []:
        with _media_lock(path):
            if (content := _read(path)) is not None:
                bundle[path] = content
    return bundle


def publish_media(proposal: Path, target: Path, snapshot: str | None) -> str:
    """Write the proposal over the snapshot the dream saw; merge in another dream's later write.

    Another repository's dream of the same lane may have written the file since this
    dream read it. Its text is merged three ways (git merge-file); on a conflict the
    current file is kept, and the lessons stay in the lane memory for a later dream.
    """
    new = proposal.read_text(encoding="utf-8")
    with _media_lock(target):
        current = _read(target)
        if current is not None and current != snapshot:
            with tempfile.TemporaryDirectory(prefix="foragent-media-") as directory:
                files = []
                for name, text in (("current", current), ("base", snapshot or ""), ("proposal", new)):
                    (path := Path(directory) / name).write_text(text, encoding="utf-8")
                    files.append(str(path))
                merged = subprocess.run(["git", "merge-file", "-p", *files], capture_output=True, text=True)
            if merged.returncode != 0:
                return f"kept {target}: another dream changed it and the texts conflict"
            new = merged.stdout
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        temporary.write_text(new, encoding="utf-8")
        os.replace(temporary, target)
    return f"wrote {target}"


def prompt(store: StateStore, config: Config, lane: str, bundle: dict[Path, str]) -> str:
    """Every run of the lane since its last dream (all of them before the first) and the whole bundle."""
    memory = memory_path(config, lane)
    media = media_root() / f"{lane}.md"
    since = last_dream_at(store, lane)
    lines = []
    for run, finished in _runs_since(store, lane, since):
        reviews = sorted((store.root / "runs" / f"issue-{run.number}").glob("review-*.txt"))
        detail = f": {run.detail}" if run.detail else ""
        lines.append(f"- #{run.number} {run.outcome} at {finished or run.started_at}{detail}\n"
                     f"  log: {store.log_path(run.number)}"
                     + "".join(f"\n  review: {path}" for path in reviews))
    history = "\n".join(lines) or "- (no finished runs)"
    scope = f"since its last dream at {since.isoformat()}" if since else "over its full history"
    media_text = (f"The existing media bundle ({media_root()}/):\n"
                  + "\n".join(f"--- {path} ---\n{content}" for path, content in bundle.items())
                  if bundle else f"The media bundle ({media_root()}/) has no files yet.")
    return f"""You are dreaming for the "{lane}" lane: tidy the lane's memory between work.
Do not touch code. Do not edit, modify or change code or any file other than {memory} and the media proposal below.

Memory file: {memory} (create it if it is missing).
Decisions: docs/decisions.md is authoritative. Follow docs/decisions.md whenever a lesson disagrees with it.

The lane's runs {scope}:
{history}

Steps:
1. Read the run logs and review files listed above. Look at the review findings, the failure reasons and repeated pitfalls.
2. Add new lessons the runs teach (project-specific patterns, pitfalls, commands) to {memory}.
3. Remove stale or outdated lessons that the code or the runs no longer support.
4. Deduplicate: merge duplicate or overlapping lessons into one line.
5. Resolve contradictions between lessons. Where a lesson conflicts with docs/decisions.md, prefer docs/decisions.md.
6. Promote lessons that recur across projects to the cross-project media bundle {media}.
   Judge "repeated across projects" from the existing media bundle below plus this lane's memory; do not read other repositories.
   Write the complete proposed text of {media} to .foragent/media-{lane}.md in this worktree; the gate copies it there.
   Leave .foragent/media-{lane}.md absent when nothing should be promoted.

{media_text}

Finish with one sentence saying what you changed.
"""


def _changed_lines(before: str, after: str) -> int:
    return sum(line[:2] in {"+ ", "- "} for line in difflib.ndiff(before.splitlines(), after.splitlines()))


def _dream_worktree(ctx: RunContext, lane: str, worktree: Path):
    """Start from the lane's dream branch when the clone has one, so an unmerged PR's memory is read.

    The base is merged in; a branch whose PR was squash-merged merges cleanly (the same
    edits are on both sides). On a conflict the dream reads the branch as it is, and
    the orchestrator sees the PR conflict with the base. Only local refs are read.
    """
    branch, base = f"foragent/dream-{lane}", f"origin/{ctx.config.base_branch}"
    remote = f"refs/remotes/origin/{branch}"
    try:
        ctx.git._call(["rev-parse", "--verify", "--quiet", remote])
    except Exception:
        remote = None
    ctx.git.add_worktree(worktree, branch, remote or base)
    git = ctx.git.at(worktree)
    if remote is not None:
        try:
            git._call([*GIT_IDENTITY, "merge", "--no-edit", base])
        except Exception:
            git._call(["merge", "--abort"])
    return git


def _publish(ctx: RunContext, git, lane: str, memory: str, count: int) -> int:
    """Push the committed memory edit and open or update the lane's dream PR.

    An open dream PR keeps its earlier, unmerged edits: this dream's commit is merged
    onto that branch and pushed with a lease on the head it merged, never forced over it.
    """
    branch, base = f"foragent/dream-{lane}", ctx.config.base_branch
    title = f"Lane {lane} memory: {ctx.now().astimezone().date().isoformat()}"
    pr = ctx.tracker.find_pr(branch)  # The one GitHub lookup, made only when memory changed.
    if pr is None:
        git.push(branch)  # A merged or closed PR's branch holds nothing unmerged.
    else:
        git.fetch("origin", branch)
        head = git._call(["rev-parse", f"refs/remotes/origin/{branch}"]).strip()
        try:
            git._call([*GIT_IDENTITY, "merge", "--no-edit", "-m", title, f"refs/remotes/origin/{branch}"])
        except Exception:
            git._call(["merge", "--abort"])
            raise
        git.push(branch, lease=head)
    body = (f"The {lane} lane's dream tidied `{memory}` ({count} lines added or removed).\n\n"
            "Only the lane's memory directory changes. The orchestrator merges it when CI passes "
            "(skills/fa-orchestrator/references/dreams.md).")
    if pr is None:
        return ctx.tracker.create_pr(head=branch, base=base, title=title, body=body)
    ctx.tracker.update_pr(pr, base=base, title=title, body=body)
    return pr


def run_dream(lane: str, ctx: RunContext) -> int:
    """The dream worker; it owns runs/dream-<lane>.json while its process lives."""
    from foragent import usage_events
    from foragent.executors import classify_result
    from foragent.processes import observe_tree, track_process_group
    from foragent.run_implementation import lane_config

    store, config = ctx.store, ctx.config
    run = store.get_dream(lane)
    if run is None or run.status != "running":
        print(f"foragent: no running dream record for lane {lane}")
        return 1

    def own(identities: list[dict]) -> None:
        store.update_dream(lane, process_identities=identities)  # Added to the record under its lock.

    if run.pid is None:
        store.update_dream(lane, pid=os.getpid())
        try:
            own(observe_tree(roots=(os.getpid(),)))
        except OSError:
            pass  # Without birth evidence a stop refuses; the tick waits for the process to end.
    worktree = worktree_path(store, lane)
    log = log_path(store, lane)
    log.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        if worktree.exists():
            ctx.git.remove_worktree(worktree)
        git = _dream_worktree(ctx, lane, worktree)
        created = True
        memory = memory_path(config, lane)
        memory_file = worktree / memory
        before = memory_file.read_text(encoding="utf-8") if memory_file.exists() else ""
        lane_settings = lane_config(config, lane)
        tier = config.tiers["s"]
        options: dict[str, Any] = {}
        if lane_settings.executor == "claude":
            model = (config.lanes.get(lane) or {}).get("model") or tier.get("claude_model") or lane_settings.executor_model
        else:
            model = lane_settings.executor_model
            options["effort"] = tier.get("codex_effort")
        executor = ctx.executor_factory(lane_settings.executor)
        target = media_root() / f"{lane}.md"
        bundle = _media_bundle()
        covered_until = ctx.now()  # Runs finishing from here on belong to the next dream.
        text = prompt(store, config, lane, bundle)
        offset = log.stat().st_size if log.exists() else 0
        size = offset

        def poll() -> None:
            nonlocal size
            current = log.stat().st_size if log.exists() else 0
            if current > size:
                stamp = ctx.now().astimezone(UTC).isoformat()
                store.update_dream(lane, last_progress_at=stamp, last_activity_at=stamp)
            size = current

        def execute():
            with track_process_group(lambda pgid: store.update_dream(lane, executor_pgid=pgid), tree_callback=own):
                return executor.run(worktree=worktree, prompt=text, model=model,
                                    timeout_s=config.max_runtime_minutes * 60, log_path=log, **options)

        try:
            result = ctx.poll_hook(execute, poll)
        finally:
            usage_events.record_from_log(store, log, offset, ctx.now())
        if classify_result(result) != "normal" or result.timed_out or result.exit_code not in {None, 0}:
            outcome = "limit-wait" if classify_result(result) == "limit" else "error"
            store.finish_dream(lane, outcome, f"Dream executor ended without finishing (exit {result.exit_code}).")
            return 1
        proposal = worktree / ".foragent" / f"media-{lane}.md"
        if proposal.is_file():
            with log.open("a", encoding="utf-8") as stream:
                stream.write(f"foragent: media {publish_media(proposal, target, bundle.get(target))}\n")
        directory = str(Path(memory).parent)
        status = git._call(["status", "--porcelain", "--untracked-files=all", "--", directory])
        count, pr = 0, None
        if status.strip():
            after = memory_file.read_text(encoding="utf-8") if memory_file.exists() else ""
            count = _changed_lines(before, after)
            git.commit_paths([directory], f"Lane {lane} memory: {ctx.now().astimezone().date().isoformat()}")
            pr = _publish(ctx, git, lane, memory, count)
        record_done(store, lane, count, config.language, ctx.now(), covered_until=covered_until, pr=pr)
        store.finish_dream(lane, "dreamed", f"{count} memory lines tidied.")
        return 0
    except Exception as exc:
        store.finish_dream(lane, "error", f"Dream failed: {exc}")
        raise
    finally:
        if created:
            try:
                ctx.git.remove_worktree(worktree)
            except Exception:
                pass  # The next dream removes a left-over worktree before it starts.
