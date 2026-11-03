# Delete the unread push outbox (#1650)

Decision: Jin, 2026-11-02. The push outbox goes. Messages to the person go through the
PM (planning) session.

## Facts on main (2011d973)

- One writer: `deliver` in `src/foragent/notify.py`. When `notify` is unset or
  `claude-push`, it appends `{title, line, link}` to `<state>/push-outbox.jsonl`.
- No reader. Nothing in `src/`, `skills/`, hooks or scripts opens that file. Only tests
  read it, as a stand-in for "the alert was sent". The planning session never sees these
  alerts. It sees only the notices in the planning journal (the prompt hook shows the
  last 24 hours).
- The person's own config still says `notify: claude-push`
  (`~/.config/foragent/config.yml`). So that value must keep working. If it became an
  unknown channel, every alert would fail and retry.

## Behaviour after the change

1. When `notify` is unset (the default) or set to the older value `claude-push`, an alert
   goes to the planning session. It becomes a notice that the planning session's prompt
   shows. The notice keeps the title, the line and the link. No outbox file is written.
2. `notify_filter: decisions` works as before. A decision kind goes to the planning
   session as an alert for the person. Any other kind stays a "not pushed" notice.
3. Delivery is acknowledged only when the notice was written. If there is no repository,
   or the journal cannot be written, `send` returns False and `fa notify` exits non-zero
   with the reason.
4. Nothing changes for `none`, `macos`, `command:`, `slack:` and `ntfy:`. They deliver,
   fail, log and keep failure notices exactly as before.
5. Settings drop `claude-push` from the fixed choices. The description names "unset"
   as the planning-session route instead. `notify` stays read-only in the settings
   window. Files that already say `claude-push` still load.

## Locked tests whose expectations change (names kept)

These tests used the outbox file as their record of "sent". After the change, that
record is the planning session's notice. Each test keeps its name and its body. Only the
helper that reads "what was sent", or the one line that pinned the outbox file, changes.

- `tests/acceptance/test_view_notify.py`: adds a `relayed(store)` helper that reads the
  alerts relayed to the person. `test_default_and_cli_append_push_outbox_and_none_is_silent`
  and `test_delivery_failure_is_logged_without_escaping_and_inspect_uses_default_outbox`
  read through it. They also assert that no outbox exists. The link now travels inside
  the relayed line.
- `tests/acceptance/test_notify_personal_decisions.py`: `records(repo)` reads through
  `relayed`. The forced write failure now makes the planning journal unwritable, because
  the outbox file no longer exists.
- `tests/acceptance/test_notify_only_actions.py`: `notification_case` returns the delivery
  store, and `pushes(store)` reads through `relayed`.
- `tests/acceptance/test_star_prompt.py`: the no-star check reads the relayed alert
  instead of the outbox text.

Unchanged: `test_tick_failure_alert.py` (it captures `send` and only names `claude-push`
in config) and `test_flow_record_decisions.py` (it reads the outbox only if it exists).

## New lock

`tests/acceptance/test_notify_without_outbox.py` checks the result through `fa notify`,
the planning session's prompt hook and the files on disk only:

- `test_default_route_alerts_reach_planning_session_without_outbox[default|older-claude-push]`
- `test_decision_alert_under_decisions_filter_reaches_planning_session_without_outbox`

Each alert must show up exactly once in the planning prompt, with its link. No file or
folder named like an outbox may exist under the isolated home, the state folder or the
project.

## Unit tests the lane updates (not locked)

`tests/test_notify.py` (6 tests) and `tests/test_cli.py::test_notify_cli_resolves_repo_without_github_access`
read the outbox. Keep their names and read the planning journal instead.
`prototype.patch` shows one way to do this.

## Not changed

- The dated specs under `docs/specs/` and ledger D36 in `docs/decisions.md` are history.
  They stay as they are.
- `.foragent.yml` has a comment that names `claude-push`. It is a dated note, so it stays.
