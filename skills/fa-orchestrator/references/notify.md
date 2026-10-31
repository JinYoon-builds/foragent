# Notification commands and timing

For newly pending `needs-human` items, send a push through the configured notifier:
`fa notify --kind question --title "Decision needed" --line "<question and recommendation>" --link "<issue URL>"`.
When the queue becomes empty, send
`fa notify --kind status --title "Queue empty" --line "<done count; items left to inspect>" --link "<project URL>"`.

On the initial snapshot, notify if either condition already holds. Avoid duplicate pushes for
unchanged pending decisions; batch ordinary merge/lane completion updates until the queue empties or
the next useful recap. The notifier owns delivery.

`--kind` names what the alert is: `question`, `merge` or `order` for a person's decision, and
`blocked`, `orchestrator-stopped`, `silence`, `summary` or `status` for facts. A person with
`notify_filter: decisions` gets pushes only for the first three; the rest reach the planning
session's context as notices. Pick the kind from what you are sending, not from its words.
