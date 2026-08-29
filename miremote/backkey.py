"""返回键/音量键拦截：钩住 RC003 的 HidOverGatt 宿主（WUDFHost）取回被丢弃的报文。

原理（移植自 GPL-3.0 项目 xxb26553663-star/remote-bridge-hub，致谢并遵循其协议精神）：
Windows 的 HidOverGatt 驱动能收到遥控器的完整报文（9 字节：`01 00 00` 前缀 +
3 个小端 16 位键盘页 usage），但对部分 usage 不生成键盘事件、直接丢弃——
Back=0xF1、音量静音/增/减=0x7F/0x80/0x81。本模块用 frida 钩住 WUDFHost 里的
NtDeviceIoControlFile（IOCTL 0x80018483 = BTHLE 读特征值，输出恰好 9 字节），
把这些按键的按下/松开边沿发回来。

WUDFHost 是 SYSTEM 进程，附加需要管理员：主进程（非提权）通过 UAC 启动提权的
helper（`python -m miremote backkey --helper --pid <N>`），helper attach 后把
JSON 行发到 127.0.0.1:30685，由本进程的消费线程解析成按键边沿。

需要 `pip install frida`。杀毒软件可能对 frida 附加系统进程告警，属预期行为。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import select
import socket
import threading
import time
import winreg
from pathlib import Path

# RC003 键盘页 usage（来自 remote-bridge-hub 真机逆向）
USAGE_NAMES = {
    0x00F1: "back",
    0x0028: "ok",
    0x0035: "tv",
    0x004A: "home",
    0x004F: "right",
    0x0050: "left",
    0x0051: "down",
    0x0052: "up",
    0x0065: "menu",
    0x0066: "power",
    0x007F: "volume_mute",
    0x0080: "volume_up",
    0x0081: "volume_down",
}
# 只转发 Windows 原生不转发的键（避免与 Raw Input 双触发）
DEAD_USAGES = {0x00F1, 0x007F, 0x0080, 0x0081}
VOICE_USAGE = 0x003E
GADGET_PROTOCOL_VERSION = 4
GADGET_SCRIPT_VERSION = "2026.08.29-tap4"

TAP_PORT = int(os.environ.get("MIREMOTE_TAP_PORT", "30685"))

BTHLE_ENUM_KEY = r"SYSTEM\CurrentControlSet\Enum\BTHLEDevice"
HID_SERVICE_PREFIX = "{00001812-0000-1000-8000-00805f9b34fb}"
RC003_TOKEN = "dev_vid&012717_pid&32b8_rev&00a4"
WUDF_DIAGNOSTIC = "Device Parameters\\WUDFDiagnosticInfo"

# 钩子脚本：拦 BTHLE 读特征值的 IOCTL，输出 9 字节即遥控器按键报文
HOOK_JS = r"""
const READ_CHARACTERISTIC_IOCTL = 0x80018483;
const EXPECTED_OUTPUT_LENGTH = 9;
const HEARTBEAT_INTERVAL_MS = 5000;

let hookInstalled = false;

function hexOf(pointer, length) {
  if (pointer.isNull() || length <= 0) return "";
  const bytes = new Uint8Array(pointer.readByteArray(length));
  let out = "";
  for (let i = 0; i < bytes.length; i++) {
    out += bytes[i].toString(16).padStart(2, "0");
  }
  return out;
}

function installHook() {
  if (hookInstalled) return;
  const ntdll = Process.findModuleByName("ntdll.dll");
  const target = ntdll ? ntdll.findExportByName("NtDeviceIoControlFile") : null;
  if (target === null) {
    send({ kind: "error", message: "NtDeviceIoControlFile not found" });
    return;
  }
  Interceptor.attach(target, {
    onEnter(args) {
      this.capture = args[5].toUInt32() === READ_CHARACTERISTIC_IOCTL;
      if (this.capture) {
        this.output = args[8];
        this.outputLength = args[9].toUInt32();
      }
    },
    onLeave(retval) {
      if (!this.capture || retval.toUInt32() !== 0 || this.output.isNull()) return;
      try {
        if (this.outputLength === EXPECTED_OUTPUT_LENGTH) {
          send({ kind: "gatt_read", raw: hexOf(this.output, this.outputLength) });
        }
      } catch (e) {
        send({ kind: "error", message: String(e) });
      }
    }
  });
  hookInstalled = true;
  send({ kind: "ready", pid: Process.id });
}

installHook();
setInterval(() => send({ kind: "heartbeat", pid: Process.id }), HEARTBEAT_INTERVAL_MS);
"""


def find_rc003_host_pid() -> int | None:
    """从注册表定位承载 RC003 HID 服务的 WUDFHost PID。"""
    if os.name != "nt":
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, BTHLE_ENUM_KEY) as root:
            i = 0
            while True:
                try:
                    svc_name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                folded = svc_name.casefold()
                if not folded.startswith(HID_SERVICE_PREFIX):
                    continue
                if RC003_TOKEN not in folded:
                    continue
                with winreg.OpenKey(root, svc_name) as svc:
                    j = 0
                    while True:
                        try:
                            inst = winreg.EnumKey(svc, j)
                        except OSError:
                            break
                        j += 1
                        try:
                            with winreg.OpenKey(
                                root, f"{svc_name}\\{inst}\\{WUDF_DIAGNOSTIC}"
                            ) as dk:
                                value, _ = winreg.QueryValueEx(dk, "HostPid")
                            pid = int(value)
                            if pid > 0:
                                return pid
                        except (OSError, TypeError, ValueError):
                            continue
    except OSError:
        return None
    return None


def decode_tap_report(data: bytes) -> set[int]:
    """9 字节报文 -> usage 集合。前缀 01 00 00 + 3×LE16。"""
    if len(data) != 9 or data[:3] != b"\x01\x00\x00":
        return set()
    usages = {
        int.from_bytes(data[k:k + 2], "little") for k in range(3, 9, 2)
    }
    return usages - {0}


def suppress_voice_usage_report(data: bytes, enabled: bool = True) -> tuple[bytes, bool]:
    """镜像 Gadget 的 9 字节变换契约；只清 F5 槽，其余 usage 原样保留。"""
    if not enabled or len(data) != 9 or data[:3] != b"\x01\x00\x00":
        return data, False
    output = bytearray(data)
    changed = False
    for offset in range(3, 9, 2):
        if int.from_bytes(output[offset:offset + 2], "little") == VOICE_USAGE:
            output[offset:offset + 2] = b"\x00\x00"
            changed = True
    return bytes(output), changed


def enable_debug_privilege() -> None:
    """提权 helper 里启用 SeDebugPrivilege（附加 SYSTEM 进程需要）。"""
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class LUID(ctypes.Structure):
        _fields_ = (("LowPart", ctypes.c_ulong), ("HighPart", ctypes.c_long))

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = (("Luid", LUID), ("Attributes", ctypes.c_ulong))

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = (
            ("PrivilegeCount", ctypes.c_ulong),
            ("Privileges", LUID_AND_ATTRIBUTES * 1),
        )

    # 句柄宽度敏感：GetCurrentProcess 的伪句柄(0xFF..FF)必须按 64 位传递，
    # 不设 restype 会被截成 32 位导致 OpenProcessToken 报"句柄无效"
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetCurrentProcess.argtypes = ()
    advapi32.OpenProcessToken.argtypes = (
        ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p),
    )
    advapi32.OpenProcessToken.restype = ctypes.c_int
    advapi32.LookupPrivilegeValueW.argtypes = (
        ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p,
    )

    token = ctypes.c_void_p()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), 0x0020 | 0x0008, ctypes.byref(token)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        luid = LUID()
        if not advapi32.LookupPrivilegeValueW(
            None, "SeDebugPrivilege", ctypes.byref(luid)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = 2  # SE_PRIVILEGE_ENABLED
        ctypes.set_last_error(0)
        advapi32.AdjustTokenPrivileges(
            token, False, ctypes.byref(tp), 0, None, None
        )
        err = ctypes.get_last_error()
        if err == 1300:
            raise PermissionError("SeDebugPrivilege 未分配（需要管理员）")
        if err:
            raise ctypes.WinError(err)
    finally:
        kernel32.CloseHandle(token)


def _helper_log_path() -> Path:
    return Path(__file__).resolve().parent.parent / "logs" / "backkey-helper.log"


def _log(msg: str):
    """helper 双写：控制台 + 日志文件（提权控制台闪退时留证据）。"""
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        _helper_log_path().parent.mkdir(exist_ok=True)
        with _helper_log_path().open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# 发现模式脚本：记录所有设备 IO 完成，用于定位本机遥控器报文走的真实通道
DISCOVER_JS = r"""
function hexOf(pointer, length) {
  if (pointer.isNull() || length <= 0) return "";
  const n = Math.min(length, 16);
  const bytes = new Uint8Array(pointer.readByteArray(n));
  let out = "";
  for (let i = 0; i < bytes.length; i++) {
    out += bytes[i].toString(16).padStart(2, "0");
  }
  return out + (length > n ? ".." : "");
}

const ntdll = Process.findModuleByName("ntdll.dll");

Interceptor.attach(ntdll.findExportByName("NtDeviceIoControlFile"), {
  onEnter(args) {
    this.code = args[5].toUInt32();
    this.outBuf = args[8];
    this.outLen = args[9].toUInt32();
    this.status = args[4];
  },
  onLeave(retval) {
    if (retval.toUInt32() !== 0) return;
    if (this.outLen === 0 || this.outLen > 32) return;
    let info = this.outLen;
    try {
      info = this.status.add(8).readU64().toNumber();
    } catch (e) {}
    if (info === 0) return;
    send({ api: "ioctl", code: this.code, len: info, hex: hexOf(this.outBuf, info) });
  }
});

Interceptor.attach(ntdll.findExportByName("NtReadFile"), {
  onEnter(args) {
    this.buf = args[5];
    this.len = args[6].toUInt32();
    this.status = args[4];
  },
  onLeave(retval) {
    if (retval.toUInt32() !== 0) return;
    if (this.len === 0 || this.len > 32) return;
    let info = this.len;
    try {
      info = this.status.add(8).readU64().toNumber();
    } catch (e) {}
    if (info === 0) return;
    send({ api: "read", code: 0, len: info, hex: hexOf(this.buf, info) });
  }
});

send({ kind: "discover-ready", pid: Process.id });
"""


def run_discover_helper(pid: int, seconds: float = 45.0) -> int:
    """提权发现模式：把 WUDFHost 里的设备 IO 完成记到日志，供定位按键通道。"""
    try:
        return _run_discover(pid, seconds)
    finally:
        try:  # 无论正常结束还是崩溃，窗口都等回车再关
            input("\n按回车关闭窗口…")
        except Exception:
            pass


def _run_discover(pid: int, seconds: float) -> int:
    import frida

    if not ctypes.windll.shell32.IsUserAnAdmin():
        _log("discover helper 需要管理员权限")
        return 1
    enable_debug_privilege()
    session = frida.attach(pid)
    _log(f"discover 已附加 WUDFHost pid={pid}，现在按遥控器各键（含返回/音量）…")

    done = threading.Event()

    def on_message(message, _data):
        if message.get("type") == "send":
            p = message.get("payload") or {}
            if p.get("kind") == "discover-ready":
                _log("钩子就绪")
                return
            api = p.get("api", "?")
            code = p.get("code", 0)
            n = p.get("len", 0)
            hx = p.get("hex", "")
            mark = " <== 9字节!" if n == 9 else ""
            _log(f"{api} ioctl=0x{code:08X} len={n} {hx}{mark}")
        elif message.get("type") == "error":
            _log("脚本错误: " + str(message.get("description")))

    script = session.create_script(DISCOVER_JS)
    script.on("message", on_message)
    script.load()

    def stopper():
        time.sleep(seconds)
        done.set()

    threading.Thread(target=stopper, daemon=True).start()
    try:
        while not done.wait(1.0):
            pass
    except KeyboardInterrupt:
        pass
    _log("discover 结束")
    return 0


def run_helper(pid: int, port: int = TAP_PORT) -> int:
    """提权 helper：attach 到 WUDFHost，把钩子消息转发给主进程的本地端口。"""
    import frida

    if not ctypes.windll.shell32.IsUserAnAdmin():
        _log("helper 需要管理员权限运行")
        return 1
    enable_debug_privilege()
    try:
        session = frida.attach(pid)
    except Exception as e:
        _log(f"frida.attach 失败: {type(e).__name__}: {e}")
        return 2
    _log(f"已附加 WUDFHost pid={pid}，向 127.0.0.1:{port} 转发")

    def stream():
        failures = 0
        while True:
            try:
                sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            except OSError:
                failures += 1
                if failures >= 30:  # 主进程已退出约 30s，helper 自杀
                    _log("消费端不在线，helper 退出")
                    os._exit(0)
                time.sleep(1.0)
                continue
            failures = 0
            alive = [True]

            def on_message(message, _data):
                if message.get("type") == "send":
                    payload = message.get("payload") or {}
                    try:
                        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
                    except OSError:
                        alive[0] = False
                elif message.get("type") == "error":
                    _log("脚本错误: " + str(message.get("description")))

            try:
                script = session.create_script(HOOK_JS)
                script.on("message", on_message)
                script.load()
                while alive[0]:
                    time.sleep(0.5)
            finally:
                try:
                    sock.close()
                except OSError:
                    pass

    threading.Thread(target=stream, daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0


class BackKeyTap:
    """守护侧：拉起提权 helper + 本地消费报文 -> 按键边沿回调。"""

    def __init__(self, on_edge, log=print, auto_inject: bool = True,
                 suppress_voice: bool = False, on_suppression_state=None):
        """on_edge(name: str, is_down: bool)；name 见 USAGE_NAMES。"""
        self.on_edge = on_edge
        self.log = log
        self.auto_inject = auto_inject
        self.suppress_voice = suppress_voice
        self.on_suppression_state = on_suppression_state
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._active: set[int] = set()
        self._lock = threading.Lock()
        self._helper_launched_pid: int | None = None
        self._client: socket.socket | None = None
        self._refresh_attempted: set[int] = set()
        self._refreshed_host_pid: int | None = None
        self._suppression_ack = False
        self._control_ack = False
        self._voice_report_logged = False
        self._voice_channel_bound = False

    def _set_suppression_ack(self, ready: bool):
        ready = bool(ready and self.suppress_voice)
        if self._suppression_ack == ready:
            return
        self._suppression_ack = ready
        if self.on_suppression_state is not None:
            try:
                self.on_suppression_state(ready)
            except Exception as exc:
                self.log(f"HID 抑制状态回调失败: {exc}")

    def _handle_line(self, message: dict):
        kind = message.get("kind")
        if kind == "ready":
            protocol = message.get("protocol")
            script_version = message.get("script_version")
            supported = message.get("suppression_supported") is True
            if (
                protocol != GADGET_PROTOCOL_VERSION
                or script_version != GADGET_SCRIPT_VERSION
                or (self.suppress_voice and not supported)
            ):
                self.log("检测到旧版 Gadget；将刷新 RC003 宿主以恢复稳定的哑键通道")
                return "refresh"
            self.log(
                f"Gadget 协议 v{protocol} 已确认 "
                f"(script={script_version})"
            )
            return None
        if kind == "control_ack":
            was_accepted = self._control_ack
            accepted = (
                message.get("protocol") == GADGET_PROTOCOL_VERSION
                and message.get("suppress_voice") is self.suppress_voice
            )
            self._control_ack = accepted
            if accepted and not was_accepted:
                self.log("哑键通道控制握手已确认")
            if accepted and self.suppress_voice and not self._suppression_ack:
                self.log(
                    f"HID 级语音键抑制已确认（协议 v{GADGET_PROTOCOL_VERSION}，断线自动放行）"
                )
                self._set_suppression_ack(True)
            return None
        if kind == "voice_channel_bound":
            self._voice_channel_bound = True
            fingerprint = str(message.get("fingerprint") or "")
            preview = fingerprint[:48] + ("…" if len(fingerprint) > 48 else "")
            self.log(
                "已通过首个语音报告锁定 RC003 HID 通道 "
                f"(handle={message.get('handle')}, input={preview or 'empty'}, "
                f"reason={message.get('reason')})"
            )
            return None
        if kind == "voice_channel_lost":
            self._voice_channel_bound = False
            self._voice_report_logged = False
            self.log("RC003 HID 通道句柄已关闭；下一次语音报告将自动重新学习")
            return None
        if kind == "handle_probe":
            if not self.suppress_voice:
                return None
            if message.get("matched") is True:
                self.log("已锁定 RC003 HID 句柄，其他蓝牙 HID 不受影响")
            elif not message.get("name"):
                self.log("HID 句柄名称不可用；安全起见保持放行")
            else:
                self.log(f"忽略非 RC003 HID 句柄: {message.get('name')}")
            return None
        if kind == "gatt_read":
            raw = message.get("raw", "")
            try:
                data = bytes.fromhex(raw)
            except (TypeError, ValueError):
                return
            all_usages = decode_tap_report(data)
            if VOICE_USAGE in all_usages and not self._voice_report_logged:
                self._voice_report_logged = True
                suppressed = message.get("suppressed_voice") is True
                self.log(
                    f"语音键 HID 报告已确认: {raw} "
                    f"({'已抑制' if suppressed else '未抑制'})"
                )
            usages = all_usages & DEAD_USAGES
            with self._lock:
                prev = set(self._active)
                if usages == prev:
                    return
                pressed, released = usages - prev, prev - usages
                self._active = set(usages)
            for u in sorted(pressed):
                self.on_edge(USAGE_NAMES.get(u, f"usage_0x{u:04X}"), True)
            for u in sorted(released):
                self.on_edge(USAGE_NAMES.get(u, f"usage_0x{u:04X}"), False)

    def _launch_helper(self, pid: int, refresh: bool = False) -> bool:
        import sys
        # 提权会丢工作目录；注入器校验哈希后把 Gadget DLL 装进 WUDFHost 即退出，
        # Gadget 会主动连回本进程的 30685 端口推送按键报文。
        frozen = getattr(sys, "frozen", False)
        archive = Path(__file__).resolve().parent.parent / "assets" / (
            "frida-gadget-17.15.3-windows-x86_64.dll.xz"
        )
        if not archive.is_file():
            self.log(f"缺少 Gadget 资产: {archive}，tap 停用")
            return False
        if frozen:
            # 打包后没有独立 python/脚本：exe 自调用（launcher 会转发 --inject）
            exe = str(Path(sys.executable))
            params = f"--inject {'--refresh ' if refresh else ''}--pid {pid}"
        else:
            script = Path(__file__).resolve().with_name("tapinject.py")
            exe = str(Path(sys.executable))
            params = f'"{script}" --inject {'--refresh ' if refresh else ''}--pid {pid}'
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params, str(archive.parent), 0
        )
        ok = int(result) > 32
        self._helper_launched_pid = pid if ok else None
        operation = "刷新器" if refresh else "注入器"
        self.log(f"{operation}提权启动 {'成功' if ok else '被拒绝'} (pid={pid})")
        return ok

    def _send_control(self, client: socket.socket, enabled: bool | None = None) -> bool:
        value = self.suppress_voice if enabled is None else enabled
        payload = json.dumps(
            {"suppress_voice": bool(value)}, separators=(",", ":")
        ).encode("ascii") + b"\n"
        try:
            client.sendall(payload)
            return True
        except OSError:
            return False

    def arm_voice_suppression(self, window_ms: int = 1000) -> bool:
        """ATVV AUDIO_START 时短暂允许重学习，处理蓝牙重连后的句柄变化。"""
        with self._lock:
            client = self._client
        if client is None or not self._suppression_ack or not self.suppress_voice:
            return False
        payload = json.dumps(
            {"arm_voice_ms": max(100, min(2000, int(window_ms)))},
            separators=(",", ":"),
        ).encode("ascii") + b"\n"
        try:
            client.sendall(payload)
            return True
        except OSError:
            return False

    def _probe_existing(self, server: socket.socket, pid: int,
                        timeout: float = 2.5) -> socket.socket | None:
        """给上一轮注入的 Gadget 一个回连窗口（它每秒重连一次）。返回 client 或 None。"""
        deadline = time.time() + timeout
        while time.time() < deadline and not self.stop_event.is_set():
            try:
                client, _ = server.accept()
                return client
            except socket.timeout:
                continue
        return None

    def _run(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind(("127.0.0.1", TAP_PORT))
            server.listen(8)
        except OSError as e:
            self.log(f"tap 端口绑定失败（已有实例在跑？）: {e}")
            return
        server.settimeout(1.0)
        self.log(f"tap 监听 127.0.0.1:{TAP_PORT}")
        while not self.stop_event.is_set():
            pid = find_rc003_host_pid()
            if pid is None:
                self.stop_event.wait(2.0)
                continue

            client = None
            if self.auto_inject and self._helper_launched_pid != pid:
                # 先让旧 Gadget 有机会回连，避免每次启动都重新注入弹 UAC
                timeout = 12.0 if self._refreshed_host_pid == pid else 2.5
                self._refreshed_host_pid = None
                client = self._probe_existing(server, pid, timeout=timeout)
                if client is not None:
                    self._helper_launched_pid = pid
                    self.log("检测到已注入的 Gadget 回连，跳过重复注入")
                else:
                    if not self._launch_helper(pid):
                        self.stop_event.wait(5.0)

            if client is None:
                try:
                    client, _ = server.accept()
                except socket.timeout:
                    continue
            client.settimeout(1.0)
            self.log("helper 已连接")
            buffer = b""
            refresh_required = False
            with self._lock:
                self._client = client
            self._set_suppression_ack(False)
            self._control_ack = False
            self._send_control(client)
            try:
                while not self.stop_event.is_set():
                    if find_rc003_host_pid() != pid:
                        self.log("WUDFHost 已重启，将重新注入")
                        self._helper_launched_pid = None
                        break
                    try:
                        chunk = client.recv(65536)
                    except socket.timeout:
                        if not self._control_ack and select.select([server], [], [], 0)[0]:
                            self.log("检测到重复 Gadget 回连；丢弃未响应控制握手的空闲连接")
                            break
                        continue
                    except OSError as exc:
                        self.log(f"helper 连接中断: {exc}，等待自动重连")
                        break
                    if chunk == b"":
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        try:
                            result = self._handle_line(json.loads(line.decode("utf-8")))
                            if result == "refresh":
                                refresh_required = True
                                break
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                    if refresh_required:
                        break
            finally:
                client.close()
                self._set_suppression_ack(False)
                with self._lock:
                    if self._client is client:
                        self._client = None
                    released = set(self._active)
                    self._active = set()
                for u in sorted(released):
                    self.on_edge(USAGE_NAMES.get(u, str(u)), False)
            if refresh_required and pid not in self._refresh_attempted:
                self._set_suppression_ack(False)
                self._refresh_attempted.add(pid)
                if self._launch_helper(pid, refresh=True):
                    deadline = time.monotonic() + 35.0
                    while time.monotonic() < deadline and not self.stop_event.is_set():
                        candidate = find_rc003_host_pid()
                        if candidate and candidate != pid:
                            self._helper_launched_pid = None
                            self._refreshed_host_pid = candidate
                            break
                        self.stop_event.wait(0.25)
        server.close()

    def start(self) -> bool:
        self.thread = threading.Thread(target=self._run, name="backkey-tap", daemon=True)
        self.thread.start()
        return True

    def stop(self):
        self.stop_event.set()
        self._set_suppression_ack(False)
        with self._lock:
            client = self._client
        if client is not None:
            self._send_control(client, enabled=False)
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=3.0)


def main(argv: list[str] | None = None) -> int:
    import sys as _sys
    try:
        if "--helper" in (argv if argv is not None else _sys.argv[1:]):
            _log("helper 启动: " + " ".join((argv if argv is not None else _sys.argv[1:])))
        return _main_inner(argv)
    except SystemExit:
        raise
    except Exception:
        import traceback
        tb = traceback.format_exc()
        try:
            _log("未捕获异常:\n" + tb)
        except Exception:
            pass
        raise


def _main_inner(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RC003 哑键拦截（返回/音量）")
    ap.add_argument("--helper", action="store_true", help="内部：提权附加模式")
    ap.add_argument("--discover", action="store_true", help="发现模式：记录所有设备 IO，定位按键通道")
    ap.add_argument("--seconds", type=float, default=45.0, help="发现模式时长")
    ap.add_argument("--pid", type=int, default=None, help="目标 WUDFHost PID")
    args = ap.parse_args(argv)
    pid = args.pid or find_rc003_host_pid()

    if args.helper:
        if not pid:
            print("找不到 RC003 WUDFHost")
            return 1
        if args.discover:
            return run_discover_helper(pid, args.seconds)
        return run_helper(pid)

    if args.discover:
        # 弹 UAC 启动提权发现 helper，控制台可见，实时滚日志
        if not pid:
            print("找不到 RC003 WUDFHost")
            return 1
        import sys
        # 绝对路径启动 backkey.py（提权会丢工作目录，-m 会找不到包）
        script = Path(__file__).resolve()
        exe = str(Path(sys.executable))
        params = f'"{script}" --helper --discover --seconds {args.seconds} --pid {pid}'
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params, str(script.parent), 1
        )
        ok = int(result) > 32
        print("发现模式 helper " + ("已启动，在新窗口里按遥控器各键" if ok else "启动失败(UAC被拒?)"))
        print(f"结束后日志在: {_helper_log_path()}")
        return 0 if ok else 1

    # 普通测试模式
    print(f"RC003 WUDFHost pid = {pid}")
    if pid is None:
        return 1
    tap = BackKeyTap(on_edge=lambda name, down: print(
        f"[{time.strftime('%H:%M:%S')}] {'按下' if down else '松开'} {name}"
    ))
    tap.start()
    print("tap 测试中：会弹一次 UAC 提权（附加 WUDFHost 需要），然后按遥控器的 返回/音量±/静音。Ctrl+C 退出。")
    print(f"helper 日志: {_helper_log_path()}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        tap.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
