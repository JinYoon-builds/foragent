---
name: prove-locks-can-pass
description: Before publishing locked tests or an order edit, run affected locks and prove with a scratch prototype that they can pass
metadata:
  type: feedback
---

Before publishing an order or a lock edit: run the related existing locked tests, and for new locks build a scratch prototype and show they can pass, then discard it. Collection and "fails on main" prove neither.

**Why:** on 2026-11-01 a stop for this class came up five times (#1415, #994, #1511, #1482, #1556): locks contradicted each other or no implementation could pass them, and each cost a question, an edit, a preread and a CI round. The PM noted it should not depend on a person being careful and listed an FA-side fix as a card candidate after measurement.

**How to apply:** put this in the Codex draft spec. Run a Done when command before adding it to an issue. Pair with [[no-chained-publish]].

Run the prototype on clean main alone. On 2026-11-02 the skills order's size locks passed only because its prototype was stacked on the review procedure prototype; on main alone two could not pass.
