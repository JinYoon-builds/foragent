---
name: writer-parser-one-description
description: a check that re-reads what another component wrote must share the writer's description; a free-text field the writer does not constrain can never be told from a hand edit (11-09 #2868, 2 holds)
metadata:
  type: feedback
---

#2868 (lock-only route) parsed the issue body's generated tail with its own pattern while order.py wrote it. Round after
round the reviewer found either "generated form refused" or "hand edit accepted and deleted". My fail-closed hint did not
end it; architect r1293 found why: preparation notes were free text, so a multi-line note and a hand-added section could be
byte-identical — the two statements I had asked for contradicted each other. Fix: the writer folds notes to one line and the
generated forms are described once, shared by writer and check (census of 123 real bodies chose the forms).

**Why:** two representations of one format drift; free text has no provenance.
**How to apply:** when a hold says "check rejects what X writes" or "accepts a hand edit", order the writer to constrain its
output and share one description with the checker; before writing statements, census real data and test that the
statements can all be true together.
