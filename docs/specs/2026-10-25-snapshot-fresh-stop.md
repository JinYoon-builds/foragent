# A stop takes one fresh listing of its tree (edit to #431)

A Codex adversarial review (planning session) reproduced with fake tables that a stop resolving its tree only from the tick snapshot misses a child forked after the snapshot, orphaning it to pid 1. Stops are rare, so each stopped tree may take one fresh listing; routine checks still share the snapshot. The locked one-listing assertion allows 1 + stops, and a new case checks the late child is stopped.
