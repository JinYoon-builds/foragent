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
            if name == "Status":
                names = STATUSES
            names = list(dict.fromkeys(names))
            field = fields.get(name)
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
                    # Only a board just created by us has unused defaults to replace.
                    # Reused boards may have cards using any of their existing options.
                    if not new_project:
                        options.extend(option for option in field["options"] if option["name"] not in names)
                else:
                    options = [*field["options"], *(_option(value) for value in missing)]
                if options != field["options"]:
                    # Keeping option IDs preserves values on existing items.
                    field = _data(self.tracker, """mutation ForagentUpdateBoardField($input: UpdateProjectV2FieldInput!) {
                      updateProjectV2Field(input: $input) { projectV2Field { """ + FIELD_SELECTION + """ } }
                    }""", {"input": {"fieldId": field["id"], "singleSelectOptions": options}})["updateProjectV2Field"]["projectV2Field"]
            fields[name] = field
        return fields

    def set_values(self, issue: Issue | int, values: dict[str, str]) -> None:
        number = issue.number if isinstance(issue, Issue) else issue
        # A card already set to every wanted value needs no board call.
        if values and (recorded := getattr(self.tracker, "board_value", None)) is not None and all(
            recorded(number, self.id, name) == value for name, value in values.items()
        ):
            return
        owner, name = self.repo.split("/", 1)
        content = _data(self.tracker, """query ForagentBoardIssue($owner: String!, $name: String!, $number: Int!) {
          repository(owner: $owner, name: $name) { issue(number: $number) { id } }
        }""", {"owner": owner, "name": name, "number": number})["repository"]["issue"]
        try:
            item = _data(self.tracker, """mutation ForagentAddBoardItem($input: AddProjectV2ItemByIdInput!) {
              addProjectV2ItemById(input: $input) { item { id } }
            }""", {"input": {"projectId": self.id, "contentId": content["id"]}})["addProjectV2ItemById"]["item"]
        except TrackerError as exc:
            if "Content already exists in this project" not in str(exc):
                raise
            # GitHub confirmed membership but did not return an item ID to update.
            return
        fields = self.ensure_fields({key: [value] for key, value in values.items()})
        for name, value in values.items():
            field = fields[name]
            option = next(option for option in field["options"] if option["name"] == value)
            _data(self.tracker, """mutation ForagentSetBoardValue($input: UpdateProjectV2ItemFieldValueInput!) {
              updateProjectV2ItemFieldValue(input: $input) { projectV2Item { id } }
            }""", {"input": {"projectId": self.id, "itemId": item["id"], "fieldId": field["id"],
                             "value": {"singleSelectOptionId": option["id"]}}})
            # At this boundary the issue, field and confirmed value are still known.
            if (record := getattr(self.tracker, "record_board_value", None)) is not None:
                record(number, self.id, name, value)


def ensure(tracker: Tracker, repo: str, lanes: Iterable[str] = (), project_id: str | None = None) -> str | None:
    """Find/create the owner's board and repair missing fields, without blocking init."""
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
        Project(tracker, repo, project_id).ensure_fields({
            "Lane": ["main", *lanes], "Size": ["s", "m", "l"], "Status": STATUSES,
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
    if status not in STATUSES:
        raise ValueError(f"unknown board status: {status}")
    if (project := _current.get()) is not None:
        project.set_values(issue, {"Status": status})


def sync_status(ctx: BoardContext, issue: Issue | int, status: str) -> None:
    if not ctx.config.board:
        return
    token = _current.set(Project(ctx.tracker, ctx.repo, ctx.config.board))
    try:
        set_status(issue, status)
    except Exception as exc:
        if isinstance(exc, TrackerError) and exc.is_rate_limit:
            raise
        log.warning("Could not set board Status to %s for issue %s", status,
                    getattr(issue, "number", issue), exc_info=True)
    finally:
        _current.reset(token)


def add_issue(tracker: Tracker, repo: str, project_id: str | None, issue: int,
              lane: str | None = None, size: str | None = None) -> None:
    if not project_id:
        return
    try:
        values = {name: value for name, value in (("Lane", lane), ("Size", size)) if value is not None}
        Project(tracker, repo, project_id).set_values(issue, values)
    except TrackerError:
        raise
    except Exception:
        log.warning("Could not add issue #%s to board", issue, exc_info=True)
