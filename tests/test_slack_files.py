from __future__ import annotations

from pathlib import Path
from unittest.mock import ANY, AsyncMock, patch

import httpx
import pytest
from tunapi.core.files import FilePutResult
from tunapi.slack.files import _download_slack_file, handle_file_get, handle_file_put

pytestmark = pytest.mark.anyio


class TestSlackFilesDownload:
    async def test_download_success(self):
        url = "https://slack.com/files/123"
        bot_token = "xoxb-test"

        mock_request = httpx.Request("GET", url)
        mock_response = httpx.Response(
            200, content=b"file content", request=mock_request
        )

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response
            data = await _download_slack_file(url, bot_token)
            assert data == b"file content"
            mock_get.assert_called_once_with(
                url,
                headers={"Authorization": "Bearer xoxb-test"},
                follow_redirects=True,
            )

    async def test_download_too_large(self):
        url = "https://slack.com/files/123"
        bot_token = "xoxb-test"

        mock_request = httpx.Request("GET", url)
        mock_response = httpx.Response(200, content=b"x" * 100, request=mock_request)

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response
            data = await _download_slack_file(url, bot_token, max_bytes=50)
            assert data is None

    async def test_download_exception(self):
        url = "https://slack.com/files/123"
        bot_token = "xoxb-test"

        with patch(
            "httpx.AsyncClient.get", side_effect=httpx.HTTPError("Network fail")
        ):
            data = await _download_slack_file(url, bot_token)
            assert data is None


class TestSlackHandleFilePut:
    async def test_put_no_url(self):
        files = [{"name": "test.txt"}]
        results = await handle_file_put(
            bot_token="tok",
            files=files,
            target_dir=Path("/tmp"),
        )
        assert len(results) == 1
        assert "no download URL" in results[0].message

    async def test_put_download_fails(self):
        files = [{"name": "test.txt", "url_private_download": "http://fail"}]

        with patch("tunapi.slack.files._download_slack_file", return_value=None):
            results = await handle_file_put(
                bot_token="tok",
                files=files,
                target_dir=Path("/tmp"),
            )
            assert len(results) == 1
            assert "failed to download" in results[0].message

    async def test_put_success(self):
        files = [{"name": "test.txt", "url_private_download": "http://ok"}]
        expected_result = FilePutResult(name="test.txt", path=Path("/tmp/test.txt"))

        with (
            patch("tunapi.slack.files._download_slack_file", return_value=b"hello"),
            patch(
                "tunapi.slack.files.save_file", return_value=expected_result
            ) as mock_save,
        ):
            results = await handle_file_put(
                bot_token="tok",
                files=files,
                target_dir=Path("/tmp"),
            )
            assert len(results) == 1
            assert results[0].name == "test.txt"
            mock_save.assert_called_once_with(
                "test.txt",
                b"hello",
                Path("/tmp"),
                deny_globs=ANY,
                max_bytes=ANY,
            )


class TestSlackHandleFileGet:
    async def test_get_delegates_to_read_file(self):
        expected = ("test.txt", None, b"content")
        with patch("tunapi.slack.files.read_file", return_value=expected) as mock_read:
            res = await handle_file_get(rel_path="test.txt", root=Path("/tmp"))
            assert res == expected
            mock_read.assert_called_once_with(
                "test.txt",
                Path("/tmp"),
                deny_globs=ANY,
                max_bytes=ANY,
            )
