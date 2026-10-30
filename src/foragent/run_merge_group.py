"""Single-writer merge groups over #712's append-only merge queue (#459).

State ownership (one writer each, everything appended to merge-queue.jsonl):

- a run's own ``ready`` rows carry its eligibility and check inputs; a changed
  head appends a newer ready row and keeps the first row's FIFO place;
- the FIFO turn holder that leads appends one ``group`` claim and one
  ``verdict`` for it, and nothing else about other runs;
- each member appends its own ``merged`` row after merging its own PR.

Nobody rewrites, retires or completes another run's rows, PR, issue, run
record or board card. Claim liveness and the turn are derived from the run
records: an open claim (no verdict) reserves its members only while its
leader's run is running. A member reuses a ``pass`` verdict only for its exact
claimed head and only when every main commit since the claim's base is an
earlier member's recorded merge or a result-neutral move.
"""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import tempfile
from typing import TYPE_CHECKING, Any
import uuid

from foragent.git import GitError
from foragent.run_base_move import BaseMove, base_move_is_neutral
from foragent.run_checks import _append, _pytest_command, _related_unit_tests, trusts_ci
from foragent.run_merge_turn import QUEUE, Seat, _append as append_row, _live, _waiting, latest_ready
from foragent.run_waits import _progress, _test_slot

if TYPE_CHECKING:
    from foragent.run_attempt import Implementation
    from foragent.state import StateStore


def rows(store: StateStore) -> list[dict]:
    """Every event row in order, including group and verdict rows (which name no ``n``)."""
    try:
        raw = (store.root / QUEUE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    events = []
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            events.append(row)
    return events


def _turn_rows(events: list[dict]) -> list[dict]:
    """The rows #712's FIFO reads: per-run rows that name an issue."""
    return [row for row in events if isinstance(row.get("n"), int)]


def _claims(events: list[dict], target: str) -> list[tuple[dict, dict | None]]:
    """(claim, its verdict or None) for ``target``, in append order."""
    verdicts = {row.get("id"): row for row in events if row.get("event") == "verdict"}
    return [(row, verdicts.get(row.get("id"))) for row in events
            if row.get("event") == "group" and row.get("target") == target]


def _covers(claim: dict, key: tuple[int, str | None]) -> dict | None:
    """The claimed member entry for this run instance, if the claim names it."""
    started = claim.get("started_at") or []
    for index, member in enumerate(claim.get("members") or []):
        if member.get("n") == key[0] and (started[index] if index < len(started) else None) == key[1]:
            return member
    return None


def squash_tree(git, parent: str, head: str) -> str | None:
    """The tree a squash merge of ``head`` onto ``parent`` produces, or None on conflict or error."""
    try:
        return git._call(["merge-tree", "--write-tree", parent, head]).splitlines()[0].strip()
    except (GitError, IndexError):
        return None


def is_squash_of(git, commit: str, head: str) -> bool:
    """``commit`` is exactly the squash of ``head`` onto its first parent (by content, not position)."""
    try:
        parent = git._call(["rev-parse", f"{commit}^1"]).strip()
        tree = git._call(["rev-parse", f"{commit}^{{tree}}"]).strip()
    except GitError:
        return False
    return squash_tree(git, parent, head) == tree


def _eligible(row: dict | None, max_group: int) -> bool:
    return bool(row and row.get("group") and row.get("checks") == "local" and row.get("auto")
                and max_group > 1)


def grouping_enabled(attempt: Implementation, automatic: bool) -> bool:
    """Only automatic merges into the configured base under configured local checks group."""
    config = attempt.ctx.config
    return (automatic and getattr(config, "merge_group_max", 1) > 1
            and getattr(config, "checks", "local") == "local"
            and attempt.target_branch == config.base_branch
            and attempt.target_branch != attempt.batch_branch)


class Membership:
    """One run's view of merge groups during publication; it writes only its own rows."""

    def __init__(self, attempt: Implementation, place: Seat):
        self.attempt, self.place = attempt, place
        self.automatic = False
        self.claim: dict | None = None  # A pass claim this run may still consume.
        self.solo = False  # Left the group path: today's one-PR path for the rest of the run.
        self.led = False
        self.left = False

    @property
    def enabled(self) -> bool:
        return not self.solo and grouping_enabled(self.attempt, self.automatic)

    # ---- the run's own ready row -------------------------------------------------
    def ready_fields(self) -> dict[str, Any]:
        """This run's eligibility and the check inputs a leader needs, read from no other source."""
        attempt, ctx = self.attempt, self.attempt.ctx
        fields: dict[str, Any] = {"group": grouping_enabled(attempt, self.automatic),
                                  "checks": getattr(ctx.config, "checks", "local"), "auto": self.automatic}
        if not fields["group"]:
            return fields
        from foragent import runner

        baseline = attempt.baseline
        test_command = ctx.config.test_command
        if test_command is None:
            test_command = next(iter(runner.detect_test_commands(attempt.worktree)), None)
        unit_command = _pytest_command(test_command) if test_command else None
        units = _related_unit_tests(attempt.worktree, list(attempt.paths), ctx.config.acceptance_dir,
                                    own_only=ctx.config.runner_unit_tests != "all"
                                    and trusts_ci(attempt.worktree, ctx.config))
        fields.update(
            branch=attempt.branch, done_when=list(attempt.parsed.done_when_commands),
            baseline_command=baseline.command if baseline else None,
            baseline_ids=list(baseline.node_ids) if baseline else [],
            baseline_base_sha=baseline.base_sha if baseline else None,
            baseline_test_command=(baseline.test_command or test_command) if baseline else None,
            unit_command=unit_command, unit_tests=list(units),
        )
        return fields

    # ---- at the FIFO turn --------------------------------------------------------
    def at_turn(self, head: str) -> None:
        """Holding the turn: consume a pass verdict for this exact head, or lead a new group."""
        if not self.enabled or self.claim is not None:
            return
        events = rows(self.attempt.ctx.store)
        for claim, verdict in _claims(events, self.place.target):
            member = _covers(claim, self.place.key)
            if member is None or verdict is None:
                continue
            if verdict.get("result") == "pass" and member.get("head_sha") == head:
                self.claim = claim
                return
            # A failed group (or one for an older head) is never permission to skip today's path.
            self.solo = True
            return
        if not self.led:
            self._lead(head)

    def _lead(self, head: str) -> None:
        attempt, ctx, n = self.attempt, self.attempt.ctx, self.attempt.n
        maximum = getattr(ctx.config, "merge_group_max", 1)
        ctx.git.fetch("origin", self.place.target)
        base_sha = ctx.git.ref_sha(f"origin/{self.place.target}")
        with ctx.store._state_lock():
            events = rows(ctx.store)
            own = latest_ready(_turn_rows(events), self.place)
            if not _eligible(own, maximum) or own.get("head_sha") != head:
                return
            members = [(self.place.key, own)] + self._candidates(events, maximum - 1)
            if len(members) < 2:
                return
            claim = {"event": "group", "id": uuid.uuid4().hex, "leader": n, "target": self.place.target,
                     "base_sha": base_sha,
                     "members": [{"n": key[0], "head_sha": row["head_sha"]} for key, row in members],
                     "started_at": [key[1] for key, _ in members], "at": ctx.now().isoformat()}
            append_row(ctx.store, claim)
        self.led = True
        _append(attempt.log, "Merge group " + ", ".join(f"#{m['n']}" for m in claim["members"])
                + f": joint check on {base_sha}")
        passed = False
        try:
            passed = joint_check(attempt, [row for _, row in members], base_sha)
        finally:
            # Even when the check raised, close this live leader's claim with a verdict:
            # an open claim would otherwise keep reserving members while the run cleans up.
            self._verdict(claim, passed)
        _append(attempt.log, f"Merge group joint check {'passed' if passed else 'failed'}")
        if passed:
            self.claim = claim
        else:
            self.solo = True

    def _verdict(self, claim: dict, passed: bool) -> None:
        ctx = self.attempt.ctx
        with ctx.store._state_lock():
            append_row(ctx.store, {"event": "verdict", "id": claim["id"], "target": self.place.target,
                                   "result": "pass" if passed else "fail", "culprits": [],
                                   "at": ctx.now().isoformat()})

    def _candidates(self, events: list[dict], room: int) -> list[tuple[tuple[int, str | None], dict]]:
        """The oldest live, eligible ready others no live open or decided claim names."""
        store, target = self.attempt.ctx.store, self.place.target
        taken: set[tuple[int, str | None]] = set()
        for claim, verdict in _claims(events, target):
            leader = (claim.get("leader"), (claim.get("started_at") or [None])[0])
            if verdict is not None or _live(store, leader):
                taken.update((m.get("n"), s) for m, s in zip(claim.get("members") or [],
                                                             claim.get("started_at") or []))
        picked = []
        for key in _waiting(_turn_rows(events), target):
            if len(picked) >= room:
                break
            if key == self.place.key or key in taken or not _live(store, key):
                continue
            latest = latest_ready(_turn_rows(events), Seat(key[0], key[1], target))
            if _eligible(latest, room + 1):
                picked.append((key, latest))
        return picked

    # ---- reuse of a pass verdict -------------------------------------------------
    def base_ok(self, target_sha: str) -> bool:
        """Every main commit since the claim's base is an earlier member's merge or neutral."""
        if self.claim is None:
            return False
        base = self.claim["base_sha"]
        if target_sha == base:
            return True
        git = self.attempt.git
        try:
            if not git.is_ancestor(base, target_sha):
                return False
            commits = git._call(["rev-list", "--reverse", "--first-parent", f"{base}..{target_sha}"]).split()
        except GitError:
            return False
        heads = self._earlier_heads()
        previous = base
        for commit in commits:
            # An earlier member's merge is proven by content: the commit must be exactly its
            # recorded head squashed onto the commit's parent, never just a recorded SHA.
            if not any(is_squash_of(git, commit, head) for head in heads) and not base_move_is_neutral(
                    self.attempt, BaseMove(previous, commit)):
                return False
            previous = commit
        return True

    def _members(self) -> list[tuple[dict, str | None]]:
        claim = self.claim or {}
        return list(zip(claim.get("members") or [], claim.get("started_at") or []))

    def _position(self) -> int:
        return next(index for index, (member, started) in enumerate(self._members())
                    if (member["n"], started) == self.place.key)

    def _merged_rows(self) -> dict[tuple[int, str | None], dict]:
        claim_id = (self.claim or {}).get("id")
        return {(row["n"], row.get("started_at")): row for row in rows(self.attempt.ctx.store)
                if row.get("event") == "merged" and row.get("group") == claim_id}

    def _earlier_heads(self) -> list[str]:
        """Claimed heads of earlier members that report merging exactly that head."""
        merged, heads = self._merged_rows(), []
        for member, started in self._members()[:self._position()]:
            row = merged.get((member["n"], started))
            if row is not None and row.get("head_sha") == member["head_sha"]:
                heads.append(member["head_sha"])
        return heads

    def _later_holder(self) -> bool:
        """A later live member still holds this pass verdict for its exact head."""
        store, merged = self.attempt.ctx.store, self._merged_rows()
        turn_rows = _turn_rows(rows(store))
        for member, started in self._members()[self._position() + 1:]:
            key = (member["n"], started)
            if key in merged or not _live(store, key):
                continue
            latest = latest_ready(turn_rows, Seat(key[0], key[1], self.place.target))
            if latest is not None and latest.get("head_sha") == member["head_sha"]:
                return True
        return False

    def covered(self) -> list[int] | None:
        return [member["n"] for member, _ in self._members()] if self.claim else None

    def size(self) -> int:
        return len(self._members()) if self.claim else 1

    def others(self) -> list[int]:
        return [n for n in (self.covered() or []) if n != self.attempt.n]

    def reinstall_now(self) -> bool:
        """The last merging member of a group reinstalls once; a single run always does."""
        return self.claim is None or not self._later_holder()

    def merged(self, pr: int, head: str, base_sha: str) -> None:
        """Append this run's own merged row; main is read under its own merge.lock.

        merged_sha is informational: followers prove each interval commit by content.
        """
        if self.claim is None:
            return
        ctx = self.attempt.ctx
        try:
            ctx.git.fetch("origin", self.place.target)
            tip = ctx.git.ref_sha(f"origin/{self.place.target}")
            after = ctx.git._call(["rev-list", "--reverse", "--first-parent", f"{base_sha}..{tip}"]).split()
            # The commit whose content is this head's squash, not merely the first one after
            # the base: an outside push may land between the base read and the merge.
            merged_sha = next((commit for commit in after if is_squash_of(ctx.git, commit, head)), None)
        except GitError:
            merged_sha = None  # Later members then re-prove the interval by content anyway.
        with ctx.store._state_lock():
            append_row(ctx.store, {"n": self.place.n, "event": "merged", "started_at": self.place.started_at,
                                   "target": self.place.target, "group": self.claim["id"], "head_sha": head,
                                   "merged_sha": merged_sha, "pr": pr, "at": ctx.now().isoformat()})

    def complete(self, pr: int, head: str, base_sha: str, *, reinstall: bool) -> str | None:
        """After this run merged its own PR: its own facts and rows; the close comment for a group."""
        from foragent import runner
        from foragent.i18n import text
        from foragent.orch_events import append_event

        ctx, n = self.attempt.ctx, self.attempt.n
        self.merged(pr, head, base_sha)
        ctx.store.update_run(n, merge_group_size=self.size(), merge_group_bisect_checks=0)
        if reinstall and self.reinstall_now():
            runner.after_merge(ctx, pr, self.place.target)
        others = ", ".join(f"#{other}" for other in self.others())
        if not others:
            return None
        append_event(ctx.store, ctx.now(), n, "merge",
                     text("merge_group_merged", "en", pr=pr, head=head, others=others))
        return text("merge_group_closed", ctx.config.language, others=others)

    def _pass_claim(self) -> dict | None:
        """The newest pass claim naming this run instance, read from the queue itself."""
        found = None
        for claim, verdict in _claims(rows(self.attempt.ctx.store), self.place.target):
            if verdict is not None and verdict.get("result") == "pass" and _covers(claim, self.place.key):
                found = claim
        return found

    def leave(self) -> None:
        """Leave the group path without merging: reinstall earlier group merges if none follows.

        The group is read from the queue, so a run that leaves before loading its
        claim (for example its head changed while it waited) still reinstalls.
        """
        if self.left:
            return
        if self.claim is None and grouping_enabled(self.attempt, self.automatic):
            self.claim = self._pass_claim()
        if self.claim is None:
            return
        self.left = True
        try:
            earlier = [row for key, row in self._merged_rows().items() if key != self.place.key]
            if earlier and not self._later_holder():
                from foragent import runner

                runner.after_merge(self.attempt.ctx, earlier[-1]["pr"], self.place.target)
        finally:
            self.claim, self.solo = None, True


def joint_check(attempt: Implementation, members: list[dict[str, Any]], base_sha: str) -> bool:
    """Pinned main plus every claimed head merged in queue order, with the union of checks.

    Runs in a fresh detached worktree outside merge.lock. Identical Done-when
    commands run once, the baseline IDs union once (plus what passes on the
    pinned main when a member's baseline ran on an older base), and the related
    units once per command. Checks that modify tracked files fail the group.
    """
    from foragent import runner

    n, ctx, log = attempt.n, attempt.ctx, attempt.log
    combined = squash_sequence(attempt, members, base_sha)
    if combined is None:
        return False
    with tempfile.TemporaryDirectory(prefix=f"foragent-group-{n}-", dir=ctx.store.root) as directory:
        worktree = Path(directory) / "worktree"
        ctx.git.add_detached_worktree(worktree, combined)
        try:
            git = ctx.git.at(worktree)
            combined_head = git.head_sha()
            git.stage_changes()
            combined_tree = git.staged_tree_sha()

            def check(command: str, timeout_s: int = 900):
                result = runner._check(command, worktree, log, timeout_s, progress=lambda: _progress(n, ctx),
                                       slot=_test_slot(n, ctx))
                _progress(n, ctx)
                return result

            results = [check(command) for command in dict.fromkeys(
                command for row in members for command in row.get("done_when") or ())]
            ids = {node for row in members for node in row.get("baseline_ids") or ()}
            fresh = _baseline_on(attempt, members, base_sha, worktree)
            if fresh is None:
                return False
            ids |= fresh
            command = next((row["baseline_command"] for row in members if row.get("baseline_command")), None)
            if command and ids:
                results.append(check(f"{command} {shlex.join(sorted(ids))}"))
            for unit_command, units in _unit_runs(attempt, members, worktree, git, base_sha).items():
                results.append(check(f"{unit_command} {shlex.join(units)}", max(900, runner._remaining(n, ctx))))
            git.stage_changes()
            if git.head_sha() != combined_head or git.staged_tree_sha() != combined_tree:
                _append(log, "Merge group: joint check commands modified tracked files")
                return False
            return all(result.exit_code == 0 and not result.timed_out for result in results)
        finally:
            ctx.git.remove_worktree(worktree)


def squash_sequence(attempt: Implementation, members: list[dict[str, Any]], base_sha: str) -> str | None:
    """A commit whose tree equals squash-merging every claimed head onto ``base_sha`` in order.

    Each step is the tree GitHub's squash produces (merge-tree of the head onto the
    previous result), so the joint check sees exactly what sequential merges publish,
    even when a later head contains and reverts an earlier member's commit.
    """
    ctx, current = attempt.ctx, base_sha
    for row in members:
        try:
            if row.get("branch"):
                ctx.git.fetch("origin", row["branch"])
        except GitError:
            pass  # The head may already be local; merge-tree below decides.
        tree = squash_tree(ctx.git, current, row["head_sha"])
        if tree is None:
            _append(attempt.log, f"Merge group: #{row['n']} does not squash cleanly onto the group")
            return None
        try:
            current = ctx.git._call(["-c", "user.name=foragent", "-c", "user.email=foragent@localhost",
                                     "commit-tree", tree, "-p", current, "-m", f"Merge group: #{row['n']}"]).strip()
        except GitError as exc:
            _append(attempt.log, f"Merge group: #{row['n']} could not be combined: {exc}")
            return None
    return current


def _baseline_on(attempt: Implementation, members: list[dict[str, Any]], base_sha: str,
                 worktree: Path) -> set[str] | None:
    """Pass IDs on the pinned main when a member's baseline ran elsewhere; None if unavailable."""
    from foragent.run_baseline import _capture_acceptance_baseline

    # A member without a baseline (or with one from another base) gives no evidence for
    # this main: capture what passes here, unless no test command is known anywhere.
    if all(row.get("baseline_base_sha") == base_sha for row in members):
        return set()
    test_command = next((row["baseline_test_command"] for row in members if row.get("baseline_test_command")), None)
    if test_command is None:
        from foragent import runner

        test_command = attempt.ctx.config.test_command or next(iter(runner.detect_test_commands(worktree)), None)
    if test_command is None:
        return set()
    try:
        baseline = _capture_acceptance_baseline(attempt.n, attempt.ctx, test_command, base_sha)
    except Exception as exc:  # noqa: BLE001 - an unprotected group fails over to one at a time
        _append(attempt.log, f"Merge group: acceptance baseline on {base_sha} failed: {exc}")
        return None
    return set(baseline.node_ids) if baseline else set()


def _unit_runs(attempt: Implementation, members: list[dict[str, Any]], worktree: Path, git,
               base_sha: str) -> dict[str, list[str]]:
    """Each member's own unit selection plus the combined one, once per command."""
    from foragent import runner

    ctx = attempt.ctx
    runs: dict[str, set[str]] = {}
    for row in members:
        if row.get("unit_command") and row.get("unit_tests"):
            runs.setdefault(row["unit_command"], set()).update(row["unit_tests"])
    test_command = ctx.config.test_command
    if test_command is None:
        test_command = next(iter(runner.detect_test_commands(worktree)), None)
    combined = _pytest_command(test_command) if test_command else None
    units = _related_unit_tests(worktree, git.changed_paths(base_sha), ctx.config.acceptance_dir,
                                own_only=ctx.config.runner_unit_tests != "all" and trusts_ci(worktree, ctx.config))
    if combined and units:
        runs.setdefault(combined, set()).update(units)
    return {command: sorted(path for path in tests if (worktree / path).is_file())
            for command, tests in runs.items() if any((worktree / path).is_file() for path in tests)}
