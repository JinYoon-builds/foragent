# Messages for the person go through the PM

The person hears from FA only through their PM (planning) session. The orchestrator never pushes a
message to the person itself, not even with `fa notify`.

- A newly pending `needs-human` item is a decision: record it with `fa orch ask` (see
  [request box](request-box.md)). A live planning session reads it through `fa orch ask`;
  otherwise it waits in the FA inbox for the next planning session, which asks the person.
- An empty queue is a fact, not a question. Send no message about it: the planning session reads the
  same fact from `fa brief` and `fa status` when it starts.

On the initial snapshot, handle either condition if it already holds. Do not repeat unchanged
pending decisions; batch ordinary merge/lane completion updates until the queue empties or the next
useful recap.
