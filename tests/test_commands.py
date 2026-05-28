from __future__ import annotations


from tunapi.commands import get_command, list_command_ids


class TestListCommandIdsPush:
    def test_returns_list(self):
        ids = list_command_ids()
        assert isinstance(ids, list)

    def test_with_allowlist(self):
        all_ids = list_command_ids()
        if all_ids:
            filtered = list_command_ids(allowlist={all_ids[0]})
            assert len(filtered) <= len(all_ids)


class TestGetCommandPush:
    def test_nonexistent(self):
        result = get_command("totally_nonexistent_cmd_xyz", required=False)
        assert result is None
