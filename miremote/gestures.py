"""三手势识别引擎(单击 / 双击 / 长按)——beta 测试版新增。

架构移植自 open-voice-bridge RC003 的 button_gesture.py(纯状态机 +
线程安全定时器派发器),按 miremote 的习惯简化。核心契约与 RC003 一致:

* 没配双击/长按的键:按下立即触发,零延迟(手感优先);
* 配了双击:单击延迟 300ms 等第二击,超时才算单击;
* 配了长按:按住 550ms 触发长按,松手不再补发单击;
* 每键独立状态,一个键的手势不会被另一个键打断;
* 按住重复仅在 is_repeatable() 放行时启用(立即路径才有重复;
  二次动作会让位给手势,与 RC003 一致)。

语音键(F5/按住说话)不进本引擎——service 层直接旁路,理由与 RC003
把麦克风键排除在外的理由完全相同:长按手势和"按住说话"天然冲突。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional, Set


class Trigger(str, Enum):
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    LONG_PRESS = "long_press"


class _Cmd(str, Enum):
    SCHEDULE_DOUBLE = "schedule_double"
    CANCEL_DOUBLE = "cancel_double"
    SCHEDULE_LONG = "schedule_long"
    CANCEL_LONG = "cancel_long"
    TRIGGER = "trigger"


@dataclass(frozen=True)
class GestureCommand:
    kind: _Cmd
    button: str
    trigger: Optional[Trigger] = None


@dataclass
class _State:
    is_pressed: bool = True
    is_second_press: bool = False
    waiting_second: bool = False
    long_fired: bool = False
    sees_double: bool = False
    sees_long: bool = False


class GestureRecognizer:
    """纯状态机,无定时器、无系统依赖,便于单元测试。"""

    def __init__(self) -> None:
        self._states: Dict[str, _State] = {}

    def is_tracking(self, button: str) -> bool:
        return button in self._states

    def press(self, button: str, *, sees_double: bool, sees_long: bool) -> List[GestureCommand]:
        st = self._states.get(button)
        if st is not None:
            if not st.waiting_second:
                return []          # 重复 down(自动重复/抖动):忽略
            st.is_pressed = True
            st.is_second_press = True
            st.waiting_second = False
            cmds = [GestureCommand(_Cmd.CANCEL_DOUBLE, button)]
            if st.sees_long:
                cmds.append(GestureCommand(_Cmd.SCHEDULE_LONG, button))
            return cmds

        self._states[button] = _State(sees_double=sees_double, sees_long=sees_long)
        if sees_long:
            return [GestureCommand(_Cmd.SCHEDULE_LONG, button)]
        return []

    def release(self, button: str) -> List[GestureCommand]:
        st = self._states.get(button)
        if st is None or not st.is_pressed:
            return []
        st.is_pressed = False
        cmds: List[GestureCommand] = []
        if st.sees_long:
            cmds.append(GestureCommand(_Cmd.CANCEL_LONG, button))
        if st.long_fired:
            self._states.pop(button, None)
            return cmds            # 长按已触发,松手不再补发
        if st.is_second_press:
            self._states.pop(button, None)
            cmds.append(GestureCommand(_Cmd.TRIGGER, button, Trigger.DOUBLE_CLICK))
            return cmds
        if st.sees_double:
            st.waiting_second = True
            cmds.append(GestureCommand(_Cmd.SCHEDULE_DOUBLE, button))
            return cmds            # 等 300ms 决定单击还是双击

        self._states.pop(button, None)
        cmds.append(GestureCommand(_Cmd.TRIGGER, button, Trigger.CLICK))
        return cmds

    def double_timed_out(self, button: str) -> List[GestureCommand]:
        st = self._states.get(button)
        if st is None or not st.waiting_second or st.is_pressed:
            return []
        self._states.pop(button, None)
        return [GestureCommand(_Cmd.TRIGGER, button, Trigger.CLICK)]

    def long_timed_out(self, button: str) -> List[GestureCommand]:
        """长按到点触发;按住期间允许反复触发(由派发器按节律重排定时器),
        实现像按住键盘退格键那样的连续重复。"""
        st = self._states.get(button)
        if st is None or not st.is_pressed or not st.sees_long:
            return []
        st.long_fired = True
        return [GestureCommand(_Cmd.TRIGGER, button, Trigger.LONG_PRESS)]

    def reset(self) -> None:
        self._states.clear()


TimerFactory = Callable[[float, Callable[[], None]], object]
ActionConfigured = Callable[[str, Trigger], bool]
Repeatable = Callable[[str], bool]
RepeatTiming = Callable[[str], tuple]
OnTrigger = Callable[[str, Trigger], None]


class GestureDispatcher:
    """Raw Input 线程( press/release )与定时器线程之间的线程安全适配层。

    回调:
      is_action_configured(button, trigger) -> bool  该手势槽配了动作吗
      is_repeatable(button)                 -> bool  按住重复许可
      on_trigger(button, trigger)           -> void  执行动作(引擎线程,勿阻塞)
    """

    DOUBLE_CLICK_SECONDS = 0.300
    LONG_PRESS_SECONDS = 0.550
    REPEAT_DELAY_SECONDS = 0.350
    REPEAT_INTERVAL_SECONDS = 0.100

    def __init__(
        self,
        *,
        is_action_configured: ActionConfigured,
        is_repeatable: Repeatable,
        on_trigger: OnTrigger,
        repeat_timing_of: Optional[RepeatTiming] = None,
        timer_factory: Optional[TimerFactory] = None,
    ) -> None:
        self._configured = is_action_configured
        self._repeatable = is_repeatable
        self._on_trigger = on_trigger
        # 可选:每键自定义连发 (首延迟秒, 间隔秒);缺省用类常量
        self._repeat_timing_of = repeat_timing_of
        self._timer_factory = timer_factory or (
            lambda delay, cb: threading.Timer(delay, cb)
        )
        self._lock = threading.RLock()
        self._rec = GestureRecognizer()
        self._double_timers: Dict[str, object] = {}
        self._long_timers: Dict[str, object] = {}
        self._repeat_timers: Dict[str, object] = {}
        self._immediate_held: Set[str] = set()

    # ---- 按键边沿(Raw Input 线程调用) ----
    def press(self, button: str) -> None:
        with self._lock:
            if button in self._immediate_held:
                return                     # 按住去重(旧 _held 语义)
            sees_double = self._configured(button, Trigger.DOUBLE_CLICK)
            sees_long = self._configured(button, Trigger.LONG_PRESS)
            if not sees_double and not sees_long and not self._rec.is_tracking(button):
                if not self._configured(button, Trigger.CLICK):
                    return                 # 整键无动作:纯透传
                self._immediate_held.add(button)
                self._fire(button, Trigger.CLICK)          # 立即路径:零延迟
                if self._repeatable(button):
                    delay, _interval = self._timing(button)
                    self._schedule_repeat(button, delay)
                return
            self._run(self._rec.press(button, sees_double=sees_double, sees_long=sees_long))

    def release(self, button: str) -> None:
        with self._lock:
            self._immediate_held.discard(button)
            self._cancel(self._repeat_timers, button)
            self._run(self._rec.release(button))

    def reset(self) -> None:
        with self._lock:
            for timers in (self._double_timers, self._long_timers, self._repeat_timers):
                for t in list(timers.values()):
                    self._cancel_timer(t)
                timers.clear()
            self._immediate_held.clear()
            self._rec.reset()

    # ---- 内部(持锁) ----
    def _run(self, cmds: List[GestureCommand]) -> None:
        for cmd in cmds:
            if cmd.kind is _Cmd.SCHEDULE_DOUBLE:
                self._schedule(cmd.button, self._double_timers,
                               self.DOUBLE_CLICK_SECONDS, self._double_timeout)
            elif cmd.kind is _Cmd.CANCEL_DOUBLE:
                self._cancel(self._double_timers, cmd.button)
            elif cmd.kind is _Cmd.SCHEDULE_LONG:
                self._schedule(cmd.button, self._long_timers,
                               self.LONG_PRESS_SECONDS, self._long_timeout)
            elif cmd.kind is _Cmd.CANCEL_LONG:
                self._cancel(self._long_timers, cmd.button)
            elif cmd.kind is _Cmd.TRIGGER and cmd.trigger is not None:
                self._fire(cmd.button, cmd.trigger)

    def _schedule(self, button: str, table: Dict[str, object],
                  delay: float, timeout) -> None:
        self._cancel(table, button)
        t = self._timer_factory(delay, lambda: timeout(button))
        table[button] = t
        t.start()

    def _schedule_repeat(self, button: str, delay: float) -> None:
        self._cancel(self._repeat_timers, button)
        t = self._timer_factory(delay, lambda: self._repeat_timeout(button))
        self._repeat_timers[button] = t
        t.start()

    def _double_timeout(self, button: str) -> None:
        with self._lock:
            self._double_timers.pop(button, None)
            self._run(self._rec.double_timed_out(button))

    def _long_timeout(self, button: str) -> None:
        with self._lock:
            self._long_timers.pop(button, None)
            self._run(self._rec.long_timed_out(button))
            # 长按连发:动作可重复且仍按住时,按节律继续触发
            if self._rec.is_tracking(button) and self._repeatable(button):
                _delay, interval = self._timing(button)
                self._schedule(button, self._long_timers, interval, self._long_timeout)

    def _timing(self, button: str) -> tuple:
        if self._repeat_timing_of is not None:
            try:
                delay, interval = self._repeat_timing_of(button)
                return (max(0.05, float(delay)), max(0.03, float(interval)))
            except Exception:
                pass
        return (self.REPEAT_DELAY_SECONDS, self.REPEAT_INTERVAL_SECONDS)

    def _repeat_timeout(self, button: str) -> None:
        with self._lock:
            if button not in self._immediate_held:
                self._repeat_timers.pop(button, None)
                return
            self._repeat_timers.pop(button, None)
            _delay, interval = self._timing(button)
            self._schedule_repeat(button, interval)
        self._on_trigger(button, Trigger.CLICK)    # 重复触发在锁外执行

    def _fire(self, button: str, trigger: Trigger) -> None:
        try:
            self._on_trigger(button, trigger)
        except Exception:
            pass

    @staticmethod
    def _cancel(table: Dict[str, object], button: str) -> None:
        t = table.pop(button, None)
        if t is not None:
            GestureDispatcher._cancel_timer(t)

    @staticmethod
    def _cancel_timer(t: object) -> None:
        cancel = getattr(t, "cancel", None)
        if cancel is not None:
            cancel()
