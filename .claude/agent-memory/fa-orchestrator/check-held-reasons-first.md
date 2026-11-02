---
name: check-held-reasons-first
description: Before blaming file overlap for a queued lane, read the 'held:' reason in fa orch facts; cap (machine slots) is often the binding constraint
metadata:
  type: feedback
---

Before calling the architect (or splitting a file) because one lane's queue backs up, read `fa orch facts` for each queued issue's `held:` reason and the scorecard's overlap-wait minutes.

**Why:** 2026-10-30 I called the architect to split order.py because all five order-lane issues declared it. The architect (r65) showed they were `held: cap` (machine 6/6); `overlap_per_file: 3` already lets three runs share a file, overlap-wait was 0 min, and a split would not have separated them (they also share decisions.md, config.py, fa-order SKILL.md). Idle lanes are not lost capacity when the machine is full (r29: no per-lane cap).

**How to apply:** queue complaint → `fa orch facts` held reasons first. Only a `structure`/overlap hold while cap is free justifies an architect call on file layout.

The global queue does not put an issue first outright: it ranks only among candidates with the same file-overlap count (scheduler_start.py). On 2026-11-01 #994 (few files) started ahead of first-listed #1372.

Before calling something a missing feature or a defect in a merged issue, search open approved issues for it. On 2026-11-02 I reported that retrospective requests could not be answered and drafted a fix; open #999 was already adding it and merged an hour later.
