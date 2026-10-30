---
name: principle-migrate-callers-then-delete-legacy-apis
description: "Apply when introducing a new internal API while old callers still exist. Migrate callers and delete the old API in the same wave instead of preserving compatibility layers."
---

Source: vendored from `pstack/skills/principle-migrate-callers-then-delete-legacy-apis/SKILL.md` (pstack by Lauren Tan, MIT License, Copyright (c) 2026 Lauren Tan, source commit 9b93e81). See LICENSE.
Changed: removed `disable-model-invocation: true` so an agent can load the skill after installing it. The text is otherwise unchanged.

# Migrate Callers Then Delete Legacy APIs

- Update tests to assert the new contract, and delete tests that only protect pre-refactor implementation details

**When this applies:**
- No external users depend on backward compatibility
- The project can absorb coordinated breaking changes
- The new API is part of a simplification or refactor initiative
