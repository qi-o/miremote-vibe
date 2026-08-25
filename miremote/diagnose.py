"""诊断：音量±/返回等"没反应"的键到底从哪条通道发信号。

做三件事:
1. 枚举遥控器 Raw Input 设备的所有顶层集合（HidP_GetCaps，看 usage page/usage）
2. 注册所有能注册的集合（含 vendor 页）+ 键盘 + 消费控制，实时打印 RAWHID/RAWKEYBOARD
3. 另起线程订阅遥控器的主要 GATT notify 特征，实时打印报文

用法: python -m miremote diagnose [秒数，默认45]
       运行后反复按【音量+】【音量-】【返回】，观察输出。
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes as wt

from .keys import vk_name
from .rawinput import (
    RAWINPUTDEVICE, RawInputEngine, list_raw_devices, user32,
)

hid = ctypes.WinDLL("hid.dll")

RIDI_PREPARSEDDATA = 0x20000005
RIDI_DEVICENAME = 0x20000007


def collection_caps(hdev) -> tuple[int, int, int] | None:
    """对 Raw Input 设备取 HidP_GetCaps（usage page/usage/输入报文长度）。

    HIDP_CAPS 实际 66 字节，用原始缓冲 + 手动 unpack，避免结构体定义偏小越界。
    """
    size = wt.UINT(0)
    user32.GetRawInputDeviceInfoW(hdev, RIDI_PREPARSEDDATA, None, ctypes.byref(size))
    if size.value == 0:
        return None
    buf = ctypes.create_string_buffer(size.value)
    user32.GetRawInputDeviceInfoW(hdev, RIDI_PREPARSEDDATA, buf, ctypes.byref(size))
    caps_buf = ctypes.create_string_buffer(96)  # >= sizeof(HIDP_CAPS)=66
    if hid.HidP_GetCaps(buf, caps_buf) != 0:
        return None
    import struct as _s
    us = _s.unpack_from("<33H", caps_buf.raw)
    return us[1], us[0], us[2]  # UsagePage, Usage, InputReportByteLength


def open_descriptor(paths: list[str]):
    """尝试直接打开 HID 接口读集合信息（需要交互会话，代理会话可能失败）。"""
    import struct as _s

    k32 = ctypes.WinDLL("kernel32")
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = (
        wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD,
        ctypes.c_void_p,
    )
    for path in paths:
        h = k32.CreateFileW(path, 0x80000000, 3, None, 3, 0, None)  # GENERIC_READ
        if not h or h == ctypes.c_void_p(-1).value:
            print(f"   [描述符] 打不开（可能权限/占用）: …{path[-40:]}")
            continue
        ph = ctypes.c_void_p()
        if not hid.HidD_GetPreparsedData(h, ctypes.byref(ph)):
            k32.CloseHandle(h)
            continue
        caps_buf = ctypes.create_string_buffer(96)
        if hid.HidP_GetCaps(ph, caps_buf) == 0:
            us = _s.unpack_from("<33H", caps_buf.raw)
            print(f"   [描述符] page=0x{us[1]:04X} usage=0x{us[0]:04X} "
                  f"输入报文 {us[2]}B 按钮能力 {us[24]} 组")
        hid.HidD_FreePreparsedData(ph)
        k32.CloseHandle(h)


GATT_CHARS = {
    "ab5e0003-...": "ab5e0003-5a21-4f05-bc7d-af01f617b664",  # ATVV 音频
    "ab5e0004-...": "ab5e0004-5a21-4f05-bc7d-af01f617b664",  # ATVV 控制
    "8a7a0102": "8a7a0102-2c42-c2a2-0f36-41928c259b78",
    "8a7a0103": "8a7a0103-2c42-c2a2-0f36-41928c259b78",
    "8a7a0112": "8a7a0112-2c42-c2a2-0f36-41928c259b78",
    "ff03     ": "0000ff03-0000-1000-8000-00805f9b34fb",
    "battery  ": "00002a19-0000-1000-8000-00805f9b34fb",
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


def _discover_addr() -> int:
    """自动发现已配对遥控器的蓝牙地址（无硬编码）。"""
    from .blediscover import find_remote_addr
    addr = find_remote_addr()
    if not addr:
        raise RuntimeError("未发现已配对的小米遥控器")
    return addr


def gatt_listener_thread(seconds: float):
    """订阅一批 notify 特征并打印（独立线程 + 独立事件循环）。"""
    import asyncio

    async def run():
        from winrt.windows.devices.bluetooth import (
            BluetoothLEDevice, BluetoothCacheMode,
        )
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattClientCharacteristicConfigurationDescriptorValue,
        )
        dev = await BluetoothLEDevice.from_bluetooth_address_async(_discover_addr())
        if dev is None:
            print("[GATT] 连不上设备")
            return
        res = await dev.get_gatt_services_with_cache_mode_async(
            BluetoothCacheMode.UNCACHED
        )
        targets = set(GATT_CHARS.values())
        n = 0

        def make_cb(name: str):
            def cb(_s, args):
                data = ibuffer_bytes(args.characteristic_value)
                ts = time.strftime("%H:%M:%S")
                print(f"[GATT] {ts} {name} ({len(data)}B) {data.hex(' ')}")
            return cb

        for svc in res.services:
            cr = await svc.get_characteristics_with_cache_mode_async(
                BluetoothCacheMode.UNCACHED
            )
            for ch in cr.characteristics:
                cu = str(ch.uuid).lower()
                for name, want in GATT_CHARS.items():
                    if want.startswith(cu[:8]):
                        try:
                            ch.add_value_changed(make_cb(name.strip()))
                            await ch.write_client_characteristic_configuration_descriptor_async(
                                GattClientCharacteristicConfigurationDescriptorValue.NOTIFY
                            )
                            n += 1
                        except Exception as e:
                            print(f"[GATT] 订阅失败 {name.strip()}: {e}")
        print(f"[GATT] 已订阅 {n} 个特征（按遥控器时若这里出报文，说明走私有通道）")

    try:
        asyncio.run(run())
        time.sleep(seconds)  # 事件处理器在 winrt 线程池里继续跑
    except Exception as e:
        print("[GATT] 监听线程出错:", e)


def main(seconds: float = 45.0):
    print("== 1. 遥控器 Raw Input 集合 ==")
    eng = RawInputEngine()
    remote_nodes = []
    for d in list_raw_devices():
        if eng.is_remote_device(d["path"]):
            remote_nodes.append(d)
            print(" 设备:", d["path"][:90])
    if not remote_nodes:
        print("没找到遥控器（蓝牙断开？）")
        return

    caps_set = {(0x01, 0x06), (0x0C, 0x01)}  # 键盘 + 消费控制，保底注册
    remote_paths = [d["path"] for d in remote_nodes]
    n = wt.UINT(0)

    class RIDL(ctypes.Structure):
        _fields_ = [("hDevice", wt.HANDLE), ("dwType", wt.DWORD)]

    cb = ctypes.sizeof(RIDL)
    user32.GetRawInputDeviceList(None, ctypes.byref(n), cb)
    arr = (RIDL * n.value)()
    user32.GetRawInputDeviceList(arr, ctypes.byref(n), cb)
    for i in range(n.value):
        h = arr[i].hDevice
        size = wt.UINT(0)
        user32.GetRawInputDeviceInfoW(h, RIDI_DEVICENAME, None, ctypes.byref(size))
        buf = ctypes.create_unicode_buffer(size.value + 1)
        user32.GetRawInputDeviceInfoW(h, RIDI_DEVICENAME, buf, ctypes.byref(size))
        if not eng.is_remote_device(buf.value):
            continue
        if buf.value not in remote_paths:
            remote_paths.append(buf.value)
        caps = collection_caps(h)
        if caps:
            caps_set.add((caps[0], caps[1]))
            print(f"   顶层集合: page=0x{caps[0]:04X} usage=0x{caps[1]:04X} "
                  f"输入报文 {caps[2]}B")

    print("\n== 1b. HID 描述符（直接打开设备接口）==")
    open_descriptor(remote_paths)

    print(f"\n== 2. 注册 {len(caps_set)} 个集合 + GATT 监听，跑 {seconds:.0f}s ==")
    print("   现在反复按【音量+】【音量-】【返回】！\n")
    threading.Thread(
        target=gatt_listener_thread, args=(seconds,), daemon=True
    ).start()

    events = []
    eng.on_key = lambda ev: events.append(ev)

    # 手动注册全部集合
    wnd = eng._create_window()
    reg = (RAWINPUTDEVICE * len(caps_set))()
    for i, (pg, us) in enumerate(caps_set):
        reg[i].usUsagePage = pg
        reg[i].usUsage = us
        reg[i].dwFlags = 0x00000100  # RIDEV_INPUTSINK
        reg[i].hwndTarget = wnd
    if not user32.RegisterRawInputDevices(
        reg, len(caps_set), ctypes.sizeof(RAWINPUTDEVICE)
    ):
        print("注册失败:", ctypes.WinError(ctypes.get_last_error()))
        return

    # 消息循环 + RAWHID 全量打印（复用 _handle_raw 太粗，这里直接解析）
    from ctypes import wintypes as wt2
    import ctypes as ct

    class RAWKEYBOARD(ctypes.Structure):
        _fields_ = [("MakeCode", wt.USHORT), ("Flags", wt.USHORT),
                    ("Reserved", wt.USHORT), ("VKey", wt.USHORT),
                    ("Message", wt.UINT), ("ExtraInformation", wt.ULONG)]

    msg = wt2.MSG()
    end = time.time() + seconds
    WM_INPUT = 0x00FF
    while time.time() < end:
        r = user32.MsgWaitForMultipleObjectsEx(0, None, 300, 0x04FF, 0x0000)
        while user32.PeekMessageW(ct.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ct.byref(msg))
            user32.DispatchMessageW(ct.byref(msg))
        # _handle_raw 已通过 on_key 收键盘事件；RAWHID 也在里面，读 events
        while events:
            ev = events.pop(0)
            ts = time.strftime("%H:%M:%S")
            if ev.hid_bytes is not None:
                print(f"[RAWHID] {ts} {ev.hid_bytes.hex(' ')}")
            else:
                state = "up" if ev.is_up else "down"
                print(f"[KBD] {ts} {vk_name(ev.vkey)} scan=0x{ev.scan:02X} {state}")
    print("\n诊断结束。把上面的输出发给开发者。")


if __name__ == "__main__":
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 45.0
    main(secs)
