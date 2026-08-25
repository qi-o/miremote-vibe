"""进程启动器：只接受参数列表，禁止 shell 字符串；后台线程执行不阻塞按键循环。"""

from __future__ import annotations

import subprocess
import sys
import threading


def _spawn(argv: list[str]):
    try:
        subprocess.run(
            [sys.executable if a == "{python}" else a for a in argv],
            shell=False,
        )
    except FileNotFoundError as e:
        print("[runner] 找不到可执行文件:", e)


def launch(argv: list[str]):
    """以 shell=False 在后台线程启动外部程序。argv 必须是非空参数列表。"""
    if not isinstance(argv, (list, tuple)) or len(argv) == 0:
        raise ValueError("run 动作需要 argv 列表，例如 [\"notepad\"]")
    for a in argv:
        if not isinstance(a, str):
            raise ValueError("argv 中每一项必须是字符串")
    threading.Thread(target=_spawn, args=(list(argv),), daemon=True).start()
