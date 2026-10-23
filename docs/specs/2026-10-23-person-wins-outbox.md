# A person's GitHub change wins, and rate-limited writes wait in an outbox

D58: the person's current words win. #138 keeps a local snapshot that Foragent updates when it writes; a person's direct GitHub change must replace Foragent's local view at the next refresh, and Foragent must not re-apply its own earlier change.

github-outbox: when a GitHub write (labels, comment, close) by an orchestrator verb or the gate fails on a rate limit, Foragent keeps its local record, queues the write in a local outbox (`StateStore.github_outbox()`: issue, action in add_labels/remove_labels/comment/close_issue, labels or body, queued_at) and sends it in order on the first tick after the reset. If the issue changed on GitHub after the write was queued, that issue's queued writes are dropped (event `kind=outbox-dropped`) and the local row follows GitHub. Other write errors (HTTP 422) fail as today. Runner waits stay as #210/#219 define them.
