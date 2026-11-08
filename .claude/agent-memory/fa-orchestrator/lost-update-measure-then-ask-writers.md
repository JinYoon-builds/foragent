---
name: lost-update-measure-then-ask-writers
description: 11-09 a 50-min local check came from an erased fix, not a bad test; when a measured cost reappears, check git log of the file for a later whole-file write before proposing removal
metadata:
  type: feedback
---

I measured one locked test at 47-56 min per local_only check and proposed removing it (q95). PM found the real cause:
publication dc672237 rewrote the file from a snapshot older than #2813's collect-only fix (lost update), and restored it (#2862).

**Why:** a cost that a merged fix had already removed means something undid the fix; removing the test would have treated the symptom.
**How to apply:** before proposing to cut a slow test, run `git log -p --follow` on it and check whether a recent fix was
reverted by an unrelated commit (publication, main write, role memory). Lost-update writers were ordered as #2877/#2878.
