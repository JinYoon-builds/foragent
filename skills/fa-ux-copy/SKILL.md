---
name: fa-ux-copy
description: Write or review user-facing copy in Foragent's view lane — CLI messages, status labels, errors, empty states, prompts and confirmations. Apply whenever an issue adds or changes words a person reads on screen.
---

Source: adapted from ux-copy from anthropics/knowledge-work-plugins, Apache-2.0, Copyright Anthropic, PBC (upstream path `design/skills/ux-copy/SKILL.md`, library cache commit 33d3dea, last change to that path 2d6f7e2). See LICENSE for the full Apache License, Version 2.0.

Merged rules: the Korean-copy section restates, in our own words, the rules of toss-ux-writing from NariP/toss-ux-writing-skills (MIT License, Copyright (c) 2026 NariP, library cache commit 3348873). No toss-ux-writing text is copied.

Changed: the agent now reads the issue Goal and docs/voice.md for context, tone and constraints instead of collecting them from a person. The upstream `/ux-copy $ARGUMENTS` usage and `argument-hint` are removed, because lanes preload the skill. The alternatives table in the output is removed; the agent writes the copy into the code. The connectors and tips sections are removed. The upstream principles and copy patterns are kept, and the toss-ux-writing rules are merged in where they overlap (clear, concise, solution-first errors) and added for Korean text.

# Foragent UX copy

Write or review the words a person reads in the product: command output, status labels, errors, empty states, prompts, confirmations and help text.

## Where the context comes from

Read these before you write a word. They replace a briefing from a person.

1. Read the issue Goal. It says which screen, command or flow changes, what the person is trying to do and any limits (length, format, exact strings).
2. Read the acceptance tests named in the issue. Exact strings in a test are fixed requirements.
3. Follow docs/voice.md for the project voice. Use `src/foragent/i18n.py` for existing strings and terms.
4. Look at the copy next to the change. Match its terms, case and punctuation.

When neither the issue Goal nor docs/voice.md sets a constraint, use this default: short plain sentences in the language of the surrounding text, one idea per sentence, sentence case, and the existing terms from `src/foragent/i18n.py`. State the default you used in the PR.

## Principles

1. **Clear**: Say exactly what you mean. No jargon, no ambiguity.
2. **Concise**: Use the fewest words that carry the full meaning.
3. **Consistent**: Use the same term for the same thing everywhere.
4. **Useful**: Every word helps the person reach their goal.
5. **Human**: Write like a helpful colleague, not a machine.
6. **True**: Say only what the system has confirmed. Do not promise a success that has not happened yet.

## Copy patterns

### Actions and buttons
- Start with a verb: "Save changes", "Open the view", "Retry".
- Be specific: "Create lane", not "Submit".
- Make the label match the result.

### Error messages
Structure: what happened + why + what to do now.
- "Couldn't refresh. GitHub didn't answer. I'll try again in a minute."
- Lead with what the person can do. Keep the cause and the details needed to diagnose it.

### Empty states
Structure: what this is + why it is empty + how to start.
- "No lanes yet. Run `fa lanes add` to create the first one."

### Confirmations
- Name the action: "Delete 3 branches?", not "Are you sure?"
- Say the consequence: "This can't be undone."
- Label each choice with its action: "Delete branches" / "Keep branches", not "OK" / "Cancel".

### Labels, headings and hints
- Keep them short: a name or an action is enough.
- Keep commands, paths, JSON keys, label identifiers and reason codes exactly as they are.

### Progress and loading
- Say what is happening and, if known, how long it takes.

## Tone by situation

- **Success**: warm, not over the top.
- **Error**: calm and helpful.
- **Warning**: clear and actionable.
- **Neutral**: informative and short.

## Korean copy

Apply this section only to Korean text. English text follows the sections above and docs/voice.md.

- **해요체**: end every sentence in the product with the 해요 form, such as `~해요`, `~했어요`, `~할게요`, whatever the situation. Short labels and headings may stay as plain nouns.
- **Active voice**: say who does the action. Prefer `~했어요` over `~됐어요`, and prefer `선택한` over `선택된`. Swap a passive verb for an active one when the meaning stays the same.
- **Positive phrasing**: tell the person what they can do instead of what is missing. An error ends with the next step, for example "다시 시도하면 연결할 수 있어요" rather than only "연결 오류".
- **Casual honorifics**: stay polite but light. Drop extra `~시` from verbs, use `있다` instead of `계시다`, `확인하다` or `묻다` instead of `여쭙다`, and `에게` instead of `께`. If a sentence sounds odd after this, rebuild it around the thing you want to know.
- **No noun stacks**: turn stacked Sino-Korean nouns into a verb phrase. Write "계좌를 등록했어요", not "계좌 등록 완료". When a full verb phrase is hard, use at least the `{noun}이 {noun}해서` form.
- **Allowed exceptions**: a passive sentence is fine when the subject must stand out, such as a service that ends or a period that expires.

## Output

Write the copy directly into the code, the strings in `src/foragent/i18n.py`, or the docs the issue names. In the PR, list each new or changed string with one line on why it fits the Goal and the voice. Add notes for translation when a string has an idiom, a length limit or a plural.
