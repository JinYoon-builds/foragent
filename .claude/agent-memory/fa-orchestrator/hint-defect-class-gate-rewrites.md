---
name: gate-command-rewrite-review-bar
description: A gate change that parses or rewrites user commands needs a review bar in its first Goal, or review loops on syntax forever (#1105, #2103)
metadata:
  type: feedback
---

When an order makes the gate parse, rewrite or skip parts of a user's command (Done when, test commands), put a 'Review bar' paragraph in the first Goal: only unsafe results block (a skipped test outside the allowed set, a passing command that now fails); a missed optimisation is a NOTE; wrong results are closed by outcome (fallback to the unchanged command on unexpected exit codes), not by syntax.

**Why:** #2103 (11-05) took 5+ rounds and 10 h; 14 findings, 9 were missed skips (time only), 5 wrong reds, 0 unsafe. #1105 earlier followed the same path. Shell/pytest syntax has no end.

**How to apply:** check new gate-command orders before approval for this paragraph; when a hold repeats on syntax forms, edit the Goal with the bar instead of hinting.
