"""Tests for the pure rendering/resolution helpers of `tunapi service`.

launchctl side effects are not exercised here; these cover the generated
launcher/plist content and path resolution, which is where mistakes would
silently break a background install.
"""

from __future__ import annotations

from pathlib import Path

from tunapi.cli import service


def test_resolve_repo_root_points_at_pyproject():
    root = service.resolve_repo_root()
    assert (root / "pyproject.toml").is_file()


def test_base_path_entries_dedup_and_include_uv_dir(tmp_path):
    uv = tmp_path / "bin" / "uv"
    entries = service._base_path_entries(str(uv))
    assert str(uv.parent) == entries[0]
    assert "/usr/bin" in entries
    assert len(entries) == len(set(entries))  # de-duplicated


class TestRenderLauncher:
    def test_contains_repo_uv_and_exec(self):
        script = service.render_launcher(repo=Path("/srv/tunapi"), uv_path="/opt/uv")
        assert script.startswith("#!/bin/bash")
        assert 'cd "/srv/tunapi"' in script
        # Run via uv (holds the macOS local-network grant under launchd).
        assert 'exec "/opt/uv" run tunapi' in script

    def test_cleans_stale_locks_in_config_dir(self):
        script = service.render_launcher(repo=Path("/srv/tunapi"), uv_path="/opt/uv")
        cfg = service.config_dir()
        assert f'rm -f "{cfg}/"*.lock' in script

    def test_sets_path(self):
        script = service.render_launcher(repo=Path("/srv/tunapi"), uv_path="/opt/uv")
        assert "export PATH=" in script


class TestRenderPlist:
    def test_has_required_keys(self):
        plist = service.render_plist(
            launcher=Path("/x/launcher.sh"),
            repo=Path("/srv/tunapi"),
            log=Path("/var/log/tunapi.log"),
        )
        assert f"<string>{service.LABEL}</string>" in plist
        assert "<string>/x/launcher.sh</string>" in plist
        assert "<string>/srv/tunapi</string>" in plist  # WorkingDirectory
        assert "<string>/var/log/tunapi.log</string>" in plist
        assert "<key>RunAtLoad</key>" in plist
        assert "<key>KeepAlive</key>" in plist


def test_tunadish_port_defaults_to_8765(monkeypatch, tmp_path):
    # No config file → default port.
    monkeypatch.setattr(service, "config_dir", lambda: tmp_path)
    assert service._tunadish_port() == 8765


def test_tunadish_port_reads_config(monkeypatch, tmp_path):
    (tmp_path / "tunapi.toml").write_text(
        '[transports.tunadish]\nport = 9999\n', encoding="utf-8"
    )
    monkeypatch.setattr(service, "config_dir", lambda: tmp_path)
    assert service._tunadish_port() == 9999


def test_paths_live_under_home():
    home = Path.home()
    assert service.plist_path().is_relative_to(home)
    assert service.launcher_path().is_relative_to(home)
    assert service.log_path().is_relative_to(home)
