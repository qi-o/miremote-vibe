"""Validate a candidate package without starting GUI, BLE, audio or HID hooks."""
from __future__ import annotations

import hashlib
import importlib
import json
import lzma
from pathlib import Path
import sys


def check_runtime() -> dict:
    from . import __version__, tapinject
    from .runtime import recovery_build, release_build, candidate_conflict_reason
    from .service import app_data_dir

    checks = {}
    errors = {}
    modules = {}
    for module in (
        'PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets',
        'winrt.windows.devices.bluetooth',
        'winrt.windows.devices.bluetooth.genericattributeprofile',
        'winrt.windows.storage.streams', 'sounddevice',
        'miremote.gui', 'miremote.eventlog', 'miremote.runtime',
    ):
        try:
            modules[module] = importlib.import_module(module)
            checks[module] = True
        except Exception as exc:
            checks[module] = False
            errors[module] = f'{type(exc).__name__}: {exc}'
    bluetooth = modules.get('winrt.windows.devices.bluetooth')
    checks['connection_status_api'] = hasattr(
        getattr(bluetooth, 'BluetoothLEDevice', None), 'add_connection_status_changed')
    try:
        archive = tapinject.GADGET_ARCHIVE.read_bytes()
        checks['gadget_archive_hash'] = hashlib.sha256(archive).hexdigest() == tapinject.GADGET_ARCHIVE_SHA256
        checks['gadget_dll_hash'] = hashlib.sha256(lzma.decompress(archive)).hexdigest() == tapinject.GADGET_DLL_SHA256
    except (OSError, lzma.LZMAError) as exc:
        checks['gadget_archive_hash'] = False
        errors['gadget'] = f'{type(exc).__name__}: {exc}'
    profile = 'release' if release_build() else ('candidate' if recovery_build() else 'source')
    gui = modules.get('miremote.gui')
    candidate = profile == 'candidate'
    checks['profile_identity'] = (
        getattr(gui, '_SINGLE_INSTANCE_MUTEX', '') ==
        ('Local\\MiRemoteVibe.VoiceRecovery.Gui' if candidate else 'Local\\MiRemoteVibe.Gui'))
    checks['window_identity'] = getattr(gui, '_WINDOW_TITLE', '') == (
        '小米遥控器 · 语音恢复候选版' if candidate else '小米遥控器 · 控制台')
    return {
        'ok': all(checks.values()), 'version': __version__,
        'profile': profile,
        'boot_run_name': getattr(gui, '_BOOT_RUN_NAME', None),
        'gui_mutex': getattr(gui, '_SINGLE_INSTANCE_MUTEX', None),
        'frozen': bool(getattr(sys, 'frozen', False)),
        'checks': checks, 'errors': errors,
        'profile_directory': str(app_data_dir()),
        'hardware_conflict': candidate_conflict_reason(),
        'hardware_started': False,
    }


def main(args=None):
    args = sys.argv[2:] if args is None else args
    if len(args) != 1:
        raise SystemExit('Usage: --runtime-check OUTPUT.json')
    result = check_runtime()
    destination = Path(args[0])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if result['ok'] else 1
