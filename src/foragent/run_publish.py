"""Verified PR contents, publication, CI and merge policy (compatibility facade)."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, field, replace
from glob import escape
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING

from foragent import board, cuts, irreversible_paths, orch_scope, review_perspectives, risks
from foragent import run_merge_turn as merge_turn
from foragent.actions import FlagContext, flag, invalidate_approval
from foragent.ci_mode import ComparisonEvidence, commit_exclusions, enabled, require_comparison
from foragent.git import GitError, GitRepo, PublicationError
from foragent.i18n import text
from foragent.issue import ParsedIssue, body_hash
from foragent.labels import APPROVED, BLOCKED, READY, RUNNING
from foragent.orch_events import append_event
from foragent.overlap_files import any_overlap
from foragent.policy import Decision, merge_decision, review_mode_for, touched
from foragent.reasons import Reason
from foragent.run_attempt import AttemptFailure, CIFailure, ExecutorStopped
from foragent.run_base_move import BaseMove, REUSED, REVERIFY, base_move_is_neutral, capture_move, fetch_move
from foragent.run_checks import _acceptance_matches, _append
from foragent.run_prompts import review_scope_rules
from foragent.run_pr_body import _pr_body
from foragent.run_overlap import wait_for_overlap
from foragent.run_skill_catalog import report_line
from foragent.run_waits import _credited_wait, _phase, _progress, _timeout
from foragent.state import Approval, Run
from foragent.tracker import TrackerError

if TYPE_CHECKING:
    from foragent.runner import RunContext
    from foragent.run_attempt import Implementation
    from foragent.run_checks import CheckResult
    from foragent.run_review import ReviewResult


# The implementation lives in sibling modules by concern. They look up every
# name above through this module at call time, so tests that patch
# foragent.run_publish.<name> still reach the code that uses it.
from foragent.run_publish_ci import (  # noqa: E402
    _CIWait, _ci_failure, _ci_status, _hold_changed_head, _hold_if_not_started, _wait_for_ci,
)
from foragent.run_publish_merge import (  # noqa: E402
    _MergeInputs, _batch_pr, _check_publication, _final_merge_inputs, _merge_lock, _policy_allows, retitle_for_merge,
)
from foragent.run_publish_preserve import (  # noqa: E402
    _check_wip_deletions, _preserve_failed_implementation, _preserve_locked_implementation,
)
from foragent.run_publish_pr import (  # noqa: E402
    _merge_decision, _open_or_update_pr, _push_branch, _render_pr_body, _risk_always_human, _risk_notes, _skills_line, _update_open_pr,
)
from foragent.run_publish_flow import (  # noqa: E402
    _publish, _publish_in_turn, _publish_locked,
)
from foragent.run_publish_reviewed import (  # noqa: E402
    _publish_reviewed_head, _publish_reviewed_in_turn,
)
