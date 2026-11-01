"""Character budgeting for PR bodies; independent of the tracker and state store."""

import re
from itertools import groupby

PR_BODY_LIMIT = 65_536

_MARKERS = re.compile(r"<!-- foragent:([\w-]+) -->.*?<!-- /foragent:\1 -->", re.S)
_REQUIRED_LINE = re.compile(
    r"(?:Closes #\d+\s*$|\|.*|## .*|.*\bVERDICT:.*|"
    r"Opened by Foragent gate.*|Sub-issues of #\d+.*|#\d+의 하위 이슈.*|"
    r"- \[[ xX]\].*|.*<!-- /?foragent:[^>]*-->.*|"
    r"_Shortened to fit GitHub.*|_GitHub 제한.*)"
)


def fit_pr_body(
    body: str, limit: int = PR_BODY_LIMIT, *,
    notice: str = "_Shortened to fit GitHub's limit; full text retained in the state store._",
) -> str:
    """Keep structural content whole and shorten the largest expandable runs first.

    Callers retain the original before sending a shortened result and supply a
    notice with its durable location. Length is Python characters, not UTF-8 bytes.
    Bodies that fit are returned verbatim, making repeated fitting a no-op.
    """
    if len(body) <= limit:
        return body

    markers = list(_MARKERS.finditer(body))
    parts: list[tuple[bool, str]] = []
    offset = 0
    summary = False
    paragraph_started = False
    for line in body.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("## "):
            summary = stripped in {"## What changed", "## Summary", "## Review scope"}
            paragraph_started = False
        elif summary and stripped:
            paragraph_started = True
        elif summary and paragraph_started:
            summary = False
        end = offset + len(line)
        protected = (summary or bool(_REQUIRED_LINE.fullmatch(line.rstrip("\r\n")))
                     or any(match.start() < end and match.end() > offset for match in markers))
        parts.append((protected, line))
        offset = end

    parts = [(protected, "".join(content for _, content in group))
             for protected, group in groupby(parts, key=lambda part: part[0])]

    # One notice covers all shortened runs, and previous notices remain reachable
    # when a later review appends enough material to require another fitting.
    suffix = "\n\n" + notice + "\n"
    excess = len(body) + len(suffix) - limit
    for index in sorted(range(len(parts)), key=lambda i: len(parts[i][1]), reverse=True):
        protected, content = parts[index]
        if protected or excess <= 0:
            continue
        removed = min(excess, len(content))
        prefix = content[:len(content) - removed]
        # Never join a partial prose line to a following required line.
        if prefix and not prefix.endswith("\n"):
            prefix = prefix[:-1] + "\n"
        parts[index] = (False, prefix)
        excess -= removed
    result = "".join(content for _, content in parts)
    if excess > 0:
        # Even pathological mandatory content must never cause an oversized send.
        result = result[:max(0, limit - len(suffix))]
    return (result + suffix)[:limit]
