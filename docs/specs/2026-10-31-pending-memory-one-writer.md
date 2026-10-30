# #1000 pending memory redesign (2026-10-31)

Review found consumed lessons coming back after rebase/union merge: the pending buffer had two writers (workers append, the curator truncates). Redesign under D95: append-only pending with ids, consumed ids as a separate single-writer record, pending derived.
