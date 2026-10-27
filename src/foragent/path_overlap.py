"""Intersect repository path globs, including paths not created yet."""

from collections.abc import Callable
from fnmatch import translate
from glob import has_magic
from pathlib import PurePosixPath
import re


def _file_patterns_overlap(left: str, right: str, *others: str, case_sensitive: bool = True) -> bool:
    """Intersect path globs, including files that do not exist yet.

    As in policy._matches, * and ? stay in a component; ** spans components.
    Walking tuples of glob positions intersects every pattern at once.
    """
    def intersect(patterns, star: str, compatible: Callable) -> bool:
        pending, seen = [(0,) * len(patterns)], set()
        while pending:
            positions = pending.pop()
            if positions in seen:
                continue
            seen.add(positions)
            if all(i == len(pattern) for i, pattern in zip(positions, patterns)):
                return True
            for n, (i, pattern) in enumerate(zip(positions, patterns)):
                if i < len(pattern) and pattern[i] == star:
                    following = list(positions)
                    following[n] += 1
                    pending.append(tuple(following))
            if all(i < len(pattern) for i, pattern in zip(positions, patterns)):
                values = tuple(pattern[i] for i, pattern in zip(positions, patterns))
                if compatible(values):
                    pending.append(tuple(i + (value != star) for i, value in zip(positions, values)))
        return False

    def tokens(pattern: str):
        i = 0
        while i < len(pattern):
            start, i = i, i + 1
            if pattern[start] == "[":
                end = i
                if end < len(pattern) and pattern[end] == "!":
                    end += 1
                if end < len(pattern) and pattern[end] == "]":
                    end += 1
                while end < len(pattern) and pattern[end] != "]":
                    end += 1
                if end < len(pattern):
                    i = end + 1
            yield pattern[start:i]

    def components_overlap(parts: tuple[str, ...]) -> bool:
        # Full case folding covers literal filesystem aliases such as ß/ss.
        # Leave globs unchanged so ? and character classes keep their width.
        if (
            not case_sensitive and not "".join(parts).isascii()
            and not any(has_magic(part) for part in parts) and len({part.casefold() for part in parts}) == 1
        ):
            return True
        # Literal/range endpoints and their successors represent every distinct
        # character class membership when matching case-sensitively.
        alphabet = {"\x01"} | {
            chr(code) for char in "".join(parts) for code in (ord(char), ord(char) + 1)
            if 0 < code <= 0x10ffff and code != ord("/")
        }
        if not case_sensitive:
            alphabet.update(chr(code) for code in range(1, 128) if code != ord("/"))
        patterns = tuple(tuple(tokens(part)) for part in parts)
        # Apply case equivalence to each token, preserving its width and the
        # original character classes (including punctuation inside [A-z]).
        matchers = {
            token: re.compile(translate(token), 0 if case_sensitive else re.IGNORECASE)
            for token in set(token for pattern in patterns for token in pattern)
        }

        def compatible(values: tuple[str, ...]) -> bool:
            if any(all(matchers[value].fullmatch(c) for value in values) for c in alphabet):
                return True
            # Unicode case mappings inside classes need not be monotonic, so
            # endpoints alone cannot prove two nonliteral tokens disjoint.
            return (
                not case_sensitive and not "".join(values).isascii()
                and all(len(value) > 1 or value in ("*", "?") for value in values)
            )

        return intersect(
            patterns, "*", compatible,
        )

    return intersect(tuple(PurePosixPath(part).parts for part in (left, right, *others)), "**", components_overlap)

