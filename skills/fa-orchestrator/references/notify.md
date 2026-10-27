# Notification timing

On the initial snapshot, notify if either condition (a newly pending decision or an empty queue)
already holds. Avoid duplicate pushes for unchanged pending decisions; batch ordinary merge/lane
completion updates until the queue empties or the next useful recap. The notifier owns delivery.
