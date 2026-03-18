from pathlib import Path

import pytest

from tunapi.config import ConfigError
from tunapi.settings import TunapiSettings, validate_settings_data


def test_settings_strips_and_expands_transport_config(tmp_path: Path) -> None:
    settings = TunapiSettings.model_validate(
        {
            "transport": " telegram ",
            "plugins": {"enabled": [" foo "]},
            "transports": {"telegram": {"bot_token": "  token  ", "chat_id": 123}},
        }
    )

    assert settings.transport == "telegram"
    assert settings.plugins.enabled == ["foo"]
    assert settings.transports.telegram.bot_token == "token"


def test_settings_rejects_bool_chat_id(tmp_path: Path) -> None:
    data = {
        "transport": "telegram",
        "transports": {"telegram": {"bot_token": "token", "chat_id": True}},
    }

    with pytest.raises(ConfigError, match="chat_id"):
        validate_settings_data(data, config_path=tmp_path / "tunapi.toml")


def test_settings_rejects_float_chat_id(tmp_path: Path) -> None:
    data = {
        "transport": "telegram",
        "transports": {"telegram": {"bot_token": "token", "chat_id": 123.0}},
    }

    with pytest.raises(ConfigError, match="chat_id"):
        validate_settings_data(data, config_path=tmp_path / "tunapi.toml")
