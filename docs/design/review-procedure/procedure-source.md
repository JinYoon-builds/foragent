---
name: review-procedure
description: Review a change from the configured perspectives, then score each FAIL finding in a fresh session. A finding blocks only when it is grounded in the Goal, a locked test or a concrete breaking input.
---

# Review procedure

Adapted from Anthropic's code-review plugin (Apache-2.0). See LICENSE and NOTICE.
Foragent changes: a sixth fatal-path perspective, the grounding rule, spec
questions for the planning side, and scoring in every review round.

A project can replace this file through its reviewer skills.

## Perspectives (first round, when the panel runs)

- rule-documents: Check the change against docs/rules.md and the instruction
  files that apply (AGENTS.md, CLAUDE.md). A rule finding cites the rule.
- obvious-bugs: Read the changed lines and look for substantial bugs. Skip
  small issues and nitpicks.
- git-history: Read local Git blame and history of the changed code. Use local
  Git only.
- earlier-pr-comments: Read the review comments on earlier PRs that touched
  these files, from local records first. Never invent comments.
- code-comments: Read comments in the modified files and check their guidance.
- fatal-path: Even if rare, look for a path that loses work or data, merges
  wrong code, touches another person's process, issue or branch, or stalls
  until a person acts.

## Scoring step (every round, one fresh session per FAIL finding)

1. Decide what the finding is grounded in. Exactly these count:
   - goal_line: a line of the approved Goal or Out of scope that the change
     does not meet. Quote it as written.
   - locked_test: a locked acceptance test that fails or is circumvented.
     Name its file (path::test).
   - breaking_input: a concrete input that really reaches this code after
     merge and breaks it. State the input. An input that needs an unusual
     setup nobody uses (a deeply nested file, a control character in a name,
     the 21st handover in a row) is not a breaking input unless the Goal names it.
   If none applies, leave all three null. The finding is then a NOTE, however
   confident you are.
2. If the point is real but the Goal does not decide it (the Goal never says
   what is allowed), do not ground it in your own preference. Give a
   spec_question for the planning side instead.
3. Score confidence with the table below. A finding blocks only when it is
   grounded and scores 80 or more.
4. fatal-path: you may mark any finding critical_path. For a critical path,
   answer exists (does the path really exist in the changed code). Rarity
   never discounts it. A grounded critical path that exists blocks at any score.

Round 2 and later: a newly found instance of a first-round defect class is
scored the same way. "The same kind still remains" is not a ground by itself.

   a. 0: Not confident at all. This is a false positive that doesn't stand up to light scrutiny, or is a pre-existing issue.
   b. 25: Somewhat confident. This might be a real issue, but may also be a false positive. The agent wasn't able to verify that it's a real issue. If the issue is stylistic, it is one that was not explicitly called out in the relevant CLAUDE.md.
   c. 50: Moderately confident. The agent was able to verify this is a real issue, but it might be a nitpick or not happen very often in practice. Relative to the rest of the PR, it's not very important.
   d. 75: Highly confident. The agent double checked the issue, and verified that it is very likely it is a real issue that will be hit in practice. The existing approach in the PR is insufficient. The issue is very important and will directly impact the code's functionality, or it is an issue that is directly mentioned in the relevant CLAUDE.md.
   e. 100: Absolutely certain. The agent double checked the issue, and confirmed that it is definitely a real issue, that will happen frequently in practice. The evidence directly confirms this.

Examples of false positives, for steps 4 and 5:

- Pre-existing issues
- Something that looks like a bug but is not actually a bug
- Pedantic nitpicks that a senior engineer wouldn't call out
- Issues that a linter, typechecker, or compiler would catch (eg. missing or incorrect imports, type errors, broken tests, formatting issues, pedantic style issues like newlines). No need to run these build steps yourself -- it is safe to assume that they will be run separately as part of CI.
- General code quality issues (eg. lack of test coverage, general security issues, poor documentation), unless explicitly required in CLAUDE.md
- Issues that are called out in CLAUDE.md, but explicitly silenced in the code (eg. due to a lint ignore comment)
- Changes in functionality that are likely intentional or are directly related to the broader change
- Real issues, but on lines that the user did not modify in their pull request

The upstream CLAUDE.md references above also mean this project's instruction
and rule files.
