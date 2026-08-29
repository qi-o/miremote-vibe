"""RC003 语音键 F5 吞键链路诊断（需要用户按两次遥控器语音键配合）。

回答两个问题：
  Phase 1：硬件 F5 事件，Raw Input 和 LL 钩子谁先收到？（钩子吞键判定的根基）
  Phase 2：LL 钩子盲吞硬件 F5 后，Raw Input 还能收到吗？（盲吞+补偿方案可行性）

用法（先退出小米遥控器.exe，保证环境干净）:
  python tools/f5_hook_diag.py
"""

import ctypes
import sys
import threading
import time
from ctypes import wintypes as wt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miremote.rawinput import RawInputEngine  # noqa: E402

VK_F5 = 0x74
WH_KEYBOARD_LL = 13
HC_ACTION = 0
LLKHF_INJECTED = 0x10
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101

user32 = ctypes.WinDLL("user32", use_last_error=True)
LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD)
user32.UnhookWindowsHookEx.argtypes = (wt.HHOOK,)
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = (wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.GetMessageW.restype = ctypes.c_int
user32.PostThreadMessageW.argtypes = (wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM)
_kernel32 = ctypes.WinDLL("kernel32")
_kernel32.GetModuleHandleW.restype = wt.HINSTANCE
_kernel32.GetModuleHandleW.argtypes = (wt.LPCWSTR,)
_kernel32.GetCurrentThreadId.restype = wt.DWORD


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class DiagHook:
    """记录所有硬件 F5 事件；mode="pass" 只记录，mode="swallow" 盲吞。"""

    def __init__(self):
        self.events: list[tuple[float, str]] = []   # (perf_counter, "ll-down"/"ll-up")
        self.mode = "pass"
        self._lock = threading.Lock()
        self._hook = None
        self._keepalive = None
        self._thread = None
        self._tid = 0

    def _cb(self, ncode, wparam, lparam):
        if ncode == HC_ACTION:
            info = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if info.vkCode == VK_F5 and not (info.flags & LLKHF_INJECTED):
                tag = "ll-up" if wparam in (WM_KEYUP, 0x105) else "ll-down"
                with self._lock:
                    self.events.append((time.perf_counter(), tag))
                if self.mode == "swallow":
                    return 1
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    def start(self):
        ready = threading.Event()

        def worker():
            self._tid = int(_kernel32.GetCurrentThreadId())
            self._keepalive = HOOKPROC(self._cb)
            self._hook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, self._keepalive, _kernel32.GetModuleHandleW(None), 0)
            ready.set()
            if not self._hook:
                return
            msg = wt.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                pass

        self._thread = threading.Thread(target=worker, daemon=True, name="diag-ll")
        self._thread.start()
        ready.wait(3)
        if not self._hook:
            raise RuntimeError("LL 钩子安装失败")
        return self

    def stop(self):
        if self._tid:
            user32.PostThreadMessageW(self._tid, 0x0012, 0, 0)
        if self._thread:
            self._thread.join(timeout=2)


def banner(text):
    print("\n" + "=" * 62)
    print(text)
    print("=" * 62)


def wait_for_press(raw_events, hook, timeout=90.0):
    """等一次完整的 down+up（事件驱动，超时返回是否有事件）。"""
    deadline = time.time() + timeout
    seen_down_at = None
    while time.time() < deadline:
        time.sleep(0.2)
        n_raw = len(raw_events)
        n_ll = len(hook.events)
        if (n_raw or n_ll) and seen_down_at is None:
            seen_down_at = time.time()
        # 出现过事件后再等 3 秒收尾（等 up 和 repeat）
        if seen_down_at is not None and time.time() - seen_down_at > 3.0:
            break
    return seen_down_at is not None


def main() -> int:
    raw_events: list[tuple[float, str, bool]] = []  # (t, tag, is_remote)

    def on_key(ev):
        if ev.vkey == VK_F5:
            raw_events.append((time.perf_counter(),
                               "raw-up" if ev.is_up else "raw-down",
                               ev.is_remote))

    eng = RawInputEngine(vid="2717", pid="32B8")
    eng.on_key = on_key
    eng.start_background()
    hook = DiagHook().start()
    time.sleep(0.5)

    banner("Phase 1：现在按住遥控器【语音键】2 秒再松开（90 秒内有效）")
    print("  （本阶段只记录不吞键；测 Raw Input 与 LL 钩子的先后）")
    wait_for_press(raw_events, hook)
    n_raw = len(raw_events)
    n_ll = len(hook.events)
    print(f"  Raw Input 收到 {n_raw} 个 F5 事件, LL 钩子收到 {n_ll} 个")
    for t, tag, remote in raw_events[:6]:
        print(f"    {tag} remote={remote}")
    for t, tag in hook.events[:6]:
        print(f"    {tag}")
    raw_first = None
    if n_raw and n_ll:
        raw_first = raw_events[0][0] <= hook.events[0][0]
        d = (hook.events[0][0] - raw_events[0][0]) * 1000
        print(f"  ==> {'Raw 先于 LL ' + f'{d:.1f}ms（钩子判定方案可行）' if raw_first else 'LL 先于 Raw ' + f'{-d:.1f}ms（钩子判定方案不可行！）'}")
    elif n_raw == 0:
        print("  ==> Raw Input 没收到（遥控器没连上/没按）")

    banner("Phase 2：钩子已改为【盲吞】。再按住遥控器【语音键】2 秒松开")
    print("  （测吞掉之后 Raw Input 是否仍能收到——决定盲吞+补偿方案）")
    hook.mode = "swallow"
    raw_events.clear()
    hook.events.clear()
    wait_for_press(raw_events, hook)
    n_raw2 = len(raw_events)
    n_ll2 = len(hook.events)
    print(f"  盲吞期间: Raw Input 收到 {n_raw2} 个, LL 钩子(已吞) {n_ll2} 个")
    if n_ll2 and n_raw2:
        print("  ==> 盲吞+补偿方案可行（吞了系统看不见，Raw 仍能触发语音）")
    elif n_ll2 and not n_raw2:
        print("  ==> 盲吞后 Raw 也收不到（Raw 在 LL 之后）——钩子路线全灭，需换方案")
    elif not n_ll2:
        print("  ==> LL 钩子没收到事件（钩子链问题或没按键）")

    hook.stop()
    eng.stop()
    banner("诊断结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
