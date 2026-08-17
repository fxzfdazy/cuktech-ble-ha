"""Tests for ble_manager.py - 长时供电会话结转兜底."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from ble_manager import session_lifetime_overdue, SESSION_MAX_LIFETIME_SEC


def _ts_at_hour(hour, minute=0, day_offset=0):
    """构造本地时间 day_offset 天前 hour:minute 的时间戳。"""
    base = datetime.now().replace(hour=hour, minute=minute,
                                  second=0, microsecond=0)
    return (base + timedelta(days=day_offset)).timestamp()


class TestSessionLifetimeOverdue:

    def test_no_session_start(self):
        """无开始时间不触发。"""
        assert session_lifetime_overdue(None, _ts_at_hour(3)) is False
        assert session_lifetime_overdue(0, _ts_at_hour(3)) is False

    def test_under_three_days_never_triggers(self):
        """不足 3 天即使处于凌晨窗口也不触发。"""
        now = _ts_at_hour(3)
        start = now - (SESSION_MAX_LIFETIME_SEC - 3600)
        assert session_lifetime_overdue(start, now) is False

    def test_over_three_days_in_window(self):
        """超过 3 天且处于凌晨 3/4 点窗口内触发。"""
        for hour in (3, 4):
            now = _ts_at_hour(hour, 30)
            start = now - SESSION_MAX_LIFETIME_SEC - 3600
            assert session_lifetime_overdue(start, now) is True

    def test_over_three_days_outside_window(self):
        """超过 3 天但不在凌晨窗口不触发（等待次日凌晨）。"""
        for hour in (5, 14, 23):
            now = _ts_at_hour(hour)
            start = now - SESSION_MAX_LIFETIME_SEC - 3600
            assert session_lifetime_overdue(start, now) is False

    def test_boundary_exactly_three_days(self):
        """恰好 3 天（含等于）且在窗口内触发。"""
        now = _ts_at_hour(3, 10)
        start = now - SESSION_MAX_LIFETIME_SEC
        assert session_lifetime_overdue(start, now) is True
