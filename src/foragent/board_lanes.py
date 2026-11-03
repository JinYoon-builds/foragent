"""Keep lane labels, board Lane options and card Lane values in step with configured lane paths.

The gate tick is the only writer (#1230). Provisioning is its own step and runs first: after
a tick that read the configuration, every configured lane has its GitHub label and board
Lane option, whether or not any issue moves, also with missing or unreadable state. A label
deleted outside Foragent is created again on the next listing that does not show it.

A configuration change is converged only after a
pass over a complete issue listing taken after the change was first applied, and card
Lane values are compared with the board itself, never inferred from labels. The state
file, written only here, keeps the configuration, the listing the change was first seen
with, whether a later listing confirmed it, and the issues still owed a move; repeated
ticks and a gate restart therefore make no GitHub calls once converged.

An issue's owner is the one configured lane whose paths overlap the issue's Files. When no
lane or more than one lane overlaps, the issue keeps its labels: ambiguity and unmatched
paths behave as before.
"""

from __future__ import annotations

import json
import logging

from foragent import board, overlap_files
from foragent.issue import parse_body
from foragent.state_observations import stamp
from foragent.tracker import Issue, LabelSpec, TrackerError, local_issue_models

log = logging.getLogger(__name__)

STATE_FILE = "lane-paths.json"
PREFIX = "lane:"


def _path(store):
    return store.root / STATE_FILE


def _load(store) -> dict:
    """The recorded state; missing, unreadable or malformed state means nothing is known converged."""
    try:
        value = json.loads(_path(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if (not isinstance(value, dict) or not isinstance(value.get("lanes"), dict)
            or not isinstance(value.get("pending", []), list)
            or not all(isinstance(n, int) and not isinstance(n, bool) for n in value.get("pending", []))):
        return {}
    return value


def _configured(config) -> dict[str, list[str]]:
    return {name: list(lane.get("paths", [])) for name, lane in sorted(config.lanes.items())}


def owner(ctx, issue: Issue, lanes: dict[str, list[str]]) -> str | None:
    """The single configured lane whose paths overlap the issue's Files, if exactly one does."""
    files = parse_body(issue.body).files
    owners = {name for name, paths in lanes.items()
              if overlap_files.any_overlap(ctx, files, paths)}
    return next(iter(owners)) if len(owners) == 1 else None


def _carried(ctx, listing: str | None) -> set[str]:
    """Lane labels GitHub showed on issues in the latest complete listing or a later read.

    Older rows and our own unsent writes are left out: only a read from GitHub shows a label.
    """
    at = stamp(listing)
    if at is None:
        return set()
    unsent = {(entry.get("issue"), label) for entry in ctx.store.github_outbox()
              for label in entry.get("labels") or ()}
    return {label for row in ctx.store.local_issues()
            if (seen := stamp(row.get("observed_at"))) is not None and seen >= at
            for label in row.get("labels", ())
            if label.startswith(PREFIX) and (row["number"], label) not in unsent}


def provision(ctx, lanes: dict[str, list[str]], prior: dict | None, listing: str | None) -> dict | None:
    """Make every configured lane's GitHub label and board Lane option exist; None defers to the next tick.

    This step stands alone: it does not depend on any issue moving. It is done again whenever
    the configured lanes (names or paths) or the board differ from the last completed record,
    including when that record is missing, and whenever a newer listing arrives. A lane label
    that an issue of the latest listing shows is taken as present; every other configured lane
    is checked against the label list, so a label deleted outside Foragent, used or not, is
    created again by the next listing. Board Lane options are added on every configuration change.
    """
    names = list(lanes)
    same_config = prior is not None and prior.get("lanes") == lanes and prior.get("board") == ctx.config.board
    if same_config and prior.get("listing") == listing:
        return prior  # Completed for this configuration and this listing: nothing new to look at.
    wanted = [f"{PREFIX}{name}" for name in names]
    carried = _carried(ctx, listing) & set(wanted)
    try:
        if not set(wanted) <= carried:
            existing = ctx.tracker.label_names()
            missing = [LabelSpec(label, "ededed", f"Foragent lane {label[len(PREFIX):]}.")
                       for label in wanted if label not in existing]
            if missing:  # An existing label keeps its colour and description.
                ctx.tracker.ensure_labels(missing)
        if not same_config and ctx.config.board:
            # Missing Lane options go after the existing ones, keeping their IDs.
            board.Project(ctx.tracker, ctx.repo, ctx.config.board).ensure_fields({"Lane": ["main", *names]})
    except FAILURES as exc:
        _deferred(exc, "lane labels and board Lane options")
        return None
    return {"lanes": lanes, "board": ctx.config.board, "listing": listing}


FAILURES = (TrackerError, OSError)  # GitHub or transport failures defer; programming errors surface.
CARDS_QUERY = """query ForagentLaneCards($id: ID!, $cursor: String) {
  node(id: $id) { ... on ProjectV2 { items(first: 100, after: $cursor) {
    nodes { id content { ... on Issue { number repository { nameWithOwner } } }
      fieldValues(first: 100) { nodes { ... on ProjectV2ItemFieldSingleSelectValue {
        name field { ... on ProjectV2SingleSelectField { name } }
      } } } }
    pageInfo { hasNextPage endCursor }
  } } }
}"""
SET_VALUE = """mutation ForagentSetBoardValue($input: UpdateProjectV2ItemFieldValueInput!) {
  updateProjectV2ItemFieldValue(input: $input) { projectV2Item { id } }
}"""


def _deferred(exc: Exception, what: str) -> None:
    if isinstance(exc, TrackerError) and exc.is_rate_limit:
        raise exc
    log.warning("Lane reconciliation deferred (%s): %s", what, exc)


def _cards(ctx) -> dict[int, tuple[str, str | None]]:
    """Each of this repository's cards on the board: item ID and its current Lane value."""
    cards, cursor = {}, None
    while True:
        node = board._data(ctx.tracker, CARDS_QUERY, {"id": ctx.config.board, "cursor": cursor})["node"]
        if node is None:
            raise TrackerError(f"board {ctx.config.board} was deleted or is unavailable")
        for item in node["items"]["nodes"]:
            content = item.get("content") or {}
            repository = (content.get("repository") or {}).get("nameWithOwner", "")
            if content.get("number") is None or repository.lower() != ctx.repo.lower():
                continue
            lane = next((value.get("name") for value in item["fieldValues"]["nodes"]
                         if value and (value.get("field") or {}).get("name") == "Lane"), None)
            cards[content["number"]] = (item["id"], lane)
        if not node["items"]["pageInfo"]["hasNextPage"]:
            return cards
        cursor = node["items"]["pageInfo"]["endCursor"]


class _Board:
    """The configured board's Lane field and cards, read once per pass."""

    def __init__(self, ctx, lanes: dict[str, list[str]]):
        self.ctx, self.field, self.cards = ctx, None, None
        if not ctx.config.board:
            return
        project = board.Project(ctx.tracker, ctx.repo, ctx.config.board)
        try:
            # Missing Lane options go after the existing ones, keeping their IDs; Status stays the team's.
            fields = project.ensure_fields({"Lane": ["main", *lanes]})  # Writes only a missing option.
            self.field = fields.get("Lane")
            self.cards = _cards(ctx)
        except FAILURES as exc:
            _deferred(exc, "board Lane")
            self.field = self.cards = None

    @property
    def ready(self) -> bool:
        return not self.ctx.config.board or self.cards is not None

    def stale(self, number: int, lane: str) -> bool:
        """An existing card shows another Lane; an issue without a card stays off the board."""
        return self.cards is not None and number in self.cards and self.cards[number][1] != lane

    def move(self, number: int, lane: str) -> bool:
        option = next((o for o in (self.field or {}).get("options", []) if o["name"] == lane), None)
        if option is None:
            log.warning("Board Lane option %s is unavailable; #%s waits", lane, number)
            return False
        try:
            board._data(self.ctx.tracker, SET_VALUE, {"input": {
                "projectId": self.ctx.config.board, "itemId": self.cards[number][0],
                "fieldId": self.field["id"], "value": {"singleSelectOptionId": option["id"]}}})
        except FAILURES as exc:
            _deferred(exc, f"board Lane of #{number}")
            return False
        return True


def _listing(ctx) -> str | None:
    """When the last complete issue listing started; a failed refresh leaves it unchanged."""
    return ctx.store.issues_observation().get("as_of")


def reconcile(ctx, report=None) -> None:
    """Align open issues' lane labels and card Lane values with the configured lane paths.

    A pass over every stored open issue runs when the configuration changes and once more
    after a newer complete listing, so issues missing from the store at the change are
    aligned too. Between those, only issues still owed a move are revisited.
    """
    lanes = _configured(ctx.config)
    saved = _load(ctx.store)  # {} when nothing was recorded: every issue is checked, as after a change.
    listing = _listing(ctx)
    prior = saved.get("labels")
    if not isinstance(prior, dict):
        prior = None  # A missing or malformed record provisions in full.
    labels = provision(ctx, lanes, prior, listing)
    if labels is None:
        return  # Nothing is recorded, so the next tick provisions again before anything moves.
    changed = saved.get("lanes") != lanes or saved.get("board") != ctx.config.board
    pending = set(saved.get("pending", []))
    confirming = not changed and not saved.get("confirmed") and listing != saved.get("listing")
    running = {run.number for run in ctx.store.active_runs()}
    if not (changed or confirming or pending - running):
        if labels != saved.get("labels"):
            ctx.store._write_json(_path(ctx.store), {**saved, "labels": labels})
        return  # Converged, or only running issues wait: no further GitHub call.
    cards = _Board(ctx, lanes)  # Every pass adds a missing Lane option before it relies on one.
    full = changed or confirming
    waiting, seen = set(), set()
    for issue in local_issue_models(ctx.store):
        seen.add(issue.number)
        if issue.state != "open" or not (full or issue.number in pending):
            continue
        lane = owner(ctx, issue, lanes)
        if lane is None:
            continue
        wanted = f"{PREFIX}{lane}"
        stale = sorted(label for label in issue.labels if label.startswith(PREFIX) and label != wanted)
        labelled = not stale and wanted in issue.labels
        if labelled and not cards.stale(issue.number, lane):
            continue
        if issue.number in running or not cards.ready:  # A run keeps its lane; a label waits for its option.
            waiting.add(issue.number)
            continue
        if not labelled:
            try:
                if wanted not in issue.labels:
                    ctx.tracker.add_labels(issue.number, [wanted])
                if stale:
                    ctx.tracker.remove_labels(issue.number, stale)
            except FAILURES as exc:
                _deferred(exc, f"lane label of #{issue.number}")
                waiting.add(issue.number)
                continue
        if cards.stale(issue.number, lane) and not cards.move(issue.number, lane):
            waiting.add(issue.number)
    unseen = pending - seen
    if not cards.ready:
        # Keep the change unconverged: the next tick reads the board and the issues again.
        _save(ctx, saved.get("lanes"), saved.get("listing"), confirmed=False,
              pending=waiting | unseen, labels=labels, board=None)
        return
    if changed:
        _save(ctx, lanes, listing, confirmed=False, pending=waiting, labels=labels)  # A newer listing confirms it.
    elif confirming:
        _save(ctx, lanes, saved.get("listing"), confirmed=True, pending=waiting, labels=labels)  # Complete: unseen are gone.
    else:
        # A waiting issue missing from the store returns to the next full pass, after a newer listing.
        _save(ctx, lanes, listing if unseen else saved.get("listing"),
              confirmed=saved.get("confirmed", False) and not unseen, pending=waiting, labels=labels)


def _save(ctx, lanes, listing, *, confirmed: bool, pending, labels: dict, board=...) -> None:
    ctx.store._write_json(_path(ctx.store), {
        "lanes": lanes, "board": ctx.config.board if board is ... else board,
        "listing": listing, "confirmed": confirmed, "pending": sorted(pending), "labels": labels})
