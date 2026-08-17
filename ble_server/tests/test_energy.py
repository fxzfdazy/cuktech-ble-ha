"""Tests for energy tracker."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from energy import AdaptiveEnergyIntegrator, PortEnergyState, PowerThresholdDetector


def test_basic_accumulation():
    """20V * 1A for 30s = 0.167Wh."""
    integ = AdaptiveEnergyIntegrator()
    state = PortEnergyState()
    integ.update(state, 20.0, 1.0, 0.0)
    integ.update(state, 20.0, 1.0, 30.0)
    expected = 20.0 * 30 / 3600
    assert abs(state.total_wh - expected) < 0.01, f"Expected ~{expected:.4f}Wh, got {state.total_wh}"
    print("PASS: test_basic_accumulation")


def test_zero_power():
    """Zero current = zero energy."""
    integ = AdaptiveEnergyIntegrator()
    state = PortEnergyState()
    integ.update(state, 20.0, 0.0, 0.0)
    integ.update(state, 20.0, 0.0, 10.0)
    assert state.total_wh == 0.0, f"Expected 0Wh, got {state.total_wh}"
    print("PASS: test_zero_power")


def test_irregular_interval_skipped():
    """Gap > 30s should be skipped."""
    integ = AdaptiveEnergyIntegrator()
    state = PortEnergyState()
    integ.update(state, 20.0, 1.0, 0.0)
    integ.update(state, 20.0, 1.0, 50.0)
    assert state.total_wh == 0.0, f"Expected 0Wh after skip, got {state.total_wh}"
    print("PASS: test_irregular_interval_skipped")


def test_overshoot_protection():
    """10x power spike should be capped."""
    integ = AdaptiveEnergyIntegrator()
    state = PortEnergyState()
    integ.update(state, 20.0, 1.0, 0.0)
    integ.update(state, 200.0, 1.0, 1.0)
    assert state.total_wh < 200, f"Expected capped, got {state.total_wh}"
    print("PASS: test_overshoot_protection")


def test_multiple_accumulation():
    """10 points at 5s intervals = 45s total."""
    integ = AdaptiveEnergyIntegrator()
    state = PortEnergyState()
    for i in range(10):
        integ.update(state, 20.0, 1.0, i * 5.0)
    expected = 20.0 * 45 / 3600
    assert abs(state.total_wh - expected) < 0.01, f"Expected ~{expected:.4f}Wh, got {state.total_wh}"
    print("PASS: test_multiple_accumulation")


def test_power_threshold_disabled():
    """threshold=0 表示未启用阈值模式，should_end 始终返回 False。"""
    det = PowerThresholdDetector()
    base = 1000.0
    # 即使功率极低、时间任意推移，也不应触发
    assert det.should_end(0.0, 0, base) is False
    assert det.should_end(0.1, 0, base + 1000) is False
    # 负阈值同样视为未启用
    assert det.should_end(0.0, -1, base) is False
    print("PASS: test_power_threshold_disabled")


def test_power_threshold_triggers_after_30s():
    """功率低于阈值持续 30 秒后返回 True。"""
    det = PowerThresholdDetector()
    threshold = 2.0
    base = 1000.0
    # 首次低于阈值，尚未满 30 秒
    assert det.should_end(1.0, threshold, base) is False
    # 29 秒时仍未触发
    assert det.should_end(1.0, threshold, base + 29) is False
    # 满 30 秒触发
    assert det.should_end(1.0, threshold, base + 30) is True
    print("PASS: test_power_threshold_triggers_after_30s")


def test_power_threshold_custom_duration():
    """构造参数自定义持续时长后按该时长触发。"""
    det = PowerThresholdDetector(duration_sec=10)
    threshold = 2.0
    base = 1000.0
    assert det.should_end(1.0, threshold, base) is False
    assert det.should_end(1.0, threshold, base + 9) is False
    assert det.should_end(1.0, threshold, base + 10) is True
    # 运行中改时长同样生效
    det2 = PowerThresholdDetector()
    det2.duration_sec = 5
    det2.should_end(1.0, threshold, base)
    assert det2.should_end(1.0, threshold, base + 5) is True
    print("PASS: test_power_threshold_custom_duration")


def test_power_threshold_resets_on_recovery():
    """功率恢复后 _low_since 重置，再次低于阈值需重新计满 30 秒。"""
    det = PowerThresholdDetector()
    threshold = 2.0
    base = 1000.0
    # 低于阈值累计 20 秒
    assert det.should_end(1.0, threshold, base) is False
    assert det.should_end(1.0, threshold, base + 20) is False
    # 功率恢复到阈值以上
    assert det.should_end(5.0, threshold, base + 21) is False
    # 再次低于阈值，计时从头开始
    assert det.should_end(1.0, threshold, base + 22) is False
    assert det.should_end(1.0, threshold, base + 22 + 29) is False
    # 重新满 30 秒才触发
    assert det.should_end(1.0, threshold, base + 22 + 30) is True
    print("PASS: test_power_threshold_resets_on_recovery")


def test_power_threshold_reset_method():
    """reset() 后内部状态清空，需重新累计 30 秒才会触发。"""
    det = PowerThresholdDetector()
    threshold = 2.0
    base = 1000.0
    det.should_end(1.0, threshold, base)
    det.should_end(1.0, threshold, base + 29)
    assert det._low_since is not None
    det.reset()
    assert det._low_since is None
    # 重置后即便时间大幅推移，首次低于阈值仍不触发
    assert det.should_end(1.0, threshold, base + 100) is False
    # 重新满 30 秒才触发
    assert det.should_end(1.0, threshold, base + 100 + 30) is True
    print("PASS: test_power_threshold_reset_method")


if __name__ == "__main__":
    test_basic_accumulation()
    test_zero_power()
    test_irregular_interval_skipped()
    test_overshoot_protection()
    test_multiple_accumulation()
    test_power_threshold_disabled()
    test_power_threshold_triggers_after_30s()
    test_power_threshold_resets_on_recovery()
    test_power_threshold_reset_method()
    print("\nAll tests passed!")
