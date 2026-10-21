# Remove the leftover three-skill cap from /fa-init

Implements the remaining part of D35 (docs/decisions.md): there is no skill count limit per lane. Code and lane tests already accept more than three skills; only the /fa-init instructions still say "select at most three to attach (the lane limit)".

### init-skill-cap — D35 /fa-init proposes design skills without a count limit (size s)
Goal: In skills/fa-init/SKILL.md step 9, drop the three-skill limit. The skill suggests the relevant design skills and attaches the ones that fit the project, with no count limit; selection is by relevance (D35: manage by measured effect, not count). Keep the rest of the step unchanged.
Out of scope: Code changes; other skills.
Test checks: tests/acceptance/test_init_skill_cap.py
