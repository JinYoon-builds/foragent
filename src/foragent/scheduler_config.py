"""The tick's fetch and the committed .foragent.yml it reads from origin/<base>."""

import sys

import yaml

from foragent.config import load_committed_config
from foragent.git import GitError
from foragent.i18n import text


TICK_CONFIG_FILE = "tick-config.json"


def record_tick_config(store, source: str, content: str | None, *, unreadable: bool = False) -> None:
    """Keep the committed file the tick read, so read-only views apply the same config."""
    import json

    path = store.root / TICK_CONFIG_FILE
    value = {"source": source, "content": content, **({"unreadable": True} if unreadable else {})}
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


def _names_team(content: str) -> bool:
    """The file mentions the team setting in any spelling a reader can see, valid or not."""
    if "legacy_owner" in content:
        return True
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError:
        return False
    return isinstance(parsed, dict) and "legacy_owner" in parsed


def ownership_config(store, local):
    """The config ownership is decided with, and whether it may decide at all (#1004).

    A valid committed file wins over ``local``. No record yet (no tick has fetched) or no
    committed file keeps ``local``. Evidence that may hide a team setting decides nothing, so
    the singleton path never stands in for it: a record or origin file that cannot be read,
    or an invalid file while either side names ``legacy_owner``. Other invalid files keep ``local``.
    """
    import json

    try:
        path = store.root / TICK_CONFIG_FILE
    except (AttributeError, TypeError):  # A context without a state directory has no record.
        return local, True
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        content, source = value["content"], value["source"]
    except FileNotFoundError:
        return local, True
    except (OSError, ValueError, KeyError, TypeError):
        return local, False
    if content is None:
        return local, not value.get("unreadable")
    config, _ = load_committed_config(local, content, source)
    if config is not None:
        return config, True
    return local, not (getattr(local, "legacy_owner", None) is not None or _names_team(content))


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
        unreadable = False
    except GitError:
        content, unreadable = None, True
    config, ignored = load_committed_config(ctx.config, content, f"{base_ref}:.foragent.yml")
    record_tick_config(ctx.store, f"{base_ref}:.foragent.yml", content, unreadable=unreadable)
    language = ctx.config.language
    if config is None:
        usable = ownership_config(ctx.store, ctx.config)[1]
        print(text("tick_config_fallback" if usable else "tick_config_team_invalid", language, ref=base_ref),
              file=sys.stderr)
        return
    for key in ignored:
        print(text("tick_config_key_ignored", language, ref=base_ref, key=key), file=sys.stderr)
    ctx.config = config
