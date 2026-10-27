# Tech-debt scoring

Source: adapted from the tech-debt skill in knowledge-work-plugins (anthropics), Apache License 2.0,
Copyright Anthropic, PBC. Changed: rewritten in our own words from common practice and kept only the
scoring formula; the upstream debt categories, report template and examples are not copied.

A common way to compare tech-debt items scores each one from 1 to 5 on three axes. This is
background only; it adds no step to the architect procedure.

- Impact: how much the debt slows work today.
- Risk: how likely the debt causes a real failure if left alone.
- Effort: how much work the fix takes, from 1 (small) to 5 (large).

Priority = (Impact + Risk) × (6 − Effort)

The score ranges from 2 to 50. Items with high impact and risk and low effort score highest.
