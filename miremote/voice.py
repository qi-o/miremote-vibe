"""ATVV 语音客户端：小米蓝牙遥控器 2 Pro 麦克风 → WAV。

协议依据 mi-ao 项目真机文档（固件 2671，同款）：
- 服务 AB5E0001-5A21-4F05-BC7D-AF01F617B664
- AB5E0002 = TX（主机写命令），AB5E0003 = 音频 notify，AB5E0004 = 控制 notify
- GET_CAPS `0A 01 00 00 03 03`；v1.0 MIC_OPEN `0C 00`，MIC_CLOSE `0D <stream>`
- 音频 IMA ADPCM 16kHz、120 字节帧，高 nibble 先行；v0.4 帧带 6 字节头
  （大端 seq + padding + predictor + step index）

用法:
  python -m miremote voice --caps          # 握手自检（不需要按键）
  python -m miremote voice --record 12     # 录 12 秒：MIC_OPEN 后按住遥控器语音键说话
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import struct
import threading
import time
import wave
from pathlib import Path

# 仅源码运行时启用 faulthandler：打包后(frozen) sys.stderr 为 None，
# faulthandler.enable() 会抛 RuntimeError
if not getattr(sys, "frozen", False):
    import faulthandler
    faulthandler.enable()


def _configure_stdio():
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


_configure_stdio()

DEFAULT_ADDR = None  # None = 自动发现（注册表枚举已配对设备，见 blediscover.py）

UUID_SVC = "ab5e0001-5a21-4f05-bc7d-af01f617b664"
UUID_TX = "ab5e0002-5a21-4f05-bc7d-af01f617b664"
UUID_RX_AUDIO = "ab5e0003-5a21-4f05-bc7d-af01f617b664"
UUID_CTRL = "ab5e0004-5a21-4f05-bc7d-af01f617b664"

# ATVV 控制消息（v1.0；opcode 猜测处以抓包为准）
OP_GET_CAPS = 0x0A
OP_CAPS_RESP = 0x0B
OP_MIC_OPEN = 0x0C
OP_MIC_CLOSE = 0x0D
OP_START_SEARCH = 0x10  # 部分固件按语音键时先来这个
OP_AUDIO_START = 0x11
OP_AUDIO_SYNC = 0x12
OP_AUDIO_STOP = 0x13

OPCODE_NAMES = {
    OP_CAPS_RESP: "CAPS_RESP", OP_MIC_OPEN: "MIC_OPEN", OP_MIC_CLOSE: "MIC_CLOSE",
    OP_START_SEARCH: "START_SEARCH", OP_AUDIO_START: "AUDIO_START",
    OP_AUDIO_SYNC: "AUDIO_SYNC", OP_AUDIO_STOP: "AUDIO_STOP",
}


def ibuffer_bytes(buf) -> bytes:
    try:
        return bytes(buf)
    except Exception:
        pass
    from winrt.windows.storage.streams import DataReader
    dr = DataReader.from_buffer(buf)
    out = bytearray(dr.unconsumed_buffer_length)
    for i in range(len(out)):
        out[i] = dr.read_byte()
    return bytes(out)


def to_ibuffer(data: bytes):
    """bytes -> winrt IBuffer。"""
    from winrt.windows.storage.streams import DataWriter
    w = DataWriter()
    w.write_bytes(data)
    return w.detach_buffer()


# ---- IMA ADPCM 解码（16 kHz, 高 nibble 先行） ----

STEP_TABLE = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
    253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
    1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327,
    3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
    11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
    32767,
]
INDEX_TABLE = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8]


class ImaAdpcmDecoder:
    def __init__(self, predictor: int = 0, step_index: int = 0):
        self.predictor = predictor
        self.step_index = step_index

    def decode_nibble(self, nibble: int) -> int:
        step = STEP_TABLE[self.step_index]
        diff = step >> 3
        if nibble & 1:
            diff += step >> 2
        if nibble & 2:
            diff += step >> 1
        if nibble & 4:
            diff += step
        self.predictor += diff if nibble & 8 else -diff
        self.predictor = max(-32768, min(32767, self.predictor))
        self.step_index = max(0, min(88, self.step_index + INDEX_TABLE[nibble]))
        return self.predictor

    def decode(self, data: bytes) -> list[int]:
        pcm = []
        for b in data:
            pcm.append(self.decode_nibble(b >> 4))       # 高 nibble 先
            pcm.append(self.decode_nibble(b & 0x0F))
        return pcm


def write_wav(path: Path, pcm: list[int], rate: int = 16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{len(pcm)}h", *pcm))


class AtvvClient:
    def __init__(self, addr: int = DEFAULT_ADDR):
        self.addr = addr
        self.dev = None
        self.tx = None
        self.audio_ch = None
        self.ctrl_ch = None
        self.audio_frames: list[bytes] = []
        self.events: list[tuple[str, bytes]] = []
        self.caps_resp: bytes | None = None
        self.audio_stopped = asyncio.Event()
        self.audio_started = asyncio.Event()
        self.caps_got = asyncio.Event()
        self._tokens = []
        self._decoder = ImaAdpcmDecoder()
        self._v04_frames = False
        self._audio_subscribed = False
        self.on_audio_live = None  # 微信模式实时回调: callable(raw_frame_bytes)

    async def connect(self):
        if self.addr is None:
            from .blediscover import find_remote_addr
            self.addr = find_remote_addr()
            if not self.addr:
                raise RuntimeError(
                    "未发现已配对的小米遥控器（请先在系统蓝牙设置里完成配对）")
        from winrt.windows.devices.bluetooth import BluetoothLEDevice, BluetoothCacheMode
        self.dev = await BluetoothLEDevice.from_bluetooth_address_async(self.addr)
        if self.dev is None:
            raise RuntimeError("打不开 BLE 设备（蓝牙没连上？）")
        svc_res = await self.dev.get_gatt_services_with_cache_mode_async(
            BluetoothCacheMode.UNCACHED
        )
        svc = None
        for s in svc_res.services:
            if str(s.uuid).lower() == UUID_SVC:
                svc = s
                break
        if svc is None:
            raise RuntimeError("设备上没有 AB5E0001 ATVV 服务")
        cr = await svc.get_characteristics_with_cache_mode_async(
            BluetoothCacheMode.UNCACHED
        )
        for ch in cr.characteristics:
            cu = str(ch.uuid).lower()
            if cu == UUID_TX:
                self.tx = ch
            elif cu == UUID_RX_AUDIO:
                self.audio_ch = ch
            elif cu == UUID_CTRL:
                self.ctrl_ch = ch
        if not (self.tx and self.audio_ch and self.ctrl_ch):
            raise RuntimeError("ATVV 特征不全")

        # 订阅控制通道
        token = self.ctrl_ch.add_value_changed(self._on_ctrl)
        self._tokens.append((self.ctrl_ch, token))
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattClientCharacteristicConfigurationDescriptorValue,
        )
        st = await self.ctrl_ch.write_client_characteristic_configuration_descriptor_async(
            GattClientCharacteristicConfigurationDescriptorValue.NOTIFY
        )
        print(f"控制通道已订阅 (状态 {st})")

    async def write(self, data: bytes):
        st = await self.tx.write_value_async(to_ibuffer(data))
        return st

    def _on_ctrl(self, _sender, args):
        data = ibuffer_bytes(args.characteristic_value)
        self.events.append(("ctrl", data))
        op = data[0] if data else -1
        name = OPCODE_NAMES.get(op, f"UNKNOWN_0x{op:02X}")
        print(f"  [控制] {name} {data.hex(' ')}")
        if op == OP_CAPS_RESP:
            self.caps_resp = data
            self.caps_got.set()
        elif op == OP_AUDIO_START:
            self.audio_started.set()
        elif op == OP_AUDIO_STOP:
            self.audio_stopped.set()
        elif op == 0x00 and len(data) >= 2 and data[1] == 0x02:
            # 实测（固件 2671）：松手时遥控器发 `00 02`，MIC_CLOSE 后回 `00 00`
            self.audio_stopped.set()

    def _on_audio(self, _sender, args):
        data = ibuffer_bytes(args.characteristic_value)
        self.audio_frames.append(data)
        self.events.append(("audio", data))
        if self.on_audio_live:
            try:
                self.on_audio_live(data)  # 微信模式：实时桥接（winrt 回调线程）
            except Exception:
                pass
        if len(self.audio_frames) % 40 == 0:
            print(f"  [音频] 已收 {len(self.audio_frames)} 帧")

    _audio_subscribed = False

    async def subscribe_audio(self):
        if self._audio_subscribed:
            return
        self._audio_subscribed = True
        token = self.audio_ch.add_value_changed(self._on_audio)
        self._tokens.append((self.audio_ch, token))
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattClientCharacteristicConfigurationDescriptorValue,
        )
        st = await self.audio_ch.write_client_characteristic_configuration_descriptor_async(
            GattClientCharacteristicConfigurationDescriptorValue.NOTIFY
        )
        print(f"音频通道已订阅 (状态 {st})")

    async def mic_open(self):
        """开始一段录音（常驻会话用）。"""
        await self.subscribe_audio()
        self.audio_frames.clear()
        self.audio_stopped.clear()
        self.audio_started.clear()
        await self.write(bytes([OP_MIC_OPEN, 0x00]))

    async def mic_close(self):
        await self.write(bytes([OP_MIC_CLOSE, 0x00]))

    def drain_frames(self) -> list[bytes]:
        frames, self.audio_frames = self.audio_frames, []
        return frames

    @staticmethod
    def frames_to_pcm(frames: list[bytes]) -> list[int]:
        """ADPCM 帧序列 -> PCM 采样（v0.4 帧 134B 带头，v1.0 帧 120B 无头）。"""
        pcm: list[int] = []
        dec = ImaAdpcmDecoder()
        for f in frames:
            if len(f) == 134:
                dec = ImaAdpcmDecoder(
                    predictor=struct.unpack(">h", f[3:5])[0], step_index=f[5]
                )
                pcm.extend(dec.decode(f[6:]))
            else:
                pcm.extend(dec.decode(f))
        return pcm

    async def get_caps(self, timeout: float = 5.0) -> bytes | None:
        await self.write(bytes([OP_GET_CAPS, 0x01, 0x00, 0x00, 0x03, 0x03]))
        try:
            await asyncio.wait_for(self.caps_got.wait(), timeout)
        except asyncio.TimeoutError:
            print("GET_CAPS 超时无响应")
        return self.caps_resp

    @staticmethod
    def parse_caps(resp: bytes) -> dict:
        """宽松解析，兼容固件 2671 的字节对调差异。"""
        info = {"raw": resp.hex(" "), "length": len(resp)}
        if len(resp) >= 7:
            info["version_byte"] = f"0x{resp[1]:02X}"  # 0x01 即 v1.0
            codecs_std = resp[3]
            # 对调差异：标准位无效而相邻字节含 8/16kHz ADPCM 位时换位解析
            if (codecs_std & 0x0F) == 0 and (resp[4] & 0x0F) != 0:
                codecs_std = resp[4]
                info["quirk"] = "codec 字节对调兼容"
            info["adpcm_16k"] = bool(codecs_std & 0x02)
            info["adpcm_8k"] = bool(codecs_std & 0x01)
            info["max_frame_size"] = resp[6] if len(resp) > 6 else None
        return info

    async def record(self, seconds: float, wav_out: Path) -> bool:
        """MIC_OPEN → 收音频 → AUDIO_STOP/超时 → 解码写 WAV。"""
        await self.subscribe_audio()
        print(f"MIC_OPEN，{seconds:.0f}s 内请按住遥控器【语音键】说话后松开…")
        await self.write(bytes([OP_MIC_OPEN, 0x00]))
        # 等待 AUDIO_STOP（松手）或超时
        try:
            await asyncio.wait_for(self.audio_stopped.wait(), seconds)
            print("收到 AUDIO_STOP（松手）")
        except asyncio.TimeoutError:
            print("到达最长时长")
        stream_id = 0x00
        await self.write(bytes([OP_MIC_CLOSE, stream_id]))

        if not self.audio_frames:
            print("没有收到音频帧（语音键没按住/握手指令没生效）")
            return False
        total = sum(len(f) for f in self.audio_frames)
        print(f"共 {len(self.audio_frames)} 帧 / {total} 字节 ADPCM")

        pcm: list[int] = []
        dec = ImaAdpcmDecoder()
        for f in self.audio_frames:
            if len(f) == 134:
                # v0.4 帧：大端 seq(2) + padding(1) + predictor(2) + step(1) + 128B ADPCM
                dec = ImaAdpcmDecoder(
                    predictor=struct.unpack(">h", f[3:5])[0], step_index=f[5]
                )
                pcm.extend(dec.decode(f[6:]))
            elif len(f) == 120:
                pcm.extend(dec.decode(f))  # v1.0 无头帧，状态沿用
            else:
                print(f"  非常规帧长 {len(f)}B: {f[:8].hex(' ')}")
                pcm.extend(dec.decode(f))
        dur = len(pcm) / 16000
        write_wav(wav_out, pcm)
        print(f"已解码 {dur:.1f}s PCM -> {wav_out}")
        return True

    async def close(self):
        for ch, token in self._tokens:
            try:
                ch.remove_value_changed(token)
            except Exception:
                pass


async def main_async():
    ap = argparse.ArgumentParser()
    ap.add_argument("--caps", action="store_true", help="仅做 GET_CAPS 握手自检")
    ap.add_argument("--record", type=float, metavar="SEC", help="录音秒数")
    ap.add_argument("--addr", type=lambda s: int(s.replace(":", ""), 16),
                    default=DEFAULT_ADDR)
    ap.add_argument("--out", default="voice.wav", help="输出 WAV 路径")
    ap.add_argument("--no-transcribe", action="store_true",
                    help="只录 WAV 不做本地转写")
    ap.add_argument("--transcribe", metavar="WAV",
                    help="直接转写现有 WAV 文件（不录音）")
    ap.add_argument("--model", default="medium",
                    choices=["tiny", "base", "small", "medium", "large-v3"],
                    help="whisper 模型档位（默认 medium）")
    ap.add_argument("--only-text", action="store_true",
                    help="子进程模式：静默，stdout 只输出最终文本")
    args = ap.parse_args()

    if args.transcribe:
        if args.only_text:
            # 子进程模式：禁止一切多余输出，只打印结果文本；失败详情走 stderr
            import contextlib
            import io
            import sys as _sys

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                text = _transcribe_inproc(Path(args.transcribe), model_name=args.model)
            print(text or "", file=_sys.stdout, flush=True)
            if text is None:
                print(buf.getvalue(), file=_sys.stderr, flush=True)
            return 0
        text = transcribe_local(Path(args.transcribe), model_name=args.model)
        if text is not None:
            print(text)
        return 0 if text is not None else 2

    cli = AtvvClient(args.addr)
    wav_ok = False
    try:
        await cli.connect()
        resp = await cli.get_caps()
        if resp:
            print("GET_CAPS 响应解析:", cli.parse_caps(resp))
        else:
            print("握手失败：遥控器没回应 GET_CAPS")
            return 1
        if args.record:
            wav_ok = await cli.record(args.record, Path(args.out))
    finally:
        # 先彻底关掉 BLE（winrt 回调线程与 CUDA 原生代码并存疑似引发原生崩溃）
        await cli.close()
        if cli.dev is not None:
            cli.dev.close()

    if wav_ok and not args.no_transcribe:
        print("\n>>> 开始本地转写（首次约需数秒）…", flush=True)
        try:
            text = transcribe_local(Path(args.out), model_name=args.model)
            if text is not None:
                print("=== 本地转写结果 ===")
                print(text, flush=True)
            else:
                print("(转写未产出文本)", flush=True)
        except Exception:
            import traceback
            print("转写异常:\n" + traceback.format_exc(), flush=True)
    return 0 if wav_ok else 2


def _nvidia_bin_dirs() -> list[str]:
    """pip 安装的 NVIDIA CUDA DLL 目录（GPU 转写需要）。"""
    import glob
    import site

    dirs = []
    for sp in site.getsitepackages() + [site.getusersitepackages()]:
        dirs += glob.glob(sp.replace("\\", "/") + "/nvidia/*/bin")
    return dirs


def transcribe_local(wav_path: Path, model_name: str = "medium") -> str | None:
    """本地 faster-whisper 转写。

    默认在"干净环境"的子进程里跑：宿主 shell 的配置文件（starship/nvm 等）
    可能污染 PATH 导致 ctranslate2 CUDA 加载崩溃（实测 access violation），
    子进程只继承必要的系统变量 + 我们自己的 NVIDIA DLL 目录。
    设 MIREMOTE_VOICE_ISOLATED=0 可改回本进程模式。
    """
    import os

    if os.environ.get("MIREMOTE_VOICE_ISOLATED", "1") != "0":
        return _transcribe_subprocess(wav_path, model_name)
    return _transcribe_inproc(wav_path, model_name)


def _transcribe_subprocess(wav_path: Path, model_name: str) -> str | None:
    """干净环境子进程转写，返回文本或 None。"""
    import os
    import subprocess
    import sys

    nvidia_dirs = _nvidia_bin_dirs()
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    path_parts = nvidia_dirs + [
        system_root + r"\System32",
        system_root,
        system_root + r"\System32\Wbem",
    ]
    env = {
        "SYSTEMROOT": system_root,
        "PROGRAMDATA": os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
        "USERPROFILE": os.environ.get("USERPROFILE", ""),
        # 用户 site-packages（pip 默认装这里）依赖这几个定位
        "APPDATA": os.environ.get("APPDATA", ""),
        "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
        "HOMEDRIVE": os.environ.get("HOMEDRIVE", ""),
        "HOMEPATH": os.environ.get("HOMEPATH", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
        "PATH": os.pathsep.join(path_parts),
        "PYTHONIOENCODING": "utf-8",
        # 模型已缓存，强制离线：干净环境没有代理变量，HF 在线检查会挂死
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    # 空字符串会覆盖默认值（HF_HOME="" 会让缓存定位失败），只传非空的
    for k in ("HF_HOME", "MIREMOTE_VOICE_DEVICE"):
        v = os.environ.get(k, "")
        if v:
            env[k] = v
    if getattr(sys, "frozen", False):
        command = [
            sys.executable, "voice", "--transcribe", str(wav_path),
            "--model", model_name, "--only-text",
        ]
    else:
        script = Path(__file__).resolve()
        command = [
            sys.executable, str(script), "--transcribe", str(wav_path),
            "--model", model_name, "--only-text",
        ]
    t0 = time.time()
    try:
        r = subprocess.run(
            command,
            capture_output=True, env=env, timeout=600,
            encoding="utf-8", errors="replace",  # 子进程是 UTF-8，默认 GBK 会炸读取线程
        )
    except subprocess.TimeoutExpired:
        print("(转写子进程超时)")
        return None
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-6:]
        print("(转写子进程崩溃，尾部输出:)", flush=True)
        for line in tail:
            print("  " + line, flush=True)
        return None
    text = (r.stdout or "").strip().splitlines()
    text = text[-1].strip() if text else ""
    print(f"(子进程转写 {time.time() - t0:.1f}s)")
    if not text and (r.stderr or "").strip():
        for line in r.stderr.strip().splitlines()[-4:]:
            print("  [子进程stderr] " + line, flush=True)
    return text or None


def _transcribe_inproc(wav_path: Path, model_name: str) -> str | None:
    """本进程转写（调试用）。未装依赖返回 None。"""
    import os

    for d in _nvidia_bin_dirs():
        os.add_dll_directory(d)
        os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("(未安装 faster-whisper，跳过本地转写: pip install faster-whisper "
              "nvidia-cublas-cu12 nvidia-cudnn-cu12)")
        return None

    t0 = time.time()
    model = None
    force = os.environ.get("MIREMOTE_VOICE_DEVICE", "").lower()
    candidates = (("cpu", "int8"),) if force == "cpu" else (
        ("cuda", "float16"), ("cpu", "int8"),
    )
    for device, compute in candidates:
        try:
            model = WhisperModel(model_name, device=device, compute_type=compute)
            break
        except Exception as e:
            print(f"  {device} 加载失败({type(e).__name__})，尝试下一档…")
    if model is None:
        return None
    segments, info = model.transcribe(
        str(wav_path), language="zh", beam_size=1, vad_filter=True,
        initial_prompt="以下是用户对AI编程助手说的中文语音指令。",
    )
    text = "".join(s.text for s in segments).strip()
    print(f"(转写耗时 {time.time() - t0:.1f}s)")
    return text or None


def main():
    raise SystemExit(asyncio.run(main_async()))


class VoiceDaemon:
    """常驻语音引擎：按住说话 -> 松手 -> 转写 -> 回调文本。

    在独立线程的事件循环里维持 ATVV 连接（自动重连），begin()/finish()
    由按键线程调用（线程安全）。转写在干净子进程里跑（见 transcribe_local）。
    """

    def __init__(self, on_text, log=print, model: str = "medium",
                 addr: int = DEFAULT_ADDR, mode: str = "local",
                 wechat_hotkey: str | None = None):
        self.on_text = on_text      # callable(text: str)
        self.log = log
        self.model = model
        self.addr = addr
        self.mode = mode            # "local"=本地 whisper；"wechat"=桥接输入法
        self.wechat_hotkey = wechat_hotkey  # 微信模式：触发输入法语音的组合键名列表
        self.loop: asyncio.AbstractEventLoop | None = None
        self._cmds: asyncio.Queue | None = None
        self._ready = threading.Event()
        self._collecting = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- 生命周期 ----
    def start(self) -> bool:
        self._thread = threading.Thread(target=self._thread_main, name="voice-daemon", daemon=True)
        self._thread.start()
        return self._ready.wait(10)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    def _thread_main(self):
        asyncio.run(self._main())

    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self._cmds = asyncio.Queue()
        while not self._stop.is_set():
            cli = AtvvClient(self.addr)
            try:
                await cli.connect()
                resp = await cli.get_caps()
                if not resp:
                    raise RuntimeError("GET_CAPS 无响应")
                self.log(f"语音引擎就绪 ({cli.parse_caps(resp).get('adpcm_16k') and '16kHz' or '?'})")
                self._ready.set()
                while not self._stop.is_set():
                    try:
                        cmd = await asyncio.wait_for(self._cmds.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    if cmd == "begin":
                        if self._collecting:
                            continue
                        self._collecting = True
                        await cli.mic_open()
                        self.log("录音中…（按住说话）")
                        if self.mode == "wechat":
                            # 微信模式 v2（切换式）：录音期间零注入（语音键 F5 透传
                            # 会干扰微信组合键检测），音频先缓冲；松手后再
                            # 短击热键 -> 播放 -> 再短击关闭
                            self._live_buffer = []
                            cli.on_audio_live = self._buffer_write
                        # 等松手信号（遥控器 00 02）或 finish 命令
                        stop_wait = asyncio.create_task(cli.audio_stopped.wait())
                        cmd_wait = asyncio.create_task(self._cmds.get())
                        done, pending = await asyncio.wait(
                            {stop_wait, cmd_wait},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    elif cmd == "finish":
                        pass  # 上面的 wait 已处理
                    if self._collecting:
                        self._collecting = False
                        for t in ("stop_wait", "cmd_wait"):
                            task = locals().get(t)
                            if task and not task.done():
                                task.cancel()
                        frames = cli.drain_frames()
                        try:
                            await cli.mic_close()
                        except Exception:
                            pass
                        if self.mode == "wechat":
                            # 切换式：松手后 短击热键->播放缓冲->再短击关闭
                            cli.on_audio_live = None
                            buffered = list(getattr(self, "_live_buffer", []))
                            if buffered:
                                threading.Thread(
                                    target=self._wechat_playback, args=(buffered,),
                                    daemon=True,
                                ).start()
                            else:
                                self.log("(没有收到音频帧)")
                        elif frames:
                            threading.Thread(
                                target=self._pipeline, args=(frames,), daemon=True
                            ).start()
                        else:
                            self.log("(没有收到音频帧)")
            except Exception as e:
                self._ready.clear()
                self.log(f"语音连接断开/出错({type(e).__name__}: {e})，2 秒后重连…")
                try:
                    await cli.close()
                    if cli.dev is not None:
                        cli.dev.close()
                except Exception:
                    pass
                await asyncio.sleep(2)

    # ---- 线程安全的对外接口 ----
    def begin(self):
        self._put("begin")

    def finish(self):
        self._put("finish")

    def _put(self, cmd: str):
        loop = self.loop
        if loop and self._cmds is not None:
            loop.call_soon_threadsafe(self._cmds.put_nowait, cmd)

    # ---- 收尾流水线（工作线程）----
    def _pipeline(self, frames: list[bytes]):
        try:
            pcm = AtvvClient.frames_to_pcm(frames)
            if not pcm:
                self.log("(音频为空)")
                return
            if self.mode == "wechat":
                # 微信模式：把 PCM 写进 VB-CABLE 虚拟声卡，由输入法识别润色
                self._stream_to_cable(pcm)
                return
            wav_path = Path.cwd() / "last_utterance.wav"
            write_wav(wav_path, pcm)
            dur = len(pcm) / 16000
            self.log(f"收音 {dur:.1f}s，转写中…")
            text = transcribe_local(wav_path, model_name=self.model)
            if text:
                self.log(f"识别: {text}")
                self.on_text(text)
            else:
                self.log("(转写无结果)")
        except Exception as e:
            import traceback
            self.log(f"语音流水线异常: {e}\n{traceback.format_exc()}")

    def _press_hotkey(self, down: bool):
        """按下/松开输入法语音快捷键（兼容旧按住模式，一般不再使用）。"""
        if not self.wechat_hotkey:
            return
        try:
            from . import actions
            from .keys import name_to_vk
            vks = [name_to_vk(n) for n in self.wechat_hotkey]
            if down:
                for vk in vks:
                    actions._tap(vk)
            else:
                for vk in reversed(vks):
                    actions._tap(vk, up=True)
        except Exception as e:
            self.log(f"快捷键触发失败: {e}")

    def _tap_hotkey(self):
        """短击切换式语音热键（微信 Ctrl+Win+Shift：开启/结束持续语音）。"""
        if not self.wechat_hotkey:
            return
        try:
            from . import actions
            from .keys import name_to_vk
            vks = [name_to_vk(n) for n in self.wechat_hotkey]
            for vk in vks:
                actions._tap(vk)
            time.sleep(0.22)
            for vk in reversed(vks):
                actions._tap(vk, up=True)
        except Exception as e:
            self.log(f"切换热键触发失败: {e}")

    # ---- 微信模式：缓冲 + 切换式播放 ----
    def _buffer_write(self, frame: bytes):
        """录音期实时回调：只缓冲不播放（F5 透传期间零注入零播放）。"""
        buf = getattr(self, "_live_buffer", None)
        if buf is not None:
            buf.append(frame)

    def _wechat_playback(self, frames: list[bytes]):
        """松手后（工作线程）：短击开听 -> 播放缓冲到 CABLE -> 短击结束。"""
        try:
            pcm = AtvvClient.frames_to_pcm(frames)
            if not pcm:
                self.log("(音频为空)")
                return
            dur = len(pcm) / 16000
            self.log(f"收音 {dur:.1f}s，喂给微信语音…")
            self._tap_hotkey()          # 开启持续语音（微信开始听 CABLE）
            time.sleep(0.45)            # 等微信麦克风就绪
            if not self._open_cable_stream():
                self._tap_hotkey()      # 开流失败则关掉持续语音
                return
            # 重采样并播放
            import struct as _s
            import sounddevice as sd
            idx = self._find_cable_input()
            rate = int(sd.query_devices(idx)["default_samplerate"])
            ratio = rate / 16000.0
            prev = 0.0
            out = []
            n = len(pcm)
            for k in range(int(n * ratio)):
                pos = k / ratio
                i = int(pos)
                fr = pos - i
                cur = pcm[i]
                nxt = pcm[i + 1] if i + 1 < n else cur
                out.append(int(prev + (cur - prev) * fr) if i == 0
                           else int(cur + (nxt - cur) * fr))
                prev = float(cur)
            self._cable_stream.write(_s.pack(f"<{len(out)}h", *out))
            time.sleep(0.35)            # 尾音播完
            self._tap_hotkey()          # 结束持续语音 -> 微信识别上屏
            self._close_cable_stream()
            self.log("已交由微信识别（去语气词/整理）")
        except Exception as e:
            import traceback
            self.log(f"微信播放管线异常: {e}\n{traceback.format_exc()}")
            try:
                self._tap_hotkey()      # 异常时尽力关闭持续语音
                self._close_cable_stream()
            except Exception:
                pass

    def _close_cable_stream(self):
        st = getattr(self, "_cable_stream", None)
        if st is not None:
            try:
                st.stop()
                st.close()
            except Exception:
                pass
            self._cable_stream = None

    # ---- 微信模式流式桥接 ----
    def _find_cable_input(self):
        """返回 CABLE Input（虚拟声卡输出侧）的 sounddevice 索引。"""
        import sounddevice as sd
        # 优先 2 声道条目（经典 WASAPI 共享模式），否则任一可输出的 CABLE Input
        candidates = []
        for i, d in enumerate(sd.query_devices()):
            name = d.get("name", "")
            if "CABLE Input" in name and d.get("max_output_channels", 0) > 0:
                candidates.append((i, d["max_output_channels"]))
        if not candidates:
            return None
        two_ch = [c for c in candidates if c[1] <= 2]
        return (two_ch or candidates)[0][0]

    def _open_cable_stream(self):
        """打开 CABLE 输出流并重置解码器（每段录音开始时调用）。

        CABLE 设备跑在 44.1/48kHz，遥控器音频是 16kHz，
        打开时记录采样率并在 _live_write 里线性插值重采样。
        """
        try:
            import sounddevice as sd
            idx = self._find_cable_input()
            if idx is None:
                self.log("未找到 VB-CABLE（CABLE Input），微信模式不可用")
                return False
            dev = sd.query_devices(idx)
            rate = int(dev["default_samplerate"])
            self._live_decoder = ImaAdpcmDecoder()
            self._live_rate = rate
            self._live_last = 0.0  # 上一帧末样本（重采样跨帧连续）
            self._cable_stream = sd.RawOutputStream(
                device=idx, samplerate=rate, channels=1, dtype="int16",
                blocksize=0,
            )
            self._cable_stream.start()
            self.log(f"流式桥接已开启（CABLE #{idx} @ {rate}Hz）")
            return True
        except Exception as e:
            self.log(f"CABLE 流打开失败: {e}")
            self._cable_stream = None
            return False


if __name__ == "__main__":
    main()
