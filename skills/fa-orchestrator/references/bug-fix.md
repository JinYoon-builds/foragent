# Bug fix

Source: adapted from pstack by Lauren Tan, playbook `skills/poteto-mode/playbooks/bug-fix.md` (https://github.com/cursor/plugins/tree/9b93e81fb2921b9263d9463c15bafeabfe3903aa/pstack), MIT License.

Every shipped line traces to runtime evidence. A change that "might help" is a
hypothesis, not a fix, and it does not ship. When evidence refutes a hypothesis,
revert what it motivated. Ship the smallest change the evidence justifies.

1. Reproduce the bug yourself on the surface where it was reported (the same command,
   test, page or terminal). If it does not reproduce directly, synthesize the trigger,
   tighten the conditions, or add logging until it fires. Change nothing before that.
2. Rule out hypotheses with evidence. List the candidate causes, then take the split
   that cuts the most remaining space and eliminate hypotheses with runtime evidence
   until one survives. Read the code, `git log` and the run logs to seed them.
   Do not guess. When program state is unclear or unknown, add logging or
   instrumentation, run it, and read the output it reveals.
3. Plan the fix from the evidence. Name the surviving mechanism and the smallest change
   that removes it. Fix the root cause, not the symptom.
4. Write a failing regression test first, and see it fail before you change the code.
   Skip it only when a test would be expensive or unclear, and say why.
5. Apply the fix, then confirm it on the same surface as the reproduction: the original
   repro now passes. An inconclusive result or a different surface is not a pass.
   Unit tests show branch behavior, not bug absence.
6. Remove the temporary logging unless it earns a place.

Report the result with evidence: what was broken, the root cause, the fix, and how you
verified it, with the failing-then-passing output pasted verbatim.
