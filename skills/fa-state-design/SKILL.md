---
name: fa-state-design
description: "Apply when a change reads or writes state that more than one process, agent or service can touch: records, labels, files, caches, queues. Give each piece of state one writer, derive instead of store, append instead of overwrite, and state the writer of each piece of state in the plan."
---

# State design

Shared state goes wrong when nobody owns it. Two writers that each look correct on their own still produce lost updates, flapping values and records that disagree. Decide ownership before you write code.

## The three rules

1. **One writer per piece of state.** Name the single component that may change it. Every other component reads it, or asks the owner to change it. If two components must write the same value, the design is wrong: split the state, or move the decision to one owner.
2. **Derive instead of store.** If a value can be computed from facts you already keep, compute it when you need it. A stored copy of a derived value is a second piece of state that can drift from its source, and it needs its own writer.
3. **Append instead of overwrite.** Record events (what happened, when, by whom) and derive the current value from them. An append-only log keeps history, lets a reader see why a value changed, and does not lose a concurrent writer's update the way read-modify-write does.

## Common traps

- **Check-then-act windows.** Code reads a value, decides, then writes. Between the check and the act, another writer can change the value, so the decision is based on stale state (a TOCTOU race). Close the window: make the check and the act one atomic step (a lock held across both, a transaction, a compare-and-swap, an exclusive create), or make the owner the only one that acts.
- **Duplicated state.** The same fact kept in two places, for example a label on a remote tracker and a field in a local record, will disagree for some time after every change. Each copy costs consistency work. Choose one copy as the source of truth and name its writer. For the other copy, name which reader reads it and confirm that reader tolerates the lag. A reader that cannot accept the lag must read the source of truth.
- **Fallback writers.** A watchdog, recovery job or second process that takes over when the owner seems stuck becomes a second writer. A fallback writer acts only after the owner is confirmed dead (its process is gone, its lease expired, or it released ownership). "It has been quiet for a while" is not confirmation. If the fallback acts on a live owner, both write and the rules above break.
- **Writing to fix a symptom.** When two values disagree, adding a sync step that copies one onto the other adds a writer. Find which one is the source and remove the copy, or derive it.

## In a plan

Before implementing, the plan states the writer of each piece of state that the change creates or touches:

| State | Where it lives | Writer | Readers | Derived from / lag tolerated |
|---|---|---|---|---|

If a row has two writers, redesign before you code. If a row is stored but could be derived, derive it. If a row is overwritten in place by more than one step, consider an append-only record instead.

## Review checklist

- Does every piece of state in the change have exactly one named writer?
- Is any stored value derivable from other stored facts?
- Does any read-decide-write sequence leave a window for another writer?
- For every duplicated copy, which reader uses it, and does that reader tolerate the lag?
- Does any fallback or recovery path act before the owner is confirmed dead?
