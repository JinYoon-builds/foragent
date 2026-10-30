"""Who ordered an issue, and whether this factory may act on it (D78 revision, 2026-10-30).

The one owner truth is a single ``by:<login>`` label stamped at publication.
Plan markers keep publication idempotent; they are not a second owner record.
An issue without the label is legacy work: it belongs to ``legacy_owner`` when the
repository names one, otherwise (the unchanged single-factory path) to this factory.
The exception is an issue whose body carries ``OWNED_MARKER``: it was created
under the owner-label contract, so a missing label means its owner was never
confirmed (or was removed), not that it is legacy work. It has no owner until a
person adds one ``by:<login>`` label, and no factory acts on it.
Logins compare lowercase, as GitHub treats them.
"""

from collections.abc import Iterable
import re

from foragent.i18n import text

PREFIX = "by:"
# Written in the creation request of every issue published under the owner-label contract.
# It names no owner; it only says that a by: label is expected (see module docstring).
OWNED_MARKER = "<!-- foragent:owner-label -->"
LABEL_COLOR = "c5def5"
# GitHub user logins: alphanumerics and single inner hyphens, at most 39 characters.
_LOGIN = re.compile(r"[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){0,38}")


class OwnershipError(ValueError):
    """Ownership is ambiguous, unknown, or belongs to another factory."""


def normalize_login(value: object, language: str = "en") -> str:
    """The lowercase GitHub login, or OwnershipError when ``value`` cannot be one."""
    login = value.strip().lower() if isinstance(value, str) else ""
    if not _LOGIN.fullmatch(login):
        raise OwnershipError(text("ownership_invalid_login", language, value=value))
    return login


def owner_label(login: str) -> str:
    return PREFIX + normalize_login(login)


def label_description(login: str) -> str:
    return f"Foragent order published by {normalize_login(login)}; only that person's factory works it."


def parse_orderer(labels: Iterable[str], language: str = "en") -> str | None:
    """The one normalized orderer, None for legacy work, OwnershipError for contradictions."""
    owners = sorted({label[len(PREFIX):].strip().lower() for label in labels
                     if label.lower().startswith(PREFIX)})
    if len(owners) > 1:
        raise OwnershipError(text("ownership_ambiguous", language, owners=", ".join(owners)))
    return normalize_login(owners[0], language) if owners else None


def effective_orderer(issue, config, factory_login: str | None) -> str:
    """The person whose factory works ``issue``.

    Legacy work belongs to ``config.legacy_owner``; with no setting, the unchanged
    singleton path keeps it with this factory. An unlabelled issue published under
    the owner-label contract is not legacy work and has no owner (OwnershipError).
    """
    language = getattr(config, "language", "en")
    owner = parse_orderer(issue.labels, language)
    if owner is not None:
        return owner
    if OWNED_MARKER in (getattr(issue, "body", None) or ""):
        raise OwnershipError(text("ownership_unconfirmed", language, number=issue.number))
    legacy = getattr(config, "legacy_owner", None)
    if legacy is not None:
        return normalize_login(legacy, language)
    if factory_login is None:
        raise OwnershipError(text("ownership_unknown_factory", language))
    return normalize_login(factory_login, language)


def require_owned(issue, config, factory_login: str | None) -> None:
    """Refuse an effect on ``issue`` unless this factory is its effective orderer."""
    language = getattr(config, "language", "en")
    if factory_login is None:
        raise OwnershipError(text("ownership_unknown_factory", language))
    factory = normalize_login(factory_login, language)
    owner = effective_orderer(issue, config, factory)
    if owner != factory:
        raise OwnershipError(text("ownership_foreign", language, number=issue.number,
                                  owner=owner, factory=factory))
