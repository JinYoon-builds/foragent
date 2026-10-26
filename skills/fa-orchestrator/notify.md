# Notifications

For newly pending `needs-human` items, send a push through the configured notifier:
`fa notify --title "Decision needed" --line "<question and recommendation>" --link "<issue URL>"`.
When the queue becomes empty, send
`fa notify --title "Queue empty" --line "<done count; items left to inspect>" --link "<project URL>"`.
On the initial snapshot, notify if either condition already holds. Avoid duplicate pushes for
unchanged pending decisions; batch ordinary merge/lane completion updates until the queue empties or
the next useful recap. The notifier owns delivery.
