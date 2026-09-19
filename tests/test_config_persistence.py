import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from miremote import service  # noqa: E402


def make_service(config_file: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(service, "config_path", lambda: config_file)
    monkeypatch.setattr(service, "realtime_dev_build", lambda: False)
    return service.MiRemoteService(on_log=lambda _message: None)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_load_config_deep_copies_missing_default_sections(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"keys": {}}, ensure_ascii=False), encoding="utf-8")

    first = make_service(config_file, monkeypatch)
    first.config["device"]["vid"] = "MUTATED"
    first.config["keys"]["VK_UP"]["label"] = "mutated"

    second = make_service(config_file, monkeypatch)

    assert second.config["device"]["vid"] == "2717"
    assert second.config["keys"]["VK_UP"]["label"] == "↑"
    assert service.DEFAULT_CONFIG["device"]["vid"] == "2717"
    assert service.DEFAULT_CONFIG["keys"]["VK_UP"]["label"] == "↑"


def test_save_config_success_replaces_file_and_updates_memory(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    original = {"voice_mode": "local", "keys": {"VK_F5": {"label": "语音"}}}
    config_file.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    svc = make_service(config_file, monkeypatch)

    updated = {"voice_mode": "wechat", "keys": {"VK_F5": {"label": "voice"}}}
    svc.save_config(updated)

    assert read_json(config_file) == updated
    assert svc.config == updated
    assert list(tmp_path.glob("config.json.*.tmp")) == []


def test_save_config_fsync_failure_keeps_old_file_and_memory(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    original = {"voice_mode": "local", "keys": {"VK_F5": {"label": "语音"}}}
    config_file.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    svc = make_service(config_file, monkeypatch)
    before_memory = json.loads(json.dumps(svc.config, ensure_ascii=False))

    def fail_fsync(_fd):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(service.os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="simulated fsync failure"):
        svc.save_config({"voice_mode": "wechat", "keys": {}})

    assert read_json(config_file) == original
    assert svc.config == before_memory
    assert list(tmp_path.glob("config.json.*.tmp")) == []


def test_save_config_replace_failure_keeps_old_file_and_memory(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    original = {"voice_mode": "local", "keys": {"VK_F5": {"label": "语音"}}}
    config_file.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    svc = make_service(config_file, monkeypatch)
    before_memory = json.loads(json.dumps(svc.config, ensure_ascii=False))

    def fail_replace(_src, _dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(service.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        svc.save_config({"voice_mode": "wechat", "keys": {}})

    assert read_json(config_file) == original
    assert svc.config == before_memory
    assert list(tmp_path.glob("config.json.*.tmp")) == []
