"""One-way Projects v2 visibility; issue labels remain authoritative."""

from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import dataclass
import logging
from typing import Protocol

from foragent.config import Config
from foragent.tracker import Issue, Tracker, TrackerError


log = logging.getLogger(__name__)
STATUSES = ("Approved", "Ready", "Running", "Blocked", "Needs human", "PR open", "Done")
DEFAULT_MAP = {"Backlog": "🗂 Backlog", "Up next": "🌰 Up next", "In progress": "🐿 In progress",
               "Your turn": "✋ Your turn", "Done": "📦 Done"}
COLUMNS = tuple(DEFAULT_MAP.values())
# Recorded as an issue's Status when the board had no card for it. The periodic
# board read (refresh_cards) replaces it, so a card added later is picked up.
NO_CARD = "no card"
STATE_GROUPS = {"unapproved": "Backlog", "Approved": "Up next", "Ready": "Up next",
                "Running": "In progress", "PR open": "In progress", "Blocked": "In progress",
                "Needs human": "Your turn", "Done": "Done", "merged": "Done", "closed": "Done"}


def board_column(state: str, board_map: dict[str, str] | None = None) -> str | None:
    """Translate gate state without changing it; a custom map is authoritative."""
    if state not in STATE_GROUPS:
        raise ValueError(f"unknown board status: {state}")
    return (DEFAULT_MAP if board_map is None else board_map).get(STATE_GROUPS[state])


def card_warning(state: str) -> bool:
    """Whether a card in the progress column needs the blocked warning (⚠).

    On the GitHub board the issue's existing ``blocked`` label is that ⚠: a
    Status card holds one option, so blocked cards stay in 🐿 In progress and
    no sixth option is added. Other views use this predicate to draw ⚠.
    """
    return state == "Blocked"


FIELD_SELECTION = """... on ProjectV2SingleSelectField {
  id name options { id name color description }
} ... on ProjectV2Field { id name } ... on ProjectV2IterationField { id name }"""


def _data(tracker: Tracker, query: str, variables: dict) -> dict:
    result = tracker.graphql(query, variables)
    if result.get("errors"):
        raise TrackerError(f"board GraphQL errors: {result['errors']}")
    return result.get("data", result)


def _option(name: str) -> dict:
    return {"name": name, "color": "GRAY", "description": ""}


@dataclass
class Project:
    tracker: Tracker
    repo: str
    id: str
    board_map: dict[str, str] | None = None

    def fields(self) -> dict[str, dict]:
        fields = {}
        cursor = None
        while True:
            node = _data(self.tracker, """query ForagentBoardFields($id: ID!, $cursor: String) {
              node(id: $id) { ... on ProjectV2 {
                fields(first: 100, after: $cursor) { nodes { """ + FIELD_SELECTION + """ }
                  pageInfo { hasNextPage endCursor }
                }
              } }
            }""", {"id": self.id, "cursor": cursor})["node"]
            if node is None:
                raise TrackerError(f"board {self.id} was deleted or is unavailable")
            connection = node["fields"]
            fields.update((field["name"], field) for field in connection["nodes"] if field)
            if not connection["pageInfo"]["hasNextPage"]:
                return fields
            cursor = connection["pageInfo"]["endCursor"]

    def ensure_fields(self, desired: dict[str, Iterable[str]], *, new_project: bool = False) -> dict[str, dict]:
        fields = self.fields()
        for name, names in desired.items():
            names = list(dict.fromkeys(names))
            field = fields.get(name)
            # Team columns and their metadata belong to the team. Only replace
            # GitHub's unused defaults on a project created in this call.
            if name == "Status" and not new_project:
                continue
            if field is None:
                field = _data(self.tracker, """mutation ForagentCreateBoardField($input: CreateProjectV2FieldInput!) {
                  createProjectV2Field(input: $input) { projectV2Field { """ + FIELD_SELECTION + """ } }
                }""", {"input": {"projectId": self.id, "name": name, "dataType": "SINGLE_SELECT",
                                 "singleSelectOptions": [_option(value) for value in names]}})["createProjectV2Field"]["projectV2Field"]
            elif "options" not in field:
                raise TrackerError(f"board field {name} must be single-select")
            else:
                existing = {option["name"]: option for option in field["options"]}
                missing = [value for value in names if value not in existing]
                if name == "Status":
                    options = [existing.get(value, _option(value)) for value in names]
                else:
                    options = [*field["options"], *(_option(value) for value in missing)]
                if options != field["options"]:
                    # Keeping option IDs preserves values on existing items.
                    field = _data(self.tracker, """mutation ForagentUpdateBoardField($input: UpdateProjectV2FieldInput!) {
                      updateProjectV2Field(input: $input) { projectV2Field { """ + FIELD_SELECTION + """ } }
                    }""", {"input": {"fieldId": field["id"], "singleSelectOptions": options}})["updateProjectV2Field"]["projectV2Field"]
            fields[name] = field
        return fields

    def set_values(self, issue: Issue | int, values: dict[str, str], *, initial_status: bool = False,
                   add: bool = True) -> None:
        number = issue.number if isinstance(issue, Issue) else issue
        recorded = getattr(self.tracker, "board_value", None)
        record = getattr(self.tracker, "record_board_value", None)
        # A card already set to every wanted value needs no board call.
        if values and recorded is not None and all(
            recorded(number, self.id, name) == value for name, value in values.items()
        ):
            return
        if not add and recorded is not None and recorded(number, self.id, "Status") == NO_CARD:
            return  # The last board read found no card for this issue.
        owner, name = self.repo.split("/", 1)
        fields = self.fields() if add else None
        if fields is not None and "Status" in values and not any(
            option["name"] == values["Status"] for option in fields.get("Status", {}).get("options", [])
        ):
            log.warning("Board column %s is unavailable; skipping issue #%s", values["Status"], number)
            return
        cursor = None
        item = None
        while True:
            content = _data(self.tracker, """query ForagentBoardIssue(
              $owner: String!, $name: String!, $number: Int!, $cursor: String
            ) {
              repository(owner: $owner, name: $name) { issue(number: $number) {
                id projectItems(first: 100, after: $cursor) {
                  nodes { id project { id } fieldValueByName(name: "Status") {
                    ... on ProjectV2ItemFieldSingleSelectValue { name optionId }
                  } } pageInfo { hasNextPage endCursor }
                }
              } }
            }""", {"owner": owner, "name": name, "number": number, "cursor": cursor})["repository"]["issue"]
            connection = content["projectItems"]
            item = next((node for node in connection["nodes"] if node["project"]["id"] == self.id), None)
            if item or not connection["pageInfo"]["hasNextPage"]:
                break
            cursor = connection["pageInfo"]["endCursor"]
        if item is None and not add:
            # Only move an existing card; never add one here. Remember the check.
            if record is not None:
                record(number, self.id, "Status", NO_CARD)
            return
        if fields is None:
            fields = self.fields()
            if not any(option["name"] == values["Status"]
                       for option in fields.get("Status", {}).get("options", [])):
                log.warning("Board column %s is unavailable; skipping issue #%s", values["Status"], number)
                return
        if item is None:
            try:
                item = _data(self.tracker, """mutation ForagentAddBoardItem($input: AddProjectV2ItemByIdInput!) {
                  addProjectV2ItemById(input: $input) { item { id } }
                }""", {"input": {"projectId": self.id, "contentId": content["id"]}})["addProjectV2ItemById"]["item"]
            except TrackerError as exc:
                if "Content already exists in this project" not in str(exc):
                    raise
                # Concurrent addition confirmed membership without an item ID.
                return
        other_fields = {key: [value] for key, value in values.items() if key != "Status"}
        if other_fields:
            fields = self.ensure_fields(other_fields)
        for name, value in values.items():
            field = fields[name]
            option = next(option for option in field["options"] if option["name"] == value)
            if name == "Status":
                current = item.get("fieldValueByName")
                if current and (initial_status or current["optionId"] == option["id"]):
                    # Record the confirmed card value so later ticks need no board call.
                    if (record := getattr(self.tracker, "record_board_value", None)) is not None:
                        record(number, self.id, name, current["name"])
                    continue
            _data(self.tracker, """mutation ForagentSetBoardValue($input: UpdateProjectV2ItemFieldValueInput!) {
              updateProjectV2ItemFieldValue(input: $input) { projectV2Item { id } }
            }""", {"input": {"projectId": self.id, "itemId": item["id"], "fieldId": field["id"],
                             "value": {"singleSelectOptionId": option["id"]}}})
            # At this boundary the issue, field and confirmed value are still known.
            if (record := getattr(self.tracker, "record_board_value", None)) is not None:
                record(number, self.id, name, value)


def ensure(tracker: Tracker, repo: str, lanes: Iterable[str] = (), project_id: str | None = None,
           *, created: set[str] | None = None) -> str | None:
    """Find/create the owner's board and repair missing fields, without blocking init.

    A board created by this call is added to ``created`` when given.
    """
    new_project = False
    try:
        if not project_id:
            owner, name = repo.split("/", 1)
            repository = _data(tracker, """query ForagentBoardOwner($owner: String!, $name: String!) {
              repository(owner: $owner, name: $name) { id owner { id } }
            }""", {"owner": owner, "name": name})["repository"]
            owner_id = repository["owner"]["id"]
            title = f"Foragent: {name}"
            cursor = None
            while True:
                connection = _data(tracker, """query ForagentFindBoard($id: ID!, $cursor: String) {
                  node(id: $id) {
                    ... on User { projectsV2(first: 100, after: $cursor) {
                      nodes { id title } pageInfo { hasNextPage endCursor }
                    } }
                    ... on Organization { projectsV2(first: 100, after: $cursor) {
                      nodes { id title } pageInfo { hasNextPage endCursor }
                    } }
                  }
                }""", {"id": owner_id, "cursor": cursor})["node"]["projectsV2"]
                project_id = next((node["id"] for node in connection["nodes"] if node["title"] == title), None)
                if project_id or not connection["pageInfo"]["hasNextPage"]:
                    break
                cursor = connection["pageInfo"]["endCursor"]
            if not project_id:
                project_id = _data(tracker, """mutation ForagentCreateBoard($input: CreateProjectV2Input!) {
                  createProjectV2(input: $input) { projectV2 { id } }
                }""", {"input": {"ownerId": owner_id, "repositoryId": repository["id"],
                                 "title": title}})["createProjectV2"]["projectV2"]["id"]
                new_project = True
                if created is not None:
                    created.add(project_id)
        Project(tracker, repo, project_id).ensure_fields({
            "Status": COLUMNS, "Lane": ["main", *lanes], "Size": ["s", "m", "l"],
        }, new_project=new_project)
    except Exception:
        log.warning("Could not configure board for %s", repo, exc_info=True)
    # Persist a newly created ID even if a field call failed; init can repair it.
    return project_id


class BoardContext(Protocol):
    tracker: Tracker
    config: Config
    repo: str


@dataclass(frozen=True)
class ProjectContext:
    tracker: Tracker
    repo: str
    config: Config


_current: ContextVar[Project | None] = ContextVar("foragent_board", default=None)


def set_status(issue: Issue | int, status: str) -> None:
    """Set Status within the repository context established by sync_status."""
    if (project := _current.get()) is not None:
        column = board_column(status, project.board_map)
        if column is None:
            log.warning("Board map is missing group %s; skipping issue #%s", STATE_GROUPS[status],
                        getattr(issue, "number", issue))
            return
        project.set_values(issue, {"Status": column})
    else:
        board_column(status)  # Validate even when there is no repository context.


def _order_parent(ctx: BoardContext, number: int) -> bool:
    """Whether the last listing shows ``number`` as an open order parent."""
    from foragent.board_parents import order_parents

    store = getattr(ctx, "store", None)
    if store is None:
        return False
    return number in order_parents([row for row in store.local_issues() if row["number"] == number])


_FINAL = frozenset({"Done", "merged", "closed"})


def _waits_on_person(ctx: BoardContext, issue: Issue | int) -> bool:
    """Whether a person holds the issue: needs-human, waiting-person or an open question."""
    from foragent import orch_inbox
    from foragent.labels import NEEDS_HUMAN

    number = getattr(issue, "number", issue)
    store = getattr(ctx, "store", None)
    labels = getattr(issue, "labels", None)
    if labels is None and store is not None:
        labels = next((row["labels"] for row in store.local_issues() if row["number"] == number), ())
    # "waiting-person" is written only by fa orch park/unpark; switch to its constant once on main.
    if {NEEDS_HUMAN, "waiting-person"} & set(labels or ()):
        return True
    if store is None:
        return False
    try:
        return number in orch_inbox.waiting_issues(store)
    except Exception:  # An unreadable question box must not stop the board sync.
        log.warning("Could not read open questions for issue #%s", number, exc_info=True)
        return False


def sync_status(ctx: BoardContext, issue: Issue | int, status: str, *, existing_only: bool = False,
                parent: bool = False) -> None:
    """Move the issue's card to the column for ``status`` when the column differs.

    An open order parent's column follows its children: only the children
    aggregation (``parent=True``) writes it, so work-state handlers such as
    recovery, escalation or a flag leave its card alone.

    An open issue that waits on a person (``_waits_on_person``) goes to the
    Your turn group whatever work state the caller passes.

    ``existing_only`` moves a card that is already on the board, recorded
    locally or not, and never adds one. A tracker that records board values is
    required. An issue the last board read found without a card is skipped
    until refresh_cards reads the board again.
    """
    if not ctx.config.board:
        return
    if existing_only and getattr(ctx.tracker, "board_value", None) is None:
        return
    if not parent and _order_parent(ctx, getattr(issue, "number", issue)):
        return
    if not parent and status not in _FINAL and _waits_on_person(ctx, issue):
        status = "Needs human"
    token = _current.set(Project(ctx.tracker, ctx.repo, ctx.config.board, ctx.config.board_map))
    try:
        if not existing_only:
            set_status(issue, status)
        elif (column := board_column(status, ctx.config.board_map)) is not None:
            _current.get().set_values(issue, {"Status": column}, add=False)
    except Exception as exc:
        if isinstance(exc, TrackerError) and exc.is_rate_limit:
            raise
        log.warning("Could not set board Status to %s for issue %s", status,
                    getattr(issue, "number", issue), exc_info=True)
    finally:
        _current.reset(token)


def refresh_cards(ctx: BoardContext, tracker: Tracker | None = None) -> None:
    """Record every issue's card Status from one paged read of the whole board.

    Issues with no card on the board get NO_CARD. Run with the periodic issue
    refresh, this picks up cards that people added or moved on the board.
    """
    tracker = tracker or ctx.tracker
    record = getattr(tracker, "record_board_value", None)
    recorded = getattr(tracker, "board_value", None)
    if not ctx.config.board or record is None or recorded is None:
        return
    cards: dict[int, str] = {}
    cursor = None
    try:
        while True:
            node = _data(tracker, """query ForagentBoardCards($id: ID!, $cursor: String) {
              node(id: $id) { ... on ProjectV2 { items(first: 100, after: $cursor) {
                nodes { content { ... on Issue { number repository { nameWithOwner } } }
                  fieldValueByName(name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } } }
                pageInfo { hasNextPage endCursor }
              } } }
            }""", {"id": ctx.config.board, "cursor": cursor})["node"]
            if node is None:
                return
            connection = node["items"]
            for item in connection["nodes"]:
                content = item.get("content") or {}
                repository = (content.get("repository") or {}).get("nameWithOwner", "")
                if content.get("number") is not None and repository.lower() == ctx.repo.lower():
                    cards[content["number"]] = (item.get("fieldValueByName") or {}).get("name") or ""
            if not connection["pageInfo"]["hasNextPage"]:
                break
            cursor = connection["pageInfo"]["endCursor"]
    except Exception as exc:
        if isinstance(exc, TrackerError) and exc.is_rate_limit:
            raise
        log.warning("Could not read board cards for %s", ctx.repo, exc_info=True)
        return
    for row in ctx.store.local_issues():
        value = cards.get(row["number"], NO_CARD)
        if recorded(row["number"], ctx.config.board, "Status") != value:
            record(row["number"], ctx.config.board, "Status", value)


def add_issue(tracker: Tracker, repo: str, project_id: str | None, issue: int,
              lane: str | None = None, size: str | None = None,
              board_map: dict[str, str] | None = None) -> None:
    if not project_id:
        return
    try:
        values = {name: value for name, value in (("Lane", lane), ("Size", size)) if value is not None}
        column = board_column("unapproved", board_map)
        if column is None:
            log.warning("Board map is missing group Backlog; skipping issue #%s", issue)
            return
        values["Status"] = column
        Project(tracker, repo, project_id, board_map).set_values(issue, values, initial_status=True)
    except TrackerError:
        raise
    except Exception:
        log.warning("Could not add issue #%s to board", issue, exc_info=True)
