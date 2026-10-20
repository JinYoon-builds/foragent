"""Path-based review policy, separate from execution and tracker effects."""

from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Literal

from foragent.config import Config
from foragent.labels import RISK_HIGH
from foragent.reasons import Reason


def _matches(path: str, pattern: str) -> bool:
    parts = PurePosixPath(path).parts
    globs = PurePosixPath(pattern).parts

    @lru_cache(None)
    def match(i: int, j: int) -> bool:
        if j == len(globs):
            return i == len(parts)
        if globs[j] == "**":
            return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
        return i < len(parts) and fnmatchcase(parts[i], globs[j]) and match(i + 1, j + 1)

    return match(0, 0)


def touched(paths: list[str], globs: list[str]) -> list[str]:
    return [path for path in paths if any(_matches(path, glob) for glob in globs)]


def review_mode_for(paths: list[str], config: Config) -> str:
    levels = {"fast": 0, "batch": 1, "careful": 2}
    modes = [config.review, *(mode for glob, mode in config.review_paths.items() if touched(paths, [glob]))]
    return max(modes, key=levels.__getitem__)


@dataclass(frozen=True)
class Decision:
    kind: Literal["merge", "hold", "hold_mode"]
    reason: Reason | None = None
    summary: str = ""
    mode: str | None = None


def hold_mode(mode: str) -> Decision:
    return Decision("hold_mode", summary="PR is waiting for your review", mode=mode)


def merge_decision(*, changed_paths: list[str], issue_labels, config: Config) -> Decision:
    directory = PurePosixPath(config.acceptance_dir)
    if any(PurePosixPath(path).is_relative_to(directory) for path in changed_paths):
        return Decision("hold", Reason.ACCEPTANCE_TOUCHED, "Acceptance files changed; a human must review this PR.")
    if touched(changed_paths, config.always_human):
        return Decision("hold", Reason.ALWAYS_HUMAN_PATH, "This PR touches a path requiring human review.")
    if RISK_HIGH in issue_labels:
        return Decision("hold", Reason.RISK_HIGH, "This issue is marked risk:high; a human must review this PR.")
    mode = review_mode_for(changed_paths, config)
    return Decision("merge") if mode == "fast" else hold_mode(mode)
