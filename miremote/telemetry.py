"""结构化日志(v2.0,参照 SayAll gatt_note 键值行格式)。

统一输出单行键值序列,便于 grep/统计:

    feature=voice action=session_begin phase=started result=passed mode=wechat_rt

用法:
    from .telemetry import note
    note("voice", "session_begin", phase="started", mode="wechat_rt")

约定(feature 域):keys(按键)/voice(语音)/gate(门控)/tap(哑键)/app(生命周期)。
只记状态与结果,不记路径/地址等隐私字段(见 AGENTS.md)。
"""

from __future__ import annotations

import sys
import time
from datetime import datetime


def _emit(line: str) -> None:
    # stdout 由调用方重定向(GUI 下通常无控制台,输出即丢弃,不阻塞)。
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def note(feature: str, action: str, **kv) -> None:
    """输出一条结构化日志行;异常绝不向上抛(日志不能影响主流程)。"""
    try:
        stamp = datetime.now().strftime("%H:%M:%S")
        parts = [f"{stamp}", f"feature={feature}", f"action={action}"]
        parts.extend(f"{k}={v}" for k, v in kv.items())
        _emit(" ".join(parts))
    except Exception:
        pass


class Stopwatch:
    """elapsed_ms= 的统一来源。"""

    def __init__(self) -> None:
        self._start = time.monotonic()

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._start) * 1000)
