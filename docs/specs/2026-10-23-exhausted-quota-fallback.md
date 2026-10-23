# Exhausted quota fallback (addition to #186)

On 2026-10-23 a person switched `executor` to claude at 14:42 and `reviewer` to claude at 21:30 when the Codex weekly quota reached 100 % (reset 10-30 02:09 KST). #186 now covers this: when one side is used up, both worker and reviewer go to the side with quota left; the run records it with a `same-model-review` event so the scorecard can separate those reviews; when the quota resets, cross review returns with no config change. The fixed `executor:`/`reviewer:` lines in .foragent.yml can then be removed.
