---
name: peer-messages-in-korean
description: Messages to the PM session (formerly planning) are in Korean and call it "PM"; Jin sees them
metadata:
  type: feedback
---

Write every SendMessage to the planning session in Korean (issue numbers, file names, commands stay as they are).

**Why:** 2026-10-31 the planning session reported that Jin sees our cross-session messages and kept asking "write in Korean" when they were in English.

**How to apply:** all peer messages, notes meant for people (`fa orch note`, `--say`) in the config language (ko). Hints to implementers (`fa orch retry --hint`) may stay English; they are read by lanes, not shown as chat. Related: [[questions-via-planning-session]].

Naming (2026-10-31, Jin via the PM): call the planning session "PM" in every message and note, not "기획 세션".

**11-07:** ListAgents names (foragent-45, tree-*) do not say which session is the PM, and plan-heartbeat.json holds only a time. Sending a q-handoff to a guessed name reached the architect instead. If the PM's name is not known for sure, leave the question in the FA inbox (fa orch ask already stored it) instead of guessing.
