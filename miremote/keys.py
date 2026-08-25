"""虚拟键码命名与解析。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes as wt

user32 = ctypes.WinDLL("user32")

# 常用 VK 码（与 wincon/winuser 一致）
VK_MAP = {
    0x03: "VK_CANCEL", 0x08: "VK_BACK", 0x09: "VK_TAB", 0x0D: "VK_RETURN",
    0x10: "VK_SHIFT", 0x11: "VK_CONTROL", 0x12: "VK_MENU", 0x13: "VK_PAUSE",
    0x14: "VK_CAPITAL", 0x1B: "VK_ESCAPE", 0x1E: "VK_ACCEPT", 0x1F: "VK_MODECHANGE",
    0x20: "VK_SPACE", 0x21: "VK_PRIOR", 0x22: "VK_NEXT",
    0x23: "VK_END", 0x24: "VK_HOME", 0x25: "VK_LEFT", 0x26: "VK_UP",
    0x27: "VK_RIGHT", 0x28: "VK_DOWN", 0x2A: "VK_PRINT", 0x2C: "VK_SNAPSHOT",
    0x2D: "VK_INSERT", 0x2E: "VK_DELETE", 0x2F: "VK_HELP", 0x5F: "VK_SLEEP",
    0x30: "VK_0", 0x31: "VK_1", 0x32: "VK_2", 0x33: "VK_3", 0x34: "VK_4",
    0x35: "VK_5", 0x36: "VK_6", 0x37: "VK_7", 0x38: "VK_8", 0x39: "VK_9",
    0x41: "VK_A", 0x42: "VK_B", 0x43: "VK_C", 0x44: "VK_D", 0x45: "VK_E",
    0x46: "VK_F", 0x47: "VK_G", 0x48: "VK_H", 0x49: "VK_I", 0x4A: "VK_J",
    0x4B: "VK_K", 0x4C: "VK_L", 0x4D: "VK_M", 0x4E: "VK_N", 0x4F: "VK_O",
    0x50: "VK_P", 0x51: "VK_Q", 0x52: "VK_R", 0x53: "VK_S", 0x54: "VK_T",
    0x55: "VK_U", 0x56: "VK_V", 0x57: "VK_W", 0x58: "VK_X", 0x59: "VK_Y",
    0x5A: "VK_Z",
    0x5B: "VK_LWIN", 0x5C: "VK_RWIN", 0x5D: "VK_APPS",
    0x60: "VK_NUMPAD0", 0x61: "VK_NUMPAD1", 0x62: "VK_NUMPAD2",
    0x63: "VK_NUMPAD3", 0x64: "VK_NUMPAD4", 0x65: "VK_NUMPAD5",
    0x66: "VK_NUMPAD6", 0x67: "VK_NUMPAD7", 0x68: "VK_NUMPAD8",
    0x69: "VK_NUMPAD9", 0x6A: "VK_MULTIPLY", 0x6B: "VK_ADD",
    0x6D: "VK_SUBTRACT", 0x6E: "VK_DECIMAL", 0x6F: "VK_DIVIDE",
    0x70: "VK_F1", 0x71: "VK_F2", 0x72: "VK_F3", 0x73: "VK_F4",
    0x74: "VK_F5", 0x75: "VK_F6", 0x76: "VK_F7", 0x77: "VK_F8",
    0x78: "VK_F9", 0x79: "VK_F10", 0x7A: "VK_F11", 0x7B: "VK_F12",
    0x7C: "VK_F13", 0x7D: "VK_F14", 0x7E: "VK_F15", 0x7F: "VK_F16",
    0x80: "VK_F17", 0x81: "VK_F18", 0x82: "VK_F19", 0x83: "VK_F20",
    0x84: "VK_F21", 0x85: "VK_F22", 0x86: "VK_F23", 0x87: "VK_F24",
    0x90: "VK_NUMLOCK", 0x91: "VK_SCROLL",
    0xA0: "VK_LSHIFT", 0xA1: "VK_RSHIFT", 0xA2: "VK_LCONTROL",
    0xA3: "VK_RCONTROL", 0xA4: "VK_LMENU", 0xA5: "VK_RMENU",
    0xAD: "VK_VOLUME_MUTE", 0xAE: "VK_VOLUME_DOWN", 0xAF: "VK_VOLUME_UP",
    0xB0: "VK_MEDIA_NEXT_TRACK", 0xB1: "VK_MEDIA_PREV_TRACK",
    0xB2: "VK_MEDIA_STOP", 0xB3: "VK_MEDIA_PLAY_PAUSE",
    0xB4: "VK_LAUNCH_MEDIA", 0xB5: "VK_LAUNCH_APP1", 0xB6: "VK_LAUNCH_APP2",
    0xFF: "VK_NONE",  # Raw Input 对无法映射的 usage 常给 0xFF
}

MODIFIER_VKS = {
    "VK_SHIFT", "VK_LSHIFT", "VK_RSHIFT", "VK_CONTROL", "VK_LCONTROL",
    "VK_RCONTROL", "VK_MENU", "VK_LMENU", "VK_RMENU", "VK_LWIN", "VK_RWIN",
}


def vk_name(vk: int) -> str:
    return VK_MAP.get(vk, f"VK_0x{vk:02X}")


def name_to_vk(name: str) -> int:
    n = name.upper()
    for k, v in VK_MAP.items():
        if v == n:
            return k
    if n.startswith("VK_0X"):
        return int(n[5:], 16)
    raise ValueError(f"未知按键名: {name}")


def key_label(vk: int, scan: int) -> str:
    """GetKeyNameText 的本地化名称（失败则回退 vk 名）。"""
    if vk and vk != 0xFF:
        try:
            buf = ctypes.create_unicode_buffer(64)
            # scan<<16；bit 24 (0x100) 不置也行
            if user32.GetKeyNameTextW(scan << 16, buf, 64):
                return buf.value
        except Exception:
            pass
    return vk_name(vk)
