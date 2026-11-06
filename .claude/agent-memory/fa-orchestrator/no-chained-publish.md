---
name: no-chained-publish
description: Do not chain unpark and retry on one line after fa order publish; a refused publish still runs them
metadata:
  type: feedback
---

Run `fa order publish --approve` alone, confirm the `approved` label, then run `fa orch unpark` and `fa orch retry`. Before publishing, confirm the preread ran for the exact plan.

**Why:** on 2026-11-01 a publish for #1509 was refused for lack of a preread. The same command line went on to unpark and retry, and my session note claimed re-approval. The issue had also lost its `approved` label. I re-parked it before a run started.

**How to apply:** an order edit needs a fresh preread even when only a locked test changes. Plan about 10 minutes for it before the next merge batch. Report re-approval only after reading the label.

`fa orch park` removes `approved` (orch_park.py:95) and `unpark` does not restore it, so after an unpark an issue sits `ready` and never starts until it is re-approved through an order edit with a preread. #1158 sat that way for 1.5 hours on 2026-11-01. A park also writes `## Waiting` to the body, which invalidates a preread already running.


**11-05 #2103:** my wait-then-publish-then-retry loop ran `fa orch retry` even though the publish failed ('cannot lock ref refs/heads/main', another writer won the race), so the lane restarted on the old Goal. In such loops, retry only when the publish line shows the issue row with 'yes' (or check the new body text) — gate the retry on it.

**11-06:** two publish-queue scripts running at once raced on `git pull --rebase` ("Cannot rebase onto multiple branches") and on push ("[rejected] main -> main (fetch first)"), leaving local main ahead with unpushed order and memory commits and both publishes failed. Run exactly one queue script at a time (one script lists every plan), and start it by pushing any local commits first.

**Update 2026-11-06:** a preread is clean when every issue's `unknowns` list is empty. Do not grep the output for "QUESTION": the lane's plan text can contain the word (#1488 "On QUESTION, ..."), and the queue stopped on a clean preread. Use drafts/bin/preread-clean.sh. A pull can also fail on an untracked file that upstream now tracks; if it is byte-identical, move it aside.
