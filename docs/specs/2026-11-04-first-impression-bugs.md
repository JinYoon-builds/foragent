# 2026-11-04 first impression and bug fixes

PM requests r394 and r395. Bugs: `fa inspect` prepare ran an old PR's Try it commands against the real Foragent state and created open questions q5/q6 (2026-11-03T15:52:39Z); `fa orch facts` labels a plan-paused lane as a usage-limit pause. First impression (bundle 4): #1475 one-line install; #1655 is split: this order keeps the repository first-run steps in #1655, the survey (B) and board cleanup (C) follow as separate issues.

Acceptance tests were drafted by the owning lanes' agents, shown to fail on clean main for the real reason, and shown passable with a discarded prototype.
