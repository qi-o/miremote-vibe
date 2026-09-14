"""手势引擎冒烟测试(假定时器确定性驱动)+ 配置迁移 + 语义动作解析。

只跑纯逻辑,不启动守护、不弹 UAC、不注入按键。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miremote.gestures import GestureDispatcher, Trigger

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name)


class FakeClock:
    """假定时器工厂:按时间片推进,确定性驱动 dispatcher。"""

    def __init__(self):
        self.now = 0.0
        self.timers = []  # (fire_at, seq, cb)

    def factory(self):
        seq = [0]

        def make(delay, cb):
            seq[0] += 1
            self.timers.append((self.now + delay, seq[0], cb))
            self.timers.sort(key=lambda t: (t[0], t[1]))

            class T:
                def start(self_):
                    pass

                def cancel(self_):
                    pass

            return T()

        return make

    def advance(self, seconds):
        end = self.now + seconds
        while self.timers and self.timers[0][0] <= end:
            at, _, cb = self.timers.pop(0)
            self.now = at
            cb()
        self.now = end


def make_dispatcher(slots, repeatable=(), events=None, clock=None):
    return GestureDispatcher(
        is_action_configured=lambda b, t: (b, t.value if hasattr(t, "value") else t) in slots,
        is_repeatable=lambda b: b in repeatable,
        on_trigger=lambda b, t: events.append((b, t.value)),
        timer_factory=clock.factory(),
    )


# 1) 只有单击:按下立即触发(零延迟)
clock = FakeClock()
ev = []
d = make_dispatcher({("K1", "click")}, events=ev, clock=clock)
d.press("K1")
check("immediate-click-on-down", ev == [("K1", "click")])
d.release("K1")
check("immediate-no-extra-on-up", ev == [("K1", "click")])

# 2) 单击+双击:松手后延迟 300ms 才判单击
clock = FakeClock()
ev = []
d = make_dispatcher({("K2", "click"), ("K2", "double_click")}, events=ev, clock=clock)
d.press("K2"); d.release("K2")
check("double-armed-no-immediate", ev == [])
clock.advance(0.31)
check("double-timeout-fires-click", ev == [("K2", "click")])

# 3) 双击:两次快速按压
clock = FakeClock()
ev = []
d = make_dispatcher({("K3", "click"), ("K3", "double_click")}, events=ev, clock=clock)
d.press("K3"); d.release("K3")
clock.advance(0.10)
d.press("K3"); d.release("K3")
check("double-fires", ev == [("K3", "double_click")])

# 4) 长按:按住 550ms 触发长按,抑制单击;松手不补发
clock = FakeClock()
ev = []
d = make_dispatcher({("K4", "click"), ("K4", "long_press")}, events=ev, clock=clock)
d.press("K4")
clock.advance(0.56)
check("long-fires-at-550ms", ev == [("K4", "long_press")])
d.release("K4")
check("long-suppresses-click", ev == [("K4", "long_press")])

# 5) 长按窗口内松手 = 单击(无双击配置时单击立即)
clock = FakeClock()
ev = []
d = make_dispatcher({("K5", "click"), ("K5", "long_press")}, events=ev, clock=clock)
d.press("K5")
clock.advance(0.20)
d.release("K5")
check("short-press-with-long-config-fires-click", ev == [("K5", "click")])

# 6) 按住重复:repeatable 键 350ms 后开始,每 100ms 一次
clock = FakeClock()
ev = []
d = make_dispatcher({("K6", "click")}, repeatable={"K6"}, events=ev, clock=clock)
d.press("K6")
clock.advance(0.34)
check("repeat-delay-quiet", len(ev) == 1)
clock.advance(0.01)
check("repeat-delay-then-fire", len(ev) == 2)
clock.advance(0.10)
check("repeat-interval", len(ev) == 3)
d.release("K6")
clock.advance(0.30)
check("repeat-stops-on-release", len(ev) == 3)

# 7) 每键独立:K7 长按等待中,K8 立即单击不受影响
clock = FakeClock()
ev = []
d = make_dispatcher(
    {("K7", "click"), ("K7", "long_press"), ("K8", "click")}, events=ev, clock=clock
)
d.press("K7")
d.press("K8")
check("per-key-independence", ev == [("K8", "click")])
d.release("K8")
d.release("K7")
check("per-key-independence-2", ev == [("K8", "click"), ("K7", "click")])

# 8) 配置迁移:老配置 on_down -> 单击槽
from miremote.service import key_slots, key_summary

old_style = {"label": "主页", "on_down": {"type": "tap", "key": "VK_RETURN"}}
new_style = {
    "label": "返回",
    "click": {"type": "tap", "key": "VK_BACK"},
    "double_click": {"type": "none"},
    "long_press": {"type": "keys", "combo": ["VK_CONTROL", "VK_Z"]},
}
s1 = key_slots(old_style)
check("migrate-on_down-to-click", s1["click"] == {"type": "tap", "key": "VK_RETURN"})
check("migrate-defaults-none", s1["double_click"]["type"] == "none")
s2 = key_slots(new_style)
check("v2-slots-preserved", s2["long_press"].get("combo") == ["VK_CONTROL", "VK_Z"])
check("key_summary-text", "长按=VK_CONTROL+VK_Z" in key_summary(new_style))

# 9) 语义动作:App Paths 解析(不启动、不聚焦)
from miremote import actions

weixin = actions._resolve_app_path("Weixin.exe") or actions._resolve_app_path("WeChat.exe")
print("  WeChat App Paths ->", weixin)
check("app-path-resolution-or-none-ok", weixin is None or weixin.lower().endswith(".exe"))

# 10) 按住连发:tap 类动作自动可重复;显式 repeat 配置可开关;节律可调
from miremote.service import MiRemoteService

svc = MiRemoteService.__new__(MiRemoteService)
svc.config = {"keys": {
    "TAP_BACK": {"on_down": {"type": "tap", "key": "VK_BACK"}},          # tap -> 自动连发
    "VK_F5": {"on_down": {"type": "voice"}},                              # 语音 -> 永不
    "VK_X": {"click": {"type": "keys", "combo": ["VK_CONTROL", "VK_C"]}},  # 组合键 -> 默认不连发
    "VK_Y": {"click": {"type": "tap", "key": "VK_A"}, "repeat": False},    # 显式关
    "VK_Z": {"click": {"type": "keys", "combo": ["VK_CONTROL", "VK_Z"]},
             "repeat": True, "repeat_interval": 50, "repeat_delay": 200},  # 显式开+节律
}}
check("repeat-tap-auto", svc._slot_repeatable("TAP_BACK"))
check("repeat-voice-never", not svc._slot_repeatable("VK_F5"))
check("repeat-keys-default-off", not svc._slot_repeatable("VK_X"))
check("repeat-explicit-off-overrides", not svc._slot_repeatable("VK_Y"))
check("repeat-explicit-on", svc._slot_repeatable("VK_Z"))
check("repeat-timing-custom", svc._repeat_timing("VK_Z") == (0.2, 0.05))
check("repeat-timing-default", svc._repeat_timing("TAP_BACK") == (0.35, 0.1))

# 11) 引擎层:自定义节律生效(200ms 首延迟 + 50ms 间隔)
clock = FakeClock()
ev = []
d = GestureDispatcher(
    is_action_configured=lambda b, t: b == "K10" and t.value == "click",
    is_repeatable=lambda b: True,
    on_trigger=lambda b, t: ev.append(t.value),
    repeat_timing_of=lambda b: (0.2, 0.05),
    timer_factory=clock.factory(),
)
d.press("K10")
check("custom-timing-immediate", len(ev) == 1)
clock.advance(0.19)
check("custom-timing-first-delay", len(ev) == 1)
clock.advance(0.01)
check("custom-timing-first-fire", len(ev) == 2)
clock.advance(0.05)
check("custom-timing-interval", len(ev) == 3)
d.release("K10")
clock.advance(0.2)
check("custom-timing-stops", len(ev) == 3)

# 12) 长按连发:长按到点后,按住期间按节律持续触发;松手停止,无单击补发
clock = FakeClock()
ev = []
d = GestureDispatcher(
    is_action_configured=lambda b, t: b == "K11" and t.value in ("click", "long_press"),
    is_repeatable=lambda b: True,
    on_trigger=lambda b, t: ev.append(t.value),
    repeat_timing_of=lambda b: (0.35, 0.05),
    timer_factory=clock.factory(),
)
d.press("K11")
clock.advance(0.56)
check("long-repeat-first", ev == ["long_press"])
clock.advance(0.05)
check("long-repeat-second", ev == ["long_press", "long_press"])
clock.advance(0.10)   # 0.61 -> 0.71:0.65 和 0.70 两个节拍都到期
check("long-repeat-continued", ev == ["long_press"] * 4)
d.release("K11")
clock.advance(0.5)
check("long-repeat-stops-no-click", ev == ["long_press"] * 4)

# 13) 长按不连发(repeatable=False):只触发一次
clock = FakeClock()
ev = []
d = GestureDispatcher(
    is_action_configured=lambda b, t: b == "K12" and t.value in ("click", "long_press"),
    is_repeatable=lambda b: False,
    on_trigger=lambda b, t: ev.append(t.value),
    timer_factory=clock.factory(),
)
d.press("K12")
clock.advance(0.56)
clock.advance(0.2)
check("long-single-when-not-repeatable", ev == ["long_press"])
d.release("K12")
clock.advance(0.4)
check("long-single-no-click", ev == ["long_press"])

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
