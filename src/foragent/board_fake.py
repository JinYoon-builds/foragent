"""In-memory Projects v2 operations for FakeTracker's GraphQL transport."""

from copy import deepcopy
import re

from foragent.tracker import TrackerError


class FakeProjects:
    def __init__(self):
        self.projects = {}
        self._sequence = 0

    def _id(self, kind):
        self._sequence += 1
        return f"{kind}_{self._sequence}"

    def _field(self, name, options):
        return {"id": self._id("PVTSSF"), "name": name, "options": [
            {**option, "id": option.get("id") or self._id("option")} for option in options
        ]}

    @staticmethod
    def _connection(nodes, cursor=None):
        start = int(cursor or 0)
        end = start + 100
        return {"nodes": nodes[start:end], "pageInfo": {
            "hasNextPage": end < len(nodes), "endCursor": str(end) if end < len(nodes) else None,
        }}

    def graphql(self, query, variables, tracker):
        operation = re.search(r"\b(?:query|mutation)\s+(\w+)", query)
        operation = operation[1] if operation else None
        data = variables.get("input", {})
        if operation == "ForagentBoardOwner":
            repo = f"{variables['owner']}/{variables['name']}"
            result = {"repository": {"id": f"R_{repo}", "owner": {"id": f"O_{variables['owner']}"}}}
        elif operation == "ForagentFindBoard":
            nodes = [{"id": p["id"], "title": p["title"]} for p in self.projects.values()
                     if p["ownerId"] == variables["id"]]
            result = {"node": {"projectsV2": self._connection(nodes, variables.get("cursor"))}}
        elif operation == "ForagentCreateBoard":
            project_id = self._id("PVT")
            # GitHub creates a Status field by default. Exercise extending it.
            field = self._field("Status", [{"name": name, "color": "GRAY", "description": ""}
                                           for name in ("Todo", "In Progress", "Done")])
            self.projects[project_id] = {**data, "id": project_id, "fields": {field["id"]: field}, "items": {}}
            result = {"createProjectV2": {"projectV2": {"id": project_id}}}
        elif operation == "ForagentCreateBoardField":
            project = self.projects[data["projectId"]]
            if any(field["name"] == data["name"] for field in project["fields"].values()):
                raise TrackerError(f"duplicate board field: {data['name']}")
            field = self._field(data["name"], data["singleSelectOptions"])
            project["fields"][field["id"]] = field
            result = {"createProjectV2Field": {"projectV2Field": field}}
        elif operation == "ForagentUpdateBoardField":
            field = next(field for p in self.projects.values() for field in p["fields"].values()
                         if field["id"] == data["fieldId"])
            field["options"] = self._field(field["name"], data["singleSelectOptions"])["options"]
            valid_ids = {option["id"] for option in field["options"]}
            for project in self.projects.values():
                for item in project["items"].values():
                    if field["id"] in item["values"] and item["values"][field["id"]] not in valid_ids:
                        del item["values"][field["id"]]
            result = {"updateProjectV2Field": {"projectV2Field": field}}
        elif operation == "ForagentBoardIssue":
            number = variables["number"]
            tracker.get_issue(number)
            result = {"repository": {"issue": {"id": f"I_{variables['owner']}/{variables['name']}#{number}"}}}
        elif operation == "ForagentAddBoardItem":
            project = self.projects[data["projectId"]]
            item = next((item for item in project["items"].values() if item["contentId"] == data["contentId"]), None)
            if item is None:
                item = {"id": self._id("PVTI"), "contentId": data["contentId"], "values": {}}
                project["items"][item["id"]] = item
            result = {"addProjectV2ItemById": {"item": {"id": item["id"]}}}
        elif operation == "ForagentSetBoardValue":
            project = self.projects[data["projectId"]]
            item = project["items"][data["itemId"]]
            field = project["fields"][data["fieldId"]]
            option_id = data["value"]["singleSelectOptionId"]
            if not any(option["id"] == option_id for option in field["options"]):
                raise TrackerError(f"unknown board option: {option_id}")
            item["values"][field["id"]] = option_id
            result = {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": item["id"]}}}
        elif "node" in query and "ProjectV2" in query:
            project = self.projects.get(variables["id"])
            node = None
            if project:
                items = []
                for item in project["items"].values():
                    values = []
                    for field_id, option_id in item["values"].items():
                        field = project["fields"][field_id]
                        option = next(option for option in field["options"] if option["id"] == option_id)
                        values.append({"name": option["name"], "field": {"name": field["name"]}})
                    items.append({"id": item["id"], "content": {"number": int(item["contentId"].rsplit("#", 1)[1])},
                                  "fieldValues": self._connection(values)})
                node = {"id": project["id"], "title": project["title"],
                        "fields": self._connection(list(project["fields"].values()), variables.get("cursor")),
                        "items": self._connection(items)}
            result = {"node": node}
        else:
            raise TrackerError(f"unsupported fake GraphQL operation: {operation}")
        return deepcopy({"data": result})
