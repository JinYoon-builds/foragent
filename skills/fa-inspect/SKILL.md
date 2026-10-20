---
name: fa-inspect
description: Walk through the Foragent human inbox and record decisions and feedback on GitHub.
---

1. Run `fa inspect --json` in the project folder. If `items` is empty, say
   "No items to inspect." and stop. Otherwise show one header line with the
   nonzero counts, in the order returned by the inbox.

2. Go through `items` in order, one at a time. Use AskUserQuestion (or the
   available question tool) for choices. Preserve the gate's question and
   options; keep its recommendation first and mark it as recommended.
   Do not invent recommendations or new product judgments.

   - **decision:** Read `details` and present its question and options, plus
     "Later". If `answered` is true, mention that a decision is already recorded
     for the next `/fa-order`; ask for a replacement only if the user wants one.
     On an answer, run `fa inspect answer N --text "<chosen option, plus any note>"`.
     If the gate supplied no options, collect the user's own decision or Later.
   - **reapprove:** Show what changed from the summary and details. Give the
     issue link and explain that the user reapproves by adding the `approved`
     label on GitHub.
   - **review:** Show the PR link and summary. Ask "Merge / Close / Later".
     The user merges or closes on GitHub; record nothing for this choice.
   - **human-check:** List the unchecked items with the PR link and ask whether
     they pass ("Pass / Report a failure / Later"). Turn failures into
     `fa inspect feedback --title "<failure>" --body "<details>" --about N`.
     A pass is part of the recap; checklist updates belong to the user on GitHub.
   - **try:** Show the links and ask "Anything that is not what you wanted?"
     Offer "Looks good / Give feedback / Later"; collect free text for feedback.
     Run `fa inspect feedback --title "<short title>" --body "<feedback>" --about N`.
   - **tech-failure:** Show the failure summary and details. Ask
     "Retry / Split later (feedback) / Leave". Retry runs `fa inspect retry N`.
     Split later creates feedback with the failure context and `--about N`.
     Leave records nothing.

3. Safely pass the user's text as CLI arguments without shell interpolation.
   Later leaves the item untouched. After the walkthrough, run
   `fa inspect mark-seen` and give a one-line recap of what was recorded.

Never edit issue bodies, add `approved`, merge, or close PRs yourself.
