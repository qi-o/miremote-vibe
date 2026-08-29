"""低层键盘钩子：让 RC003 语音键的 F5 对系统"不存在"。

架构移植自言灵 Vibe Flow（其 Bridge 用 LL 钩子 suppress F5 + 钩子层
自触发语音），针对 miremote 的差异做了适配：

- 言灵：无条件吞 F5（笔记本 F5 陪葬）。
- 本机实测（f5_hook_diag）：LL 钩子先于 Raw Input 分发，且钩子吞掉后
  Raw Input 收不到——miremote 的语音触发若依赖 Raw Input 就会被饿死，
  所以语音触发改由 ATVV 流自检（voice.py _on_atvv_frame），与 F5 解耦。
- 本模块的判定：F5 down 先吞（decisive），150ms 后查 ATVV 活动
  （遥控器语音键必然触发 ATVV 流，笔记本 F5 必然不会）：
    - 遥控器 -> 本轮吞到底（down/repeat/up 全吞，系统全程看不见 F5，
      微信输入法防误触不触发，按住期间可正常注入 Ctrl+Win 热键）
    - 笔记本 -> 立即 SendInput 补发完整 F5 击键（功能无损，仅延迟
      150ms 无感）；物理 up 到来时放行配对

吞掉 down 后 RIT 仍会合成 repeat 并送进钩子（实测 Phase1/2 的 66/72
事件），pending/remote 态必须继续吞 repeat。
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
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

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


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


user32.SetWindowsHookExW.restype = wt.HHOOK
user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD)
user32.UnhookWindowsHookEx.restype = wt.BOOL
user32.UnhookWindowsHookEx.argtypes = (wt.HHOOK,)
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = (wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.GetMessageW.restype = ctypes.c_int
user32.PostThreadMessageW.restype = wt.BOOL
user32.PostThreadMessageW.argtypes = (wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM)
kernel32.GetModuleHandleW.restype = wt.HINSTANCE
kernel32.GetModuleHandleW.argtypes = (wt.LPCWSTR,)
kernel32.GetCurrentThreadId.restype = wt.DWORD


def _send_vk(vk: int, down: bool):
    """补发按键：复用 actions._tap（真机验证过的 INPUT 布局）。"""
    try:
        from . import actions
        actions._tap(vk, up=not down)
    except Exception:
        pass


class F5SuppressHook:
    """吞掉遥控器语音键 F5；笔记本 F5 用 ATVV 活动判定后补发。

    voice_probe() -> bool：最近（约 250ms 内）遥控器 ATVV 流是否活跃。
    由 service 接到 VoiceDaemon.atvv_recent()。
    """

    RESOLVE_DELAY = 0.14  # 小于本机 LowLevelHooksTimeout=200ms

    def __init__(self, raw_engine=None, voice_probe=None, on_voice=None,
                 vkey: int = 0x74, log=print, eager_resolve: bool = False):
        self.raw = raw_engine          # 兼容保留（不再用于判定）
        self.voice_probe = voice_probe
        self.on_voice = on_voice       # callable(down: bool)，钩子线程调用
        self.vkey = vkey
        self.log = log
        self.eager_resolve = eager_resolve
        self._lock = threading.Lock()
        self._state: str | None = None   # None/"pending"/"remote"/"laptop"
        self._pending_up = False         # pending 期间 up 已到（短按）
        self._hook = None
        self._proc_keepalive = None
        self._thread: threading.Thread | None = None
        self._thread_id = 0

    # ---- 判定（钩子回调线程，必须快） ----
    def _decide(self, vk: int, flags: int, wparam: int) -> bool:
        if vk != self.vkey or (flags & LLKHF_INJECTED):
            return False  # 注入事件（含我们补发的）永不吞
        is_up = wparam in (WM_KEYUP, WM_SYSKEYUP)
        with self._lock:
            state = self._state
            if is_up:
                if state == "remote":
                    self._state = None
                    self._notify(False)
                    return True
                if state == "pending":
                    self._pending_up = True
                    return True
                if state == "laptop":
                    self._state = None
                    return False   # 放行，与补发的 down 配对
                return False       # 无状态 stray up：放行
            # down（含 RIT 合成的 repeat down）
            if state in ("pending", "remote"):
                return True        # 继续吞
            if state == "laptop":
                return False       # repeat 放行（系统侧由补发 down 配对）
            # 新一轮：先吞，异步判定
            try:
                raw_remote = bool(
                    self.raw and self.raw.recent_remote_key(self.vkey, within_ms=120.0)
                )
            except Exception:
                raw_remote = False
            if raw_remote:
                self._state = "remote"
                self._pending_up = False
                fast_remote = True
            else:
                fast_remote = False
            if not fast_remote:
                self._state = "pending"
                self._pending_up = False
        if fast_remote:
            self._notify(True)
            self.log("F5=遥控器（Raw Input 立即判定，吞键已完成）")
            return True
        if self.eager_resolve:
            # 实验实时模式：保持当前 LL hook 回调未返回，使物理 F5 尚未
            # 更新异步键状态；一旦 ATVV 证据到达，立刻在此窗口触发 WeType。
            self._resolve(wait_for_remote=True)
        else:
            threading.Timer(self.RESOLVE_DELAY, self._resolve).start()
        return True

    def _resolve(self, wait_for_remote: bool = False):
        remote = False
        deadline = time.monotonic() + self.RESOLVE_DELAY
        while True:
            try:
                remote = bool(self.voice_probe and self.voice_probe())
            except Exception:
                remote = False
            if remote or not wait_for_remote or time.monotonic() >= deadline:
                break
            time.sleep(0.002)
        with self._lock:
            if self._state != "pending":
                return  # 已被终止（不该发生，防御）
            up_seen = self._pending_up
            if remote:
                self._state = "remote"
            else:
                self._state = "laptop"
        if remote:
            self._notify(True)
            if up_seen:  # 短按已松开：会话即完
                with self._lock:
                    self._state = None
                self._notify(False)
            mode = "钩子内提前触发" if wait_for_remote else "异步判定"
            self.log(f"F5=遥控器（{mode}，吞键维持本段）")
        else:
            _send_vk(self.vkey, True)   # 补发完整击键给系统
            if up_seen:
                _send_vk(self.vkey, False)
                with self._lock:
                    self._state = None
            self.log("F5=笔记本（已补发击键，功能不受影响）")

    def _notify(self, down: bool):
        cb = self.on_voice
        if cb:
            try:
                cb(down)
            except Exception:
                pass

    def _on_hook(self, ncode, wparam, lparam):
        if ncode == HC_ACTION:
            info = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            try:
                if self._decide(info.vkCode, info.flags, wparam):
                    return 1
            except Exception:
                pass  # 判定异常绝不吞键
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
            # -1=错误 0=WM_QUIT：两者都退出并卸载钩子
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                pass
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None

        self._thread = threading.Thread(target=worker, name="ll-hook", daemon=True)
        self._thread.start()
        if not ready.wait(3):
            self.stop()
            raise TimeoutError("LL 钩子线程启动超时")
        if error:
            self._thread = None
            raise error[0]
        self.log("F5 吞键已启用（言灵方案：先吞+ATVV 判定，笔记本 F5 自动补发）")

    def stop(self):
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._thread = None
        self._thread_id = 0
