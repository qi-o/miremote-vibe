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
import json
import math
import os
import queue
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
OP_START_SEARCH_V1 = 0x08
OP_STREAM_START = 0x04
OP_STREAM_STOP = 0x00
OP_AUDIO_SYNC_V1 = 0x0A
OP_START_SEARCH = 0x10  # 部分固件按语音键时先来这个
OP_AUDIO_START = 0x11
OP_AUDIO_SYNC = 0x12
OP_AUDIO_STOP = 0x13

OPCODE_NAMES = {
    OP_CAPS_RESP: "CAPS_RESP", OP_MIC_OPEN: "MIC_OPEN", OP_MIC_CLOSE: "MIC_CLOSE",
    OP_START_SEARCH_V1: "START_SEARCH", OP_STREAM_START: "STREAM_START",
    OP_STREAM_STOP: "STREAM_STOP",
    OP_AUDIO_SYNC_V1: "AUDIO_SYNC",
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
        self.predictor += -diff if nibble & 8 else diff
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


class FrameAccumulator:
    def __init__(self, frame_size: int = 120):
        self.frame_size = max(1, int(frame_size or 120))
        self.pending = bytearray()

    def append(self, data: bytes) -> list[bytes]:
        self.pending.extend(data)
        frames = []
        while len(self.pending) >= self.frame_size:
            frames.append(bytes(self.pending[:self.frame_size]))
            del self.pending[:self.frame_size]
        return frames

    def reset(self):
        self.pending.clear()


class StreamingLinearResampler:
    """跨帧连续的单声道线性重采样器。"""

    def __init__(self, input_rate: int, output_rate: int):
        if input_rate <= 0 or output_rate <= 0:
            raise ValueError("sample rate must be positive")
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.step = input_rate / output_rate
        self.buffer: list[int] = []
        self.position = 0.0

    def reset(self):
        self.buffer.clear()
        self.position = 0.0

    def convert(self, samples: list[int]) -> list[int]:
        if samples:
            self.buffer.extend(samples)
        output = []
        while self.position + 1 < len(self.buffer):
            index = int(self.position)
            fraction = self.position - index
            current = self.buffer[index]
            following = self.buffer[index + 1]
            output.append(int(current + (following - current) * fraction))
            self.position += self.step
        consumed = int(self.position)
        if consumed > 0:
            del self.buffer[:consumed]
            self.position -= consumed
        return output


def _status_name(status) -> str:
    name = getattr(status, "name", None)
    if name:
        return str(name)
    text = str(status)
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text


def _status_ok(status) -> bool:
    return _status_name(status).lower() in {"success", "ok"}


def _ensure_gatt_success(status, operation: str):
    if not _status_ok(status):
        raise RuntimeError(f"{operation}失败: {_status_name(status)}")


class AtvvClient:
    def __init__(self, addr: int = DEFAULT_ADDR):
        self.addr = addr
        self.dev = None
        self.tx = None
        self.audio_ch = None
        self.ctrl_ch = None
        self.audio_frames: list[bytes] = []
        self.audio_items: list[object] = []
        self.events: list[tuple[str, bytes]] = []
        self.caps_resp: bytes | None = None
        self.audio_stopped = asyncio.Event()
        self.audio_started = asyncio.Event()
        self.caps_got = asyncio.Event()
        self._tokens = []
        self._decoder = ImaAdpcmDecoder()
        self._v04_frames = False
        self._audio_subscribed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._mic_open_future = None
        self._mic_open_requested = False
        self._capture_lock = threading.Lock()
        self._closed = False
        self._close_lock = threading.Lock()
        self._disconnect_token = None
        self._disconnect_event: asyncio.Event | None = None
        self.stream_id = 0x00
        self.stream_active = False
        self.stream_reason: int | None = None
        self.stop_reason: int | None = None
        self.protocol_version = 0x0100
        self.frame_size = 120
        self.selected_codec = 0x02
        self.interaction_model = 0
        self.on_audio_live = None  # 实时音频回调: callable(raw_frame_bytes)
        self.on_ctrl_live = None   # 控制通道回调（任意通知都算 ATVV 活动）
        self.on_stream_start_live = None
        self.on_stream_stop_live = None
        self.on_codec_sync_live = None

    async def connect(self):
        self._loop = asyncio.get_running_loop()
        self._disconnect_event = asyncio.Event()
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
        self._install_connection_status_handler()
        svc_res = await self.dev.get_gatt_services_with_cache_mode_async(
            BluetoothCacheMode.UNCACHED
        )
        status = getattr(svc_res, "status", None)
        if status is not None:
            _ensure_gatt_success(status, "发现 ATVV 服务")
        svc = None
        for s in svc_res.services:
            if str(s.uuid).lower() == UUID_SVC:
                svc = s
                break
        if svc is None:
            raise RuntimeError("设备上没有 AB5E0001 ATVV 服务")
        self.svc = svc
        cr = await svc.get_characteristics_with_cache_mode_async(
            BluetoothCacheMode.UNCACHED
        )
        status = getattr(cr, "status", None)
        if status is not None:
            _ensure_gatt_success(status, "发现 ATVV 特征")
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
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattClientCharacteristicConfigurationDescriptorValue,
        )
        try:
            st = await self.ctrl_ch.write_client_characteristic_configuration_descriptor_async(
                GattClientCharacteristicConfigurationDescriptorValue.NOTIFY
            )
            _ensure_gatt_success(st, "控制通道订阅")
            self._tokens.append((self.ctrl_ch, token))
        except BaseException:
            try:
                self.ctrl_ch.remove_value_changed(token)
            except Exception:
                pass
            raise
        print(f"控制通道已订阅 (状态 {st})")

    async def write(self, data: bytes):
        st = await asyncio.wait_for(self.tx.write_value_async(to_ibuffer(data)), 10.0)
        _ensure_gatt_success(st, "ATVV 写入")
        return st

    def _install_connection_status_handler(self):
        add = getattr(self.dev, "add_connection_status_changed", None)
        if not callable(add):
            return
        self._disconnect_token = add(self._on_connection_status_changed)

    def _on_connection_status_changed(self, sender, _args):
        status = getattr(sender, "connection_status", None)
        if _status_name(status).lower() != "disconnected":
            return
        self._signal_disconnected()

    def _signal_disconnected(self):
        if self._closed:
            return
        loop = self._loop
        event = self._disconnect_event
        if loop is None or loop.is_closed() or event is None:
            return
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass  # The owner loop may have finished during a late WinRT callback.

    def disconnected_event(self) -> asyncio.Event:
        if self._disconnect_event is None:
            self._disconnect_event = asyncio.Event()
        return self._disconnect_event

    def connection_status(self) -> str:
        if self.dev is None:
            return "unknown"
        return _status_name(getattr(self.dev, "connection_status", "unknown")).lower()

    def is_disconnected(self) -> bool:
        return self.connection_status() == "disconnected"

    def _on_ctrl(self, _sender, args):
        if self._closed:
            return
        data = ibuffer_bytes(args.characteristic_value)
        self._dispatch_notification(self._handle_ctrl, data)

    def _dispatch_notification(self, callback, data):
        loop = self._loop
        if self._closed:
            return
        if loop is not None:
            try:
                loop.call_soon_threadsafe(callback, data)
            except RuntimeError:
                pass
        else:
            callback(data)

    def _handle_ctrl(self, data):
        if self._closed:
            return
        self.events.append(("ctrl", data))
        op = data[0] if data else -1
        name = OPCODE_NAMES.get(op, f"UNKNOWN_0x{op:02X}")
        print(f"  [控制] {name} {data.hex(' ')}")
        if self.on_ctrl_live:
            try:
                self.on_ctrl_live(data)
            except Exception:
                pass
        if op == OP_CAPS_RESP:
            self.caps_resp = data
            info = self.parse_caps(data)
            self.protocol_version = info.get("version", self.protocol_version)
            self.frame_size = info.get("frame_size", self.frame_size)
            self.selected_codec = info.get("selected_codec", self.selected_codec)
            self.interaction_model = info.get("interaction", self.interaction_model)
            self.caps_got.set()
        elif op == OP_AUDIO_SYNC_V1 and len(data) >= 7:
            # v1.0 控制通道的 0x0A 是 AUDIO_SYNC，不是 caps 响应。
            # headerless ADPCM 帧必须按该 predictor/index 重置解码器。
            predictor = struct.unpack(">h", data[4:6])[0]
            step_index = data[6]
            with self._capture_lock:
                self.audio_items.append(("sync", predictor, step_index))
            if self.on_codec_sync_live:
                try:
                    self.on_codec_sync_live(predictor, step_index)
                except Exception:
                    pass
        elif op == OP_AUDIO_START:
            if len(data) >= 4:
                self.stream_id = data[3]
            self.stream_active = True
            self.audio_started.set()
        elif op == OP_AUDIO_STOP:
            self.stream_active = False
            self.audio_stopped.set()
        elif op == OP_STREAM_STOP:
            # 实测（固件 2671）：松手时遥控器发 `00 02`，MIC_CLOSE 后回 `00 00`
            was_active = self.stream_active
            self.stream_active = False
            self.stop_reason = data[1] if len(data) >= 2 else None
            if was_active:
                self.audio_stopped.set()
            self._mic_open_requested = False
            if self.on_stream_stop_live:
                try:
                    self.on_stream_stop_live(data)
                except Exception:
                    pass
        elif op == OP_STREAM_START:
            # 0x04 是协议级新会话边界；在任何音频包到达前清掉上一流残留。
            self.reset_capture()
            self.stream_reason = data[1] if len(data) >= 2 else None
            self.selected_codec = data[2] if len(data) >= 3 else self.selected_codec
            if len(data) >= 4:
                self.stream_id = data[3]
            self.stream_active = True
            self.stop_reason = None
            self.audio_stopped.clear()
            self.audio_started.set()
            if self.on_stream_start_live:
                try:
                    self.on_stream_start_live(data)
                except Exception:
                    pass
        elif op in (OP_START_SEARCH_V1, OP_START_SEARCH):
            # RC003 会用 START_SEARCH 请求主机开流；真正的流类型仍以
            # 随后的 0x04 reason/session 为准。
            self._schedule_mic_open_response()

    def _schedule_mic_open_response(self):
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        if self._mic_open_requested:
            return
        pending = self._mic_open_future
        if pending is not None and not pending.done():
            return
        self._mic_open_requested = True
        future = asyncio.run_coroutine_threadsafe(
            self._respond_to_mic_open_request(), loop
        )
        self._mic_open_future = future
        future.add_done_callback(self._mic_open_done)

    async def _respond_to_mic_open_request(self):
        await self.write(bytes([OP_MIC_OPEN, 0x00]))

    def _mic_open_done(self, future):
        try:
            future.result()
        except Exception as exc:
            self._mic_open_requested = False
            print(f"响应 MIC_OPEN 请求失败: {exc}")
            self._signal_disconnected()

    def _on_audio(self, _sender, args):
        if self._closed:
            return
        data = ibuffer_bytes(args.characteristic_value)
        self._dispatch_notification(self._handle_audio, data)

    def _handle_audio(self, data):
        if self._closed:
            return
        with self._capture_lock:
            self.audio_frames.append(data)
            self.audio_items.append(("audio", data))
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
        token = self.audio_ch.add_value_changed(self._on_audio)
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattClientCharacteristicConfigurationDescriptorValue,
        )
        try:
            st = await self.audio_ch.write_client_characteristic_configuration_descriptor_async(
                GattClientCharacteristicConfigurationDescriptorValue.NOTIFY
            )
            _ensure_gatt_success(st, "音频通道订阅")
            self._tokens.append((self.audio_ch, token))
            self._audio_subscribed = True
        except BaseException:
            self._audio_subscribed = False
            try:
                self.audio_ch.remove_value_changed(token)
            except Exception:
                pass
            raise
        print(f"音频通道已订阅 (状态 {st})")

    async def mic_open(self):
        """显式开启主机录音流（仅供诊断/手动录音，不用于物理按键会话）。"""
        await self.subscribe_audio()
        with self._capture_lock:
            self.audio_frames.clear()
            self.audio_items.clear()
        self.audio_stopped.clear()
        self.audio_started.clear()
        await self.write(bytes([OP_MIC_OPEN, 0x00]))

    async def mic_close(self):
        await self.write(bytes([OP_MIC_CLOSE, self.stream_id & 0xFF]))

    async def resubscribe_audio(self):
        """重订阅音频通知（言灵同款 REOPEN RESET 序列的核心步骤）。

        固件 2671 在 MIC_CLOSE 后音频通知订阅会失效，下一次 mic_open 后
        物理流不再送达主机；必须 取消订阅+禁用通知 -> 停 180ms ->
        重新订阅+启用通知 才能恢复。
        """
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattClientCharacteristicConfigurationDescriptorValue as CCCD,
        )
        for ch, token in list(self._tokens):
            if ch is self.audio_ch:
                try:
                    ch.remove_value_changed(token)
                except Exception:
                    pass
                self._tokens.remove((ch, token))
                break
        self._audio_subscribed = False
        try:
            none_v = getattr(CCCD, "NONE", None)
            if none_v is not None:
                st = await self.audio_ch.write_client_characteristic_configuration_descriptor_async(none_v)
                _ensure_gatt_success(st, "音频通知禁用")
        except Exception:
            pass
        await asyncio.sleep(0.18)
        token = self.audio_ch.add_value_changed(self._on_audio)
        try:
            st = await self.audio_ch.write_client_characteristic_configuration_descriptor_async(
                CCCD.NOTIFY
            )
            _ensure_gatt_success(st, "音频通知重订阅")
            self._tokens.append((self.audio_ch, token))
            self._audio_subscribed = True
        except BaseException:
            try:
                self.audio_ch.remove_value_changed(token)
            except Exception:
                pass
            self._audio_subscribed = False
            raise
        print(f"音频通知已重订阅 (状态 {st})")

    def reset_capture(self):
        with self._capture_lock:
            self.audio_frames.clear()
            self.audio_items.clear()

    def drain_frames(self) -> list[bytes]:
        with self._capture_lock:
            frames, self.audio_frames = self.audio_frames, []
            self.audio_items = []
        return frames

    def drain_audio_items(self) -> list[object]:
        with self._capture_lock:
            items, self.audio_items = self.audio_items, []
            self.audio_frames = []
        return items

    @staticmethod
    def decode_audio_items(items: list[object], frame_size: int = 120) -> tuple[list[int], dict]:
        """按 ATVV 帧边界与 AUDIO_SYNC 顺序解码一段捕获。"""
        pcm: list[int] = []
        dec = ImaAdpcmDecoder()
        accumulator = FrameAccumulator(frame_size)
        stats = {
            "notifications": 0,
            "raw_bytes": 0,
            "decoded_frames": 0,
            "sync_count": 0,
            "headered_frames": 0,
            "chunk_lengths": {},
            "partial_bytes": 0,
        }
        for item in items:
            if isinstance(item, tuple) and item and item[0] == "sync":
                accumulator.reset()
                dec = ImaAdpcmDecoder(predictor=int(item[1]), step_index=int(item[2]))
                stats["sync_count"] += 1
                continue
            f = item[1] if isinstance(item, tuple) and item and item[0] == "audio" else item
            if not isinstance(f, (bytes, bytearray)) or not f:
                continue
            f = bytes(f)
            stats["notifications"] += 1
            stats["raw_bytes"] += len(f)
            key = str(len(f))
            stats["chunk_lengths"][key] = stats["chunk_lengths"].get(key, 0) + 1

            # 部分 v0.4/兼容固件把 predictor/index 放在每帧 6B 头里。
            if len(f) in (frame_size + 6, 134):
                accumulator.reset()
                dec = ImaAdpcmDecoder(
                    predictor=struct.unpack(">h", f[3:5])[0], step_index=f[5]
                )
                pcm.extend(dec.decode(f[6:]))
                stats["decoded_frames"] += 1
                stats["headered_frames"] += 1
                continue

            for frame in accumulator.append(f):
                pcm.extend(dec.decode(frame))
                stats["decoded_frames"] += 1
        stats["partial_bytes"] = len(accumulator.pending)
        return pcm, stats

    @staticmethod
    def frames_to_pcm(frames: list[bytes], frame_size: int = 120) -> list[int]:
        pcm, _stats = AtvvClient.decode_audio_items(frames, frame_size=frame_size)
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
            version = (resp[1] << 8) | resp[2]
            info["version"] = version
            info["version_byte"] = f"0x{version:04X}"
            codecs_std = resp[3]
            interaction = resp[4]
            # 对调差异：标准位无效而相邻字节含 8/16kHz ADPCM 位时换位解析
            if (codecs_std & 0x0F) == 0 and (resp[4] & 0x0F) != 0:
                codecs_std = resp[4]
                interaction = 0x03
                info["quirk"] = "codec 字节对调兼容"
            frame_size = (resp[5] << 8) | resp[6]
            info["adpcm_16k"] = bool(codecs_std & 0x02)
            info["adpcm_8k"] = bool(codecs_std & 0x01)
            info["selected_codec"] = 0x02 if codecs_std & 0x02 else 0x01
            info["interaction"] = interaction
            info["frame_size"] = frame_size or 120
            info["max_frame_size"] = frame_size or 120
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
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self.audio_stopped.set()
        self.audio_started.set()
        event = self._disconnect_event
        if event is not None:
            event.set()
        if self._mic_open_future is not None:
            self._mic_open_future.cancel()
        remove_conn = getattr(self.dev, "remove_connection_status_changed", None)
        if callable(remove_conn) and self._disconnect_token is not None:
            try:
                remove_conn(self._disconnect_token)
            except Exception:
                pass
            self._disconnect_token = None
        for ch, token in list(self._tokens):
            try:
                ch.remove_value_changed(token)
            except Exception:
                pass
        self._tokens.clear()
        self._audio_subscribed = False
        for obj in (self.audio_ch, self.ctrl_ch, self.tx, getattr(self, "svc", None), self.dev):
            close = getattr(obj, "close", None)
            if callable(close):
                try:
                    close()
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

    CONNECT_TIMEOUT = 20.0
    OPERATION_TIMEOUT = 15.0
    RECONNECT_INITIAL = 2.0

    def __init__(self, on_text, log=print, model: str = "medium",
                 addr: int = DEFAULT_ADDR, mode: str = "local",
                 wechat_hotkey: str | None = None,
                 live: bool = True, ready_delay: float = 0.45,
                 live2: bool = False,
                 toggle_hotkey: list[str] | None = None,
                 arm_suppression=None,
                 diagnostics: bool = False,
                 diagnostics_root: Path | None = None,
                 on_event=None):
        self.on_text = on_text      # callable(text: str)
        self.log = log
        self.model = model
        self.addr = addr
        self.mode = mode            # "local"=本地 whisper；"wechat"=桥接输入法
        self.wechat_hotkey = wechat_hotkey  # 微信模式：触发输入法语音的组合键名列表
        self.live = live            # 微信模式 v3：按下即实时送音（False=松手后整段播放）
        self.ready_delay = ready_delay  # 实时模式：面板开启热键后等输入法就绪的秒数
        # live2（2026-08-29 时序错位路线）：切换式面板预开复用 + 按住期间零热键
        # 注入 + 松手后慢速 toggle 关闭 + 剪贴板收割 + Ctrl+V 粘贴上屏。
        # 全部环节 8-29 实测验证（面板检测/慢速节奏/剪贴板路径）。
        self.live2 = live2
        self.toggle_hotkey = toggle_hotkey or ["VK_CONTROL", "VK_LWIN", "VK_SHIFT"]
        self.arm_suppression = arm_suppression  # callable() -> bool：ATVV 开流时
        # 触发 Gadget 重学习窗口（处理蓝牙重连后的句柄/通道变化）
        self._clip_before: str | None = None  # 会话开始时的剪贴板快照
        self.diagnostics = diagnostics
        self.diagnostics_root = diagnostics_root
        self.on_event = on_event
        self.loop: asyncio.AbstractEventLoop | None = None
        self._cmds: asyncio.Queue | None = None
        self._stop_async: asyncio.Event | None = None
        self._ready = threading.Event()
        self._collecting = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._live_q: queue.Queue | None = None        # 实时模式：重采样后待播样本
        self._live_drain = threading.Event()            # 松手信号：排干队列后收尾
        self._live_started = threading.Event()
        self._live_ready = threading.Event()
        self._live_active = False                       # 实时会话线程存活标记
        self._live_armed = False                        # 本段实时会话是否成功武装
        self._live_thread: threading.Thread | None = None
        self._live_fallback_needed = False
        self._live_bytes_written = 0
        self._live_failed = False
        self._live_failure_reason = ""
        self._live_provider_started = False
        self._live_fallback_capture = None
        self._live_generation = 0
        self._live_request_at = 0.0
        self._live_first_audio_logged = False
        self._live_first_write_logged = False
        self._atvv_last = 0.0            # 最近一次 ATVV 活动（ctrl 通知/音频帧）
        self._remote_f5_swallowed_at = 0.0
        self._live_prelude: list[object] = []  # 会话开启前的 sync/音频（开头不丢字）
        self._last_session_end = 0.0     # 上一会话结束时刻（自检防抖）
        self._live_lock = threading.Lock()  # _live_write 的解码状态互斥
        self._live_state_lock = threading.Lock()
        self._live_order_lock = threading.Lock()
        self._live_pipeline_ready = False
        self._playback_lock = threading.Lock()  # v2 播放互斥（防并发写 CABLE）
        self._capture_frame_size = 120
        self._capture_meta = {}
        self._connection_generation = 0
        self._active_generation = 0
        self._recovery_state = "idle"
        self._recovery_attempt = 0
        self._recovery_last_error = ""
        self._recovery_session = 0
        self._main_task = None

    # ---- 生命周期 ----
    def start(self) -> bool:
        self._thread = threading.Thread(target=self._thread_main, name="voice-daemon", daemon=True)
        self._thread.start()
        return self._ready.wait(10)

    def stop(self):
        self._stop.set()
        loop = self.loop
        stop_async = self._stop_async
        if loop and stop_async is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop_async.set)
                if self._main_task is not None:
                    loop.call_soon_threadsafe(self._main_task.cancel)
            except RuntimeError:
                pass
        self._live_drain.set()
        self._live_started.set()
        self._stop_live_provider("守护停止")
        if self._live2_panel_cleanup_needed():
            try:
                self._tap_toggle_slow()
            except Exception:
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._close_cable_stream_when_idle()  # 常开流随守护退出统一关闭

    def _live2_panel_cleanup_needed(self) -> bool:
        return bool(self.live2 and self.mode == "wechat" and self._wetype_panel_visible())

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    def _thread_main(self):
        asyncio.run(self._main())

    async def _main(self):
        self._main_task = asyncio.current_task()
        try:
            await self._run_loop()
        except asyncio.CancelledError:
            if not self._stop.is_set():
                raise
        finally:
            self._active_generation = 0
            self._ready.clear()
            self._clear_voice_session()
            self._recovery_state = "stopped"
            self._emit_event("voice.stopped", session=self._recovery_session, ready=False)
            self._main_task = None

    async def _wait_disconnect(self, cli):
        """Status-only fallback also wakes capture if Windows misses the event."""
        while not cli.is_disconnected():
            try:
                await asyncio.wait_for(cli.disconnected_event().wait(), 1.0)
                return
            except asyncio.TimeoutError:
                continue

    async def _run_loop(self):
        self.loop = asyncio.get_running_loop()
        self._cmds = asyncio.Queue()
        self._stop_async = asyncio.Event()
        backoff = self.RECONNECT_INITIAL
        while not self._stop.is_set():
            cli = AtvvClient(self.addr)
            connected_at = 0.0
            client_closed = False
            self._connection_generation += 1
            generation = self._connection_generation
            self._active_generation = generation
            self._recovery_session += 1
            self._recovery_state = "connecting"
            self._emit_event("voice.connecting", attempt=self._recovery_attempt,
                             session=self._recovery_session, generation=generation)
            try:
                await asyncio.wait_for(cli.connect(), self.CONNECT_TIMEOUT)
                resp = await asyncio.wait_for(cli.get_caps(), self.OPERATION_TIMEOUT)
                if not resp:
                    raise RuntimeError("GET_CAPS 无响应")
                caps = cli.parse_caps(resp)
                self.log(
                    "语音引擎就绪 "
                    f"({caps.get('adpcm_16k') and '16kHz' or '?'}, "
                    f"ATVV {caps.get('version_byte', '?')}, "
                    f"frame={caps.get('frame_size', '?')}B, "
                    f"interaction=0x{caps.get('interaction', 0):02X})"
                )
                # 常驻订阅（收帧不丢），但不常驻 mic_open：固件 2671 的主机流
                # （mic_open 命令流，start_reason=0x00）零音频，只有按住语音键
                # 触发的物理流（start_reason=0x03）有音频。言灵是"订阅常驻、
                # mic_open 响应式"；miremote v1/v2 也是 begin 才 mic_open。
                # 会话开始由 on_voice（LL 钩子判定）或音频帧自检触发。
                await asyncio.wait_for(cli.subscribe_audio(), self.OPERATION_TIMEOUT)
                if cli.is_disconnected():
                    raise RuntimeError("ATVV 订阅完成时连接已断开")
                connected_at = time.monotonic()
                cli.on_audio_live = lambda frame, g=generation: self._if_current(g, self._on_atvv_frame, frame)
                cli.on_ctrl_live = lambda data, g=generation: self._if_current(g, self._on_atvv_ctrl, data)
                cli.on_stream_start_live = lambda data, g=generation: self._if_current(g, self._on_atvv_stream_start, data)
                cli.on_stream_stop_live = lambda data, g=generation: self._if_current(g, self._on_atvv_stream_stop, data)
                cli.on_codec_sync_live = lambda predictor, step_index, g=generation: self._if_current(
                    g, self._on_atvv_codec_sync, predictor, step_index
                )
                self._ready.set()
                self._recovery_state = "ready"
                self._recovery_last_error = ""
                self._emit_event(
                    "voice.ready",
                    attempt=self._recovery_attempt,
                    session=self._recovery_session,
                    generation=generation,
                    ready=True,
                    connected=True,
                    subscribed=True,
                    frame_size=cli.frame_size,
                    protocol=cli.protocol_version,
                )
                # live2 不预开面板：切换式语音会话激活会切系统通信模式、
                # 静音其他声音流（用户实测看视频无声）——面板必须用完即关，
                # 每段在按下瞬间现开（F5 已被 LL 钩子吞，防误触不触发）。
                while not self._stop.is_set():
                    if cli.is_disconnected():
                        raise RuntimeError("ATVV 连接已断开")
                    if not self._collecting:
                        # 空闲：等 begin 命令（on_voice / rawinput 兜底 / 帧自检）
                        cmd_wait = asyncio.create_task(self._cmds.get())
                        disconnect_wait = asyncio.create_task(self._wait_disconnect(cli))
                        stop_wait = asyncio.create_task(self._stop_async.wait())
                        try:
                            done, _pending = await asyncio.wait(
                                {cmd_wait, disconnect_wait, stop_wait},
                                timeout=1.0,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                        finally:
                            for t in (cmd_wait, disconnect_wait, stop_wait):
                                if not t.done():
                                    t.cancel()
                            await asyncio.gather(cmd_wait, disconnect_wait, stop_wait, return_exceptions=True)
                        if stop_wait in done:
                            break
                        if disconnect_wait in done:
                            raise RuntimeError("ATVV 连接已断开")
                        if not done:
                            if cli.audio_stopped.is_set():
                                # 游离短流的结束信号：清掉，避免下次误判
                                cli.audio_stopped.clear()
                            continue
                        cmd = cmd_wait.result()
                        if cmd == "prepare_live":
                            await self._prepare_live_session(cli)
                        elif cmd == "begin":
                            await self._begin_session(cli)
                        continue
                    # 会话中：等遥控器松手（00 02）/ finish 兜底。
                    # v2 松手播放模式不能把固定时长 watchdog 放进 FIRST_COMPLETED：
                    # 历史上 4s watchdog 已经导致过录音被截断；本轮 2s dog
                    # 与用户复现的固定 1.9s 对上。该模式有 RawInput up 兜底。
                    stop_wait = asyncio.create_task(cli.audio_stopped.wait())
                    cmd_wait = asyncio.create_task(self._cmds.get())
                    disconnect_wait = asyncio.create_task(self._wait_disconnect(cli))
                    stop_daemon_wait = asyncio.create_task(self._stop_async.wait())
                    dog = None
                    wait_set = {stop_wait, cmd_wait, disconnect_wait, stop_daemon_wait}
                    if self.mode != "wechat" or self.live:
                        dog = asyncio.create_task(asyncio.sleep(2.0))
                        wait_set.add(dog)
                    frame_mark = len(cli.audio_frames)
                    try:
                        done, _pending = await asyncio.wait(
                            wait_set,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    finally:
                        for t in (stop_wait, cmd_wait, disconnect_wait, stop_daemon_wait, dog):
                            if t is not None and not t.done():
                                t.cancel()
                        await asyncio.gather(*wait_set, return_exceptions=True)
                    if stop_daemon_wait in done:
                        self._clear_voice_session()
                        break
                    if disconnect_wait in done:
                        self._clear_voice_session()
                        raise RuntimeError("ATVV 连接已断开")
                    should_end = stop_wait in done
                    if cmd_wait in done and not should_end:
                        command = cmd_wait.result()
                        if command != "finish":
                            # RawInput/控制流同时触发的重复 begin 不能结束会话。
                            continue
                        try:
                            await asyncio.wait_for(cli.audio_stopped.wait(), 0.26)
                        except asyncio.TimeoutError:
                            pass
                        should_end = True
                    if dog is not None and dog in done and not should_end:
                        if len(cli.audio_frames) != frame_mark:
                            continue
                        self.log("(会话开启但 2 秒无音频，结束)")
                        should_end = True
                    if should_end:
                        await asyncio.wait_for(self._end_session(cli), self.OPERATION_TIMEOUT)
            except Exception as e:
                self._clear_voice_session()
                self._ready.clear()
                self._active_generation = 0
                self._recovery_state = "reconnecting"
                self._recovery_last_error = f"{type(e).__name__}: {e}"
                if connected_at and time.monotonic() - connected_at >= 10.0:
                    self._recovery_attempt = 0
                    backoff = self.RECONNECT_INITIAL
                else:
                    self._recovery_attempt += 1
                delay = backoff
                self.log(f"语音连接断开/出错({type(e).__name__}: {e})，{delay:.0f} 秒后重连…")
                self._emit_event(
                    "voice.disconnected",
                    attempt=self._recovery_attempt,
                    session=self._recovery_session,
                    generation=generation,
                    error=str(e),
                    error_type=type(e).__name__,
                    state=self._recovery_state,
                    connected=False,
                    ready=False,
                    collecting=self._collecting,
                )
                try:
                    await asyncio.wait_for(cli.close(), 2.0)
                except Exception as cleanup_error:
                    self._emit_event("voice.cleanup_failed", error=str(cleanup_error))
                client_closed = True
                if self._stop.is_set():
                    break
                self._emit_event(
                    "voice.retry",
                    attempt=self._recovery_attempt,
                    session=self._recovery_session,
                    generation=generation,
                    delay=delay,
                    retry_in=delay,
                )
                try:
                    await asyncio.wait_for(self._stop_async.wait(), delay)
                except asyncio.TimeoutError:
                    pass
                backoff = min(30.0, backoff * 2.0)
            finally:
                if self._active_generation == generation:
                    self._active_generation = 0
                if not client_closed:
                    try:
                        await asyncio.wait_for(cli.close(), 2.0)
                    except Exception as cleanup_error:
                        self._emit_event("voice.cleanup_failed", error=str(cleanup_error))

    def _emit_event(self, event_name: str, **fields):
        callback = self.on_event
        if callback is None:
            return
        clean = {}
        for key, value in fields.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                clean[key] = value[:4096] if key == "error" and isinstance(value, str) else value
        try:
            callback(event_name, **clean)
        except Exception:
            pass

    def recovery_status(self) -> dict:
        return {
            "state": self._recovery_state,
            "attempt": self._recovery_attempt,
            "last_error": self._recovery_last_error,
            "session": self._recovery_session,
        }

    def _if_current(self, generation: int, callback, *args):
        if generation != self._active_generation or self._stop.is_set():
            return None
        return callback(*args)

    def _clear_voice_session(self):
        self._collecting = False
        self._live_drain.set()
        self._live_started.set()
        self._live_ready.clear()
        self._live_active = False
        self._live_armed = False
        self._live_q = None
        self._live_prelude = []
        self._live_request_at = 0.0
        self._capture_meta = {}
        q = self._cmds
        if q is not None:
            while True:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break

    # ---- ATVV 常驻回调（winrt 线程） ----
    def _on_atvv_ctrl(self, data: bytes):
        self._atvv_last = time.monotonic()
        if not data:
            return
        if data[0] in (OP_START_SEARCH_V1, OP_START_SEARCH):
            self.log("ATVV START_SEARCH，响应 MIC_OPEN")
            # live2 不提前 prepare：与 begin 的 prepare 双触发会在快速连按
            # 时互相打架（旧会话未清 → 新会话被守卫挡掉 → 该段音频全丢），
            # 统一由 begin 单点启动
            if self.mode == "wechat" and self.live and not self.live2:
                self._live_request_at = time.monotonic()
                self._put("prepare_live")
        elif data[0] == OP_MIC_OPEN and len(data) >= 3:
            self.log(f"ATVV MIC_OPEN_ERROR code=0x{data[1]:02X}{data[2]:02X}")

    def _on_atvv_stream_start(self, data: bytes):
        """控制通道 0x04 是 ATVV 会话真实开始，F5 只作为兜底。"""
        self._atvv_last = time.monotonic()
        reason = data[1] if len(data) > 1 else 0xFF
        codec = data[2] if len(data) > 2 else 0xFF
        stream_id = data[3] if len(data) > 3 else 0x00
        self.log(
            f"ATVV AUDIO_START reason=0x{reason:02X} "
            f"codec=0x{codec:02X} stream=0x{stream_id:02X}"
        )
        if self.mode == "wechat" and self.live and self._live_request_at > 0:
            elapsed = (time.monotonic() - self._live_request_at) * 1000
            self.log(f"实时延迟 AUDIO_START +{elapsed:.0f}ms")
        if self.live2 and self.arm_suppression is not None:
            try:
                self.arm_suppression()
            except Exception:
                pass
        if not self._collecting and time.monotonic() - self._last_session_end > 0.5:
            self.log("检测到语音流，开始会话…")
            self.begin()

    def _on_atvv_stream_stop(self, data: bytes):
        self._atvv_last = time.monotonic()
        reason = data[1] if len(data) > 1 else 0xFF
        self.log(f"ATVV AUDIO_STOP reason=0x{reason:02X}")
        if self.mode == "wechat" and self.live and self._live_request_at > 0:
            elapsed = (time.monotonic() - self._live_request_at) * 1000
            self.log(f"实时延迟 AUDIO_STOP +{elapsed:.0f}ms")

    def _on_atvv_codec_sync(self, predictor: int, step_index: int):
        item = ("sync", predictor, step_index)
        if not self._collecting:
            if self.mode == "wechat" and self.live:
                with self._live_order_lock:
                    self._live_prelude.append(item)
            return
        self._session_sync_count = getattr(self, "_session_sync_count", 0) + 1
        if self._session_sync_count <= 3:
            self.log(
                f"ATVV AUDIO_SYNC predictor={predictor} "
                f"step={step_index}"
            )
        if self.mode == "wechat" and self.live:
            with self._live_order_lock:
                if not self._live_pipeline_ready or not self._live_process_item(item):
                    self._live_prelude.append(item)

    def _on_atvv_frame(self, frame: bytes):
        """音频帧到达：更新活动时间戳；live 模式空闲时自检开会话。"""
        self._atvv_last = time.monotonic()
        item = ("audio", frame)
        if self._collecting:
            if self._live_q is not None:
                with self._live_order_lock:
                    if not self._live_pipeline_ready or not self._live_process_item(item):
                        self._live_prelude.append(item)
            else:
                # 实时管道未建好（正在启动/等锁）：帧进 prelude 绝不丢
                with self._live_order_lock:
                    self._live_prelude.append(item)
            return
        # v2（松手播放）模式：会话完全由 rawinput 的 F5 down/up 驱动，
        # 帧只在上面的 collecting 分支缓冲，绝不自检触发 begin——否则与
        # F5 触发交叠产生并发会话、并发写 CABLE（AUDCLNT_E_OUT_OF_ORDER）。
        if not self.live:
            return
        # 0.3s 防抖：只挡上一段 stop 后的迟到尾包，不挡正常的连按
        if time.monotonic() - self._last_session_end > 0.3:
            if not self._live_prelude:
                self.log("检测到语音流，开始会话…")
            with self._live_order_lock:
                self._live_prelude.append(item)
            self.begin()

    def atvv_recent(self, within_ms: float = 250.0) -> bool:
        """最近 within_ms 毫秒内遥控器 ATVV 是否活跃（供 LL 钩子判定 F5 来源）。"""
        return self._atvv_last > 0 and (time.monotonic() - self._atvv_last) * 1000.0 <= within_ms

    def note_remote_f5(self, down: bool):
        """LL hook 已完成 RC003 F5 吞键；provider 可安全发送合成快捷键。"""
        if down:
            self._remote_f5_swallowed_at = time.monotonic()

    def _wait_remote_f5_gate(self, timeout: float = 0.35) -> bool:
        request_at = self._live_request_at
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            swallowed_at = self._remote_f5_swallowed_at
            if swallowed_at > 0 and swallowed_at >= request_at - 0.15:
                elapsed = (time.monotonic() - request_at) * 1000 if request_at > 0 else 0
                self.log(f"F5 吞键门禁已通过 +{elapsed:.0f}ms")
                return True
            time.sleep(0.002)
        self.log("F5 吞键门禁超时，已超过钩子判定预算后继续")
        return False

    # ---- 会话生命周期 ----
    async def _prepare_live_session(self, _cli) -> bool:
        """启动本段实时会话线程（不等待/不阻塞主循环）。

        旧会话未退出时不等待：递增 generation 让旧线程自退（其循环会检测
        代际不匹配并让位，面板留给新段）；新线程在 playback_lock 上限时
        等待旧线程释放（等待期间本段帧全部安全进 prelude）。"""
        if self.mode != "wechat" or not self.live:
            return False
        self._capture_frame_size = _cli.frame_size
        self._live_generation += 1
        generation = self._live_generation
        self._live_q = queue.Queue()
        self._live_drain.clear()
        self._live_started.clear()
        self._live_ready.clear()
        self._live_failed = False
        self._live_failure_reason = ""
        self._live_fallback_needed = False
        self._live_bytes_written = 0
        self._live_first_audio_logged = False
        self._live_first_write_logged = False
        self._live_request_at = time.monotonic()
        self._live_fallback_capture = None
        if self.live2:
            # 剪贴板快照：结束后收割"与快照不同的新文本"（切换式提交路径）
            try:
                from . import actions as _act
                self._clip_before = _act.get_clipboard_text()
            except Exception:
                self._clip_before = None
        self._live_active = True
        with self._live_lock:
            self._live_decoder = None
            self._live_accumulator = None
            self._live_resampler = None
        with self._live_order_lock:
            self._live_pipeline_ready = False
        self._live_thread = threading.Thread(
            target=self._live_session,
            args=(generation,),
            daemon=True,
            name="voice-live",
        )
        self._live_thread.start()
        return True

    async def _begin_session(self, cli):
        if self._collecting:
            return
        if cli.audio_stopped.is_set():
            # 流已停（结束信号先于会话开启的超短按）：丢弃
            self.log("(超短按，丢弃)")
            if self.mode == "wechat" and self.live and self._live_active:
                self._mark_live_failure("超短按在会话建立前已结束")
                self._live_started.set()
                self._live_drain.set()
                self._live_prelude = []
            cli.audio_stopped.clear()
            cli.audio_started.clear()
            return
        self._collecting = True
        cli.audio_stopped.clear()
        cli.audio_started.clear()
        self._session_sync_count = 0
        self._capture_frame_size = cli.frame_size
        self._capture_meta = {
            "protocol_version": cli.protocol_version,
            "frame_size": cli.frame_size,
            "stream_reason": cli.stream_reason,
            "stream_id": cli.stream_id,
            "codec": cli.selected_codec,
        }
        self._recovery_state = "recording"
        self._emit_event("voice.recording", state="recording", session=self._recovery_session)
        # RC003 的 START_SEARCH 响应由 AtvvClient 处理；会话 begin 只负责
        # 建立本地收集边界，绝不能再次主动 MIC_OPEN。
        if self.mode == "wechat" and self.live:
            self._live_armed = await self._prepare_live_session(cli)
            self._live_started.set()
            self.log("录音中…（实时送入输入法，松手出字）" if self._live_armed
                     else "录音中…（实时未就绪，本段将回退松手播放）")
        else:
            self.log("录音中…（按住说话）")
            # local 模式帧全在 cli.audio_frames（含 prelude 时期的帧）

    async def _end_session(self, cli):
        # 遥控器会在 AUDIO_STOP 后补少量尾包；保持 collecting/live writer
        # 80ms，避免尾音被误判成下一段或在 provider 提交后才写入 CABLE。
        await asyncio.sleep(0.08)
        self._collecting = False
        frames = cli.drain_audio_items()
        if self.mode == "wechat" and self.live:
            self._live_drain.set()
        try:
            await cli.mic_close()
        except Exception:
            pass
        # 固件 2671：MIC_CLOSE 后音频通知订阅失效（下一段收不到流），
        # 按言灵 REOPEN RESET 序列重订阅恢复
        await cli.resubscribe_audio()
        self._last_session_end = time.monotonic()
        self._recovery_state = "ready"
        self._emit_event("voice.capture_done", state="ready", session=self._recovery_session,
                         frames=sum(1 for item in frames if not isinstance(item, tuple) or item[0] == "audio"))
        has_audio = any(
            not isinstance(x, tuple) or (x and x[0] == "audio")
            for x in frames
        )
        # 连续两段无音频 = 链路已死，抛异常走外层断线重连
        if not has_audio and self.mode == "wechat":
            self._empty_streak = getattr(self, "_empty_streak", 0) + 1
            if self._empty_streak >= 2:
                self._empty_streak = 0
                self.log("(连续无音频，重置蓝牙链路)")
                raise RuntimeError("ATVV 链路无音频，重连")
        else:
            self._empty_streak = 0
        if self.mode == "wechat":
            if self.live:
                # v4 实时：AUDIO_STOP 后尾包/队列排干，再松开热键。
                live_thread = getattr(self, "_live_thread", None)
                if not self.live2 and live_thread is not None and live_thread.is_alive():
                    await asyncio.to_thread(live_thread.join, 1.5)
                if not self.live2:
                    if live_thread is not None and live_thread.is_alive():
                        self._mark_live_failure("实时队列 1.5 秒内未排空")
                        self._stop_live_provider("队列排空超时")
                        await asyncio.to_thread(live_thread.join, 0.5)
                else:
                    # live2：只等音频侧排干（drain+尾音 ~0.7s）；提交序列
                    # （toggle 关+剪贴板+粘贴+重开）已转交独立线程，不强停
                    if live_thread is not None and live_thread.is_alive():
                        await asyncio.to_thread(live_thread.join, 1.5)
                self._live_q = None
                self._live_started.clear()
                self._live_prelude = []
                self._live_request_at = 0.0
                # 回退条件：实时未武装（prepare 失败/被挡）或本段实时失败。
                # 旧实现只看 fallback_needed（上一段残留值）→ prepare 被
                # 挡掉的段会被误判"成功"而无声丢弃（本次修复的第二半）
                if getattr(self, "_live_armed", False) and not getattr(
                        self, "_live_fallback_needed", False):
                    return
                self.log("实时桥接失败/未启动，本段回退为松手播放")
            # v2 松手播放，或 v3 门禁失败后的本段回退
            if True:
                buffered = list(frames)
                if any(
                    not isinstance(x, tuple) or (x and x[0] == "audio")
                    for x in buffered
                ):
                    meta = dict(self._capture_meta)
                    meta.update({
                        "frame_size": cli.frame_size,
                        "stream_reason": cli.stream_reason,
                        "stream_id": cli.stream_id,
                        "codec": cli.selected_codec,
                        "stop_reason": cli.stop_reason,
                    })
                    threading.Thread(
                        target=self._wechat_playback, args=(buffered, meta),
                        daemon=True,
                    ).start()
                else:
                    self.log("(没有收到音频帧)")
        elif frames:
            meta = dict(self._capture_meta)
            meta["frame_size"] = cli.frame_size
            threading.Thread(
                target=self._pipeline, args=(frames, meta), daemon=True
            ).start()
        else:
            self.log("(没有收到音频帧)")
        self._live_prelude = []

    # ---- 线程安全的对外接口 ----
    def begin(self):
        self._put("begin")

    def finish(self):
        self._put("finish")

    def _put(self, cmd: str):
        loop = self.loop
        if loop and self._cmds is not None:
            loop.call_soon_threadsafe(self._cmds.put_nowait, cmd)

    # ---- 收尾流水线（local 模式工作线程）----
    @staticmethod
    def _pcm_metrics(pcm: list[int]) -> dict:
        if not pcm:
            return {"duration": 0.0, "rms": 0.0, "peak": 0, "nonzero_pct": 0.0}
        square_sum = sum(sample * sample for sample in pcm)
        nonzero = sum(sample != 0 for sample in pcm)
        return {
            "duration": len(pcm) / 16000.0,
            "rms": math.sqrt(square_sum / len(pcm)),
            "peak": max(abs(sample) for sample in pcm),
            "nonzero_pct": nonzero * 100.0 / len(pcm),
        }

    def _write_capture_diagnostics(self, items, pcm, stats, meta) -> Path | None:
        if not self.diagnostics:
            return None
        try:
            root = self.diagnostics_root or (
                Path(os.environ.get("APPDATA", str(Path.home())))
                / "MiRemoteVibe" / "diagnostics"
            )
            root.mkdir(parents=True, exist_ok=True)
            wav_path = root / "last_utterance.wav"
            raw_path = root / "last_capture.adpcm"
            json_path = root / "last_capture.json"
            write_wav(wav_path, pcm)
            raw = bytearray()
            syncs = []
            for item in items:
                if isinstance(item, tuple) and item and item[0] == "sync":
                    syncs.append({
                        "byte_offset": len(raw),
                        "predictor": int(item[1]),
                        "step_index": int(item[2]),
                    })
                elif isinstance(item, tuple) and item and item[0] == "audio":
                    raw.extend(item[1])
                elif isinstance(item, (bytes, bytearray)):
                    raw.extend(item)
            raw_path.write_bytes(raw)
            payload = {"capture": meta, "decode": stats, "pcm": self._pcm_metrics(pcm), "syncs": syncs}
            json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return wav_path
        except Exception as exc:
            self.log(f"诊断文件写入失败: {exc}")
            return None

    def _decode_session(self, items, meta):
        frame_size = int((meta or {}).get("frame_size") or self._capture_frame_size or 120)
        pcm, stats = AtvvClient.decode_audio_items(items, frame_size=frame_size)
        metrics = self._pcm_metrics(pcm)
        return pcm, stats, metrics

    def _pipeline(self, frames: list[bytes], meta: dict | None = None):
        try:
            pcm, stats, metrics = self._decode_session(frames, meta)
            if not pcm:
                self.log("(音频为空)")
                return
            wav_path = Path.cwd() / "last_utterance.wav"
            write_wav(wav_path, pcm)
            self.log(
                f"收音 {metrics['duration']:.1f}s / {stats['notifications']}包 / "
                f"{stats['raw_bytes']}B，RMS={metrics['rms']:.0f} "
                f"peak={metrics['peak']} sync={stats['sync_count']}，转写中…"
            )
            text = transcribe_local(wav_path, model_name=self.model)
            if text:
                if self._stop.is_set():
                    self.log("(守护已停止，丢弃迟到转写结果)")
                    return
                self.log(f"识别: {text}")
                self.on_text(text)
            else:
                self.log("(转写无结果)")
        except Exception as e:
            import traceback
            self.log(f"语音流水线异常: {e}\n{traceback.format_exc()}")

    def _press_hotkey(self, down: bool):
        """按住式输入法语音热键：down=按下并保持（开录音），up=松开（结束识别上屏）。

        用户微信输入法的语音热键是 Ctrl+Alt+V 按住式；要求 LL 钩子已吞掉
        RC003 的 F5（否则 F5 在场会干扰组合键检测，浮窗不出现——v1 教训）。
        """
        if not self.wechat_hotkey:
            return False
        try:
            from . import actions
            from .keys import name_to_vk
            vks = [name_to_vk(n) for n in self.wechat_hotkey]
            sent = True
            if down:
                for vk in vks:
                    sent = actions._tap(vk) and sent
            else:
                for vk in reversed(vks):
                    sent = actions._tap(vk, up=True) and sent
            return sent
        except Exception as e:
            self.log(f"快捷键触发失败: {e}")
            return False

    def _tap_hotkey(self, hold_ms: int = 80) -> bool:
        """完整短按一次输入法热键；WeType toggle 必须在等待面板前完成 key-up。"""
        if not self.wechat_hotkey:
            self.log("快捷键触发失败: 未配置微信语音快捷键")
            return False
        down_sent = False
        up_sent = False
        try:
            down_sent = self._press_hotkey(down=True)
            if not down_sent:
                return False
            time.sleep(max(0.03, min(0.3, hold_ms / 1000.0)))
            up_sent = self._press_hotkey(down=False)
            return up_sent
        except Exception as exc:
            self.log(f"快捷键短按失败: {exc}")
            return False
        finally:
            if not up_sent:
                self._press_hotkey(down=False)

    # ---- 微信模式 v5：ATVV 驱动 + provider toggle 的实时流 ----
    def _start_live_provider(self) -> bool:
        with self._live_state_lock:
            if self._live_provider_started:
                return True
            self._live_provider_started = True
            if self._live_request_at <= 0:
                self._live_request_at = time.monotonic()
        if self._tap_hotkey(80):
            self.log("WeType toggle 已发送（启动，80ms）")
            return True
        with self._live_state_lock:
            self._live_provider_started = False
        return False

    def _stop_live_provider(self, reason: str) -> bool:
        with self._live_state_lock:
            if not self._live_provider_started:
                return False
            self._live_provider_started = False
        sent = self._tap_hotkey(80)
        self.log(
            f"WeType toggle 已发送（提交，80ms，reason={reason}）"
            if sent else f"WeType toggle 提交失败（reason={reason}）"
        )
        return sent

    def _close_stale_wetype_panel(self):
        try:
            from . import actions
            if actions.close_windows(r"^语音输入$"):
                deadline = time.monotonic() + 0.2
                while time.monotonic() < deadline and self._wetype_panel_visible():
                    time.sleep(0.02)
                self.log("已关闭上一轮残留的 WeType 语音面板")
        except Exception as exc:
            self.log(f"关闭残留 WeType 面板失败: {exc}")

    def _mark_live_failure(self, reason: str):
        first = False
        with self._live_state_lock:
            if not self._live_failed:
                first = True
            self._live_failed = True
            self._live_fallback_needed = self._live_bytes_written == 0
            self._live_failure_reason = reason
        if first:
            self.log(f"实时桥接门禁失败: {reason}")

    @staticmethod
    def _wetype_panel_visible() -> bool:
        try:
            from . import actions
            return bool(actions.find_windows(r"^语音输入$"))
        except Exception:
            return False

    def _wait_wetype_ready(self) -> bool:
        timeout = self._wetype_ready_timeout()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            if self._wetype_panel_visible():
                return True
            time.sleep(0.02)
        return False

    def _wetype_ready_timeout(self) -> float:
        return max(1.2, self.ready_delay)

    def _reset_live_pipeline(self):
        rate = int(getattr(self, "_live_rate", 0))
        if rate <= 0:
            raise RuntimeError("CABLE sample rate unavailable")
        with self._live_lock:
            self._live_decoder = ImaAdpcmDecoder()
            self._live_accumulator = FrameAccumulator(self._capture_frame_size)
            self._live_resampler = StreamingLinearResampler(16000, rate)

    def _live_process_item(self, item: object) -> bool:
        q = self._live_q
        if q is None or self._live_failed:
            return q is not None
        packets = []
        try:
            with self._live_lock:
                decoder = getattr(self, "_live_decoder", None)
                accumulator = getattr(self, "_live_accumulator", None)
                resampler = getattr(self, "_live_resampler", None)
                if decoder is None or accumulator is None or resampler is None:
                    return False
                if isinstance(item, tuple) and item and item[0] == "sync":
                    accumulator.reset()
                    decoder.predictor = int(item[1])
                    decoder.step_index = max(0, min(88, int(item[2])))
                    resampler.reset()
                    return True
                data = item[1] if isinstance(item, tuple) and item and item[0] == "audio" else item
                if not isinstance(data, (bytes, bytearray)) or not data:
                    return True
                raw = bytes(data)
                frame_size = self._capture_frame_size
                if len(raw) in (frame_size + 6, 134):
                    accumulator.reset()
                    decoder.predictor = struct.unpack(">h", raw[3:5])[0]
                    decoder.step_index = max(0, min(88, raw[5]))
                    frames = [raw[6:]]
                else:
                    frames = accumulator.append(raw)
                for frame in frames:
                    output = resampler.convert(decoder.decode(frame))
                    if output:
                        packets.append(struct.pack(f"<{len(output)}h", *output))
            for packet in packets:
                q.put_nowait(packet)
            if packets and not self._live_first_audio_logged:
                self._live_first_audio_logged = True
                elapsed = (time.monotonic() - self._live_request_at) * 1000
                self.log(f"实时延迟 首个PCM +{elapsed:.0f}ms")
            return True
        except Exception as exc:
            self._mark_live_failure(f"音频解码/重采样异常: {exc}")
            return True

    def _live_write(self, frame: bytes):
        self._live_process_item(("audio", frame))

    def _live_session(self, generation: int):
        """实时会话线程：面板现开/复用，实时写 CABLE，STOP 后排空收尾。"""
        q = self._live_q
        drain = self._live_drain
        pending = bytearray()
        started_deadline = time.monotonic() + 4.0
        # 限时等待锁：快速连按时等上一段排空释放（期间本段帧安全在 prelude）；
        # 阻塞的是本线程（live_session），不影响 asyncio 主循环与帧回调
        writer_locked = self._playback_lock.acquire(timeout=3.5)
        try:
            if not writer_locked:
                self._mark_live_failure("等待 CABLE 写入器超时（上一段未排空）")
                return
            if not self._ensure_cable_stream():
                self._mark_live_failure("无法打开 CABLE Input")
                return
            self._reset_live_pipeline()
            if self.live2:
                # live2（用户方案）：按下瞬间现开面板，音频先缓冲（prelude）、
                # 面板就绪后补进+实时跟随（"声音稍延迟输入"）。此时 F5 已被
                # LL 钩子吞（系统无 F5 按住），防误触不触发；若上段面板残留
                # 未关则直接复用。开不了（1.8s×2 重试）→ 回退松手播放。
                if not self._ensure_panel_open():
                    self._mark_live_failure("live2 面板现开失败（看『语音键 HID 报告』日志行：未抑制=抹除失效，已抑制=面板注入被拒）")
                    return
            else:
                self._close_stale_wetype_panel()
                self._wait_remote_f5_gate()
                if not self._start_live_provider():
                    self._mark_live_failure("WeType toggle 启动失败")
                    return
                if not self._wait_wetype_ready():
                    self._mark_live_failure(
                        f"WeType 语音面板未在 {self._wetype_ready_timeout():.1f} 秒内出现"
                    )
                    return
            self._live_ready.set()
            ready_elapsed = (time.monotonic() - self._live_request_at) * 1000
            self.log(f"WeType 已进入监听 +{ready_elapsed:.0f}ms，开始实时送音")

            # 切换为 direct 模式时持有 order lock：保证面板启动期积压帧
            # 先于同时到达的新帧进入 decoder/resampler。
            with self._live_order_lock:
                prelude, self._live_prelude = self._live_prelude, []
                for item in prelude:
                    self._live_process_item(item)
                self._live_pipeline_ready = True

            block_bytes = max(2, int(self._live_rate * 0.06) * 2)
            while not self._stop.is_set():
                if generation != self._live_generation:
                    self._mark_live_failure("实时会话被新一代覆盖")
                    break
                if not self._live_started.is_set() and time.monotonic() >= started_deadline:
                    self._mark_live_failure("ATVV AUDIO_START 超时")
                    break
                try:
                    packet = q.get(timeout=0.02)
                    if packet:
                        pending.extend(packet)
                except queue.Empty:
                    pass
                while len(pending) >= block_bytes:
                    block = bytes(pending[:block_bytes])
                    del pending[:block_bytes]
                    self._cable_stream.write(block)
                    self._live_bytes_written += len(block)
                    if not self._live_first_write_logged:
                        self._live_first_write_logged = True
                        elapsed = (time.monotonic() - self._live_request_at) * 1000
                        self.log(f"实时延迟 首次CABLE写入 +{elapsed:.0f}ms")
                if drain.is_set() and q.empty():
                    if pending:
                        block = bytes(pending)
                        self._cable_stream.write(block)
                        self._live_bytes_written += len(block)
                        if not self._live_first_write_logged:
                            self._live_first_write_logged = True
                            elapsed = (time.monotonic() - self._live_request_at) * 1000
                            self.log(f"实时延迟 首次CABLE写入 +{elapsed:.0f}ms")
                        pending.clear()
                    break

            if not self._live_failed and self._cable_stream is not None:
                # 尾音与收尾等待加长（0.15/0.18 -> 0.35/0.5）：WeType 的 VAD
                # 需要足够静音确认句尾，过早 toggle 关会吞掉最后几个字
                tail_samples = int(self._live_rate * 0.35)
                self._cable_stream.write(b"\x00\x00" * tail_samples)
                time.sleep(0.5)
        except Exception as exc:
            self._close_cable_stream()
            self._mark_live_failure(f"CABLE 实时写入中断: {exc}")
        finally:
            superseded = generation != self._live_generation  # 已被更新的段取代
            if self.live2 and not self._live_failed and not superseded:
                # live2 结束序列（toggle 关+剪贴板+粘贴）独立线程，
                # 不阻塞 _end_session（那里对 live2 跳过 join）
                threading.Thread(
                    target=self._finish_live2_session, daemon=True, name="live2-finish",
                ).start()
            elif not self.live2 and not superseded:
                self._stop_live_provider("音频已排空" if not self._live_failed else "实时门禁失败")
            # 被取代的旧段：不收尾、不关面板（面板与新段连续复用，两段话
            # 由新段的结束序列一并收割提交）——这是快速连按的让位机制
            with self._live_order_lock:
                self._live_pipeline_ready = False
            if not superseded and self._live_request_at > 0:
                elapsed = (time.monotonic() - self._live_request_at) * 1000
                self.log(f"实时延迟 provider 提交 +{elapsed:.0f}ms")
            self._live_ready.clear()
            if generation == self._live_generation:
                self._live_active = False
            if writer_locked:
                self._playback_lock.release()
            if self._live_failed:
                self.log(f"实时桥接结束（将回退松手播放）：{self._live_failure_reason}")
            else:
                self.log("已送入输入法（实时模式）")

    # ---- live2（时序错位路线，2026-08-29 实测定型）----
    def _release_all_modifiers(self):
        """强制松开全部修饰键（注入卫生）：toggle 三键的慢速 up 在某些
        时序下不能完全消化，残留的 Win 会把粘贴 Ctrl+V 变成 Win+Ctrl+V
        （弹出系统声音设置）。粘贴前必须清场。"""
        from . import actions
        for vk in (0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5,  # L/R Shift/Ctrl/Alt
                   0x5B, 0x5C):                          # L/R Win
            actions._tap(vk, up=True)

    def _paste_text(self):
        """清场修饰键后粘贴（Ctrl+V）。"""
        from . import actions
        self._release_all_modifiers()
        time.sleep(0.05)
        actions.send_combo(["VK_CONTROL", "VK_V"])

    def _tap_toggle_slow(self, hold: float = 0.5, gap: float = 0.06):
        """慢速三键 toggle（人类节奏）。快速连发会被 WeType 事件流吞掉——
        8-29 实测：微秒级连发关不掉面板，60ms 节奏 0.5s 干净关闭。"""
        from . import actions
        from .keys import name_to_vk
        vks = [name_to_vk(n) for n in self.toggle_hotkey]
        for vk in vks:
            actions._tap(vk)
            time.sleep(gap)
        time.sleep(hold)
        for vk in reversed(vks):
            actions._tap(vk, up=True)
            time.sleep(gap)

    def _ensure_panel_open(self, timeout: float = 1.8, retries: int = 2) -> bool:
        """切换式面板在=直接复用；不在=注入开启并确认（带重试）。live2 核心：
        所有面板操作都发生在空闲窗口（无硬件键按住），实测注入有效。
        重试必要：紧贴的"关→开"序列会让 WeType 状态机短暂紊乱（首测段4起
        连续现开失败的根因），第二次 toggle 前留 0.8s 喘息。"""
        if self._wetype_panel_visible():
            return True
        for attempt in range(retries):
            self._tap_toggle_slow(hold=0.8)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and not self._stop.is_set():
                if self._wetype_panel_visible():
                    return True
                time.sleep(0.08)
            if attempt + 1 < retries:
                time.sleep(0.8)  # 给 WeType 状态机喘息
        return False

    def _prime_panel(self):
        """已废弃：不再预开面板（语音会话常开=通信模式静音其他声音流）。
        保留空实现仅为兼容可能的旧调用。"""
        return

    def _harvest_clipboard(self, timeout: float = 2.5) -> str | None:
        """等待 WeType 切换式结束写入剪贴板的新文本（与快照不同且非空）。"""
        from . import actions
        old = self._clip_before
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            text = actions.get_clipboard_text()
            if text and text.strip() and text != old:
                return text
            time.sleep(0.1)
        return None

    def _toggle_close_and_paste(self, old_clip: str | None, timeout: float = 4.5):
        """live2 统一收尾：慢速 toggle 关面板（失败自动补关）→ 收割剪贴板
        （增量去重）→ 清修饰键后 Ctrl+V 粘贴。"""
        from . import actions
        self._tap_toggle_slow()
        text = None
        closed = False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            if not closed and not self._wetype_panel_visible():
                closed = True
            t = actions.get_clipboard_text()
            if t and t.strip() and t != old_clip:
                text = t
                break
            time.sleep(0.1)
        # 面板关不掉=会话未结束，下一段识别会与旧文本累积拼接（重复主因）
        # ——补关：再 toggle 一次，仍不行注入 Esc 兜底
        if self._wetype_panel_visible():
            self._tap_toggle_slow(hold=0.4)
            close_deadline = time.monotonic() + 1.5
            while time.monotonic() < close_deadline and self._wetype_panel_visible():
                time.sleep(0.1)
            if self._wetype_panel_visible():
                from .keys import name_to_vk
                actions._tap(name_to_vk("VK_ESCAPE"))
                esc_deadline = time.monotonic() + 3.0
                while time.monotonic() < esc_deadline and self._wetype_panel_visible():
                    time.sleep(0.1)
            if not self._wetype_panel_visible():
                closed = True
                # Esc 兜底关闭后剪贴板可能才写入，再补收割一轮
                extra_deadline = time.monotonic() + 2.0
                while time.monotonic() < extra_deadline and not text:
                    t = actions.get_clipboard_text()
                    if t and t.strip() and t != old_clip:
                        text = t
                    time.sleep(0.1)
        last = getattr(self, "_last_pasted_text", "")
        if text and text == last:
            self.log(f"live2: 跳过重复文本（与上一段相同）: {text[:30]}")
            text = None
        if text and last and text.startswith(last) and len(text) > len(last):
            # 残留面板累积=旧文本+新文本拼接：只粘增量，防旧文重复
            text = text[len(last):]
            self.log(f"live2: 检测到累积拼接，仅粘贴增量")
        if text:
            # 稳定窗口：WeType 对长文本会分块写剪贴板，等文本停止变化再粘
            stable_until = time.monotonic() + 0.5
            while time.monotonic() < stable_until and not self._stop.is_set():
                t2 = actions.get_clipboard_text()
                if t2 and t2 != text:
                    text = t2
                    stable_until = time.monotonic() + 0.5
                time.sleep(0.1)
        full_text = text
        if text:
            if self._stop.is_set():
                self.log("live2: 守护已停止，丢弃迟到文本")
                return
            time.sleep(0.15)  # 等焦点稳定
            self._paste_text()
            # 记录完整文本（非增量），供下一段做前缀对比
            self._last_pasted_text = full_text
            self.log(f"live2 已上屏: {text[:50]}")
        elif text is None and closed:
            self.log("live2: 剪贴板无新文本（识别空/写入延迟/未提交）")
        if not closed:
            self.log("live2: toggle 未关掉面板")

    def _finish_live2_session(self):
        """live2 结束序列（独立线程，不阻塞会话主循环）：切换式收尾
        （关面板+收割+粘贴），不重开面板（语音会话常开会切通信模式
        静音其他声音流，用户要求用完即关）。"""
        try:
            if not self._wetype_panel_visible():
                self.log("live2: 面板已不在（可能被手动关闭），跳过提交")
                return
            self._toggle_close_and_paste(self._clip_before)
        except Exception as e:
            self.log(f"live2 结束序列异常: {e}")

    def _wechat_playback(self, frames: list[bytes], meta: dict | None = None):
        """松手后播放（回退模式，工作线程）。

        live2 回退：统一切换式（toggle 开→播放→toggle 关→收割粘贴），
        与主路径面板控制完全一致——按住式（Ctrl+Alt+V）与切换式两种
        会话类型交替会污染 WeType 状态机（用户实测"两种模式混杂导致
        唤起不稳定"）。非 live2 保持原行为（v4 toggle / 稳定版按住式）。
        串行互斥：连续两段不会并发写 CABLE（OUT_OF_ORDER）。
        """
        if not self._playback_lock.acquire(blocking=False):
            self.log("(上一段仍在播放，本段丢弃)")
            return
        toggle_started = False
        live2_toggle = False
        hold_started = False
        try:
            if self._stop.is_set():
                return
            pcm, stats, metrics = self._decode_session(frames, meta)
            if not pcm:
                self.log("(音频为空)")
                return
            diagnostic_path = self._write_capture_diagnostics(frames, pcm, stats, meta or {})
            message = (
                f"收音 {metrics['duration']:.1f}s / {stats['notifications']}包 / "
                f"{stats['raw_bytes']}B，RMS={metrics['rms']:.0f} "
                f"peak={metrics['peak']} sync={stats['sync_count']}，喂给微信语音…"
            )
            self.log(message)
            if diagnostic_path is not None:
                self.log(f"诊断 WAV: {diagnostic_path}")
            if self._stop.is_set():
                return
            if self.live2 or self.live:
                # 实时系（live2/开发版）回退：切换式开面板（与主路径同一套
                # 动作）；非实时稳定版保持按住式原行为
                if not self._ensure_panel_open():
                    self.log("live2 回退播放失败: 面板无法开启")
                    return
                live2_toggle = True
            elif self.live:
                toggle_started = self._tap_hotkey(80)
                if not toggle_started:
                    self.log("微信回退播放失败: WeType toggle 启动失败")
                    return
            else:
                # 稳定版：按住式 down→播放→up（松手后空闲注入，已验证）
                if not self._press_hotkey(down=True):
                    raise RuntimeError("微信语音热键发送失败")
                hold_started = True
            if self._stop.wait(self.ready_delay):
                return
            if not self._ensure_cable_stream():
                if live2_toggle:
                    self._toggle_close_and_paste(None)
                    live2_toggle = False
                elif toggle_started:
                    self._tap_hotkey(80)
                    toggle_started = False
                else:
                    self._press_hotkey(down=False)
                    hold_started = False
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
            data = _s.pack(f"<{len(out)}h", *out)
            chunk_bytes = max(2, int(rate * 0.05) * 2)
            for offset in range(0, len(data), chunk_bytes):
                if self._stop.is_set():
                    return
                self._cable_stream.write(data[offset:offset + chunk_bytes])
            if self._stop.wait(0.35):
                return
            if live2_toggle:
                # live2 回退：切换式收尾（与主路径一致：关面板+收割+粘贴）
                from . import actions as _act
                old_clip = None
                try:
                    old_clip = _act.get_clipboard_text()
                except Exception:
                    pass
                self._toggle_close_and_paste(old_clip)
                toggle_started = False
                live2_toggle = False
            elif toggle_started:
                self._tap_hotkey(80)
                toggle_started = False
            else:
                self._press_hotkey(down=False)  # 松开 -> 输入法识别上屏
                hold_started = False
            self.log("已交由微信识别（去语气词/整理）")
        except Exception as e:
            import traceback
            self.log(f"微信播放管线异常: {e}\n{traceback.format_exc()}")
            try:
                if live2_toggle:
                    self._toggle_close_and_paste(None)
                    live2_toggle = False
                elif toggle_started:
                    self._tap_hotkey(80)
                    toggle_started = False
                else:
                    self._press_hotkey(down=False)   # 异常时尽力松开热键
                    hold_started = False
                if self._cable_stream is None:
                    pass  # 常开流：仅在写失败时已置空，无需处理
            except Exception:
                pass
        finally:
            try:
                if hold_started:
                    self._press_hotkey(down=False)
                if self._stop.is_set():
                    self._close_cable_stream()
            finally:
                self._playback_lock.release()

    def _close_cable_stream(self):
        st = getattr(self, "_cable_stream", None)
        if st is not None:
            try:
                st.stop()
                st.close()
            except Exception:
                pass
            self._cable_stream = None

    def _close_cable_stream_when_idle(self):
        acquired = self._playback_lock.acquire(timeout=2.0)
        if not acquired:
            self.log("CABLE 流仍在播放，跳过本次关闭以避免并发关闭")
            return
        try:
            self._close_cable_stream()
        finally:
            self._playback_lock.release()

    def _ensure_cable_stream(self):
        """CABLE 输出流常开复用（服务生命周期内不关，写失败自动置空重开）。

        每段会话开关流曾在 WASAPI/PortAudio 路径触发堆损坏（2026-08-28
        两份崩溃 dump 定位），改为常开后同时省掉每段 ~50ms 的开流延迟。
        """
        if getattr(self, "_cable_stream", None) is not None:
            return True
        return self._open_cable_stream()

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
        """打开并常驻复用 CABLE 输出流；每段 codec 状态另行重置。"""
        try:
            import sounddevice as sd
            idx = self._find_cable_input()
            if idx is None:
                self.log("未找到 VB-CABLE（CABLE Input），微信模式不可用")
                return False
            dev = sd.query_devices(idx)
            rate = int(dev["default_samplerate"])
            self._live_rate = rate
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
