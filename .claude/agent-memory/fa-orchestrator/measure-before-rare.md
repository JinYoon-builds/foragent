---
name: measure-before-rare
description: Before answering a review hold "rare, ship/narrow away", measure how often the case happens; 11-09 r1458 called a 91%-of-the-time case rare
metadata:
  type: feedback
---
Before labelling a review-hold finding "rare" (ship with follow-up, or narrow it away), measure its frequency from real records.

**Why:** 11-09 r1458 I narrowed #2893's "keeps its place" as "rare, a delay only". The architect measured (r1460): PR CI median 20.4 min vs main gap 2.3 min, so a refreshed workflow member finds a quiet gap only 8.8% of the time; the "rare" case was the normal one (#1230 had waited 22.8 h). A wrong "rare" moves real work into a follow-up nobody prioritises.

**How to apply:** when a hold quotes timing or ordering, compare the needed window with measured rates (main merge gaps from `git log origin/main`, PR CI durations from `gh run list`) before choosing; when two review rounds pull in opposite directions, look for a bounded rule (time or count limit) instead of dropping one side. Related: [[measure-before-fixing-locks]].
