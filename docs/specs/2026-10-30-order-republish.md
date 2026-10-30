# Order re-publishing

Re-publishing a previously published plan after a clean preread must let `--approve-keys` apply without a duplicate sub-issue HTTP 422.

Publish reads the parent's current sub-issues once and adds only missing child links. Keep blocked_by reconciliation idempotent and surface errors when a child belongs to a different parent.

Out of scope: changing or widening the tracker error-message regex.
