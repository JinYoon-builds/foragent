"""Locked: a board card exposes the current issue-owned wait."""
from dataclasses import replace
import json

from foragent import board
from foragent.scheduler import tick
from test_board_projects import board_snapshot
from test_orch_review_handoff import make_case, tick_context
from test_skills_autosync import isolated_home  # noqa: F401
from test_ssot_waiting_order import WAITING, REASON, CONDITION, ROLE


def text_fields_transport(tracker, monkeypatch):
    """Extend the existing fake only with GitHub's TEXT field transport capability.

    No waiting interpretation or production projection is supplied by the fake.
    Field names and the number of fields remain the implementer's choice.
    """
    original = tracker.graphql
    fields, values = {}, {}

    def graphql(query, variables):
        data = variables.get('input', {})
        if 'createProjectV2Field(' in query and data.get('dataType') == 'TEXT':
            field = {'id': f'TEXT_{len(fields)}', 'name': data['name'], 'dataType': 'TEXT'}
            fields[field['id']] = (data['projectId'], field)
            return {'data': {'createProjectV2Field': {'projectV2Field': dict(field)}}}
        if 'updateProjectV2ItemFieldValue(' in query and 'text' in data.get('value', {}):
            values[data['projectId'], data['itemId'], data['fieldId']] = data['value']['text']
            return {'data': {'updateProjectV2ItemFieldValue': {'projectV2Item': {'id': data['itemId']}}}}
        if 'clearProjectV2ItemFieldValue(' in query:
            values.pop((data['projectId'], data['itemId'], data['fieldId']), None)
            return {'data': {'clearProjectV2ItemFieldValue': {'projectV2Item': {'id': data['itemId']}}}}
        result = original(query, variables)
        node = result.get('data', {}).get('node')
        if node:
            project = variables.get('id')
            if 'fields' in node:
                node['fields']['nodes'].extend(dict(field) for owner, field in fields.values() if owner == project)
            for item in node.get('items', {}).get('nodes', []):
                for (owner, number, field_id), text in values.items():
                    if owner == project and number == item.get('id'):
                        item.setdefault('fieldValues', {'nodes': []})['nodes'].append(
                            {'text': text, 'field': {'name': fields[field_id][1]['name']}})
        return result

    monkeypatch.setattr(tracker, 'graphql', graphql)


def read_card(tracker, project):
    result = tracker.graphql("""query($id: ID!) {
      node(id: $id) { ... on ProjectV2 { items(first: 100) { nodes {
        id content { ... on Issue { number } }
        fieldValues(first: 100) { nodes {
          ... on ProjectV2ItemFieldTextValue { text field { ... on ProjectV2Field { name } } }
          ... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2SingleSelectField { name } } }
        } }
      } } } }
    }""", {'id': project})
    return next(item for item in result['data']['node']['items']['nodes'] if item['content']['number'] == 1)


def test_board_card_exposes_wait_and_refreshes_when_issue_changes(git_history, tmp_path, monkeypatch):
    """Reject a card that merely says Backlog or keeps its own stale waiting text."""
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command='true')
    case.store.finish_run(1, 'cancelled')
    case.tracker.remove_labels(1, ['running', 'approved'])
    case.tracker.update_issue(1, title=case.tracker.get_issue(1).title, body=case.tracker.get_issue(1).body + '\n' + WAITING)
    text_fields_transport(case.tracker, monkeypatch)
    project = board.ensure(case.tracker, 'owner/repo')
    case.ctx.config = replace(case.config, board=project)
    board.add_issue(case.tracker, 'owner/repo', project, 1)
    assert tick(tick_context(case, start_runs=False)).skipped is None
    card = read_card(case.tracker, project)
    text = json.dumps(card, ensure_ascii=False)
    for value in (REASON, CONDITION, ROLE):
        assert value in text, f'Board card omitted issue-owned wait: {value}'
    # A changed issue is the authority, even when the previous board fields survive.
    from datetime import timedelta
    case.clock[0] += timedelta(minutes=6)
    case.tracker.update_issue(1, title=case.tracker.get_issue(1).title, body=case.tracker.get_issue(1).body.replace(REASON, 'Locale choice recorded'))
    assert tick(tick_context(case, start_runs=False)).skipped is None
    card = read_card(case.tracker, project)
    assert 'Locale choice recorded' in json.dumps(card)
    assert REASON not in json.dumps(card)

    case.clock[0] += timedelta(minutes=6)
    case.tracker.update_issue(1, title=case.tracker.get_issue(1).title,
                             body=case.tracker.get_issue(1).body.split('\n## Waiting', 1)[0])
    assert tick(tick_context(case, start_runs=False)).skipped is None
    cleared = json.dumps(read_card(case.tracker, project))
    assert 'Locale choice recorded' not in cleared and CONDITION not in cleared and ROLE not in cleared
