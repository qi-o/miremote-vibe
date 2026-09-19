import importlib
import json
import os
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from miremote import service  # noqa: E402
from miremote import runtime
_tap_conflict_check = runtime.tap_listener_conflict_reason


@pytest.fixture(autouse=True)
def isolated_profile(tmp_path, monkeypatch):
    monkeypatch.setenv('APPDATA', str(tmp_path))
    monkeypatch.setattr(runtime, 'stable_gui_mutex_exists', lambda: False)
    monkeypatch.setattr(runtime, 'tap_listener_conflict_reason', lambda: None)


def _write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def test_candidate_uses_isolated_appdata_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("MIREMOTE_RECOVERY_BUILD", "1")

    assert service.recovery_build()
    assert service.app_data_dir() == tmp_path / "MiRemoteVibe-VoiceRecovery"
    assert service.config_path() == tmp_path / "MiRemoteVibe-VoiceRecovery" / "config.json"


def test_candidate_reads_stable_once_without_saving_to_stable(tmp_path, monkeypatch):
    stable_config = tmp_path / "MiRemoteVibe" / "config.json"
    _write_json(
        stable_config,
        {
            "voice_mode": "wechat",
            "wechat_live": True,
            "wechat_live2": True,
            "auto_start_service": True,
            "hide_tray": True,
            "keys": {"VK_F5": {"label": "MyVoice", "on_down": {"type": "voice"}}},
        },
    )
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("MIREMOTE_RECOVERY_BUILD", "1")

    svc = service.MiRemoteService(on_log=lambda _message: None)

    assert svc.config["voice_mode"] == "wechat"
    assert svc.config["keys"]["VK_F5"]["label"] == "MyVoice"
    assert svc.config["auto_start_service"] is False
    assert svc.config["hide_tray"] is False
    assert svc.config["wechat_live"] is False
    assert svc.config["wechat_live2"] is False
    assert not (tmp_path / "MiRemoteVibe-VoiceRecovery" / "config.json").exists()

    svc.config["voice_mode"] = "local"
    svc.save_config()

    candidate_config = tmp_path / "MiRemoteVibe-VoiceRecovery" / "config.json"
    assert candidate_config.exists()
    assert json.loads(stable_config.read_text(encoding="utf-8"))["voice_mode"] == "wechat"
    assert json.loads(candidate_config.read_text(encoding="utf-8"))["voice_mode"] == "local"


def test_candidate_safety_flags_cannot_be_overridden_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("MIREMOTE_RECOVERY_BUILD", "1")
    monkeypatch.setenv("MIREMOTE_WECHAT_LIVE", "1")
    monkeypatch.setenv("MIREMOTE_HIDE_TRAY", "1")
    monkeypatch.setenv("MIREMOTE_AUTO_START_SERVICE", "1")

    svc = service.MiRemoteService(on_log=lambda _message: None)

    assert svc.config["wechat_live"] is False
    assert svc.config["wechat_live2"] is False
    assert svc.config["hide_tray"] is False
    assert svc.config["auto_start_service"] is False


def test_candidate_start_is_blocked_when_stable_mutex_exists(monkeypatch):
    monkeypatch.setenv("MIREMOTE_RECOVERY_BUILD", "1")
    monkeypatch.setattr(runtime, "stable_gui_mutex_exists", lambda: True)

    called = False

    def fake_start_inner(self):
        nonlocal called
        called = True

    monkeypatch.setattr(service.MiRemoteService, "_start_inner", fake_start_inner)

    svc = service.MiRemoteService(on_log=lambda _message: None)

    assert svc.start() is False
    assert called is False
    assert svc.running is False


def test_candidate_start_is_blocked_when_tap_listener_busy(monkeypatch):
    monkeypatch.setenv("MIREMOTE_RECOVERY_BUILD", "1")
    monkeypatch.setattr(runtime, "tap_listener_conflict_reason", lambda: 'tap_listener_busy')

    called = False

    def fake_start_inner(self):
        nonlocal called
        called = True

    monkeypatch.setattr(service.MiRemoteService, "_start_inner", fake_start_inner)

    svc = service.MiRemoteService(on_log=lambda _message: None)

    assert svc.start() is False
    assert called is False
    assert svc.running is False


def test_candidate_tap_listener_check_uses_bind_not_connect(monkeypatch):
    # Restore only this function so the real implementation receives a fake socket.
    from unittest import mock
    import errno
    fake = mock.Mock()
    fake.bind.side_effect = OSError(errno.EADDRINUSE, 'busy')
    monkeypatch.setattr(runtime.socket, 'socket', lambda *args: fake)
    assert _tap_conflict_check() == 'tap_listener_busy'
    fake.bind.assert_called_once_with(('127.0.0.1', 30685))
    fake.connect.assert_not_called()
    fake.close.assert_called_once()


def test_candidate_gui_identity_and_boot_prevention(monkeypatch):
    monkeypatch.setenv("MIREMOTE_RECOVERY_BUILD", "1")
    sys.modules.pop("miremote.gui", None)
    gui = importlib.import_module("miremote.gui")

    assert gui._WINDOW_TITLE == "小米遥控器 · 语音恢复候选版"
    assert gui._SINGLE_INSTANCE_MUTEX == "Local\\MiRemoteVibe.VoiceRecovery.Gui"
    assert gui._BOOT_RUN_NAME == "MiRemoteVibe-VoiceRecovery"
    assert "voice_recovery" in gui._SHOW_FLAG
    assert gui._boot_launch_enabled() is False
    assert gui._set_boot_launch(True) is False
    assert gui._set_boot_launch(False) is False

    sys.modules.pop("miremote.gui", None)
