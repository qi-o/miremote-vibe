import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from miremote import runtime, service

@pytest.fixture
def release(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setenv('APPDATA', str(tmp_path))
    monkeypatch.setenv('MIREMOTE_RECOVERY_RELEASE', '1')
    monkeypatch.setenv('MIREMOTE_RECOVERY_BUILD', '1')
    monkeypatch.setenv('MIREMOTE_REALTIME_DEV', '1')
    return tmp_path

def test_release_identity_wins(release):
    assert runtime.release_build()
    assert not runtime.recovery_build()
    assert not runtime.realtime_dev_build()
    assert service.app_data_dir() == release / 'MiRemoteVibe'

def test_release_preserves_config_and_logs(release, monkeypatch):
    monkeypatch.setenv('MIREMOTE_WECHAT_LIVE', '1')
    path = release / 'MiRemoteVibe' / 'config.json'
    path.parent.mkdir()
    keys = {'VK_F5': {'label': 'custom', 'on_down': {'type': 'voice'}}}
    path.write_text(json.dumps(dict(voice_mode='wechat', keys=keys, hide_tray=True,
        auto_start_service=True, wechat_live=True, wechat_live2=True)), encoding='utf-8')
    svc = service.MiRemoteService(on_log=lambda _: None)
    assert svc.config['keys']['VK_F5'] == keys['VK_F5']
    assert svc.config['voice_mode'] == 'wechat'
    assert svc.config['hide_tray'] is True
    assert svc.config['auto_start_service'] is True
    assert svc.config['wechat_live'] is False
    assert svc.config['wechat_live2'] is False
    assert svc._recovery_log is not None
    assert svc.status()['recovery_log'] == str(path.parent / 'logs' / 'voice-recovery.jsonl')
    svc._recovery_log.close()

def test_release_gui_boot_uses_stable_identity(release, monkeypatch):
    gui = importlib.import_module('miremote.gui')
    registry = MagicMock()
    monkeypatch.setitem(sys.modules, 'winreg', registry)
    importlib.reload(gui)
    try:
        assert gui._SINGLE_INSTANCE_MUTEX == runtime.STABLE_GUI_MUTEX
        assert gui._BOOT_RUN_NAME == 'MiRemoteVibe'
        assert gui._WINDOW_TITLE == '小米遥控器 · 控制台'
        assert gui._set_boot_launch(True)
        registry.SetValueEx.assert_called_once()
        assert registry.SetValueEx.call_args.args[1] == 'MiRemoteVibe'
        assert registry.SetValueEx.call_args.args[-1].endswith(' --silent')
    finally:
        monkeypatch.delenv('MIREMOTE_RECOVERY_RELEASE')
        monkeypatch.delenv('MIREMOTE_RECOVERY_BUILD')
        monkeypatch.delenv('MIREMOTE_REALTIME_DEV')
        importlib.reload(gui)

def test_release_runtime_report_identity(release, monkeypatch):
    gui = importlib.import_module('miremote.gui')
    from miremote import runtime_check
    importlib.reload(gui)
    try:
        result = runtime_check.check_runtime()
        assert result['profile'] == 'release'
        assert result['checks']['profile_identity']
        assert result['checks']['window_identity']
        assert result['boot_run_name'] == 'MiRemoteVibe'
        assert result['profile_directory'] == str(release / 'MiRemoteVibe')
        assert result['hardware_started'] is False
    finally:
        monkeypatch.delenv('MIREMOTE_RECOVERY_RELEASE')
        monkeypatch.delenv('MIREMOTE_RECOVERY_BUILD')
        monkeypatch.delenv('MIREMOTE_REALTIME_DEV')
        importlib.reload(gui)
