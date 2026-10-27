# Investigation

Source: adapted from `pstack/playbooks/investigation.md` (pstack by Lauren Tan, MIT License).

An investigation answers a question with a cited explanation or a recommendation.
It changes no code. Use it before deciding what to do with a blocker, a repeated
failure or an unclear request.

1. Reproduce the observation first: rerun the failing command, or open the exact log,
   issue comment or state file that shows it. Do not reason from a summary.
2. Rule out hypotheses with evidence. Write down each candidate explanation and the
   observation that would eliminate it, then collect that evidence and reject the
   hypotheses it refutes. Do not guess. When the cause is unknown or unclear, add
   logging or instrumentation in a scratch run and read the output and runtime state
   it reveals. Read the code and `git log` for how and why the area works as it does.
3. Plan the answer from the evidence: an overview, how it works, where things live,
   and the gotchas. For a choice between options, give a recommendation with a
   trade-offs table.
4. Confirm the conclusion on the same path that produced the observation: the
   explanation must predict what the reproduction shows.
5. If the answer leads to a code change, hand it to a bug fix or a feature issue.
   That work writes a failing test first, before the fix.

Report the result with the evidence: cite files with line numbers, commands and their
output. If the premise of the question is wrong, say so with reasons.
