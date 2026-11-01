# CI mode keeps local protection local_only across batch targets

For a supported pytest project with checks: ci and no explicit local fallback, local protected acceptance checks remain limited to local_only on first capture, adoption of a different base, target changes between main and a batch branch, and retries. A target change does not authorize changing the project's chosen check location. Keep enforcing the selected machine tests on the implementation head.

CI evidence covers the other protected tests. When usable evidence cannot be obtained for the current target/base/head, wait for or request valid evidence, or report the specific unavailable evidence/unsupported target and hold. Do not silently substitute a full local protected capture or verification, and do not merge without usable protection.

This order fixes the undocumented branch-dependent expansion. The explicitly documented checks_fallback: local option remains outside this order, as does the existing trusted-workflow-change local comparison contract. Preserve their safety checks. Ordinary local-mode behavior and the worker's own Done when/related-unit checks are outside scope.

Close when the locked public-run tests prove exact-base local_only re-capture, retention through a worker retry, initial batch selection, and an explained hold for unusable CI evidence without portable protected tests executing locally. The ordinary main-target invalid-evidence case is a passing control.

The faulty CI selection files have no explicit lane owner in .foragent.yml, so this issue uses the implicit main lane. Implementation design remains with that lane.
