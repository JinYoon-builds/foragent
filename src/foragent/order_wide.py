"""Refuse publishing an issue whose preread expects more changed files than ``issue_max_files`` (#2352).

The latest preread of an issue's current content carries the lane's expected size. When its
file count is above the setting, the issue is too wide for one session: publication stops
before any write and names the issue and the count, so the plan splits it first. Every
changed path counts, mechanical moves included. An issue without an estimate keeps today's
publication; the limit applies per issue, never to the plan total.
"""

from collections.abc import Callable, Iterable


def check(items: Iterable[dict], record_for: Callable[[dict], dict | None], limit: int) -> None:
    """Raise ``ValueError`` naming every issue whose expected file count is above ``limit``."""
    wide = []
    for item in items:
        record = record_for(item)
        size = record.get("size") if isinstance(record, dict) else None
        files = size.get("file_count") if isinstance(size, dict) else None
        if type(files) is int and files > limit:
            wide.append(f"{item['key']} ({item['title']}) expects {files} files")
    if wide:
        raise ValueError(
            f"cannot publish: issue(s) wider than issue_max_files={limit}: {'; '.join(wide)}. "
            "Split each into issues expected to change at most "
            f"{limit} files, then run fa order preread PLAN.json and publish again")
