from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from miremote.gestures import GestureDispatcher, Trigger


class CapturedTimer:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.callback()


class CapturingTimers:
    def __init__(self):
        self.timers = []

    def factory(self, delay, callback):
        timer = CapturedTimer(delay, callback)
        self.timers.append(timer)
        return timer


def test_stale_double_timer_callback_does_not_finish_new_gesture_after_reset():
    timers = CapturingTimers()
    events = []
    dispatcher = GestureDispatcher(
        is_action_configured=lambda b, t: b == "K" and t in {Trigger.CLICK, Trigger.DOUBLE_CLICK},
        is_repeatable=lambda b: False,
        on_trigger=lambda b, t: events.append((b, t)),
        timer_factory=timers.factory,
    )

    dispatcher.press("K")
    dispatcher.release("K")
    stale_double = timers.timers[-1]
    dispatcher.reset()

    dispatcher.press("K")
    dispatcher.release("K")
    current_double = timers.timers[-1]
    stale_double.fire()

    assert events == []
    assert current_double.cancelled is False

    current_double.fire()
    assert events == [("K", Trigger.CLICK)]


def test_stale_long_timer_callback_does_not_fire_on_new_press_after_cancel():
    timers = CapturingTimers()
    events = []
    dispatcher = GestureDispatcher(
        is_action_configured=lambda b, t: b == "K" and t in {Trigger.CLICK, Trigger.LONG_PRESS},
        is_repeatable=lambda b: False,
        on_trigger=lambda b, t: events.append((b, t)),
        timer_factory=timers.factory,
    )

    dispatcher.press("K")
    stale_long = timers.timers[-1]
    dispatcher.release("K")
    assert events == [("K", Trigger.CLICK)]
    events.clear()

    dispatcher.press("K")
    current_long = timers.timers[-1]
    stale_long.fire()

    assert events == []
    assert current_long.cancelled is False

    current_long.fire()
    assert events == [("K", Trigger.LONG_PRESS)]


def test_stale_repeat_timer_callback_does_not_repeat_new_immediate_hold():
    timers = CapturingTimers()
    events = []
    dispatcher = GestureDispatcher(
        is_action_configured=lambda b, t: b == "K" and t is Trigger.CLICK,
        is_repeatable=lambda b: True,
        on_trigger=lambda b, t: events.append((b, t)),
        timer_factory=timers.factory,
    )

    dispatcher.press("K")
    stale_repeat = timers.timers[-1]
    dispatcher.reset()
    events.clear()

    dispatcher.press("K")
    current_repeat = timers.timers[-1]
    assert events == [("K", Trigger.CLICK)]
    stale_repeat.fire()

    assert events == [("K", Trigger.CLICK)]
    assert current_repeat.cancelled is False

    current_repeat.fire()
    assert events == [("K", Trigger.CLICK), ("K", Trigger.CLICK)]
