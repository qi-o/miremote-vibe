"""GATT 探测（winrt 直连版）：摸清小米蓝牙遥控器 2 Pro 的语音服务。

用法（在本机终端运行）:
  python tools/gatt_probe.py              # 枚举服务/特征/描述符（UNCACHED 全量发现）
  python tools/gatt_probe.py --listen 60  # 订阅全部 notify 特征 60 秒，按住遥控器语音键采集报文

背景：macOS 项目(MiRemote/mi-ao)实测语音走 ATVV GATT 服务
(8e400001-f315-4f60-9fb8-838830daea50, IMA ADPCM 16kHz)。
这台设备还暴露了 8a7a0001-... 疑似小米私有语音服务，需要抓包确认。
"""

from __future__ import annotations

import argparse
import pathlib
import asyncio
import sys
import time
from pathlib import Path

DEFAULT_ADDR = None  # None = 自动发现已配对设备（miremote.blediscover）

KNOWN = {
    "00001800-0000-1000-8000-00805f9b34fb": "Generic Access",
    "00001801-0000-1000-8000-00805f9b34fb": "GATT",
    "0000180a-0000-1000-8000-00805f9b34fb": "Device Information",
    "0000180f-0000-1000-8000-00805f9b34fb": "Battery",
    "00001812-0000-1000-8000-00805f9b34fb": "HID (按键通道)",
    "0000fe59-0000-1000-8000-00805f9b34fb": "Nordic DFU (固件升级)",
    "000001bf-0000-1000-8000-00805f9b34fb": "未知 0x01BF",
    "ab5e0001-5a21-4f05-bc7d-af01f617b664": "小米厂商服务 (Mi Vendor)",
    "8a7a0001-2c42-c2a2-0f36-41928c259b78": "疑似小米语音私有服务",
    "8e400001-f315-4f60-9fb8-838830daea50": "Google ATVV 语音服务(macOS 项目实测)",
}

PROP_BITS = {
    0x01: "broadcast", 0x02: "read", 0x04: "write_no_rsp", 0x08: "write",
    0x10: "notify", 0x20: "indicate", 0x40: "signed_write", 0x80: "ext_props",
}


def ibuffer_bytes(buf) -> bytes:
    """winrt IBuffer -> bytes。"""
    # pywinrt 3.x: Buffer 实现了 buffer 协议可直接 bytes()
    try:
        return bytes(buf)
    except Exception:
        pass
    try:
        from winrt.windows.storage.streams import DataReader
        dr = DataReader.from_buffer(buf)
        out = bytearray(dr.unconsumed_buffer_length)
        for i in range(len(out)):
            out[i] = dr.read_byte()
        return bytes(out)
    except Exception as e:
        print("buffer 转换失败:", e)
        return b""


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=int, metavar="SEC", default=0, help="订阅采集秒数")
    ap.add_argument("--addr", type=lambda s: int(s.replace(":", ""), 16), default=DEFAULT_ADDR)
    args = ap.parse_args()

    from winrt.windows.devices.bluetooth import BluetoothLEDevice, BluetoothCacheMode

    addr = args.addr
    if addr is None:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
        from miremote.blediscover import find_remote_addr
        addr = find_remote_addr()
        if not addr:
            print("未发现已配对的小米遥控器")
            return
    dev = await BluetoothLEDevice.from_bluetooth_address_async(addr)
    if dev is None:
        print("打不开设备（蓝牙没连上？）")
        return
    print("设备:", dev.device_id)

    # UNCACHED 强制完整服务发现（缓存里可能缺 ATVV）
    res = await dev.get_gatt_services_with_cache_mode_async(BluetoothCacheMode.UNCACHED)
    print("服务发现状态:", res.status, "数量:", len(list(res.services)))

    notifiable = []
    for svc in res.services:
        su = str(svc.uuid).lower()
        print(f"\n服务 {su}  {KNOWN.get(su, '')}")
        try:
            cr = await svc.get_characteristics_with_cache_mode_async(BluetoothCacheMode.UNCACHED)
            for ch in cr.characteristics:
                cu = str(ch.uuid).lower()
                bits = int(ch.characteristic_properties)
                props = [name for bit, name in PROP_BITS.items() if bits & bit]
                print(f"  CH {cu} [{','.join(props)}]")
                if "notify" in props or "indicate" in props:
                    notifiable.append((ch, cu))
                if "read" in props:
                    try:
                        vr = await ch.read_value_async()
                        if vr.status == 0:
                            data = ibuffer_bytes(vr.value)
                            print(f"     读到: {data.hex(' ')!r}")
                    except Exception as e:
                        print(f"     读取失败: {e}")
        except Exception as e:
            print("  特征枚举失败:", e)

    if args.listen <= 0:
        return

    print(f"\n== 采集 {args.listen}s：订阅 {len(notifiable)} 个 notify 特征 ==")
    print("现在按住遥控器【语音键】说话，松开。报文实时打印并写日志。\n")
    log = Path(__file__).parent / "atvv_capture.log"
    fh = log.open("w", encoding="utf-8")

    def make_cb(cu: str):
        def cb(sender, args):
            data = ibuffer_bytes(args.characteristic_value)
            ms = f'{time.time() % 1:.3f}'[1:]
            line = f"{time.strftime('%H:%M:%S')}{ms} {cu} ({len(data)}B) {data.hex(' ')}"
            print(line)
            fh.write(line + "\n")
            fh.flush()
        return cb

    tokens = []
    for ch, cu in notifiable:
        try:
            token = ch.add_value_changed(make_cb(cu))
            from winrt.windows.devices.bluetooth.genericattributeprofile import (
                GattClientCharacteristicConfigurationDescriptorValue,
            )
            st = await ch.write_client_characteristic_configuration_descriptor_async(
                GattClientCharacteristicConfigurationDescriptorValue.NOTIFY
            )
            tokens.append((ch, token))
            print(f"已订阅 {cu} (状态 {st})")
        except Exception as e:
            print(f"订阅失败 {cu}: {e}")

    await asyncio.sleep(args.listen)
    for ch, token in tokens:
        try:
            ch.remove_value_changed(token)
        except Exception:
            pass
    fh.close()
    print(f"\n采集结束，日志: {log}")


if __name__ == "__main__":
    asyncio.run(main())
