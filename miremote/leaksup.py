"""选配的按键泄漏抑制(arm/consume 式)——beta 测试版新增。

问题:遥控器的普通键在系统里同时是一条真实键盘事件(如 OK=回车、
语音键=F5、TV=`),Raw Input 只是旁观,焦点应用照样收到。多数时候
"透传"正是想要的;但个别键(典型:F5 会让浏览器刷新)需要可选地拦掉。

方案移植自 RC003 的 arm/consume 思路:
* Raw Input(设备过滤,只认遥控器)收到某键边沿 -> arm 一条记录;
* LL 钩子看到同名"非注入"物理边沿时,短暂等待武装记录(本机实测
  LL 钩子先于 Raw Input 分发);等到 -> 吞掉;等不到 -> 放行
  (笔记本键盘按同一个键永远不会被 arm,因此不受影响,只是该键
  多了至多 wait 秒的放行延迟)。

注意:
* 本模块不做语音键(F5)的完整方案——吞掉 F5 会饿死 Raw Input,
  语音触发需要像 llhook.F5SuppressHook 那样从钩子回调补发语音边沿。
  service 层对语音键直接忽略 suppress_leak。
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_QUIT = 0x0012
LLKHF_INJECTED = 0x10

LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD),
        ("scanCode", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


user32.SetWindowsHookExW.restype = wt.HHOOK
user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD)
user32.UnhookWindowsHookEx.restype = wt.BOOL
user32.CallNextHookEx.restype = LRESULT
user32.GetMessageW.restype = ctypes.c_int
user32.PostThreadMessageW.restype = wt.BOOL
kernel32.GetModuleHandleW.restype = wt.HINSTANCE
kernel32.GetCurrentThreadId.restype = wt.DWORD


class LeakSuppressor:
    """只针对配置过的 VK 生效的 arm/consume 吞键器。

    arm(vk, pressed):Raw Input 线程在收到遥控器该键边沿时调用。
    窗口期内(默认 180ms)钩子会等待匹配的武装记录并吞掉对应物理事件。
    """

    ARM_WINDOW = 0.18
    HOOK_WAIT = 0.12   # 钩子回调里等待武装记录的上限(< LowLevelHooksTimeout)

    def __init__(self, vks, log=print):
        self._vks = frozenset(int(v) for v in vks)
        self.log = log
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._armed: set = set()          # {(vk, pressed)}
        self._hook = None
        self._proc_keepalive = None
        self._thread = None
        self._thread_id = 0

    # ---- Raw Input 线程 ----
    def arm(self, vk: int, pressed: bool) -> None:
        if int(vk) not in self._vks:
            return
        with self._cond:
            self._armed.add((int(vk), bool(pressed)))
            self._cond.notify_all()
        threading.Timer(self.ARM_WINDOW, self._expire, args=(int(vk), bool(pressed))).start()

    def _expire(self, vk: int, pressed: bool) -> None:
        with self._cond:
            self._armed.discard((vk, pressed))

    # ---- LL 钩子线程 ----
    def _decide(self, vk: int, flags: int, wparam: int) -> bool:
        if int(vk) not in self._vks or (flags & LLKHF_INJECTED):
            return False
        pressed = wparam not in (WM_KEYUP, WM_SYSKEYUP)
        with self._cond:
            end = _monotonic() + self.HOOK_WAIT
            while (int(vk), pressed) not in self._armed:
                remaining = end - _monotonic()
                if remaining <= 0:
                    return False           # 没等到武装记录:笔记本键盘,放行
                self._cond.wait(remaining)
            self._armed.discard((int(vk), pressed))
        return True                        # 吞掉

    def _on_hook(self, ncode, wparam, lparam):
        if ncode == HC_ACTION:
            info = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            try:
                if self._decide(info.vkCode, info.flags, wparam):
                    return 1
            except Exception:
                pass                       # 判定异常绝不吞键
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    # ---- 生命周期 ----
    def start(self):
        if self._thread is not None:
            return
        ready = threading.Event()
        error: list = []

        def worker():
            self._thread_id = int(kernel32.GetCurrentThreadId())
            hinst = kernel32.GetModuleHandleW(None)
            self._proc_keepalive = HOOKPROC(self._on_hook)
            hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc_keepalive, hinst, 0)
            if not hook:
                error.append(ctypes.WinError(ctypes.get_last_error()))
                ready.set()
                return
            self._hook = hook
            ready.set()
            msg = wt.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                pass
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None

        self._thread = threading.Thread(target=worker, name="leak-hook", daemon=True)
        self._thread.start()
        if not ready.wait(3):
            self.stop()
            raise TimeoutError("泄漏抑制钩子线程启动超时")
        if error:
            self._thread = None
            raise error[0]
        self.log(f"按键泄漏抑制已启用(VK: {', '.join(hex(v) for v in sorted(self._vks))})")

    def stop(self):
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._thread = None
        self._thread_id = 0


def _monotonic() -> float:
    return time.monotonic()
