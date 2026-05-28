from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tunapi.backends import EngineBackend
from tunapi.mattermost.onboarding import check_setup, interactive_setup

pytestmark = pytest.mark.anyio


class TestMattermostCheckSetup:
    def test_missing_config(self):
        with patch(
            "tunapi.mattermost.onboarding.load_settings_if_exists", return_value=None
        ):
            engine = MagicMock(spec=EngineBackend)
            res = check_setup(engine)
            assert len(res.issues) == 1
            assert "Missing config file" in res.issues[0].lines[0]

    def test_wrong_transport(self):
        settings = MagicMock()
        settings.transport = "slack"
        settings.transports.mattermost = None
        with patch(
            "tunapi.mattermost.onboarding.load_settings_if_exists",
            return_value=(settings, Path("/tmp/cfg")),
        ):
            engine = MagicMock(spec=EngineBackend)
            engine.cli_cmd = None
            res = check_setup(engine, transport_override=None)
            assert any(
                "expected 'mattermost'" in line
                for issue in res.issues
                for line in issue.lines
            )

    def test_missing_mattermost_section(self):
        settings = MagicMock()
        settings.transport = "mattermost"
        settings.transports.mattermost = None
        with patch(
            "tunapi.mattermost.onboarding.load_settings_if_exists",
            return_value=(settings, Path("/tmp/cfg")),
        ):
            engine = MagicMock(spec=EngineBackend)
            engine.cli_cmd = None
            res = check_setup(engine)
            assert any(
                "Missing [transports.mattermost] section" in line
                for issue in res.issues
                for line in issue.lines
            )

    def test_missing_url_and_token(self):
        settings = MagicMock()
        settings.transport = "mattermost"
        mm_config = MagicMock()
        mm_config.url = ""
        mm_config.token = ""
        settings.transports.mattermost = mm_config
        with patch(
            "tunapi.mattermost.onboarding.load_settings_if_exists",
            return_value=(settings, Path("/tmp/cfg")),
        ):
            engine = MagicMock(spec=EngineBackend)
            engine.cli_cmd = None
            res = check_setup(engine)
            assert any(
                "Missing transports.mattermost.url" in line
                for issue in res.issues
                for line in issue.lines
            )
            assert any(
                "Missing transports.mattermost.token" in line
                for issue in res.issues
                for line in issue.lines
            )

    def test_missing_engine_cli(self):
        settings = MagicMock()
        settings.transport = "mattermost"
        mm_config = MagicMock()
        mm_config.url = "http://mm"
        mm_config.token = "tok"
        settings.transports.mattermost = mm_config

        with (
            patch(
                "tunapi.mattermost.onboarding.load_settings_if_exists",
                return_value=(settings, Path("/tmp/cfg")),
            ),
            patch("shutil.which", return_value=None),
        ):
            engine = MagicMock(spec=EngineBackend)
            engine.id = "mock-engine"
            engine.cli_cmd = "mock-cli"
            engine.install_cmd = "npm i mock-cli"
            res = check_setup(engine)
            assert any(
                "Engine CLI `mock-cli` not found on PATH" in line
                for issue in res.issues
                for line in issue.lines
            )

    def test_setup_ok(self):
        settings = MagicMock()
        settings.transport = "mattermost"
        mm_config = MagicMock()
        mm_config.url = "http://mm"
        mm_config.token = "tok"
        settings.transports.mattermost = mm_config

        with (
            patch(
                "tunapi.mattermost.onboarding.load_settings_if_exists",
                return_value=(settings, Path("/tmp/cfg")),
            ),
            patch("shutil.which", return_value="/usr/bin/mock-cli"),
        ):
            engine = MagicMock(spec=EngineBackend)
            engine.cli_cmd = "mock-cli"
            res = check_setup(engine)
            assert len(res.issues) == 0


class TestMattermostInteractiveSetup:
    async def test_already_configured_and_decline_reconfigure(self):
        config = {
            "transport": "mattermost",
            "transports": {"mattermost": {"url": "http://mm", "token": "tok"}},
        }
        confirm_mock = MagicMock()
        confirm_mock.ask_async = AsyncMock(return_value=False)

        with (
            patch(
                "tunapi.mattermost.onboarding.load_or_init_config",
                return_value=(config, Path("/tmp/cfg")),
            ),
            patch("questionary.confirm", return_value=confirm_mock),
        ):
            res = await interactive_setup(force=False)
            assert res is False

    async def test_no_url_entered(self):
        config = {}
        text_mock = MagicMock()
        text_mock.ask_async = AsyncMock(return_value="")

        with (
            patch(
                "tunapi.mattermost.onboarding.load_or_init_config",
                return_value=(config, Path("/tmp/cfg")),
            ),
            patch("questionary.text", return_value=text_mock),
        ):
            res = await interactive_setup(force=False)
            assert res is False

    async def test_no_token_entered(self):
        config = {}
        url_text_mock = MagicMock()
        url_text_mock.ask_async = AsyncMock(return_value="http://mm/")
        token_text_mock = MagicMock()
        token_text_mock.ask_async = AsyncMock(return_value="")

        with (
            patch(
                "tunapi.mattermost.onboarding.load_or_init_config",
                return_value=(config, Path("/tmp/cfg")),
            ),
            patch("questionary.text") as mock_text,
        ):
            mock_text.side_effect = [url_text_mock, token_text_mock]
            res = await interactive_setup(force=False)
            assert res is False

    async def test_success_configuration(self):
        config = {}
        url_text_mock = MagicMock()
        url_text_mock.ask_async = AsyncMock(return_value=" http://mm/ ")
        token_text_mock = MagicMock()
        token_text_mock.ask_async = AsyncMock(return_value=" tok ")

        with (
            patch(
                "tunapi.mattermost.onboarding.load_or_init_config",
                return_value=(config, Path("/tmp/cfg")),
            ),
            patch("questionary.text") as mock_text,
            patch("tunapi.mattermost.onboarding.write_config") as mock_write,
        ):
            mock_text.side_effect = [url_text_mock, token_text_mock]
            res = await interactive_setup(force=True)
            assert res is True
            assert config["transport"] == "mattermost"
            assert config["transports"]["mattermost"]["url"] == "http://mm"
            assert config["transports"]["mattermost"]["token"] == "tok"
            mock_write.assert_called_once_with(config, Path("/tmp/cfg"))
