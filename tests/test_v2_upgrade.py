"""v2.0 升级专项测试(SayAll 交叉研究吸收项的纯逻辑部分)。

覆盖:间隔注入(时序+回滚)、指数退避、滚轮/移动数值域、身份对冲查表。
不注入真实按键(_tap 全部 monkeypatch)。
"""

from __future__ import annotations

import time

import miremote.actions as actions
from miremote.reconnect import ReconnectBackoff
from miremote.service import _NATIVE_VK, _REPEAT_TABLE


class FakeTapRecorder:
    """记录每次 _tap 调用 (vk, up);可编排第 N 次调用失败。"""

    def __init__(self, fail_at: int | None = None):
        self.calls: list[tuple[int, bool]] = []
        self.fail_at = fail_at
        self.sleeps: list[float] = []

    def install(self, monkeypatch):
        recorder = self

        def fake_tap(vk, up=False):
            recorder.calls.append((vk, up))
            if recorder.fail_at is not None and len(recorder.calls) == recorder.fail_at:
                return False
            return True

        def fake_sleep(seconds):
            recorder.sleeps.append(seconds)

        monkeypatch.setattr(actions, "_tap", fake_tap)
        monkeypatch.setattr(time, "sleep", fake_sleep)


def test_spaced_chord_down_orders_keys_with_gap(monkeypatch):
    recorder = FakeTapRecorder()
    recorder.install(monkeypatch)

    ok = actions.hold_chord_spaced(
        ["VK_LCONTROL", "VK_LWIN", "VK_H"], gap_ms=80.0, down=True
    )

    assert ok
    ctrl, win, h = (
        actions.name_to_vk("VK_LCONTROL"),
        actions.name_to_vk("VK_LWIN"),
        actions.name_to_vk("VK_H"),
    )
    assert recorder.calls == [(ctrl, False), (win, False), (h, False)]
    # 两个间隔各 80ms,首键前无间隔。
    assert recorder.sleeps == [0.08, 0.08]


def test_spaced_chord_up_reverses_order(monkeypatch):
    recorder = FakeTapRecorder()
    recorder.install(monkeypatch)

    assert actions.hold_chord_spaced(
        ["VK_LCONTROL", "VK_LWIN"], gap_ms=80.0, down=False
    )

    ctrl, win = actions.name_to_vk("VK_LCONTROL"), actions.name_to_vk("VK_LWIN")
    # 松开:先 LWin↑ 再 LCtrl↑(反序),同样带间隔。
    assert recorder.calls == [(win, True), (ctrl, True)]


def test_spaced_chord_rolls_back_on_partial_failure(monkeypatch):
    # 第 2 键(共 3 键)发送失败:已送达的第 1 键必须被反向释放,防粘键。
    recorder = FakeTapRecorder(fail_at=2)
    recorder.install(monkeypatch)

    ok = actions.hold_chord_spaced(
        ["VK_LCONTROL", "VK_LWIN", "VK_H"], gap_ms=0.0, down=True
    )

    assert not ok
    ctrl = actions.name_to_vk("VK_LCONTROL")
    assert recorder.calls[0] == (ctrl, False)
    assert recorder.calls[-1] == (ctrl, True), "失败后必须回滚已按下的键"


def test_reconnect_backoff_doubles_and_caps_then_resets():
    backoff = ReconnectBackoff(base_delay=2.0, max_delay=30.0)
    assert backoff.schedule_next() == (1, 2.0)
    assert backoff.schedule_next() == (2, 4.0)
    assert backoff.schedule_next() == (3, 8.0)
    assert backoff.schedule_next() == (4, 16.0)
    assert backoff.schedule_next() == (5, 30.0)
    assert backoff.schedule_next() == (6, 30.0), "封顶后不再增长"
    backoff.reset()
    assert backoff.schedule_next() == (1, 2.0), "成功后归零重来"


def test_mouse_amount_validation_domain():
    assert actions.validate_amount(1, 100) == 1
    assert actions.validate_amount(100, 100) == 100
    for bad in (0, -1, 101):
        try:
            actions.validate_amount(bad, 100)
        except ValueError:
            continue
        raise AssertionError(f"{bad} 应被拒绝")


def test_repeat_table_matches_mac_cadence():
    assert _REPEAT_TABLE["TAP_BACK"] == 50
    for arrow in ("VK_UP", "VK_DOWN", "VK_LEFT", "VK_RIGHT"):
        assert _REPEAT_TABLE[arrow] == 100
    assert _REPEAT_TABLE["TAP_VOLUME_UP"] == 100
    assert "VK_HOME" not in _REPEAT_TABLE, "一次性语义键默认不连发"


def test_native_vk_table_covers_identity_offset_cases():
    for arrow in ("VK_UP", "VK_DOWN", "VK_LEFT", "VK_RIGHT"):
        assert _NATIVE_VK[arrow] == arrow
    assert _NATIVE_VK["VK_RETURN"] == "VK_RETURN"
    # 无原生形态的键(哑键/语音/电源)不做对冲。
    assert _NATIVE_VK["TAP_BACK"] is None
    assert _NATIVE_VK["VK_F5"] is None
