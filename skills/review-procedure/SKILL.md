---
name: review-procedure
description: "Apply when you review a change or score a review finding. The six review perspectives, the 0-100 score table, the false-positive list and the grounding rule that decides whether a finding blocks the merge."
---

Source: adapted from the code-review plugin in anthropics/claude-plugins-official, Apache-2.0, Copyright Anthropic, PBC (path `plugins/code-review/commands/code-review.md`, commit 4ca561f). See LICENSE and NOTICE.
Changed: the steps are rewritten for Foragent's reviewer, perspective panel and scoring sessions. CLAUDE.md becomes the issue's Goal and Out of scope plus the project's rule documents. A fatal-path perspective and the grounding rule are added. The score table and the false-positive list are copied verbatim.

## Perspectives

Each perspective is one independent look at the whole change. Report each issue with the reason it was flagged.

1. rule-documents: Audit the changes against the project's rule documents (CLAUDE.md, AGENTS.md, docs/rules.md) and the issue's Goal and Out of scope. Rule documents are guidance for writing code, so not every instruction applies during review.
2. obvious-bugs: Read the file changes, then do a shallow scan for obvious bugs. Focus on large bugs and avoid small issues and nitpicks.
3. git-history: Read the git blame and history of the modified code, to find bugs in light of that history.
4. earlier-pr-comments: Read earlier pull requests that touched these files and check whether their comments also apply here.
5. code-comments: Read code comments in the modified files and check that the change complies with their guidance.
6. fatal-path: Look for a path, however rare, that loses work or data, breaks main, merges without approval, or hands a person-only permission to an agent.

## Scoring a finding

Score each finding on a scale from 0-100 for how confident you are that it is real and not a false positive. The scale (verbatim from the plugin):

   a. 0: Not confident at all. This is a false positive that doesn't stand up to light scrutiny, or is a pre-existing issue.
   b. 25: Somewhat confident. This might be a real issue, but may also be a false positive. The agent wasn't able to verify that it's a real issue. If the issue is stylistic, it is one that was not explicitly called out in the relevant CLAUDE.md.
   c. 50: Moderately confident. The agent was able to verify this is a real issue, but it might be a nitpick or not happen very often in practice. Relative to the rest of the PR, it's not very important.
   d. 75: Highly confident. The agent double checked the issue, and verified that it is very likely it is a real issue that will be hit in practice. The existing approach in the PR is insufficient. The issue is very important and will directly impact the code's functionality, or it is an issue that is directly mentioned in the relevant CLAUDE.md.
   e. 100: Absolutely certain. The agent double checked the issue, and confirmed that it is definitely a real issue, that will happen frequently in practice. The evidence directly confirms this.

Examples of false positives (verbatim from the plugin):

- Pre-existing issues
- Something that looks like a bug but is not actually a bug
- Pedantic nitpicks that a senior engineer wouldn't call out
- Issues that a linter, typechecker, or compiler would catch (eg. missing or incorrect imports, type errors, broken tests, formatting issues, pedantic style issues like newlines). No need to run these build steps yourself -- it is safe to assume that they will be run separately as part of CI.
- General code quality issues (eg. lack of test coverage, general security issues, poor documentation), unless explicitly required in CLAUDE.md
- Issues that are called out in CLAUDE.md, but explicitly silenced in the code (eg. due to a lint ignore comment)
- Changes in functionality that are likely intentional or are directly related to the broader change
- Real issues, but on lines that the user did not modify in their pull request

## Grounding rule

Say what the finding stands on. A ground is one of:

- `goal_line`: a line quoted exactly from the issue's Goal or Out of scope;
- `locked_test`: a locked acceptance test, by path under the acceptance directory (optionally `::name`), that this change makes fail;
- `breaking_input`: a concrete input or sequence that really breaks if this change merges.

A ground establishes evidence, not severity. For each finding, assess `exists`, `wrong_result`, `ordinary_input` and `irreversible`. A grounded finding whose failure exists blocks only when it causes a wrong result, lost work, unverified code reaching main, a stalled gate or a broken locked test on ordinary inputs, or harm that cannot be undone even on rare inputs (work lost, data deleted, something sent outside). The confidence score stays on record; it does not decide the block.

A rare bug that can be undone, or a finding with no wrong result (slower, less tidy, wording), does not block even with a Goal quote, a locked-test reference or a high score. It causes no rework. Each non-blocking scored finding becomes one follow-up issue naming its source issue, and stays on record as a NOTE with its score and follow-up link. Plain NOTE lines may stay as notes.

When the Goal does not decide the point, do not invent a rule: write the question in `spec_question`. The gate asks the planning side instead of sending the work back.

A finding in a later round that is another instance of a first-round defect class follows the same grounding and consequence rule. Name each finding's root-cause class in `finding_class`, reusing the supplied `previous_classes` name for the same cause even when the instance, wording or file differs. The agent judges the class; the harness counts distinct failed rounds. After two failed rounds of the same class in the approved scope, a third round does not start: the existing orchestrator question inbox hands the PM the evidence and two choices, ship with follow-ups or narrow the scope.
