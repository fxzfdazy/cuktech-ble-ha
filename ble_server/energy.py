"""CUKTECH BLE Server - Energy accumulation with adaptive integration."""
from dataclasses import dataclass
from typing import Optional


@dataclass
class PortEnergyState:
    """Per-port energy tracking state."""
    total_wh: float = 0.0
    session_wh: float = 0.0
    is_charging: bool = False
    session_start: Optional[float] = None
    last_power: float = 0.0
    last_time: Optional[float] = None
    max_power: float = 0.0
    max_current: float = 0.0
    last_end_time: float = 0.0


class AdaptiveEnergyIntegrator:
    """Trapezoidal integration for charger output energy.

    Trapezoidal integration is used for all intervals — the accuracy
    difference vs Simpson at 1s BLE push intervals is <0.1%, while
    trapezoidal is simpler and avoids double-counting issues with
    overlapping Simpson windows on irregular data.
    """

    MAX_GAP_SEC = 30.0

    def update(self, state: PortEnergyState, voltage: float, current: float,
               timestamp: float) -> float:
        """Update energy state with new measurement. Returns total_wh."""
        power = voltage * current

        if state.last_time is None:
            state.last_time = timestamp
            state.last_power = power
            return state.total_wh

        dt = timestamp - state.last_time

        # Skip irregular intervals (disconnection, pause, time rollback)
        if dt <= 0 or dt > self.MAX_GAP_SEC:
            state.last_time = timestamp
            state.last_power = power
            return state.total_wh

        dt_hours = dt / 3600.0

        # Trapezoidal integration
        energy = (state.last_power + power) / 2.0 * dt_hours

        state.total_wh += energy
        state.session_wh += energy
        state.last_power = power
        state.last_time = timestamp
        if power > state.max_power:
            state.max_power = power
        if current > state.max_current:
            state.max_current = current

        return state.total_wh


class PowerThresholdDetector:
    """功率阈值检测器：判断功率低于阈值是否持续足够久。

    无滑动窗口，仅维护一个时间戳，CPU 开销极低。
    仅在配置了 end_power_w > 0 时由 ble_manager 启用。
    """

    DEFAULT_DURATION_SEC = 30  # 默认持续时长，可被构造参数覆盖

    def __init__(self, duration_sec: int = DEFAULT_DURATION_SEC):
        self.duration_sec = duration_sec  # 功率低于阈值持续该秒数后判定结束
        self._low_since = None  # 记录功率首次低于阈值的时间戳

    def should_end(self, power: float, threshold: float, timestamp: float) -> bool:
        """判断是否应结束会话（功率 < threshold 持续 duration_sec 秒）。

        threshold 为 0 时直接返回 False（未启用阈值模式）。
        """
        if threshold <= 0:
            return False
        if power < threshold:
            if self._low_since is None:
                self._low_since = timestamp
            return (timestamp - self._low_since) >= self.duration_sec
        else:
            self._low_since = None
            return False

    def reset(self):
        """会话开始/结束时重置状态。"""
        self._low_since = None
