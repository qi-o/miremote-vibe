"""动作执行：发送按键、聚焦窗口、音量、输入文本。

run 动作委托给 runner.py（argv 列表、shell=False）。
"""

from __future__ import annotations

import ctypes
import os
import re
import threading
import time
import winreg
from ctypes import wintypes as wt
from pathlib import Path

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


def hold_chord_spaced(names: list[str], gap_ms: float = 80.0,
                      down: bool = True) -> bool:
    """按下/松开一个"按住式"和弦,键与键之间留时间间隔。

    依据 SayAll 2026-09-04 受控实验(A/B/AB 交替):WeType 拒绝零间隔批量注入的
    和弦,逐事件、间隔 80ms 才被接受;任一键发送失败时尽力回滚已送达键,
    保证按住的热键不粘死。down=False 时按相反顺序松开(同样间隔)。
    """
    vks = [name_to_vk(n) for n in names]
    if not vks:
        return False
    if not down:
        vks = list(reversed(vks))
    delivered: list[int] = []
    for index, vk in enumerate(vks):
        if index > 0 and gap_ms > 0:
            time.sleep(gap_ms / 1000.0)
        if not _tap(vk, up=not down):
            # 回滚:把已送达的键反向释放,不留粘键。
            for sent in reversed(delivered):
                _tap(sent, up=down)
            return False
        delivered.append(vk)
    return True


# ---- 鼠标动作(v2.0,对齐 SayAll 的 Scroll/MouseClick/MouseMove 功能面) ----

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800


def _mouse(flags: int, dx: int = 0, dy: int = 0, wheel: int = 0) -> bool:
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.union.mi.dx = dx
    inp.union.mi.dy = dy
    inp.union.mi.mouseData = wheel
    inp.union.mi.dwFlags = flags
    return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) == 1


def validate_amount(value: int, maximum: int) -> int:
    """滚轮格数/移动像素的合法域校验(1..=maximum);越界抛 ValueError。"""
    if not 1 <= value <= maximum:
        raise ValueError(f"数值须在 1..={maximum}: {value}")
    return value


def scroll(direction: str, steps: int = 1) -> str:
    """滚轮:direction="up"/"down",每次 1..=100 格(格数≠像素)。"""
    steps = validate_amount(int(steps), 100)
    wheel = steps if direction == "up" else -steps
    ok = _mouse(MOUSEEVENTF_WHEEL, wheel=wheel)
    return f"滚轮{'上' if direction == 'up' else '下'} {steps} 格" + ("" if ok else "(失败)")


_MOUSE_BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def mouse_click(kind: str = "left") -> str:
    """鼠标点击:left/right/middle/double_left;按下与松开成对提交。"""
    if kind == "double_left":
        mouse_click("left")
        time.sleep(0.05)
        return mouse_click("left")
    down_flag, up_flag = _MOUSE_BUTTON_FLAGS[kind]
    ok = _mouse(down_flag) and _mouse(up_flag)
    return f"鼠标{kind}单击" + ("" if ok else "(失败)")


def mouse_move(direction: str, distance: int = 100) -> str:
    """指针相对移动(物理像素,DPI 缩放由系统处理):上下左右,1..=2000。"""
    distance = validate_amount(int(distance), 2000)
    deltas = {
        "up": (0, -distance), "down": (0, distance),
        "left": (-distance, 0), "right": (distance, 0),
    }
    dx, dy = deltas[direction]
    ok = _mouse(MOUSEEVENTF_MOVE, dx=dx, dy=dy)
    return f"指针{direction} {distance}px" + ("" if ok else "(失败)")


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
    if not set_clipboard_text(text):
        raise RuntimeError("剪贴板写入失败，已取消粘贴")
    time.sleep(0.03)
    send_combo(["VK_CONTROL", "VK_V"])


def get_clipboard_text() -> str | None:
    """读剪贴板文本（CF_UNICODETEXT）；无文本/失败返回 None。"""
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = (ctypes.c_uint,)
    if not user32.OpenClipboard(None):
        return None
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


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


# ---- 语义动作(beta 新增,思路来自 RC003 的 ActionKind)----
# 配置里写 {"type": "show_desktop"} 这类语义名,而不是具体按键串,
# 以后调整实现不影响用户配置。

def _app_switcher():
    """Alt+Tab:按住 Alt、点 Tab、松 Alt。"""
    _tap(name_to_vk("VK_MENU"))
    time.sleep(0.03)
    _tap(name_to_vk("VK_TAB"))
    time.sleep(0.03)
    _tap(name_to_vk("VK_TAB"), up=True)
    _tap(name_to_vk("VK_MENU"), up=True)


def _resolve_app_path(exe: str) -> str | None:
    """从注册表 App Paths(HKCU/HKLM)解析 exe 绝对路径,失败返回 None。"""
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(
                root,
                r"Software\Microsoft\Windows\CurrentVersion\App Paths\\" + exe,
            ) as k:
                val, _t = winreg.QueryValueEx(k, None)
                if val:
                    path = os.path.expandvars(val.strip().strip('"'))
                    if os.path.isfile(path):
                        return path
        except OSError:
            continue
    return None


def _start_menu_lnk(names: list[str]) -> str | None:
    """在两级开始菜单里找 names 对应的 .lnk(如 "微信" -> 微信.lnk)。"""
    roots = [
        Path(os.environ.get("ProgramData", r"C:\ProgramData"))
        / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ]
    wanted = [f"{n}.lnk".lower() for n in names if n]
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if f.lower() in wanted:
                    return str(Path(dirpath) / f)
    return None


def open_app(targets: list[str], window: list[str] | None = None,
             args: list[str] | None = None, lnk: list[str] | None = None) -> str:
    """打开或聚焦一个应用:窗口在 -> 聚焦;否则解析 exe / 开始菜单快捷方式启动。

    targets: 候选 exe 文件名列表(按序尝试,如 ["Weixin.exe", "WeChat.exe"])
    window:  可选的窗口标题正则列表(命中即聚焦,不再启动)
    lnk:     可选的开始菜单快捷方式名列表(如 ["微信", "WeChat"] -> 微信.lnk)
    """
    for pattern in window or []:
        try:
            if focus_window(pattern):
                return f"open_app 聚焦[{pattern}]"
        except Exception:
            continue
    args = args or []
    for exe in targets or []:
        path = _resolve_app_path(exe)
        if path:
            runner.launch([path, *args])
            return f"open_app 启动 {path}"
    lnk_path = _start_menu_lnk(lnk or [])
    if lnk_path:
        threading.Thread(
            target=os.startfile, args=(lnk_path,), daemon=True
        ).start()
        return f"open_app 启动快捷方式 {lnk_path}"
    for exe in targets or []:
        try:
            runner.launch([exe, *args])
            return f"open_app 启动 {exe}(PATH)"
        except Exception:
            continue
    return "open_app 失败:窗口未找到且无法启动"


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
    if t == "show_desktop":
        send_combo(["VK_LWIN", "VK_D"])
        return "显示桌面"
    if t == "context_menu":
        send_combo(["VK_SHIFT", "VK_F10"])
        return "右键菜单"
    if t == "app_switcher":
        _app_switcher()
        return "Alt+Tab 切窗口"
    if t == "play_pause":
        tap_key("VK_MEDIA_PLAY_PAUSE")
        return "媒体 播放/暂停"
    if t == "media_next":
        tap_key("VK_MEDIA_NEXT_TRACK")
        return "媒体 下一首"
    if t == "media_prev":
        tap_key("VK_MEDIA_PREV_TRACK")
        return "媒体 上一首"
    if t == "open_app":
        return open_app(
            action.get("targets", []),
            window=action.get("window"),
            args=action.get("args"),
            lnk=action.get("lnk"),
        )
    if t == "scroll":
        return scroll(action.get("direction", "down"), int(action.get("steps", 1)))
    if t == "mouse_click":
        return mouse_click(action.get("kind", "left"))
    if t == "mouse_move":
        return mouse_move(action.get("direction", "down"), int(action.get("distance", 100)))
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
