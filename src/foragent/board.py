"""One-way Projects v2 visibility. Issue labels remain the source of truth."""

from contextvars import ContextVar
import logging

from foragent.config import Config
from foragent.labels import issue_size
from foragent.tracker import Issue, Tracker, TrackerError


log = logging.getLogger(__name__)
STATUSES = ("Approved", "Ready", "Running", "Blocked", "Needs human", "PR open", "Done")
FIELD_FRAGMENT = """... on ProjectV2SingleSelectField {
    id name options { id name color description }
}"""


class Board:
    def __init__(self, tracker: Tracker, repo: str, config: Config):
        self.tracker = tracker
        self.owner, self.name = repo.split("/", 1)
        self.config = config
        self.id = config.board or None

    def _graphql(self, query: str, variables: dict) -> dict:
        result = self.tracker.graphql(query, variables)
        if result.get("errors"):
            raise TrackerError(f"board GraphQL failed: {result['errors']}")
        return result.get("data", result)

    def ensure(self) -> str:
        """Find the owner's board, or create it; repair missing fields/options."""
        if self.id is None:
            title = f"Foragent: {self.name}"
            after = None
            while True:
                repository = self._graphql("""query($owner: String!, $name: String!, $after: String) {
                  repository(owner: $owner, name: $name) {
                    id owner { id ... on ProjectV2Owner {
                      projectsV2(first: 100, after: $after) {
                        nodes { id title } pageInfo { hasNextPage endCursor }
                      }
                    } }
                  }
                }""", {"owner": self.owner, "name": self.name, "after": after})["repository"]
                owner = repository["owner"]
                projects = owner["projectsV2"]
                self.id = next((p["id"] for p in projects["nodes"] if p["title"] == title), None)
                if self.id or not projects["pageInfo"]["hasNextPage"]:
                    break
                after = projects["pageInfo"]["endCursor"]
            if self.id is None:
                self.id = self._graphql("""mutation($input: CreateProjectV2Input!) {
                  createProjectV2(input: $input) { projectV2 { id } }
                }""", {"input": {"ownerId": owner["id"], "repositoryId": repository["id"],
                                 "title": title}})["createProjectV2"]["projectV2"]["id"]
        fields = self._fields()
        for name, options in (("Lane", [*sorted(self.config.lanes), "main"]),
                              ("Size", ["s", "m", "l"]), ("Status", STATUSES)):
            self._ensure_field(fields, name, options, exact=name in {"Size", "Status"})
        return self.id

    def _fields(self) -> dict:
        fields = {}
        after = None
        while True:
            project = self._graphql("""query($id: ID!, $after: String) {
              node(id: $id) { ... on ProjectV2 {
                fields(first: 100, after: $after) { nodes { """ + FIELD_FRAGMENT + """ }
                  pageInfo { hasNextPage endCursor }
                }
              } }
            }""", {"id": self.id, "after": after})["node"]
            if project is None:
                raise TrackerError(f"board {self.id} is unavailable")
            connection = project["fields"]
            fields.update((field["name"], field) for field in connection["nodes"] if field)
            if not connection["pageInfo"]["hasNextPage"]:
                return fields
            after = connection["pageInfo"]["endCursor"]

    def _ensure_field(self, fields: dict, name: str, names, *, exact: bool = False) -> dict:
        field = fields.get(name)
        options = list(field["options"]) if field else []
        existing = {option["name"] for option in options}
        missing = list(dict.fromkeys(value for value in names if value not in existing))
        # Existing IDs preserve values already assigned to project items.
        options.extend({"name": value, "color": "GRAY", "description": ""} for value in missing)
        if exact:
            by_name = {option["name"]: option for option in options}
            options = [by_name[value] for value in dict.fromkeys(names)]
        if field and options == field["options"]:
            return field
        if field:
            operation, input_type = "updateProjectV2Field", "UpdateProjectV2FieldInput"
            inputs = {"fieldId": field["id"], "singleSelectOptions": options}
        else:
            operation, input_type = "createProjectV2Field", "CreateProjectV2FieldInput"
            inputs = {"projectId": self.id, "name": name, "dataType": "SINGLE_SELECT",
                      "singleSelectOptions": options}
        field = self._graphql(
            f"mutation($input: {input_type}!) {{ {operation}(input: $input) {{ "
            f"projectV2Field {{ {FIELD_FRAGMENT} }} }} }}", {"input": inputs},
        )[operation]["projectV2Field"]
        fields[name] = field
        return field

    def _item(self, number: int) -> str:
        issue = self._graphql("""query($owner: String!, $name: String!, $number: Int!) {
          repository(owner: $owner, name: $name) { issue(number: $number) { id } }
        }""", {"owner": self.owner, "name": self.name, "number": number})["repository"]["issue"]
        # GitHub returns the existing item when the issue is already on the board.
        return self._graphql("""mutation($input: AddProjectV2ItemByIdInput!) {
          addProjectV2ItemById(input: $input) { item { id } }
        }""", {"input": {"projectId": self.id, "contentId": issue["id"]}})["addProjectV2ItemById"]["item"]["id"]

    def _set_value(self, item: str, field: dict, value: str) -> None:
        option = next(option for option in field["options"] if option["name"] == value)
        self._graphql("""mutation($input: UpdateProjectV2ItemFieldValueInput!) {
          updateProjectV2ItemFieldValue(input: $input) { projectV2Item { id } }
        }""", {"input": {"projectId": self.id, "itemId": item, "fieldId": field["id"],
                         "value": {"singleSelectOptionId": option["id"]}}})

    def add_issue(self, issue: Issue) -> None:
        fields = self._fields()
        lane = next(iter(sorted(label[5:] for label in issue.labels if label.startswith("lane:"))), "main")
        self._ensure_field(fields, "Lane", [lane])
        self._ensure_field(fields, "Size", ["s", "m", "l"])
        item = self._item(issue.number)
        self._set_value(item, fields["Lane"], lane)
        self._set_value(item, fields["Size"], issue_size(issue.labels))

    def set_status(self, issue: Issue | int, status: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown board status: {status}")
        fields = self._fields()
        field = self._ensure_field(fields, "Status", STATUSES)
        self._set_value(self._item(getattr(issue, "number", issue)), field, status)


_current: ContextVar[Board | None] = ContextVar("foragent_board", default=None)


def set_status(issue: Issue | int, status: str) -> None:
    """Set a status using the calling scheduler/runner's repository connection."""
    connection = _current.get()
    if connection is not None:
        connection.set_status(issue, status)


def sync_status(ctx, issue: Issue | int, status: str) -> None:
    """Isolate even a replaced set_status implementation from tick/run failures."""
    update_status(ctx.config, ctx.tracker, ctx.repo, issue, status)


def update_status(config: Config, tracker: Tracker, repo: str, issue: Issue | int, status: str) -> None:
    if not config.board:
        return
    token = _current.set(Board(tracker, repo, config))
    try:
        set_status(issue, status)
    except Exception:
        log.warning("Board status update failed for #%s (%s)", getattr(issue, "number", issue), status,
                    exc_info=True)
    finally:
        _current.reset(token)


def add_issue(config: Config, tracker: Tracker, repo: str, issue: Issue) -> None:
    if not config.board:
        return
    try:
        Board(tracker, repo, config).add_issue(issue)
    except Exception:
        log.warning("Board item update failed for #%s", issue.number, exc_info=True)
