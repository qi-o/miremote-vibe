"""Frida Gadget 注入器：把按键钩子 DLL 装进 RC003 的 HidOverGatt 宿主（WUDFHost）。

为什么不用 frida-python 的 attach：实测 frida-agent 对 WUDFHost 这种驱动宿主注入会被拒
（ProcessNotRespondingError）。本模块移植自 GPL-3.0 项目
xxb26553663-star/remote-bridge-hub 的 hid_tap_injector.py，用其真机验证过的
"轻量 Frida Gadget DLL + LoadLibraryW 远程线程"路线，并保留全部安全护栏：

- 只注入 注册表 HostPid 指向的、进程名必须是 wudfhost.exe 的目标
- DLL 与压缩包双重 SHA-256 校验（锁定 frida-gadget 17.15.3 官方发布物）
- 注入器必须以管理员运行（UAC 同意后才会走到这里）

注入成功后 Gadget 常驻 WUDFHost 内，主动连接 127.0.0.1:30685 推送按键报文，
注入器进程随即退出。Gadget 断线自动重连；WUDFHost 重启后需重新注入一次。
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import lzma
import os
import shutil
import threading
from ctypes import wintypes
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

GADGET_VERSION = "17.15.3"
GADGET_ARCHIVE = PROJECT_ROOT / "assets" / "frida-gadget-17.15.3-windows-x86_64.dll.xz"
GADGET_ARCHIVE_SHA256 = (
    "b566d70189b6d551ad8f4e0bea24de08a3d4c0f559bb35b2bdb67d45182240c2"
)
GADGET_DLL_SHA256 = (
    "6fca4007b2284c765a6c15c967a741f536b5865bf83867326a54029a3b752748"
)
TAP_PORT = int(os.environ.get("MIREMOTE_TAP_PORT", "30685"))
DLL_BASENAME = "miremote-tap"

# Gadget 内运行的钩子脚本：拦 BTHLE 读特征值的 IOCTL，9 字节输出经本地 socket 推回。
# 开发版可通过双向控制通道要求仅在 RC003 句柄上抹掉 F5 usage；断线时立即恢复放行。
GADGET_JS = """
const READ_CHARACTERISTIC_IOCTL = 0x80018483;
const EXPECTED_OUTPUT_LENGTH = 9;
const PROTOCOL_VERSION = 4;
const SCRIPT_VERSION = "2026.08.29-tap4";
const VOICE_USAGE = 0x003e;
const RC003_TOKEN = "vid&012717_pid&32b8";
const HEARTBEAT_INTERVAL_MS = 5000;
const RECONNECT_DELAY_MS = 1000;
const MAX_OBJECT_NAME_BYTES = 65536;

let connection = null;
let output = null;
let connectionPending = false;
let writeChain = Promise.resolve();
let reconnectTimer = null;
let hookInstalled = false;
let suppressVoice = false;
let voiceArmUntil = 0;
let voiceChannel = null;
let commandBuffer = "";
const handleNames = new Map();

function asciiBytes(text) {
  const result = [];
  for (let i = 0; i < text.length; i++) result.push(text.charCodeAt(i) & 0xff);
  return result;
}

function hexOf(pointer, length) {
  if (pointer.isNull() || length <= 0) return "";
  const bytes = new Uint8Array(pointer.readByteArray(length));
  let out = "";
  for (let i = 0; i < bytes.length; i++) {
    out += bytes[i].toString(16).padStart(2, "0");
  }
  return out;
}

function textOf(buffer) {
  const bytes = new Uint8Array(buffer);
  let out = "";
  for (let i = 0; i < bytes.length; i++) out += String.fromCharCode(bytes[i]);
  return out;
}

function scheduleReconnect() {
  if (output !== null || connectionPending || reconnectTimer !== null) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectToHub();
  }, RECONNECT_DELAY_MS);
}

function dropConnection(current) {
  if (connection !== current) return;
  suppressVoice = false;
  voiceArmUntil = 0;
  commandBuffer = "";
  connection = null;
  output = null;
  scheduleReconnect();
}

function emit(payload) {
  const current = output;
  if (current === null) {
    scheduleReconnect();
    return;
  }
  const line = JSON.stringify(payload) + "\\n";
  writeChain = writeChain
    .then(() => current.writeAll(asciiBytes(line)))
    .catch(() => {
      if (output === current) dropConnection(connection);
    });
}

function applyCommand(command) {
  if (typeof command !== "object" || command === null) return;
  if (typeof command.suppress_voice === "boolean") {
    suppressVoice = command.suppress_voice;
    if (!suppressVoice) voiceArmUntil = 0;
    emit({
      kind: "control_ack",
      protocol: PROTOCOL_VERSION,
      suppress_voice: suppressVoice,
      channel_learned: voiceChannel !== null
    });
  }
  if (suppressVoice && typeof command.arm_voice_ms === "number") {
    const duration = Math.max(100, Math.min(2000, command.arm_voice_ms));
    voiceArmUntil = Date.now() + duration;
    emit({ kind: "voice_arm_ack", duration_ms: duration });
  }
}

async function readCommands(current) {
  try {
    while (connection === current) {
      const chunk = await current.input.read(4096);
      if (chunk.byteLength === 0) break;
      commandBuffer += textOf(chunk);
      let newline = commandBuffer.indexOf("\\n");
      while (newline !== -1) {
        const line = commandBuffer.slice(0, newline).trim();
        commandBuffer = commandBuffer.slice(newline + 1);
        if (line.length > 0) {
          try {
            applyCommand(JSON.parse(line));
          } catch (_e) {}
        }
        newline = commandBuffer.indexOf("\\n");
      }
    }
  } catch (_e) {}
  dropConnection(current);
}

async function connectToHub() {
  if (output !== null || connectionPending) return;
  connectionPending = true;
  try {
    const current = await Socket.connect({
      family: "ipv4", host: "127.0.0.1", port: %PORT%
    });
    connection = current;
    output = current.output;
    readCommands(current);
    emit({
      kind: "ready",
      pid: Process.id,
      hook_installed: hookInstalled,
      protocol: PROTOCOL_VERSION,
      script_version: SCRIPT_VERSION,
      suppression_supported: true,
      channel_learned: voiceChannel !== null
    });
  } catch (_e) {
    connection = null;
    output = null;
  } finally {
    connectionPending = false;
    if (output === null) scheduleReconnect();
  }
}

function makeObjectNameReader(ntdll) {
  const address = ntdll.findExportByName("NtQueryObject");
  if (address === null) return null;
  const query = new NativeFunction(
    address, "int", ["pointer", "uint", "pointer", "uint", "pointer"]
  );
  return function (handle) {
    const returnLength = Memory.alloc(4);
    returnLength.writeU32(0);
    query(handle, 1, ptr(0), 0, returnLength);
    const needed = returnLength.readU32();
    if (needed < 16 || needed > MAX_OBJECT_NAME_BYTES) return "";
    const info = Memory.alloc(needed);
    returnLength.writeU32(0);
    const status = query(handle, 1, info, needed, returnLength);
    if (status < 0) return "";
    const length = info.readU16();
    const namePointer = info.add(Process.pointerSize === 8 ? 8 : 4).readPointer();
    if (length === 0 || (length & 1) !== 0 || namePointer.isNull()) return "";
    const used = Math.min(returnLength.readU32() || needed, needed);
    if (
      namePointer.compare(info) < 0 ||
      namePointer.add(length).compare(info.add(used)) > 0
    ) return "";
    return namePointer.readUtf16String(length / 2);
  };
}

function describeHandle(handle, readObjectName) {
  const key = handle.toString();
  if (handleNames.has(key)) return handleNames.get(key);
  let name = "";
  try {
    if (readObjectName !== null) name = readObjectName(handle);
  } catch (_e) {}
  const matched = name.toLowerCase().indexOf(RC003_TOKEN) !== -1;
  const description = { key: key, name: name, matched: matched };
  handleNames.set(key, description);
  emit({ kind: "handle_probe", handle: key, name: name, matched: matched });
  return description;
}

function fingerprintOf(pointer, length) {
  if (pointer.isNull() || length <= 0) return "";
  const captured = Math.min(length, 96);
  return String(length) + ":" + hexOf(pointer, captured);
}

function hasVoiceUsage(pointer, length) {
  if (length !== EXPECTED_OUTPUT_LENGTH) return false;
  const bytes = new Uint8Array(pointer.readByteArray(length));
  if (bytes[0] !== 1 || bytes[1] !== 0 || bytes[2] !== 0) return false;
  for (let offset = 3; offset < 9; offset += 2) {
    if ((bytes[offset] | (bytes[offset + 1] << 8)) === VOICE_USAGE) return true;
  }
  return false;
}

function voiceChannelMatches(handleKey, fingerprint) {
  if (voiceChannel === null || voiceChannel.handle !== handleKey) return false;
  if (voiceChannel.fingerprint === "") return true;
  return voiceChannel.fingerprint === fingerprint;
}

function learnVoiceChannel(description, fingerprint, reason) {
  voiceChannel = {
    handle: description.key,
    name: description.name,
    fingerprint: fingerprint
  };
  voiceArmUntil = 0;
  emit({
    kind: "voice_channel_bound",
    handle: description.key,
    name: description.name,
    fingerprint: fingerprint,
    reason: reason
  });
}

function suppressVoiceUsage(pointer, length) {
  if (!suppressVoice || length !== EXPECTED_OUTPUT_LENGTH) return false;
  const bytes = new Uint8Array(pointer.readByteArray(length));
  if (bytes[0] !== 1 || bytes[1] !== 0 || bytes[2] !== 0) return false;
  let changed = false;
  for (let offset = 3; offset < 9; offset += 2) {
    const usage = bytes[offset] | (bytes[offset + 1] << 8);
    if (usage === VOICE_USAGE) {
      pointer.add(offset).writeU8(0);
      pointer.add(offset + 1).writeU8(0);
      changed = true;
    }
  }
  return changed;
}

function installHook() {
  if (hookInstalled) return;
  const ntdll = Process.findModuleByName("ntdll.dll");
  const target = ntdll ? ntdll.findExportByName("NtDeviceIoControlFile") : null;
  if (target === null) return;
  const readObjectName = makeObjectNameReader(ntdll);
  Interceptor.attach(target, {
    onEnter(args) {
      this.capture = args[5].toUInt32() === READ_CHARACTERISTIC_IOCTL;
      if (this.capture) {
        this.fileHandle = args[0];
        this.inputLength = args[7].toUInt32();
        this.inputFingerprint = fingerprintOf(args[6], this.inputLength);
        this.output = args[8];
        this.outputLength = args[9].toUInt32();
      }
    },
    onLeave(retval) {
      if (!this.capture || retval.toUInt32() !== 0 || this.output.isNull()) return;
      try {
        if (this.outputLength === EXPECTED_OUTPUT_LENGTH) {
          const raw = hexOf(this.output, this.outputLength);
          const description = describeHandle(this.fileHandle, readObjectName);
          const containsVoice = hasVoiceUsage(this.output, this.outputLength);
          let channelMatch = description.matched || voiceChannelMatches(
            description.key, this.inputFingerprint
          );
          if (
            suppressVoice && containsVoice && !channelMatch &&
            (voiceChannel === null || Date.now() <= voiceArmUntil)
          ) {
            learnVoiceChannel(
              description,
              this.inputFingerprint,
              voiceChannel === null ? "first_voice_report" : "atvv_rearm"
            );
            channelMatch = true;
          }
          let suppressed = false;
          if (channelMatch) {
            suppressed = suppressVoiceUsage(this.output, this.outputLength);
          }
          emit({
            kind: "gatt_read",
            raw: raw,
            suppressed_voice: suppressed,
            handle: description.key,
            fingerprint: this.inputFingerprint,
            channel_match: channelMatch
          });
        }
      } catch (_e) {}
    }
  });
  const closeTarget = ntdll.findExportByName("NtClose");
  if (closeTarget !== null) {
    Interceptor.attach(closeTarget, {
      onEnter(args) {
        const key = args[0].toString();
        handleNames.delete(key);
        if (voiceChannel !== null && voiceChannel.handle === key) {
          const lost = voiceChannel;
          voiceChannel = null;
          emit({ kind: "voice_channel_lost", handle: key, name: lost.name });
        }
      }
    });
  }
  hookInstalled = true;
}

setInterval(() => {
  if (output === null) scheduleReconnect();
  else emit({
    kind: "heartbeat",
    pid: Process.id,
    protocol: PROTOCOL_VERSION,
    script_version: SCRIPT_VERSION,
    suppress_voice: suppressVoice,
    channel_learned: voiceChannel !== null
  });
}, HEARTBEAT_INTERVAL_MS);

installHook();
connectToHub();
""".replace("%PORT%", str(TAP_PORT))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def runtime_dir() -> Path:
    base = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    return (
        base / "MiRemoteVibe" / "hid-tap"
        / f"{GADGET_VERSION}-x64-{GADGET_DLL_SHA256[:12]}"
    )


def gadget_config_text() -> str:
    return json.dumps(
        {
            "interaction": {
                "type": "script",
                "path": f"{DLL_BASENAME}.js",
                "on_change": "reload",
            },
            "runtime": "qjs",
            "teardown": "minimal",
        },
        indent=2,
    ) + "\n"


def prepare_runtime(log=print) -> Path:
    """校验并布置 Gadget 运行时（DLL + config + js），返回 DLL 路径。"""
    if not GADGET_ARCHIVE.is_file():
        raise FileNotFoundError(f"缺少 Gadget 压缩包: {GADGET_ARCHIVE}")
    if sha256_file(GADGET_ARCHIVE) != GADGET_ARCHIVE_SHA256:
        raise RuntimeError("Gadget 压缩包 SHA-256 校验失败")

    dest = runtime_dir()
    dest.mkdir(parents=True, exist_ok=True)
    dll_path = dest / f"{DLL_BASENAME}.dll"
    if not dll_path.is_file() or sha256_file(dll_path) != GADGET_DLL_SHA256:
        tmp = dll_path.with_suffix(f".dll.{os.getpid()}.tmp")
        try:
            with lzma.open(GADGET_ARCHIVE, "rb") as src, tmp.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            if sha256_file(tmp) != GADGET_DLL_SHA256:
                raise RuntimeError("Gadget DLL SHA-256 校验失败")
            os.replace(tmp, dll_path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    # 原子写 config 与 js（存在且内容一致则跳过）
    for name, text in (
        (f"{DLL_BASENAME}.config", gadget_config_text()),
        (f"{DLL_BASENAME}.js", GADGET_JS),
    ):
        p = dest / name
        encoded = text.encode("utf-8")
        if not (p.is_file() and p.read_bytes() == encoded):
            tmp = p.with_name(p.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_bytes(encoded)
            os.replace(tmp, p)
    log(f"运行时就绪: {dll_path}")
    return dll_path


def is_wudfhost_process(pid: int) -> bool:
    """只接受仍存活且进程名精确为 WUDFHost.exe 的目标。"""
    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260),
        ]

    k32 = ctypes.WinDLL("kernel32")
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    k32.Process32FirstW.argtypes = (ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W))
    k32.Process32NextW.argtypes = (ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W))
    k32.CloseHandle.argtypes = (ctypes.c_void_p,)

    snapshot = k32.CreateToolhelp32Snapshot(2, 0)
    if snapshot in (None, ctypes.c_void_p(-1).value):
        return False
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        if not k32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return False
        while True:
            if entry.th32ProcessID == pid:
                return entry.szExeFile.casefold() == "wudfhost.exe"
            if not k32.Process32NextW(snapshot, ctypes.byref(entry)):
                return False
    finally:
        k32.CloseHandle(snapshot)


def restart_host_and_inject(pid: int, dll_path: Path, log=print) -> int:
    """结束已验证的 RC003 宿主，等待系统重建后注入新版 Gadget。"""
    try:
        from .backkey import find_rc003_host_pid
    except ImportError:
        from backkey import find_rc003_host_pid

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.TerminateProcess.argtypes = (ctypes.c_void_p, wintypes.UINT)
    k32.TerminateProcess.restype = wintypes.BOOL
    k32.WaitForSingleObject.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.CloseHandle.argtypes = (ctypes.c_void_p,)

    process = k32.OpenProcess(0x0001 | 0x00100000, False, pid)
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not k32.TerminateProcess(process, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        k32.WaitForSingleObject(process, 10_000)
    finally:
        k32.CloseHandle(process)
    log(f"旧 Gadget 宿主已结束 pid={pid}，等待 RC003 宿主重建")

    import time
    deadline = time.monotonic() + 25.0
    new_pid = None
    while time.monotonic() < deadline:
        candidate = find_rc003_host_pid()
        if candidate and candidate != pid and is_wudfhost_process(candidate):
            new_pid = candidate
            break
        time.sleep(0.25)
    if new_pid is None:
        raise TimeoutError("RC003 WUDFHost 未在 25 秒内重建")
    log(f"RC003 新宿主 pid={new_pid}，注入最新版 Gadget")
    inject_library(new_pid, dll_path, log=log)
    return new_pid


def inject_library(pid: int, dll_path: Path, log=print) -> None:
    """经典 LoadLibraryW 远程线程注入（仅限已验证的 WUDFHost 目标）。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.VirtualAllocEx.restype = ctypes.c_void_p
    k32.VirtualAllocEx.argtypes = (
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD,
        wintypes.DWORD,
    )
    k32.WriteProcessMemory.restype = wintypes.BOOL
    k32.WriteProcessMemory.argtypes = (
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    )
    k32.VirtualFreeEx.restype = wintypes.BOOL
    k32.VirtualFreeEx.argtypes = (
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD,
    )
    k32.GetModuleHandleW.restype = ctypes.c_void_p
    k32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    k32.GetProcAddress.restype = ctypes.c_void_p
    k32.GetProcAddress.argtypes = (ctypes.c_void_p, wintypes.LPCSTR)
    k32.CreateRemoteThread.restype = ctypes.c_void_p
    k32.CreateRemoteThread.argtypes = (
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    )
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.WaitForSingleObject.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    k32.GetExitCodeThread.restype = wintypes.BOOL
    k32.GetExitCodeThread.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD))
    k32.CloseHandle.argtypes = (ctypes.c_void_p,)

    rights = 0x0002 | 0x0400 | 0x0008 | 0x0010 | 0x0020  # CREATE_THREAD|QUERY|VM_*
    process = k32.OpenProcess(rights, False, pid)
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    remote_path = None
    thread = None
    try:
        encoded = (str(dll_path.resolve()) + "\0").encode("utf-16-le")
        remote_path = k32.VirtualAllocEx(
            process, None, len(encoded), 0x3000, 0x04  # MEM_COMMIT|RESERVE, RW
        )
        if not remote_path:
            raise ctypes.WinError(ctypes.get_last_error())
        buf = ctypes.create_string_buffer(encoded)
        written = ctypes.c_size_t()
        if not k32.WriteProcessMemory(
            process, remote_path, buf, len(encoded), ctypes.byref(written)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        kernel = k32.GetModuleHandleW("kernel32.dll")
        load_library = k32.GetProcAddress(kernel, b"LoadLibraryW")
        if not load_library:
            raise ctypes.WinError(ctypes.get_last_error())
        tid = wintypes.DWORD()
        thread = k32.CreateRemoteThread(
            process, None, 0, load_library, remote_path, 0, ctypes.byref(tid)
        )
        if not thread:
            raise ctypes.WinError(ctypes.get_last_error())
        if k32.WaitForSingleObject(thread, 20_000) != 0:
            raise TimeoutError("远程 LoadLibraryW 超时")
        code = wintypes.DWORD()
        if not k32.GetExitCodeThread(thread, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        if code.value == 0:
            raise RuntimeError("远程 LoadLibraryW 返回 NULL（DLL 加载失败）")
        log(f"注入成功 pid={pid} 模块句柄={code.value:#x}")
    finally:
        if thread:
            k32.CloseHandle(thread)
        if remote_path:
            k32.VirtualFreeEx(process, remote_path, 0, 0x8000)
        k32.CloseHandle(process)


def run_inject(pid: int, log=print, refresh: bool = False) -> int:
    """提权入口：护栏校验 -> 布置运行时 -> 注入。"""
    try:
        from .backkey import enable_debug_privilege, find_rc003_host_pid
    except ImportError:  # 作为独立脚本运行（提权 helper 的启动方式）
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from backkey import enable_debug_privilege, find_rc003_host_pid

    if not ctypes.windll.shell32.IsUserAnAdmin():
        log("注入器需要管理员权限")
        return 1
    expected = find_rc003_host_pid()
    if expected != pid:
        log(f"目标已变化: 注册表={expected} 请求={pid}，拒绝注入")
        return 1

    if not is_wudfhost_process(pid):
        log(f"目标进程不是 wudfhost.exe，拒绝注入 (pid={pid})")
        return 1

    enable_debug_privilege()
    dll = prepare_runtime(log=log)
    if sha256_file(dll) != GADGET_DLL_SHA256:
        log("DLL 哈希在注入前发生变化，中止")
        return 1
    if refresh:
        restart_host_and_inject(pid, dll, log=log)
    else:
        inject_library(pid, dll, log=log)
    return 0


def cli(argv: list[str] | None = None) -> int:
    """命令行入口（源码脚本直跑与 frozen exe 转发共用）。"""
    import argparse

    def _log(msg: str):
        line = f"{__import__('time').strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        try:
            logdir = PROJECT_ROOT / "logs"
            logdir.mkdir(exist_ok=True)
            (logdir / "backkey-helper.log").open("a", encoding="utf-8").write(line + "\n")
        except OSError:
            pass

    ap = argparse.ArgumentParser(description="RC003 Gadget 注入器（需管理员）")
    ap.add_argument("--inject", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--pid", type=int, required=True)
    args = ap.parse_args(argv)
    try:
        rc = run_inject(args.pid, log=_log, refresh=args.refresh)
    except Exception:
        import traceback
        _log("注入异常:\n" + traceback.format_exc())
        rc = 1
    _log(f"注入器退出 rc={rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(cli())
