"""Allowlist helpers for Discord transport."""

from __future__ import annotations

from collections.abc import Iterable


def normalize_user_id_set(value: object) -> frozenset[int] | None:
    """Normalize a config value into a set of Discord user IDs.

    Accepts iterables of ints and/or digit strings. Returns None when the allowlist
    is unset or empty.
    """
    if value is None:
        return None

    if isinstance(value, int) and not isinstance(value, bool):
        return frozenset({value})

    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        return None

    user_ids: set[int] = set()
    for item in value:
        if isinstance(item, int) and not isinstance(item, bool):
            user_ids.add(item)
            continue
        if isinstance(item, str):
            stripped = item.strip()
            if stripped.isdigit():
                user_ids.add(int(stripped))

    return frozenset(user_ids) if user_ids else None


def is_user_allowed(
    allowed_user_ids: frozenset[int] | None, user_id: int | None
) -> bool:
    """Return True when the user passes the allowlist gate."""
    if not allowed_user_ids:
        return True
    return user_id in allowed_user_ids if user_id is not None else False
