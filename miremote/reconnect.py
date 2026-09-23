"""指数退避重连调度(v2.0,移植自 SayAll reconnect.rs 的纯逻辑)。

2s 起步、每次 ×2、封顶;成功重置。无 IO、无时间依赖(时间由调用方推进),
便于确定性单测。
"""

from __future__ import annotations


class ReconnectBackoff:
    def __init__(self, base_delay: float = 2.0, max_delay: float = 30.0):
        self.base_delay = min(base_delay, max_delay)
        self.max_delay = max_delay
        self._next_delay = self.base_delay
        self._attempt = 0

    def reset(self) -> None:
        self._next_delay = self.base_delay
        self._attempt = 0

    def schedule_next(self) -> tuple[int, float]:
        """返回 (第几次尝试, 本次应等待的秒数);连续失败逐次翻倍封顶。"""
        self._attempt += 1
        delay = self._next_delay
        self._next_delay = min(self._next_delay * 2, self.max_delay)
        return self._attempt, delay

    @property
    def attempt(self) -> int:
        return self._attempt
