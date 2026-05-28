from __future__ import annotations

from tunapi.ids import RESERVED_ENGINE_IDS, RESERVED_CHAT_COMMANDS


class TestReservedIdsPush:
    def test_engine_ids_exist(self):
        assert isinstance(RESERVED_ENGINE_IDS, (set, frozenset))

    def test_chat_commands_exist(self):
        assert isinstance(RESERVED_CHAT_COMMANDS, (set, frozenset, tuple, list))
