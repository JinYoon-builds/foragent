"""One-way Projects v2 visibility. Issue labels remain the source of truth."""

from collections.abc import Iterable
from contextvars import ContextVar
import logging

from foragent.config import Config
from foragent.tracker import Issue, Tracker, TrackerError


log = logging.getLogger(__name__)
STATUSES = ("Approved", "Ready", "Running", "Blocked", "Needs human", "PR open", "Done")
_FIELD = """
  ... on ProjectV2SingleSelectField { id name options { id name color description } }
  ... on ProjectV2Field { id name }
  ... on ProjectV2IterationField { id name }
"""


def _data(tracker: Tracker, query: str, variables: dict) -> dict:
    result = tracker.graphql(query, variables)
    if result.get("errors"):
        raise TrackerError(f"Projects v2: {result['errors']}")
    data = result.get("data", result)
    if not isinstance(data, dict):
        raise TrackerError("Projects v2 returned no data")
    return data


class Project:
    """A project connection scoped to one operation, without process-global caches."""

    def __init__(self, tracker: Tracker, repo: str, project_id: str):
        self.tracker, self.repo, self.id = tracker, repo, project_id
        self._fields: dict | None = None

    def fields(self) -> dict:
        if self._fields is None:
            fields, cursor = {}, None
            while True:
                node = _data(self.tracker, """query($id: ID!, $after: String) {
                  node(id: $id) { ... on ProjectV2 {
                    fields(first: 100, after: $after) { nodes { %s }
                      pageInfo { hasNextPage endCursor }
                    }
                  } }
                }""" % _FIELD, {"id": self.id, "after": cursor})["node"]
                if node is None:
                    raise TrackerError(f"Projects v2 board {self.id} is unavailable")
                connection = node["fields"]
                fields.update((field["name"], field) for field in connection["nodes"] if field)
                if not connection["pageInfo"]["hasNextPage"]:
                    break
                cursor = connection["pageInfo"]["endCursor"]
            self._fields = fields
        return self._fields

    def ensure_field(self, name: str, names: Iterable[str], *, extend: bool = False) -> dict:
        fields = self.fields()
        field = fields.get(name)
        if field is not None and "options" not in field:
            raise TrackerError(f"Projects v2 field {name} must be single-select")
        existing = field["options"] if field else []
        names = list(dict.fromkeys([*(option["name"] for option in existing if extend), *names]))
        if field and [option["name"] for option in existing] == names:
            return field
        by_name = {option["name"]: option for option in existing}
        # Preserve option IDs; replacing them clears the values of existing items.
        options = [by_name.get(name, {"name": name, "color": "GRAY", "description": ""})
                   for name in names]
        if field is None:
            query = """mutation($input: CreateProjectV2FieldInput!) {
              createProjectV2Field(input: $input) { projectV2Field { %s } }
            }""" % _FIELD
            values = {"projectId": self.id, "name": name, "dataType": "SINGLE_SELECT",
                      "singleSelectOptions": options}
            operation = "createProjectV2Field"
        else:
            query = """mutation($input: UpdateProjectV2FieldInput!) {
              updateProjectV2Field(input: $input) { projectV2Field { %s } }
            }""" % _FIELD
            values = {"fieldId": field["id"], "singleSelectOptions": options}
            operation = "updateProjectV2Field"
        field = _data(self.tracker, query, {"input": values})[operation]["projectV2Field"]
        fields[name] = field
        return field

    def ensure_fields(self, lanes: Iterable[str]) -> None:
        self.ensure_field("Lane", ["main", *sorted(lanes)], extend=True)
        self.ensure_field("Size", ["s", "m", "l"])
        self.ensure_field("Status", STATUSES)

    def item(self, issue: Issue | int) -> str:
        number = issue.number if isinstance(issue, Issue) else issue
        owner, name = self.repo.split("/", 1)
        content = _data(self.tracker, """query($owner: String!, $name: String!, $number: Int!) {
          repository(owner: $owner, name: $name) { issue(number: $number) { id } }
        }""", {"owner": owner, "name": name, "number": number})["repository"]["issue"]
        if content is None:
            raise TrackerError(f"Projects v2 cannot find issue #{number}")
        # GitHub returns the existing item when the content is already in the project.
        return _data(self.tracker, """mutation($input: AddProjectV2ItemByIdInput!) {
          addProjectV2ItemById(input: $input) { item { id } }
        }""", {"input": {"projectId": self.id, "contentId": content["id"]}})["addProjectV2ItemById"]["item"]["id"]

    def set_value(self, item: str, field: dict, value: str) -> None:
        option = next(option for option in field["options"] if option["name"] == value)
        _data(self.tracker, """mutation($input: UpdateProjectV2ItemFieldValueInput!) {
          updateProjectV2ItemFieldValue(input: $input) { projectV2Item { id } }
        }""", {"input": {"projectId": self.id, "itemId": item, "fieldId": field["id"],
                        "value": {"singleSelectOptionId": option["id"]}}})

    def add_issue(self, issue: Issue | int, lane: str, size: str) -> None:
        item = self.item(issue)
        self.set_value(item, self.ensure_field("Lane", [lane], extend=True), lane)
        self.set_value(item, self.ensure_field("Size", ["s", "m", "l"]), size)

    def set_status(self, issue: Issue | int, status: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown board status: {status}")
        self.set_value(self.item(issue), self.ensure_field("Status", STATUSES), status)


def ensure_board(tracker: Tracker, repo: str, config: Config) -> str | None:
    """Find/create the owner's board and repair its fields; setup is best effort."""
    project_id = config.board
    try:
        if not project_id:
            owner, name = repo.split("/", 1)
            title, cursor = f"Foragent: {name}", None
            while True:
                connection = """projectsV2(first: 100, after: $after) {
                  nodes { id title } pageInfo { hasNextPage endCursor }
                }"""
                result = _data(tracker, """query($owner: String!, $after: String) {
                  repositoryOwner(login: $owner) { id
                    ... on User { %s } ... on Organization { %s }
                  }
                }""" % (connection, connection), {"owner": owner, "after": cursor})["repositoryOwner"]
                if result is None:
                    raise TrackerError(f"Projects v2 owner {owner} is unavailable")
                projects = result["projectsV2"]
                project_id = next((project["id"] for project in projects["nodes"]
                                   if project["title"] == title), None)
                if project_id or not projects["pageInfo"]["hasNextPage"]:
                    break
                cursor = projects["pageInfo"]["endCursor"]
            if not project_id:
                project_id = _data(tracker, """mutation($input: CreateProjectV2Input!) {
                  createProjectV2(input: $input) { projectV2 { id } }
                }""", {"input": {"ownerId": result["id"], "title": title}})["createProjectV2"]["projectV2"]["id"]
        Project(tracker, repo, project_id).ensure_fields(config.lanes)
    except Exception as exc:
        log.warning("Board setup failed for %s: %s", repo, exc)
    # Keep the id even if field setup failed so the next init can repair the same board.
    return project_id


def add_issue(config: Config, tracker: Tracker, repo: str, issue: Issue | int, lane: str, size: str) -> None:
    if config.board:
        try:
            Project(tracker, repo, config.board).add_issue(issue, lane, size)
        except Exception as exc:
            log.warning("Board item update failed for %s #%s: %s", repo, getattr(issue, "number", issue), exc)


_current: ContextVar[Project | None] = ContextVar("foragent_board", default=None)


def set_status(issue: Issue | int, status: str) -> None:
    """Set Status on the project bound by sync_status; never read state from it."""
    if (project := _current.get()) is not None:
        project.set_status(issue, status)


def sync_status(ctx, issue: Issue | int, status: str) -> None:
    """Bind a tick/run's project and isolate all board failures at the call boundary."""
    if not ctx.config.board:
        return
    token = _current.set(Project(ctx.tracker, ctx.repo, ctx.config.board))
    try:
        set_status(issue, status)
    except Exception as exc:
        log.warning("Board status update failed for %s #%s (%s): %s",
                    ctx.repo, getattr(issue, "number", issue), status, exc)
    finally:
        _current.reset(token)
