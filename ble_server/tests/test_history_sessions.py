"""Tests for history.py - Charge session management."""
import time
from unittest.mock import patch
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


class TestChargeSessions:
    """Test charge session CRUD operations."""

    def test_start_session(self, history):
        """Starting a session returns a valid session_id."""
        sid = history.start_session(1, protocol="PD")
        assert sid > 0, f"Expected valid session_id, got {sid}"

    def test_start_session_returns_incrementing_ids(self, history):
        """Consecutive session starts return different IDs."""
        sid1 = history.start_session(1)
        sid2 = history.start_session(2)
        assert sid2 > sid1

    def test_start_session_stores_port_and_protocol(self, history):
        """Session metadata is stored correctly after ending."""
        sid = history.start_session(2, protocol="QC")
        history.end_session(sid, 1.0, 50.0, 20.0, 1.0, 600)
        sessions, _ = history.get_sessions(2, 5)
        match = [s for s in sessions if s["id"] == sid]
        assert len(match) == 1
        assert match[0]["port"] == 2
        assert match[0]["protocol"] == "QC"
        assert match[0]["start_time"] > 0

    def test_record_charge_point(self, history):
        """Recording points makes them queryable."""
        sid = history.start_session(1)
        history.record_charge_point(sid, 20.0, 2.5, 50.0, "PD")
        # 清除节流记录，确保下一条点能立即写入
        history.clear_point_throttle(sid)
        history.record_charge_point(sid, 20.1, 2.5, 50.25, "PD")
        points = history.get_session_points(sid)
        assert len(points) == 2
        assert points[0]["voltage"] == 20.0
        assert points[1]["voltage"] == 20.1

    def test_record_charge_point_no_session_id(self, history):
        """record_charge_point with session_id=0 is silently ignored."""
        history.record_charge_point(0, 20.0, 2.5, 50.0, "PD")
        # No error expected

    def test_end_session_updates_stats(self, history):
        """Ending a session correctly stores stats."""
        sid = history.start_session(1)
        history.record_charge_point(sid, 20.0, 2.5, 50.0, "PD")
        history.end_session(sid, total_wh=1.5, peak_power_w=50.0,
                            avg_voltage=20.0, avg_current=2.5, duration_sec=3600)
        sessions, _ = history.get_sessions(1, 5)
        match = [s for s in sessions if s["id"] == sid]
        assert len(match) == 1
        s = match[0]
        assert s["total_wh"] == 1.5
        assert s["peak_power_w"] == 50.0
        assert s["avg_voltage"] == 20.0
        assert s["avg_current"] == 2.5
        assert s["duration_sec"] == 3600
        assert s["end_time"] is not None

    def test_end_session_calculates_avg_power(self, history):
        """end_session calculates avg_power_w from total_wh and duration."""
        sid = history.start_session(1)
        history.end_session(sid, total_wh=20.0, peak_power_w=50.0,
                            avg_voltage=20.0, avg_current=2.5, duration_sec=3600)
        sessions, _ = history.get_sessions(1, 5)
        match = [s for s in sessions if s["id"] == sid]
        assert abs(match[0]["avg_power_w"] - 20.0) < 0.1

    def test_end_session_zero_duration(self, history):
        """end_session with duration=0 should set avg_power=0."""
        sid = history.start_session(1)
        history.end_session(sid, total_wh=10.0, peak_power_w=50.0,
                            avg_voltage=0, avg_current=0, duration_sec=0)
        sessions, _ = history.get_sessions(1, 5)
        match = [s for s in sessions if s["id"] == sid]
        assert match[0]["avg_power_w"] == 0

    def test_delete_session_removes_points(self, history):
        """Deleting a session removes both session and points."""
        sid = history.start_session(1)
        history.record_charge_point(sid, 20.0, 2.5, 50.0)
        history.delete_session(sid)
        points = history.get_session_points(sid)
        assert points == []
        sessions, _ = history.get_sessions(1, 5)
        match = [s for s in sessions if s["id"] == sid]
        assert len(match) == 0

    def test_clear_sessions_removes_all(self, history):
        """clear_sessions 清空全部端口会话及明细点，返回删除条数。"""
        s1 = history.start_session(1)
        s2 = history.start_session(2)
        for s in [s1, s2]:
            history.record_charge_point(s, 20.0, 2.5, 50.0, "PD")
            history.end_session(s, 1.0, 50.0, 20.0, 2.5, 600)
        deleted = history.clear_sessions()
        assert deleted == 2
        sessions_c1, total_c1 = history.get_sessions(1, 5)
        sessions_c2, total_c2 = history.get_sessions(2, 5)
        assert sessions_c1 == [] and total_c1 == 0
        assert sessions_c2 == [] and total_c2 == 0
        # 明细点也一并清理
        assert history.get_session_points(s1) == []
        assert history.get_session_points(s2) == []

    def test_clear_sessions_empty_db(self, history):
        """空库调用 clear_sessions 返回 0。"""
        assert history.clear_sessions() == 0

    def test_get_sessions_filters_by_port(self, history):
        """get_sessions returns only sessions for specified port."""
        s1 = history.start_session(1)
        s2 = history.start_session(2)
        s3 = history.start_session(1)
        for s in [s1, s2, s3]:
            history.end_session(s, 1.0, 30.0, 20.0, 1.0, 600)
        sessions_c1, _ = history.get_sessions(1, 5)
        sessions_c2, _ = history.get_sessions(2, 5)
        assert len(sessions_c1) == 2
        assert len(sessions_c2) == 1

    def test_get_sessions_respects_limit(self, history):
        """get_sessions 仅返回最近 limit 条会话。"""
        sids = []
        for _ in range(5):
            sid = history.start_session(1)
            history.end_session(sid, 1.0, 30.0, 20.0, 1.0, 600)
            sids.append(sid)
        # 用递增时间戳保证 start_time 互不相同，使倒序结果稳定
        sessions, total = history.get_sessions(1, 2)
        assert len(sessions) == 2
        assert total == 2

    def test_get_sessions_excludes_zero_wh(self, history):
        """Sessions with total_wh=0 are excluded from results."""
        sid = history.start_session(1)
        history.end_session(sid, total_wh=0, peak_power_w=0,
                            avg_voltage=0, avg_current=0, duration_sec=0)
        sessions, total = history.get_sessions(1, 5)
        assert len(sessions) == 0
        assert total == 0

    def test_get_sessions_default_limit(self, history):
        """limit 默认为 5，超出部分不返回。"""
        for _ in range(7):
            sid = history.start_session(1)
            history.end_session(sid, 1.0, 30.0, 20.0, 1.0, 600)
        sessions, total = history.get_sessions(1)
        assert len(sessions) == 5
        assert total == 5

    def test_connection_closed_safe(self, history):
        """Methods are safe to call after closing the connection."""
        history.close()
        assert history.start_session(1) == 0
        history.record_charge_point(1, 20.0, 2.5, 50.0)  # no error
        history.end_session(1, 1.0, 30.0, 20.0, 1.0, 600)
        history.delete_session(1)
        points = history.get_session_points(1)
        assert points == []


class TestPruneSessions:
    """prune_sessions 超额清理测试。"""

    def test_prune_sessions_keeps_5(self, history):
        """插入 7 条同口会话后 prune 保留最近 5 条，且旧会话明细点被清理。"""
        counter = [0]

        def fake_time():
            counter[0] += 1
            return 1000.0 + counter[0]

        sids = []
        with patch("history.time.time", side_effect=fake_time):
            for i in range(7):
                sid = history.start_session(1, protocol="PD")
                history.record_charge_point(sid, 20.0, 2.5, 50.0, "PD")
                history.clear_point_throttle(sid)
                history.record_charge_point(sid, 20.1, 2.5, 50.25, "PD")
                history.end_session(sid, 1.0 + i, 50.0, 20.0, 2.5, 600)
                sids.append(sid)
            history.prune_sessions(1, keep=5)

        sessions, total = history.get_sessions(1, 10)
        assert total == 5
        # 最旧的两条已被删除
        remaining_ids = [s["id"] for s in sessions]
        assert sids[0] not in remaining_ids
        assert sids[1] not in remaining_ids
        # 被删会话的明细点也一并清理
        assert history.get_session_points(sids[0]) == []
        assert history.get_session_points(sids[1]) == []

    def test_prune_sessions_other_port_unaffected(self, history):
        """prune C1 不影响 C2 的会话。"""
        counter = [0]

        def fake_time():
            counter[0] += 1
            return 2000.0 + counter[0]

        with patch("history.time.time", side_effect=fake_time):
            c1_sids = []
            for _ in range(6):
                sid = history.start_session(1)
                history.end_session(sid, 1.0, 30.0, 20.0, 1.0, 600)
                c1_sids.append(sid)
            c2_sids = []
            for _ in range(3):
                sid = history.start_session(2)
                history.end_session(sid, 1.0, 30.0, 20.0, 1.0, 600)
                c2_sids.append(sid)
            history.prune_sessions(1, keep=5)

        _, c1_total = history.get_sessions(1, 10)
        _, c2_total = history.get_sessions(2, 10)
        assert c1_total == 5
        assert c2_total == 3

    def test_prune_sessions_no_excess(self, history):
        """不足 keep 条时 prune 不删除任何会话。"""
        sid = history.start_session(1)
        history.end_session(sid, 1.0, 30.0, 20.0, 1.0, 600)
        history.prune_sessions(1, keep=5)
        sessions, total = history.get_sessions(1, 10)
        assert total == 1
        assert sessions[0]["id"] == sid


class TestChargePointThrottle:
    """charge_points 60 秒降采样测试。"""

    def test_record_charge_point_throttle(self, history):
        """同一会话连续两次写入间隔不足 60 秒，第二次被跳过；满 60 秒后才写入。"""
        sid = history.start_session(1)
        now = [1000.0]

        # 同一时刻连续两次写入，第二次被节流跳过
        with patch("history.time.time", side_effect=lambda: now[0]):
            history.record_charge_point(sid, 20.0, 2.5, 50.0, "PD")
            history.record_charge_point(sid, 20.1, 2.5, 50.25, "PD")
        points = history.get_session_points(sid)
        assert len(points) == 1

        # 推进到 61 秒后再次写入，应成功
        now[0] = 1061.0
        with patch("history.time.time", side_effect=lambda: now[0]):
            history.record_charge_point(sid, 20.2, 2.5, 50.5, "PD")
        points = history.get_session_points(sid)
        assert len(points) == 2

    def test_clear_point_throttle_allows_immediate_write(self, history):
        """clear_point_throttle 清除节流后可立即写入下一条点。"""
        sid = history.start_session(1)
        now = [1000.0]
        with patch("history.time.time", side_effect=lambda: now[0]):
            history.record_charge_point(sid, 20.0, 2.5, 50.0, "PD")
            # 不清除节流时，紧接的写入会被跳过
            history.record_charge_point(sid, 20.1, 2.5, 50.25, "PD")
            assert len(history.get_session_points(sid)) == 1
            # 清除节流后立即可写
            history.clear_point_throttle(sid)
            history.record_charge_point(sid, 20.2, 2.5, 50.5, "PD")
        points = history.get_session_points(sid)
        assert len(points) == 2
