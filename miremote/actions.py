"""动作执行：发送按键、聚焦窗口、音量、输入文本。

run 动作委托给 runner.py（argv 列表、shell=False）。
"""

from __future__ import annotations

import ctypes
import re
import time
from ctypes import wintypes as wt

from . import runner
from .keys import name_to_vk

user32 = ctypes.WinDLL("user32")

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD), ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wt.ULONG)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
        ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTU(ctypes.Union):
    # 必须包含全部三种输入结构：union 大小取最大者(MOUSEINPUT=32)，
    # 否则 INPUT 只有 32 字节，SendInput 会因 cbSize 不匹配而静默失败。
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("union", _INPUTU)]


def _tap(vk: int, up: bool = False):
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.union.ki.wVk = vk
    inp.union.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
    return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) == 1


def send_combo(names: list[str], hold_last_ms: int = 30):
    """按下修饰键组合 + 主键，例如 ["VK_CONTROL","VK_SHIFT","VK_M"]。"""
    vks = [name_to_vk(n) for n in names]
    if not vks:
        return
    try:
        for vk in vks:
            _tap(vk)
        time.sleep(hold_last_ms / 1000)
    finally:
        for vk in reversed(vks):
            _tap(vk, up=True)


def tap_key(name: str):
    vk = name_to_vk(name)
    _tap(vk)
    time.sleep(0.01)
    _tap(vk, up=True)


def volume(delta: int):
    """delta>0 增大音量，delta<0 减小，0 静音切换。"""
    if delta == 0:
        tap_key("VK_VOLUME_MUTE")
        return
    vk = name_to_vk("VK_VOLUME_UP") if delta > 0 else name_to_vk("VK_VOLUME_DOWN")
    for _ in range(abs(delta)):
        _tap(vk)
        time.sleep(0.01)
        _tap(vk, up=True)
        time.sleep(0.01)


# ---- 剪贴板输入文本 ----

kernel32 = ctypes.WinDLL("kernel32")
CF_UNICODETEXT = 13

# 句柄宽度敏感：GlobalAlloc/GlobalLock 返回 64 位指针，不声明 restype 会被
# ctypes 按 C int 截断成 32 位（曾导致 memmove 写空指针 access violation）
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = (ctypes.c_uint, ctypes.c_size_t)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
kernel32.GlobalFree.argtypes = (ctypes.c_void_p,)
user32.SetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = (ctypes.c_uint, ctypes.c_void_p)


def set_clipboard_text(text: str) -> bool:
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        n = (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)
        h = kernel32.GlobalAlloc(0x0002, n)  # GMEM_MOVEABLE
        if not h:
            return False
        p = kernel32.GlobalLock(h)
        if not p:
            kernel32.GlobalFree(h)
            return False
        try:
            ctypes.memmove(p, ctypes.create_unicode_buffer(text), n)
        finally:
            kernel32.GlobalUnlock(h)
        ok = bool(user32.SetClipboardData(CF_UNICODETEXT, h))
        if not ok:
            kernel32.GlobalFree(h)
        return ok
    finally:
        user32.CloseClipboard()


def type_text(text: str):
    """把文本写进当前焦点窗口（剪贴板 + Ctrl+V）。注意会覆盖剪贴板。"""
    set_clipboard_text(text)
    time.sleep(0.03)
    send_combo(["VK_CONTROL", "VK_V"])


# ---- 窗口聚焦 ----

EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def find_windows(title_regex: str) -> list[tuple[int, str]]:
    """按标题正则找可见顶层窗口。"""
    rx = re.compile(title_regex)
    found: list[tuple[int, str]] = []

    @EnumWindowsProc
    def cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        title = buf.value
        if title and rx.search(title):
            found.append((hwnd, title))
        return True

    user32.EnumWindows(cb, 0)
    return found


def close_windows(title_regex: str) -> int:
    """向匹配的可见顶层窗口发送 WM_CLOSE，返回成功发送数量。"""
    closed = 0
    for hwnd, _title in find_windows(title_regex):
        if user32.PostMessageW(hwnd, 0x0010, 0, 0):
            closed += 1
    return closed


def focus_window(title_regex: str) -> bool:
    """聚焦第一个匹配窗口；受前台锁定限制可能失败，返回是否成功。"""
    wins = find_windows(title_regex)
    if not wins:
        return False
    hwnd = wins[0][0]
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    return bool(user32.SetForegroundWindow(hwnd))


# ---- 动作分发 ----

def perform(action: dict) -> str:
    """执行配置里的一个动作对象，返回描述字符串（用于日志）。"""
    t = action.get("type")
    if t == "keys":
        combo = action.get("combo", [])
        send_combo(combo)
        return "+".join(combo)
    if t == "tap":
        key = action["key"]
        tap_key(key)
        return "tap " + key
    if t == "volume":
        delta = int(action.get("delta", 0))
        volume(delta)
        return "volume " + str(delta)
    if t == "focus":
        pattern = action["title_regex"]
        ok = focus_window(pattern)
        return "focus ok=" + str(ok)
    if t == "focus_then_keys":
        pattern = action.get("title_regex", ".")
        combo = action.get("combo", [])
        if focus_window(pattern):
            time.sleep(0.15)
            send_combo(combo)
            return "focus+" + "+".join(combo)
        return "focus 失败"
    if t == "type":
        text = action.get("text", "")
        type_text(text)
        return "type " + str(len(text)) + " chars"
    if t == "run":
        argv = action.get("argv", [])
        runner.launch(argv)
        return "run argv len=" + str(len(argv))
    if t == "voice":
        return "语音（由守护引擎处理）"
    if t == "none":
        return "no-op"
    return "未知动作类型 " + str(t)
