from __future__ import annotations

from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock, patch
import pytest

from tunapi.core.files import FilePutResult
from tunapi.mattermost.files import handle_file_get, handle_file_put

pytestmark = pytest.mark.anyio


class TestMattermostHandleFilePut:
    async def test_put_success(self):
        mock_client = MagicMock()
        mock_client.get_file = AsyncMock(return_value=b"hello")

        mock_info = MagicMock()
        mock_info.name = "test.txt"
        mock_client._client.get_file_info = AsyncMock(return_value=mock_info)

        expected_result = FilePutResult(name="test.txt", path=Path("/tmp/test.txt"))

        with patch(
            "tunapi.mattermost.files.save_file", return_value=expected_result
        ) as mock_save:
            results = await handle_file_put(
                client=mock_client,
                channel_id="chan1",
                file_ids=["file123"],
                target_dir=Path("/tmp"),
            )

            assert len(results) == 1
            assert results[0].name == "test.txt"
            mock_client.get_file.assert_called_once_with("file123")
            mock_client._client.get_file_info.assert_called_once_with("file123")
            mock_save.assert_called_once_with(
                "test.txt",
                b"hello",
                Path("/tmp"),
                deny_globs=ANY,
                max_bytes=ANY,
            )

    async def test_put_download_fails(self):
        mock_client = MagicMock()
        mock_client.get_file = AsyncMock(return_value=None)
        mock_client._client.get_file_info = AsyncMock(return_value=None)

        results = await handle_file_put(
            client=mock_client,
            channel_id="chan1",
            file_ids=["file123"],
            target_dir=Path("/tmp"),
        )

        assert len(results) == 1
        assert "failed to download" in results[0].message
        mock_client.get_file.assert_called_once_with("file123")

    async def test_put_no_info_success(self):
        mock_client = MagicMock()
        mock_client.get_file = AsyncMock(return_value=b"no_info")
        del mock_client._client.get_file_info

        expected_result = FilePutResult(name="file123", path=Path("/tmp/file123"))

        with patch(
            "tunapi.mattermost.files.save_file", return_value=expected_result
        ) as mock_save:
            results = await handle_file_put(
                client=mock_client,
                channel_id="chan1",
                file_ids=["file123"],
                target_dir=Path("/tmp"),
            )

            assert len(results) == 1
            assert results[0].name == "file123"
            mock_save.assert_called_once_with(
                "file123",
                b"no_info",
                Path("/tmp"),
                deny_globs=ANY,
                max_bytes=ANY,
            )


class TestMattermostHandleFileGet:
    async def test_get_delegates_to_read_file(self):
        expected = ("test.txt", None, b"content")
        with patch(
            "tunapi.mattermost.files.read_file", return_value=expected
        ) as mock_read:
            res = await handle_file_get(
                client=None,
                channel_id="chan1",
                rel_path="test.txt",
                root=Path("/tmp"),
            )
            assert res == expected
            mock_read.assert_called_once_with(
                "test.txt",
                Path("/tmp"),
                deny_globs=ANY,
                max_bytes=ANY,
            )
