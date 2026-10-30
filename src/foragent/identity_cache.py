"""Machine-wide cache of the GitHub login behind the active gh credential (D78 revision).

Entries are keyed by a hash of the host and the credential gh would use, so a
different token or ``gh auth switch`` misses the cache and is read afresh. When the
active credential cannot be identified, nothing is cached or reused. The file stores
only hashes and logins, never a credential.
"""

import hashlib
import json
import os
from pathlib import Path
import tempfile

from foragent import capacity

CACHE_FILE = "github-identity.json"


def _gh_hosts_file() -> Path:
    if directory := os.environ.get("GH_CONFIG_DIR"):
        return Path(directory) / "hosts.yml"
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "gh" / "hosts.yml"


TOKEN_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")


def credential_key() -> str | None:
    """A hash of every input that decides which principal gh uses now, or None when unknown.

    gh picks its host from GH_HOST or its config, then a token from one of these
    variables (which one depends on the host kind, e.g. github.com and *.ghe.com use
    GH_TOKEN) or else from the signed-in account recorded in hosts.yml. Rather than
    re-deriving that choice, the key covers all of these inputs, so any change to
    any of them reads the login again.
    """
    environment = [f"{name}={os.environ.get(name, '')}" for name in ("GH_HOST", *TOKEN_VARIABLES)]
    try:
        # `gh auth login`/`switch`/`logout` rewrite this (the token may live in the keyring).
        hosts = _gh_hosts_file().read_text(errors="replace")
    except OSError:
        hosts = None
    if hosts is None and not any(os.environ.get(name) for name in TOKEN_VARIABLES):
        return None
    material = "\0".join([*environment, f"hosts={hosts}"])
    return hashlib.sha256(material.encode()).hexdigest()


def _path() -> Path:
    return capacity.machine_root() / CACHE_FILE


def _entries() -> dict:
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read(key: str) -> str | None:
    value = _entries().get(key)
    return value if isinstance(value, str) and value else None


def write(key: str, login: str) -> None:
    """Record ``login`` for ``key``; a cache that cannot be written only costs a later read."""
    entries = {k: v for k, v in _entries().items() if isinstance(v, str)}
    entries[key] = login
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".github-identity.")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(entries, handle, sort_keys=True)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except OSError:
        pass
