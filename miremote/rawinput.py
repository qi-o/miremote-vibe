"""Win32 Raw Input 捕获引擎（纯 ctypes，无第三方依赖）。

- 注册 HID 键盘(usage 0x01/0x06) 与消费控制(0x0C/0x01) 两个集合，
  RIDEV_INPUTSINK 保证后台也能收到（无需管理员权限）。
- 每个事件携带来源设备路径，按 VID/PID 过滤，只处理遥控器。
"""

from __future__ import annotations

import ctypes
import itertools
import os
import re
import threading
import time
from ctypes import wintypes as wt
from dataclasses import dataclass, field

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---- 常量 ----
WM_INPUT = 0x00FF
WM_QUIT = 0x0012
RIDEV_INPUTSINK = 0x00000100
RIDEV_REMOVE = 0x00000001
RIDI_DEVICENAME = 0x20000007
RID_INPUT = 0x10000003
RIM_TYPEMOUSE, RIM_TYPEKEYBOARD, RIM_TYPEHID = 0, 1, 2

KEY_UP = 1  # RAWKEYBOARD.Flags 中的 RI_KEY_BREAK

# ---- 结构 ----


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wt.USHORT),
        ("usUsage", wt.USHORT),
        ("dwFlags", wt.DWORD),
        ("hwndTarget", wt.HWND),
    ]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wt.DWORD),
        ("dwSize", wt.DWORD),
        ("hDevice", wt.HANDLE),
        ("wParam", wt.WPARAM),
    ]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [
        ("MakeCode", wt.USHORT),
        ("Flags", wt.USHORT),
        ("Reserved", wt.USHORT),
        ("VKey", wt.USHORT),
        ("Message", wt.UINT),
        ("ExtraInformation", wt.ULONG),
    ]


class RAWHID(ctypes.Structure):
    _fields_ = [("dwSizeHid", wt.DWORD), ("dwCount", wt.DWORD)]


class RAWINPUT(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("keyboard", RAWKEYBOARD)]


LRESULT = ctypes.c_ssize_t  # wintypes 里没有 LRESULT
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

# 显式声明指针宽度敏感的 API 签名（默认 int 是 32 位，会截断句柄）
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = (
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
)
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.GetMessageW.restype = ctypes.c_int  # BOOL，-1/0/正数三态
user32.PostThreadMessageW.argtypes = (wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.PostThreadMessageW.restype = wt.BOOL
user32.DestroyWindow.argtypes = (wt.HWND,)
user32.DestroyWindow.restype = wt.BOOL
user32.UnregisterClassW.argtypes = (wt.LPCWSTR, wt.HINSTANCE)
user32.UnregisterClassW.restype = wt.BOOL
user32.RegisterRawInputDevices.argtypes = (
    ctypes.POINTER(RAWINPUTDEVICE), wt.UINT, wt.UINT
)
user32.RegisterRawInputDevices.restype = wt.BOOL
user32.SetForegroundWindow.argtypes = (wt.HWND,)
user32.SetForegroundWindow.restype = wt.BOOL
user32.ShowWindow.argtypes = (wt.HWND, ctypes.c_int)
user32.ShowWindow.restype = wt.BOOL
kernel32.GetCurrentThreadId.restype = wt.DWORD
kernel32.GetModuleHandleW.argtypes = (wt.LPCWSTR,)
kernel32.GetModuleHandleW.restype = wt.HINSTANCE
HWND_MESSAGE = wt.HWND(-3)

_CLASS_COUNTER = itertools.count()


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


user32.RegisterClassW.argtypes = (ctypes.POINTER(WNDCLASS),)
user32.RegisterClassW.restype = wt.ATOM


@dataclass
class KeyEvent:
    """一次按键事件（键盘或 HID 报文）。"""

    vkey: int
    scan: int  # MakeCode
    is_up: bool
    device: str  # 来源设备路径
    is_remote: bool = False
    hid_bytes: bytes | None = None  # RAWHID 报文（如有）


@dataclass
class RawInputEngine:
    """后台消息循环，把遥控器按键回调给 handler(KeyEvent)。"""

    vid: str = "2717"
    pid: str = "32B8"
    name_contains: str | None = None  # 额外按设备路径子串过滤（可选）
    on_key: object = None
    _proc_keepalive: object = field(default=None, init=False, repr=False)
    _wnd: int = field(default=0, init=False)
    _device_paths: dict = field(default_factory=dict, init=False)
    _class_name: str = field(default="", init=False)
    _class_registered: bool = field(default=False, init=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _thread_id: int = field(default=0, init=False, repr=False)
    _start_ready: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _start_error: Exception | None = field(default=None, init=False, repr=False)
    # 最近一次遥控器各键键盘事件的时间（monotonic），供 LL 钩子判定 F5 来源
    _remote_key_log: dict = field(default_factory=dict, init=False, repr=False)

    # ---- 设备路径缓存 / 遥控器判定 ----
    def _device_path(self, hdev) -> str:
        if not hdev:
            return ""
        if hdev in self._device_paths:
            return self._device_paths[hdev]
        size = wt.UINT(0)
        user32.GetRawInputDeviceInfoW(hdev, RIDI_DEVICENAME, None, ctypes.byref(size))
        buf = ctypes.create_unicode_buffer(size.value + 1)
        user32.GetRawInputDeviceInfoW(hdev, RIDI_DEVICENAME, buf, ctypes.byref(size))
        path = buf.value
        self._device_paths[hdev] = path
        return path

    def is_remote_device(self, path: str) -> bool:
        if not path:
            return False
        if self.name_contains:
            return self.name_contains.lower() in path.lower()
        # USB 风格: VID_2717；BTHLE 风格: VID&012717（四位前导零填充）
        rx = re.compile(
            rf"VID[&_]\d*{re.escape(self.vid)}" + r".*" + rf"PID[&_]\d*{re.escape(self.pid)}",
            re.IGNORECASE,
        )
        return bool(rx.search(path))

    # ---- 窗口与注册 ----
    def _create_window(self) -> int:
        hinst = kernel32.GetModuleHandleW(None)
        self._proc_keepalive = WNDPROC(self._wnd_proc)  # 防 GC
        self._class_name = f"MiRemoteVibeWnd_{os.getpid()}_{next(_CLASS_COUNTER)}"
        wc = WNDCLASS()
        wc.lpfnWndProc = self._proc_keepalive
        wc.lpszClassName = self._class_name
        wc.hInstance = hinst
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())
        self._class_registered = True
        # HWND_MESSAGE：不可见的消息专用窗口，适合后台 INPUTSINK
        hwnd = user32.CreateWindowExW(
            0, self._class_name, "miremote", 0, 0, 0, 0, 0,
            HWND_MESSAGE, None, hinst, None,
        )
        if not hwnd:
            user32.UnregisterClassW(self._class_name, hinst)
            self._class_registered = False
            raise ctypes.WinError(ctypes.get_last_error())
        return hwnd

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            self._handle_raw(lparam)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _handle_raw(self, lparam):
        size = wt.UINT(0)
        user32.GetRawInputData(
            lparam, RID_INPUT, None, ctypes.byref(size),
            ctypes.sizeof(RAWINPUTHEADER),
        )
        buf = ctypes.create_string_buffer(size.value)
        got = user32.GetRawInputData(
            lparam, RID_INPUT, buf, ctypes.byref(size),
            ctypes.sizeof(RAWINPUTHEADER),
        )
        if got in (0, -1):
            return
        raw = ctypes.cast(buf, ctypes.POINTER(RAWINPUT)).contents
        path = self._device_path(raw.header.hDevice)
        remote = self.is_remote_device(path)

        if raw.header.dwType == RIM_TYPEKEYBOARD:
            ev = KeyEvent(
                vkey=raw.keyboard.VKey,
                scan=raw.keyboard.MakeCode,
                is_up=bool(raw.keyboard.Flags & KEY_UP),
                device=path,
                is_remote=remote,
            )
            if remote:
                self._remote_key_log[ev.vkey] = time.monotonic()
        elif raw.header.dwType == RIM_TYPEHID:
            hid = ctypes.cast(
                ctypes.byref(raw, ctypes.sizeof(RAWINPUTHEADER)),
                ctypes.POINTER(RAWHID),
            ).contents
            n = hid.dwSizeHid * hid.dwCount
            if n:
                payload = bytes(
                    ctypes.cast(
                        ctypes.byref(
                            raw,
                            ctypes.sizeof(RAWINPUTHEADER) + ctypes.sizeof(RAWHID),
                        ),
                        ctypes.POINTER(ctypes.c_ubyte * n),
                    ).contents
                )
            else:
                payload = b""
            ev = KeyEvent(
                vkey=0, scan=0, is_up=False, device=path,
                is_remote=remote, hid_bytes=payload,
            )
        else:
            return

        if self.on_key:
            try:
                self.on_key(ev)
            except Exception:  # 回调异常不能打断消息循环
                import traceback
                traceback.print_exc()

    # ---- 公共 API ----
    def recent_remote_key(self, vkey: int, within_ms: float = 80.0) -> bool:
        """最近 within_ms 毫秒内是否收到过遥控器上某键的键盘事件。

        raw input 先于低层键盘钩子分发（输入路径 RIT -> Raw Input -> LL 钩子
        -> 系统队列），因此 LL 钩子回调里查这个记录即可判定当前硬件按键的
        来源设备（见 llhook.py）。记录是单调时钟时间戳，dict 赋值原子。
        """
        t = self._remote_key_log.get(vkey)
        return t is not None and (time.monotonic() - t) * 1000.0 <= within_ms

    def start(self):
        self._wnd = self._create_window()
        devs = (
            (0x01, 0x06),  # Generic Desktop / Keyboard
            (0x0C, 0x01),  # Consumer / Consumer Control
        )
        arr = (RAWINPUTDEVICE * len(devs))()
        for i, (pg, us) in enumerate(devs):
            arr[i].usUsagePage = pg
            arr[i].usUsage = us
            arr[i].dwFlags = RIDEV_INPUTSINK
            arr[i].hwndTarget = self._wnd
        if not user32.RegisterRawInputDevices(
            arr, len(devs), ctypes.sizeof(RAWINPUTDEVICE)
        ):
            self._destroy_window()
            raise ctypes.WinError(ctypes.get_last_error())

    def start_background(self, timeout: float = 3.0):
        self._start_ready.clear()
        self._start_error = None

        def worker():
            self._thread_id = int(kernel32.GetCurrentThreadId())
            try:
                self.start()
            except Exception as exc:
                self._start_error = exc
                self._start_ready.set()
                return
            self._start_ready.set()
            self.run_forever()

        self._thread = threading.Thread(target=worker, name="raw-input", daemon=True)
        self._thread.start()
        if not self._start_ready.wait(timeout):
            self.stop()
            raise TimeoutError("Raw Input 消息线程启动超时")
        if self._start_error:
            error = self._start_error
            self._thread = None
            raise error

    def stop(self):
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3)
        self._thread = None
        self._thread_id = 0
        if self._wnd:
            self._destroy_window()

    def _destroy_window(self):
        hinst = kernel32.GetModuleHandleW(None)
        if self._wnd:
            user32.DestroyWindow(self._wnd)
            self._wnd = 0
        if self._class_registered and self._class_name:
            user32.UnregisterClassW(self._class_name, hinst)
            self._class_registered = False

    def run_forever(self):
        """阻塞消息循环，Ctrl+C 退出。"""
        msg = wt.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except KeyboardInterrupt:
            pass
        finally:
            self._destroy_window()

    def pump(self, max_messages: int = 64) -> bool:
        """非阻塞处理一批已排队消息（用于自检）。返回是否处理过消息。"""
        msg = wt.MSG()
        got_any = False
        for _ in range(max_messages):
            if not user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
            got_any = True
        return got_any


class RAWINPUTDEVICELIST(ctypes.Structure):
    _fields_ = [("hDevice", wt.HANDLE), ("dwType", wt.DWORD)]


def list_raw_devices() -> list[dict]:
    """枚举系统 Raw Input 设备，用于诊断遥控器是否可见。"""
    n = wt.UINT(0)
    user32.GetRawInputDeviceList(
        None, ctypes.byref(n), ctypes.sizeof(RAWINPUTDEVICELIST)
    )
    if n.value == 0:
        return []

    arr = (RAWINPUTDEVICELIST * n.value)()
    user32.GetRawInputDeviceList(
        arr, ctypes.byref(n), ctypes.sizeof(RAWINPUTDEVICELIST)
    )
    out = []
    for i in range(n.value):
        h = arr[i].hDevice
        size = wt.UINT(0)
        user32.GetRawInputDeviceInfoW(h, RIDI_DEVICENAME, None, ctypes.byref(size))
        buf = ctypes.create_unicode_buffer(size.value + 1)
        user32.GetRawInputDeviceInfoW(h, RIDI_DEVICENAME, buf, ctypes.byref(size))
        out.append({"type": arr[i].dwType, "path": buf.value})
    return out
