"""Tests for ha_server.py - Session API endpoints."""
import asyncio
import json
import pytest
import sys
import os
import time
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from history import PortHistory


@pytest.fixture
def history_with_sessions():
    """Create a PortHistory with some charge sessions."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    h = PortHistory(db_path=db_path, retention_days=2)
    h.connect()
    # 两条会话：C1 一条（带两个明细点）、C2 一条
    s1 = h.start_session(1, protocol="PD")
    s2 = h.start_session(2, protocol="QC")
    h.record_charge_point(s1, 20.0, 2.5, 50.0, "PD")
    # 清除节流，确保第二条点能立即写入
    h.clear_point_throttle(s1)
    h.record_charge_point(s1, 20.1, 2.5, 50.25, "PD")
    h.record_charge_point(s2, 10.0, 1.0, 10.0, "QC")
    h.end_session(s1, 1.5, 50.25, 20.05, 2.5, 1800)
    h.end_session(s2, 0.5, 10.0, 10.0, 1.0, 600)
    yield h
    h.close()
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
def server_with_sessions(history_with_sessions):
    """Create a Server instance with session history and mocked BLE."""
    from ha_server import Server, reset_server
    reset_server()
    s = Server.__new__(Server)
    s.history = history_with_sessions
    s.ble = MagicMock()
    s.ble.get_live_session_data = MagicMock(return_value={})
    s.ble.state = MagicMock()
    s.ble.state.ports = {}
    s.config = MagicMock()
    s.bemfa = None
    return s


class TestHandleSessions:
    """Test GET /api/sessions?port=<c1|c2|c3|a>&limit=5。"""

    @pytest.mark.asyncio
    async def test_sessions_requires_port(self, server_with_sessions):
        """缺少 port 参数返回 400。"""
        request = AsyncMock()
        request.query = {}
        result = await server_with_sessions.handle_sessions(request)
        assert result.status == 400

    @pytest.mark.asyncio
    async def test_sessions_invalid_port(self, server_with_sessions):
        """非法 port 返回 400。"""
        request = AsyncMock()
        request.query = {"port": "c9"}
        result = await server_with_sessions.handle_sessions(request)
        assert result.status == 400

    @pytest.mark.asyncio
    async def test_sessions_returns_list(self, server_with_sessions):
        """返回会话列表和总数。"""
        request = AsyncMock()
        request.query = {"port": "c1"}
        result = await server_with_sessions.handle_sessions(request)
        body = json.loads(result.body)
        assert "sessions" in body
        assert "total" in body

    @pytest.mark.asyncio
    async def test_sessions_port_filter(self, server_with_sessions):
        """按端口过滤，只返回该口会话。"""
        request = AsyncMock()
        request.query = {"port": "c1"}
        result = await server_with_sessions.handle_sessions(request)
        body = json.loads(result.body)
        assert body["total"] == 1
        for s in body["sessions"]:
            assert s["port"] == 1

    @pytest.mark.asyncio
    async def test_sessions_count_per_port(self, server_with_sessions):
        """C1 与 C2 各自返回对应数量的会话。"""
        req_c1 = AsyncMock()
        req_c1.query = {"port": "c1"}
        body_c1 = json.loads((await server_with_sessions.handle_sessions(req_c1)).body)
        req_c2 = AsyncMock()
        req_c2.query = {"port": "c2"}
        body_c2 = json.loads((await server_with_sessions.handle_sessions(req_c2)).body)
        assert body_c1["total"] == 1
        assert body_c2["total"] == 1

    @pytest.mark.asyncio
    async def test_sessions_limit(self, server_with_sessions):
        """limit 参数被接受。"""
        request = AsyncMock()
        request.query = {"port": "c1", "limit": "5"}
        result = await server_with_sessions.handle_sessions(request)
        body = json.loads(result.body)
        assert body["total"] == 1

    @pytest.mark.asyncio
    async def test_sessions_merge_live_data(self, server_with_sessions):
        """活跃会话合并实时数据，覆盖数据库旧值并标记 is_active。"""
        server_with_sessions.ble.get_live_session_data = MagicMock(
            return_value={
                1: {"session_id": 1, "session_wh": 2.0, "max_power": 60.0, "start_time": time.time() - 900},
            }
        )
        request = AsyncMock()
        request.query = {"port": "c1"}
        result = await server_with_sessions.handle_sessions(request)
        body = json.loads(result.body)
        s1 = next(s for s in body["sessions"] if s["id"] == 1)
        assert s1["total_wh"] == 2.0  # 实时数据覆盖数据库
        assert s1["is_active"] is True

    @pytest.mark.asyncio
    async def test_sessions_inactive_mark(self, server_with_sessions):
        """非活跃会话标记 is_active=False。"""
        request = AsyncMock()
        request.query = {"port": "c1"}
        result = await server_with_sessions.handle_sessions(request)
        body = json.loads(result.body)
        for s in body["sessions"]:
            assert s["is_active"] is False

    @pytest.mark.asyncio
    async def test_sessions_live_session_not_in_db(self, server_with_sessions):
        """活跃会话未入库时补充到列表头部。"""
        server_with_sessions.ble.get_live_session_data = MagicMock(
            return_value={
                3: {"session_id": 999, "session_wh": 0.5, "max_power": 30.0, "start_time": time.time() - 300},
            }
        )
        server_with_sessions.ble.state.ports = {3: MagicMock(voltage=15.0, current=0.5, protocol="PD")}
        request = AsyncMock()
        request.query = {"port": "c3"}
        result = await server_with_sessions.handle_sessions(request)
        body = json.loads(result.body)
        # C1/C2 没有数据，仅补入活跃会话
        assert body["total"] == 1
        live_ids = [s["id"] for s in body["sessions"] if s.get("is_active")]
        assert 999 in live_ids


class TestHandleSessionPoints:
    """Test GET /api/sessions/{id}/points。"""

    @pytest.mark.asyncio
    async def test_session_points_returns_points(self, server_with_sessions):
        """返回某会话的明细点。"""
        request = AsyncMock()
        request.match_info = {"id": "1"}
        request.query = {}
        result = await server_with_sessions.handle_session_points(request)
        body = json.loads(result.body)
        assert "points" in body
        assert len(body["points"]) == 2

    @pytest.mark.asyncio
    async def test_session_points_includes_fields(self, server_with_sessions):
        """明细点包含 timestamp/voltage/current/power/protocol。"""
        request = AsyncMock()
        request.match_info = {"id": "1"}
        request.query = {}
        result = await server_with_sessions.handle_session_points(request)
        body = json.loads(result.body)
        p = body["points"][0]
        assert "timestamp" in p
        assert "voltage" in p
        assert "current" in p
        assert "power" in p
        assert p["voltage"] == 20.0

    @pytest.mark.asyncio
    async def test_session_points_empty(self, server_with_sessions):
        """无明细点的会话返回空列表。"""
        sid = server_with_sessions.history.start_session(3)
        server_with_sessions.history.end_session(sid, 0, 0, 0, 0, 0)
        request = AsyncMock()
        request.match_info = {"id": str(sid)}
        request.query = {}
        result = await server_with_sessions.handle_session_points(request)
        body = json.loads(result.body)
        assert body["points"] == []

    @pytest.mark.asyncio
    async def test_session_points_returns_all(self, server_with_sessions):
        """不传 downsample 时返回全部明细点。"""
        request = AsyncMock()
        request.match_info = {"id": "1"}
        request.query = {}
        result = await server_with_sessions.handle_session_points(request)
        body = json.loads(result.body)
        # 会话 1 有 2 个点
        assert len(body["points"]) == 2


class TestHandleSessionsClear:
    """Test POST /api/sessions/clear。"""

    @pytest.mark.asyncio
    async def test_clear_sessions(self, server_with_sessions):
        """清空后返回删除条数，各端口会话查询均为空。"""
        request = AsyncMock()
        result = await server_with_sessions.handle_sessions_clear(request)
        body = json.loads(result.body)
        assert body["ok"] is True
        # fixture 预置 C1/C2 各一条会话
        assert body["deleted"] == 2
        for port in ("c1", "c2"):
            req = AsyncMock()
            req.query = {"port": port}
            body_after = json.loads(
                (await server_with_sessions.handle_sessions(req)).body)
            assert body_after["sessions"] == []
            assert body_after["total"] == 0

    @pytest.mark.asyncio
    async def test_clear_sessions_empty_history(self, server_with_sessions):
        """库中无会话时清理返回 deleted=0。"""
        server_with_sessions.history.clear_sessions()
        request = AsyncMock()
        result = await server_with_sessions.handle_sessions_clear(request)
        body = json.loads(result.body)
        assert body == {"ok": True, "deleted": 0}


class TestTrimSessionPoints:
    """会话明细点的一天滚动窗口与抽稀。"""

    @staticmethod
    def _make_points(count, start_ts, step_sec):
        return [
            {"timestamp": start_ts + i * step_sec, "voltage": 9.0,
             "current": 1.0, "power": 9.0, "protocol": ""}
            for i in range(count)
        ]

    def test_within_day_and_limit_unchanged(self):
        """一天内且点数不超上限：原样返回。"""
        from ha_server import trim_session_points
        pts = self._make_points(100, 1000.0, 60)
        out = trim_session_points(pts)
        assert out is pts or out == pts

    def test_rolls_to_last_day(self):
        """跨天会话只保留最近一天，且末点保留。"""
        from ha_server import trim_session_points, SESSION_POINTS_MAX_SPAN_SEC
        # 两天的点，每 600 秒一个，共 289 点（窗口内 145 点 < 上限）
        start = 1000.0
        pts = self._make_points(289, start, 600)
        out = trim_session_points(pts)
        last_ts = pts[-1]["timestamp"]
        assert len(out) < len(pts)
        assert out[-1]["timestamp"] == last_ts
        assert out[0]["timestamp"] >= last_ts - SESSION_POINTS_MAX_SPAN_SEC

    def test_decimates_over_limit_keeps_first_and_last(self):
        """窗口内点数超上限时等间隔抽稀，首尾点保留。"""
        from ha_server import trim_session_points, SESSION_POINTS_MAX_COUNT
        pts = self._make_points(1200, 1000.0, 60)
        out = trim_session_points(pts)
        assert len(out) <= SESSION_POINTS_MAX_COUNT
        assert out[0]["timestamp"] == pts[0]["timestamp"]
        assert out[-1]["timestamp"] == pts[-1]["timestamp"]

    def test_empty_points(self):
        """空列表原样返回。"""
        from ha_server import trim_session_points
        assert trim_session_points([]) == []
