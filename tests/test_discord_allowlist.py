"""Tests for allowlist helpers."""

from __future__ import annotations

from tunapi.discord.allowlist import is_user_allowed, normalize_user_id_set


class TestNormalizeUserIdSet:
    def test_none(self) -> None:
        assert normalize_user_id_set(None) is None

    def test_empty(self) -> None:
        assert normalize_user_id_set([]) is None

    def test_single_int(self) -> None:
        assert normalize_user_id_set(123) == frozenset({123})

    def test_iterable_of_ints(self) -> None:
        assert normalize_user_id_set([1, 2, 3]) == frozenset({1, 2, 3})

    def test_iterable_of_digit_strings(self) -> None:
        assert normalize_user_id_set(["1", " 2 "]) == frozenset({1, 2})

    def test_ignores_bool(self) -> None:
        assert normalize_user_id_set([True, 1]) == frozenset({1})

    def test_invalid_type(self) -> None:
        assert normalize_user_id_set("123") is None


class TestIsUserAllowed:
    def test_unset_is_allowed(self) -> None:
        assert is_user_allowed(None, None)
        assert is_user_allowed(None, 123)

    def test_set_requires_membership(self) -> None:
        allowed = frozenset({1})
        assert is_user_allowed(allowed, 1)
        assert not is_user_allowed(allowed, 2)
        assert not is_user_allowed(allowed, None)
