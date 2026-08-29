"""按住遥控器语音键期间，延迟注入 Ctrl+Win，测 WeType 防误触是时效型还是状态型。

请配合：看到提示后按住遥控器【语音键】约 5 秒不要松手。
  F5 down 后 2.5 秒注入 Ctrl+Win（按住 1.5 秒）→ 检测"语音输入"面板出现与否。
  弹 = 时效型保护（延迟注入方案成立）；不弹 = 状态型（面板只能松手后开）。

用法（先退出小米遥控器.exe）: python tools/hold_probe2.py
"""

import ctypes
import sys
import time
from ctypes import wintypes as wt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from miremote.rawinput import RawInputEngine  # noqa: E402
from miremote import actions  # noqa: E402
from miremote.keys import name_to_vk  # noqa: E402
from hotkey_combo_probe import snapshot  # noqa: E402

VK_F5 = 0x74


def main() -> int:
    got = {}

    def on_key(ev):
        if ev.vkey == VK_F5 and ev.is_remote:
            got.setdefault("down_at", time.perf_counter())

    eng = RawInputEngine(vid="2717", pid="32B8")
    eng.on_key = on_key
    eng.start_background()

    print("=" * 60)
    print("请现在按住遥控器【语音键】约 5 秒不要松手（90 秒内有效）")
    print("=" * 60)
    deadline = time.time() + 90
    while "down_at" not in got and time.time() < deadline:
        time.sleep(0.05)
    if "down_at" not in got:
        print("没等到遥控器 F5（没按/没连上）")
        return 1
    print(f"F5 down 已捕获，按住期间保持别松… 2.5 秒后注入热键")

    time.sleep(2.5)                     # 关键变量：硬件 F5 已按住 2.5 秒
    base = snapshot()
    C, W = name_to_vk("VK_CONTROL"), name_to_vk("VK_LWIN")
    actions._tap(C)
    actions._tap(W)
    time.sleep(1.5)
    during = snapshot()
    actions._tap(W, up=True)
    actions._tap(C, up=True)
    time.sleep(1.0)
    after = snapshot()
    new = [h for h in during if h not in base]
    eng.stop()

    print()
    if new:
        titles = [during[h] for h in new]
        print(f"面板出现了 {len(new)} 个新窗口: {titles}")
        print("==> 时效型保护：按住 2.5s 后热键就能触发 → 延迟注入方案成立！")
    else:
        print("面板没出现")
        print("==> 状态型保护：F5 按住期间一直拒绝 → 面板只能松手后开（v2 极限）")
    print("（现在可以松开语音键了）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
