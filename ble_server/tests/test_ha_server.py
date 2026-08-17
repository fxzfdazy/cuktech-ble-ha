"""Tests for ha_server.py - HTTP API endpoints."""
import asyncio
import sys
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestHandleLogLevel:
    """Test log level API endpoint."""

    @pytest.mark.asyncio
    async def test_get_log_level(self):
        from ha_server import Server
        s = Server.__new__(Server)

        request = AsyncMock()
        request.method = "GET"

        result = await s.handle_log_level(request)
        body = json.loads(result.body)
        assert "level" in body
        assert body["level"] in ["debug", "info", "warning", "error"]

    @pytest.mark.asyncio
    async def test_set_log_level(self):
        from ha_server import Server
        s = Server.__new__(Server)

        request = AsyncMock()
        request.method = "POST"
        request.json = AsyncMock(return_value={"level": "debug"})

        result = await s.handle_log_level(request)
        body = json.loads(result.body)
        assert body["ok"] is True

    @pytest.mark.asyncio
    async def test_set_invalid_log_level(self):
        from ha_server import Server
        s = Server.__new__(Server)

        request = AsyncMock()
        request.method = "POST"
        request.json = AsyncMock(return_value={"level": "invalid"})

        result = await s.handle_log_level(request)
        assert result.status == 400


class TestHandleProtocol:
    """Test /api/protocol endpoint."""

    @pytest.fixture
    def server(self):
        """Create a Server instance with mocked BLE state."""
        from ha_server import Server
        from state import ChargerState

        s = Server.__new__(Server)
        s.ble = MagicMock()
        s.ble.state = ChargerState()

        async def init_state():
            # Start with all protocols ON (c1/c2: 0x0F each, c3: 0x03, a: 0x03)
            await s.ble.state.update_protocol_extend(0x03030F0F)

        asyncio.run(init_state())
        s.ble.send_command = AsyncMock(return_value={"ok": True})
        return s

    @pytest.mark.asyncio
    async def test_protocol_toggle(self, server):
        """Test toggling a protocol switch."""
        request = AsyncMock()
        request.json = AsyncMock(return_value={"port": "c1", "protocol": "pd"})

        result = await server.handle_protocol(request)
        body = json.loads(result.body)
        assert body["ok"] is True
        # PD was ON, now should be OFF (state synced locally)
        assert server.ble.state.protocol_switches["c1"]["pd"] is False

    @pytest.mark.asyncio
    async def test_protocol_turn_on(self, server):
        """Test explicitly turning on a protocol switch."""
        # First turn it off
        await server.ble.state.update_protocol_extend(0x03030F0F & ~(1 << 0))

        request = AsyncMock()
        request.json = AsyncMock(return_value={"port": "c1", "protocol": "pd", "action": "on"})

        result = await server.handle_protocol(request)
        body = json.loads(result.body)
        assert body["ok"] is True
        assert server.ble.state.protocol_switches["c1"]["pd"] is True

    @pytest.mark.asyncio
    async def test_protocol_turn_off(self, server):
        """Test explicitly turning off a protocol switch."""
        request = AsyncMock()
        request.json = AsyncMock(return_value={"port": "c2", "protocol": "pps", "action": "off"})

        result = await server.handle_protocol(request)
        body = json.loads(result.body)
        assert body["ok"] is True
        assert server.ble.state.protocol_switches["c2"]["pps"] is False

    @pytest.mark.asyncio
    async def test_protocol_invalid_port(self, server):
        """Test invalid port returns error."""
        request = AsyncMock()
        request.json = AsyncMock(return_value={"port": "c5", "protocol": "pd"})

        result = await server.handle_protocol(request)
        assert result.status == 400
        body = json.loads(result.body)
        assert body["ok"] is False

    @pytest.mark.asyncio
    async def test_protocol_invalid_protocol(self, server):
        """Test invalid protocol returns error."""
        request = AsyncMock()
        request.json = AsyncMock(return_value={"port": "c1", "protocol": "invalid"})

        result = await server.handle_protocol(request)
        assert result.status == 400

    @pytest.mark.asyncio
    async def test_protocol_missing_params(self, server):
        """Test missing parameters returns error."""
        request = AsyncMock()
        request.json = AsyncMock(return_value={})

        result = await server.handle_protocol(request)
        assert result.status == 400

    @pytest.mark.asyncio
    async def test_protocol_value_mode(self, server):
        """Test setting raw value."""
        request = AsyncMock()
        request.json = AsyncMock(return_value={"value": 0})

        result = await server.handle_protocol(request)
        body = json.loads(result.body)
        assert body["ok"] is True
        assert server.ble.state.protocol_switches["c1"]["pd"] is False

    @pytest.mark.asyncio
    async def test_protocol_switches_mode(self, server):
        """Test bulk switch setting."""
        request = AsyncMock()
        request.json = AsyncMock(return_value={
            "switches": {
                "c1": {"pd": False, "pps": False, "ufcs": False},
                "c2": {"pd": False, "pps": False, "ufcs": False},
                "c3": {"ufcs": False, "scp": False},
                "a":  {"ufcs": False, "scp": False},
            }
        })

        result = await server.handle_protocol(request)
        body = json.loads(result.body)
        assert body["ok"] is True

    @pytest.mark.asyncio
    async def test_protocol_bad_json(self, server):
        """Test invalid JSON returns error."""
        import json as _json
        request = AsyncMock()
        request.json = AsyncMock(side_effect=_json.JSONDecodeError("bad", "", 0))

        result = await server.handle_protocol(request)
        assert result.status == 400


class TestHandleChargeTracking:
    """充电记录配置 /api/charge_tracking 接口测试。"""

    @pytest.fixture
    def server(self):
        """构造一个带充电记录配置的 Server 实例，持久化用 mock 避免写真实文件。"""
        from ha_server import Server
        from config import ChargeTrackingConfig

        s = Server.__new__(Server)
        s.ble = MagicMock()
        s.ble.config = MagicMock()
        s.ble.config.charge_tracking = ChargeTrackingConfig()
        # 持久化用 mock，避免写真实配置文件
        s._persist_charge_tracking = MagicMock(return_value=(True, None))
        return s

    @pytest.mark.asyncio
    async def test_get_charge_tracking(self, server):
        """GET 返回默认配置。"""
        request = AsyncMock()
        result = await server.handle_get_charge_tracking(request)
        body = json.loads(result.body)
        assert body["enabled_ports"] == [1]
        assert body["start_power_w"] == {"c1": 0.0, "c2": 0.0}
        assert body["end_power_w"] == {"c1": 0.0, "c2": 0.0}

    @pytest.mark.asyncio
    async def test_set_charge_tracking(self, server):
        """POST 更新 enabled_ports 与按口独立的功率阈值。"""
        request = AsyncMock()
        request.json = AsyncMock(return_value={
            "enabled_ports": [1, 2],
            "start_power_w": {"c1": 2.5, "c2": 3.5},
            "end_power_w": {"c1": 1.0, "c2": 0},
            "end_power_duration_sec": 10,
        })
        result = await server.handle_set_charge_tracking(request)
        body = json.loads(result.body)
        assert body["ok"] is True
        assert body["enabled_ports"] == [1, 2]
        assert body["start_power_w"] == {"c1": 2.5, "c2": 3.5}
        assert body["end_power_w"] == {"c1": 1.0, "c2": 0.0}
        assert body["end_power_duration_sec"] == 10
        # 内存配置已更新
        ct = server.ble.config.charge_tracking
        assert ct.enabled_ports == [1, 2]
        assert ct.start_power_w == {1: 2.5, 2: 3.5}
        assert ct.end_power_w == {1: 1.0, 2: 0.0}
        assert ct.end_power_duration_sec == 10
        # 持久化被调用
        server._persist_charge_tracking.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_charge_tracking_scalar_applies_to_both(self, server):
        """POST 标量阈值表示两口同值（兼容旧客户端）。"""
        request = AsyncMock()
        request.json = AsyncMock(return_value={
            "enabled_ports": [1],
            "start_power_w": 2.5,
            "end_power_w": 1.0,
        })
        result = await server.handle_set_charge_tracking(request)
        body = json.loads(result.body)
        assert body["ok"] is True
        ct = server.ble.config.charge_tracking
        assert ct.start_power_w == {1: 2.5, 2: 2.5}
        assert ct.end_power_w == {1: 1.0, 2: 1.0}

    @pytest.mark.asyncio
    async def test_set_charge_tracking_invalid_port(self, server):
        """POST 非法端口（4）被拒绝，返回 400。"""
        request = AsyncMock()
        request.json = AsyncMock(return_value={
            "enabled_ports": [1, 4],
            "start_power_w": 0,
            "end_power_w": 0,
        })
        result = await server.handle_set_charge_tracking(request)
        assert result.status == 400
        body = json.loads(result.body)
        assert body["ok"] is False

    @pytest.mark.asyncio
    async def test_set_charge_tracking_rejects_port3(self, server):
        """POST 端口 3（C3）被拒绝，返回 400。"""
        request = AsyncMock()
        request.json = AsyncMock(return_value={
            "enabled_ports": [1, 3],
            "start_power_w": 0,
            "end_power_w": 0,
        })
        result = await server.handle_set_charge_tracking(request)
        assert result.status == 400
        body = json.loads(result.body)
        assert body["ok"] is False
        assert "C1/C2" in body["error"]

    @pytest.mark.asyncio
    async def test_set_charge_tracking_negative_power_rejected(self, server):
        """POST 负值功率阈值被拒绝。"""
        request = AsyncMock()
        request.json = AsyncMock(return_value={
            "enabled_ports": [1],
            "start_power_w": -1,
            "end_power_w": 0,
        })
        result = await server.handle_set_charge_tracking(request)
        assert result.status == 400

    @pytest.mark.asyncio
    async def test_set_charge_tracking_missing_ports(self, server):
        """POST 缺少 enabled_ports 被拒绝。"""
        request = AsyncMock()
        request.json = AsyncMock(return_value={
            "start_power_w": 0,
            "end_power_w": 0,
        })
        result = await server.handle_set_charge_tracking(request)
        assert result.status == 400

    @pytest.mark.asyncio
    async def test_set_charge_tracking_bad_json(self):
        """POST 非法 JSON 被拒绝。"""
        import json as _json
        request = AsyncMock()
        request.json = AsyncMock(side_effect=_json.JSONDecodeError("bad", "", 0))
        result = await server.handle_set_charge_tracking(request)
        assert result.status == 400


class TestHandleChart:
    """功率图表 /api/chart 接口测试。"""

    @pytest.fixture
    def server(self, history):
        """构造带真实 PortHistory 与图表缓存的 Server 实例。"""
        from collections import OrderedDict
        from ha_server import Server

        s = Server.__new__(Server)
        s.history = history
        s._chart_cache = OrderedDict()
        s._chart_cache_ttl = 10
        s._chart_cache_max = 10
        return s

    @pytest.mark.asyncio
    async def test_chart_returns_data(self, server, mock_ble_data):
        """GET /api/chart 返回 200 且包含 labels/datasets。"""
        for port in range(1, 5):
            server.history.record_port_data(port, mock_ble_data)

        request = AsyncMock()
        request.query = {"hours": "1", "interval": "30"}
        request.headers = {}

        result = await server.handle_chart(request)
        assert result.status == 200
        body = json.loads(result.body)
        assert body["ok"] is True
        assert "labels" in body
        assert "datasets" in body
        assert "power" in body["datasets"]

    @pytest.mark.asyncio
    async def test_chart_invalid_hours(self, server):
        """非法 hours 参数返回 400。"""
        request = AsyncMock()
        request.query = {"hours": "abc", "interval": "30"}
        request.headers = {}

        result = await server.handle_chart(request)
        assert result.status == 400


class TestHandleStatistics:
    """端口统计 /api/statistics/{port} 接口测试。"""

    @pytest.fixture
    def server(self, history):
        from ha_server import Server

        s = Server.__new__(Server)
        s.history = history
        return s

    @pytest.mark.asyncio
    async def test_statistics_returns_200(self, server, mock_ble_data):
        """GET /api/statistics/1 返回 200 且包含 data。"""
        for _ in range(3):
            server.history.record_port_data(1, mock_ble_data)

        request = AsyncMock()
        request.match_info = {"port": "1"}
        request.query = {"hours": "24"}

        result = await server.handle_statistics(request)
        assert result.status == 200
        body = json.loads(result.body)
        assert body["ok"] is True
        assert "data" in body
        assert body["data"]["port"] == 1

    @pytest.mark.asyncio
    async def test_statistics_invalid_port(self, server):
        """非法端口返回 400。"""
        request = AsyncMock()
        request.match_info = {"port": "9"}
        request.query = {}

        result = await server.handle_statistics(request)
        assert result.status == 400


class TestHandleExport:
    """CSV 导出 /api/export/{port} 接口测试。"""

    @pytest.fixture
    def server(self, history):
        from ha_server import Server

        s = Server.__new__(Server)
        s.history = history
        return s

    @pytest.mark.asyncio
    async def test_export_returns_csv(self, server, mock_ble_data):
        """GET /api/export/1 返回 200 且 content-type 为 text/csv。"""
        server.history.record_port_data(1, mock_ble_data)

        request = AsyncMock()
        request.match_info = {"port": "1"}
        request.query = {"hours": "24"}

        result = await server.handle_export(request)
        assert result.status == 200
        assert result.content_type == "text/csv"
        body = result.body.decode() if isinstance(result.body, (bytes, bytearray)) else result.body
        assert "timestamp" in body
        assert "voltage" in body

    @pytest.mark.asyncio
    async def test_export_invalid_port(self, server):
        """非法端口返回 400。"""
        request = AsyncMock()
        request.match_info = {"port": "9"}
        request.query = {}

        result = await server.handle_export(request)
        assert result.status == 400
