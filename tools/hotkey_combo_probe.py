"""探测微信输入法(WeType)语音面板的有效热键组合。

自动依次注入候选组合（按住 1.6s 再松开），用 EnumWindows 快照 diff
检测面板浮窗的出现/消失。不依赖遥控器与 miremote 服务（服务只吞 F5，
注入的其他键不受影响）。

用法: python tools/hotkey_combo_probe.py
"""

import ctypes
import sys
import time
from ctypes import wintypes as wt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

u32 = ctypes.WinDLL("user32")
k32 = ctypes.WinDLL("kernel32")
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
u32.GetWindowTextW.argtypes = (wt.HWND, wt.LPWSTR, ctypes.c_int)
u32.GetClassNameW.argtypes = (wt.HWND, wt.LPWSTR, ctypes.c_int)
u32.IsWindowVisible.argtypes = (wt.HWND,)
u32.GetWindowThreadProcessId.argtypes = (wt.HHWND if hasattr(wt, "HHWND") else wt.HWND,
                                         ctypes.POINTER(wt.DWORD))
k32.OpenProcess.restype = ctypes.c_void_p
k32.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
k32.CloseHandle.argtypes = (ctypes.c_void_p,)
k32.QueryFullProcessImageNameW.restype = wt.BOOL
k32.QueryFullProcessImageNameW.argtypes = (
    ctypes.c_void_p, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD))


def pid_of(hwnd) -> int:
    pid = wt.DWORD(0)
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def proc_name(pid: int) -> str:
    if not pid:
        return "?"
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return f"pid{pid}"
    buf = ctypes.create_unicode_buffer(512)
    n = wt.DWORD(512)
    ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
    k32.CloseHandle(h)
    return buf.value.rsplit("\\", 1)[-1] if ok else f"pid{pid}"


def snapshot() -> dict:
    wins = {}

    @WNDENUMPROC
    def cb(hwnd, _l):
        if u32.IsWindowVisible(hwnd):
            t = ctypes.create_unicode_buffer(128)
            u32.GetWindowTextW(hwnd, t, 128)
            c = ctypes.create_unicode_buffer(128)
            u32.GetClassNameW(hwnd, c, 128)
            wins[hwnd] = (t.value, c.value)
        return True

    u32.EnumWindows(cb, 0)
    return wins


COMBOS = [
    ("Ctrl+Win", ["VK_CONTROL", "VK_LWIN"]),
    ("Ctrl+Win+Shift", ["VK_CONTROL", "VK_LWIN", "VK_SHIFT"]),
    ("Ctrl+Shift", ["VK_CONTROL", "VK_SHIFT"]),
]


def main() -> int:
    from miremote import actions
    from miremote.keys import name_to_vk

    hits = []
    for name, keys in COMBOS:
        base = snapshot()
        vks = [name_to_vk(k) for k in keys]
        for vk in vks:
            actions._tap(vk)
        time.sleep(1.6)
        during = snapshot()
        new = {h: w for h, w in during.items() if h not in base}
        for vk in reversed(vks):
            actions._tap(vk, up=True)
        time.sleep(1.6)
        after = snapshot()
        gone = [h for h in new if h not in after]
        print(f"\n=== {name}: 新窗口 {len(new)} 个，松开后消失 {len(gone)} 个 ===")
        for h, (t, c) in new.items():
            mark = "*出现后消失*" if h in gone else " 持续存在 "
            pn = proc_name(pid_of(h))
            print(f"  [{h}] {mark} proc={pn} title={t!r} class={c!r}")
        if new:
            hits.append((name, [(proc_name(pid_of(h)), t, c) for h, (t, c) in new.items()]))
        time.sleep(1.2)

    print("\n==== 结论 ====")
    if not hits:
        print("所有候选组合都没有唤起任何窗口——输入法语音热键可能不在候选里，"
              "或注入链路被输入法忽略。")
    else:
        for name, ws in hits:
            print(f"{name} 唤起了: " + "; ".join(f"{p}({t!r}/{c!r})" for p, t, c in ws))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
