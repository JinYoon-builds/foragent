"""Keep lane labels, board Lane options and card Lane values in step with configured lane paths.

The gate tick is the only writer (#1230). A configuration change is converged only after a
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


class _Labels:
    """The configured lane labels, checked against GitHub's own label list at most once per pass.

    Issue rows are never evidence that a label exists: a label deleted on GitHub after any read
    would still show there. Only the label list is, so it is read before work depends on it.
    """

    def __init__(self, ctx, lanes: dict[str, list[str]]):
        self.ctx, self.lanes, self.checked = ctx, lanes, None

    def ensure(self) -> bool:
        """Create only the missing lane labels; an existing label keeps its colour and description."""
        if self.checked is None:
            try:
                existing = self.ctx.tracker.label_names()
                missing = [LabelSpec(f"{PREFIX}{name}", "ededed", f"Foragent lane {name}.")
                           for name in self.lanes if f"{PREFIX}{name}" not in existing]
                if missing:
                    self.ctx.tracker.ensure_labels(missing)
                self.checked = True
            except FAILURES as exc:
                _deferred(exc, "lane labels")
                self.checked = False
        return self.checked


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
    changed = saved.get("lanes") != lanes or saved.get("board") != ctx.config.board
    pending = set(saved.get("pending", []))
    confirming = not changed and not saved.get("confirmed") and listing != saved.get("listing")
    running = {run.number for run in ctx.store.active_runs()}
    if not (changed or confirming or pending - running):
        return  # Converged, or only running issues wait: no GitHub call.
    labels = _Labels(ctx, lanes)
    # A recorded change of the configured lanes always checks GitHub's labels on its own tick.
    if saved.get("lanes") is not None and saved["lanes"] != lanes and not labels.ensure():
        return  # Nothing is recorded, so the next tick tries again.
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
            if not labels.ensure():  # Any pass checks the labels before it applies one.
                waiting.add(issue.number)
                continue
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
              pending=waiting | unseen, board=None)
        return
    if changed:
        _save(ctx, lanes, listing, confirmed=False, pending=waiting)  # A newer listing confirms it.
    elif confirming:
        _save(ctx, lanes, saved.get("listing"), confirmed=True, pending=waiting)  # Complete: unseen are gone.
    else:
        # A waiting issue missing from the store returns to the next full pass, after a newer listing.
        _save(ctx, lanes, listing if unseen else saved.get("listing"),
              confirmed=saved.get("confirmed", False) and not unseen, pending=waiting)


def _save(ctx, lanes, listing, *, confirmed: bool, pending, board=...) -> None:
    ctx.store._write_json(_path(ctx.store), {
        "lanes": lanes, "board": ctx.config.board if board is ... else board,
        "listing": listing, "confirmed": confirmed, "pending": sorted(pending)})
