---
name: fa-inspect
description: Walk through the Foragent human inbox and record decisions and feedback on GitHub.
---

1. Run `fa inspect prepare`, then `fa inspect --json` in the project folder.
   Read `language` and speak to the user in that language throughout, including
   questions, summaries, options and the recap. Preserve exact commands, URLs,
   machine headings, markers and quoted evidence. Show one summary line with the
   nonzero `summary.counts` and `summary.estimated_minutes`. If `items` is empty,
   say there are no items and proceed to step 3.

2. Go through `items` in order, one at a time. Use AskUserQuestion (or the
   available question tool) for choices. Preserve the gate's question and
   options; keep its recommendation first and mark it as recommended.
   Do not invent recommendations or new product judgments.

   - **decision:** Read `details`. Show the question, then the `## Evidence`
     quotes side by side (issue sentence and file:line with the exact code),
     then the options, recommended first, plus "Later", using AskUserQuestion.
     If evidence is absent, say so without inventing it.
     If `answered` is true, mention that a decision is already recorded
     for the next planning interview (`/fa`); ask for a replacement only if the user wants one.
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
   - **try (cli):** Show `try_card.what_changed`, each exact command,
     `try_card.expect`, and `prepared.results` (combined output, exit code and
     timeout status). If absent, say "not prepared"; if the card is missing,
     explain that no try instructions were provided and show the PR link.
     Ask "Does this look right?" with "Yes / Not what I wanted / Later".
     For "Not what I wanted", collect feedback text and run
     `fa inspect feedback --title "<short title>" --body "<feedback>" --about N`.
   - **try (web):** Run `fa inspect serve N`. Show What changed, the returned
     URLs, `try_card.web.steps` and Expect. The tailnet URL works from a phone
     or laptop connected to the tailnet. If a headless browser skill or tool
     is available, open the page, follow the steps and show screenshots.
     Ask the same question as for cli and record feedback the same way.
     Run `fa inspect stop` when done with the preview, including early exits.
   - **tech-failure:** Show the failure summary and details. Ask
     "Retry / Split later (feedback) / Leave". Retry runs `fa inspect retry N`.
     Split later creates feedback with the failure context and `--about N`.
     Leave records nothing.

3. Safely pass the user's text as CLI arguments without shell interpolation.
   Later records no answer. Always run `fa inspect mark-seen` at the end,
   even for an empty inbox or if the user stops early; treat it as cleanup
   that also stops served apps. Tell the user it is done. Give a one-line recap
   of what was recorded.

Never edit issue bodies, add `approved`, merge, or close PRs yourself.
