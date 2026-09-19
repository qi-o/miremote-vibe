"""Runtime identity and isolation helpers."""

from __future__ import annotations

import ctypes
import errno
import os
import socket
import sys
from pathlib import Path


STABLE_APP_NAME = "MiRemoteVibe"
REALTIME_APP_NAME = "MiRemoteVibe-RealtimeDev"
RECOVERY_APP_NAME = "MiRemoteVibe-VoiceRecovery"

STABLE_GUI_MUTEX = "Local\\MiRemoteVibe.Gui"
RECOVERY_GUI_MUTEX = "Local\\MiRemoteVibe.VoiceRecovery.Gui"
REALTIME_GUI_MUTEX = "Local\\MiRemoteVibe.RealtimeDev.Gui"

RECOVERY_TITLE = "小米遥控器 · 语音恢复候选版"
RECOVERY_BOOT_NAME = "MiRemoteVibe-VoiceRecovery"
RECOVERY_SHOW_FLAG_NAME = "miremote_voice_recovery_show.flag"

TAP_LISTENER_PORT = 30685


def _env_bool(name: str):
    value = os.environ.get(name)
    if value is None:
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


def executable_stem() -> str:
    return Path(sys.executable).stem


def release_build() -> bool:
    """Set by the release-only PyInstaller runtime hook."""
    return bool(_env_bool("MIREMOTE_RECOVERY_RELEASE"))


def recovery_build() -> bool:
    if release_build():
        return False
    if _env_bool("MIREMOTE_RECOVERY_BUILD"):
        return True
    return "语音恢复候选版" in executable_stem()


def realtime_dev_build() -> bool:
    if release_build():
        return False
    if _env_bool("MIREMOTE_REALTIME_DEV"):
        return True
    return bool(
        getattr(sys, "frozen", False)
        and any(marker in executable_stem() for marker in ("实时实验版", "实时输入开发版"))
    )


def appdata_base() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home())))


def stable_app_data_dir() -> Path:
    return appdata_base() / STABLE_APP_NAME


def app_data_dir(source_root: Path) -> Path:
    if recovery_build():
        return appdata_base() / RECOVERY_APP_NAME
    if getattr(sys, "frozen", False):
        name = REALTIME_APP_NAME if realtime_dev_build() else STABLE_APP_NAME
        return appdata_base() / name
    return source_root


def stable_gui_mutex_exists() -> bool:
    if sys.platform != "win32":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_mutex = kernel32.OpenMutexW
    open_mutex.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p]
    open_mutex.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_bool

    synchronize = 0x00100000
    handle = open_mutex(synchronize, False, STABLE_GUI_MUTEX)
    if not handle:
        return ctypes.get_last_error() == 5
    close_handle(handle)
    return True


def tap_listener_conflict_reason(port: int = TAP_LISTENER_PORT) -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        return None
    except OSError as exc:
        if exc.errno in {errno.EADDRINUSE, errno.EACCES, 10013, 10048}:
            return "tap_listener_busy"
        return "tap_listener_unavailable"
    finally:
        sock.close()


def tap_listener_busy(port: int = TAP_LISTENER_PORT) -> bool:
    return tap_listener_conflict_reason(port) is not None


def candidate_conflict_reason() -> str | None:
    if not recovery_build():
        return None
    if stable_gui_mutex_exists():
        return "stable_gui_mutex"
    return tap_listener_conflict_reason()
