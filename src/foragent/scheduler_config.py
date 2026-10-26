"""The tick's fetch and the committed .foragent.yml it reads from origin/<base>."""

import sys

from foragent.config import load_committed_config
from foragent.git import GitError
from foragent.i18n import text


def fetch_base(ctx) -> None:
    """Fetch the base branch; for fa gate tick, then apply origin's committed config.

    The shared checkout is never advanced or modified: the file is read from the
    fetched ref. A missing or invalid committed file keeps the local config.
    """
    ctx.git.fetch("origin", ctx.config.base_branch)
    if not ctx.committed_config:
        return
    base_ref = f"origin/{ctx.config.base_branch}"
    try:
        sha = ctx.git.blob_sha(base_ref, ".foragent.yml")
        content = None if sha is None else ctx.git._call(["cat-file", "blob", sha])
    except GitError:
        content = None
    config, ignored = load_committed_config(ctx.config, content, f"{base_ref}:.foragent.yml")
    language = ctx.config.language
    if config is None:
        print(text("tick_config_fallback", language, ref=base_ref), file=sys.stderr)
        return
    for key in ignored:
        print(text("tick_config_key_ignored", language, ref=base_ref, key=key), file=sys.stderr)
    ctx.config = config
