"""BLE 遥控器自动发现：注册表枚举已配对设备，避免硬编码蓝牙地址。

配对的 BLE 设备在 HKLM\\SYSTEM\\CurrentControlSet\\Enum\\BTHLEDevice 下，
键名形如 "{00001812-...}_DEV_VID&012717_PID&32B8_REV&00A4_<12位十六进制地址>"：
末段就是该设备的 48 位蓝牙地址。
"""

from __future__ import annotations

import winreg

_BTHLE_KEY = r"SYSTEM\CurrentControlSet\Enum\BTHLEDevice"
# 小米遥控器 2 Pro 的硬件标识（BTHLE 键名中的小写形态）
_HW_TOKEN = "dev_vid&012717_pid&32b8"


def find_remote_addr() -> int | None:
    """返回已配对小米遥控器的蓝牙地址；找不到返回 None。"""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _BTHLE_KEY) as root:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                if _HW_TOKEN in name.casefold():
                    tail = name.rsplit("_", 1)[-1]
                    try:
                        addr = int(tail, 16)
                        if addr:
                            return addr
                    except ValueError:
                        continue
    except OSError:
        return None
    return None
