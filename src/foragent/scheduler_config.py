"""The tick's fetch and the committed .foragent.yml it reads from origin/<base>."""

import sys

from foragent.config import load_committed_config
from foragent.git import GitError
from foragent.i18n import text


TICK_CONFIG_FILE = "tick-config.json"


def record_tick_config(store, source: str, content: str | None) -> None:
    """Keep the committed file the tick read, so read-only views apply the same config."""
    import json

    path = store.root / TICK_CONFIG_FILE
    value = {"source": source, "content": content}
    try:
        if json.loads(path.read_text(encoding="utf-8")) == value:
            return  # Unchanged: no write on every tick.
    except (OSError, ValueError):
        pass
    store._write_json(path, value)


def tick_config(store, local, global_path=None):
    """The config the last tick admitted work with: the recorded committed file over ``local``.

    ``global_path`` is the global config to merge (a frozen copy for read-only views).
    """
    import json

    try:
        value = json.loads((store.root / TICK_CONFIG_FILE).read_text(encoding="utf-8"))
        config, _ = load_committed_config(local, value["content"], value["source"], global_path)
    except (OSError, ValueError, KeyError, TypeError):
        return local
    return config or local


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
    record_tick_config(ctx.store, f"{base_ref}:.foragent.yml", content)
    language = ctx.config.language
    if config is None:
        print(text("tick_config_fallback", language, ref=base_ref), file=sys.stderr)
        return
    for key in ignored:
        print(text("tick_config_key_ignored", language, ref=base_ref, key=key), file=sys.stderr)
    ctx.config = config
