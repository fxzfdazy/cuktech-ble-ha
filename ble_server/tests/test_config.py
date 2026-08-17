"""Tests for config.py - Configuration management."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import (load_config, BLEConfig, MQTTConfig, ServerConfig, Config,
                    ChargeTrackingConfig)


class TestBLEConfig:
    """Test BLE configuration."""

    def test_valid_config(self):
        """Test valid BLE config."""
        config = BLEConfig(mac="AA:BB:CC:DD:EE:FF", token="aabbccddeeff")
        assert config.mac == "AA:BB:CC:DD:EE:FF"
        assert config.token == "aabbccddeeff"

    @patch.dict(os.environ, {"CUKTECH_DEVICE_MAC": ""})
    def test_missing_mac_raises(self):
        """Test that missing MAC does not crash (graceful warning)."""
        cfg = BLEConfig(mac="", token="aabbccddeeff")
        assert cfg.mac == ""

    @patch.dict(os.environ, {"CUKTECH_DEVICE_MAC": "XX:XX:XX:XX:XX:XX"})
    def test_placeholder_mac_raises(self):
        """Test that placeholder MAC does not crash (graceful warning)."""
        cfg = BLEConfig(mac="XX:XX:XX:XX:XX:XX", token="aabbccddeeff")
        assert cfg.mac == "XX:XX:XX:XX:XX:XX"

    @patch.dict(os.environ, {"CUKTECH_DEVICE_TOKEN": ""})
    def test_missing_token_raises(self):
        """Test that missing token does not crash (graceful warning)."""
        cfg = BLEConfig(mac="AA:BB:CC:DD:EE:FF", token="")
        assert cfg.token == ""


class TestMQTTConfig:
    """Test MQTT configuration."""

    def test_default_values(self):
        """Test default MQTT config values."""
        config = MQTTConfig()
        assert config.host == "localhost"
        assert config.port == 1883
        assert config.username == ""
        assert config.password == ""
        assert config.keepalive == 60
        assert config.topic_prefix == "cuktech/charger"


class TestServerConfig:
    """Test Server configuration."""

    def test_default_values(self):
        """Test default server config values."""
        config = ServerConfig()
        assert config.host == "0.0.0.0"
        assert config.port == 8199
        assert config.log_level == "info"
        assert config.history_retention_days == 2
        assert config.reconnect_base_delay == 1.0
        assert config.reconnect_max_delay == 300.0


class TestConfig:
    """Test Config class."""

    @patch.dict(os.environ, {
        "CUKTECH_DEVICE_MAC": "AA:BB:CC:DD:EE:FF",
        "CUKTECH_DEVICE_TOKEN": "aabbccddeeff",
    })
    def test_topic_properties(self):
        """Test topic property generation."""
        config = load_config()
        assert config.topic_port == "cuktech/charger/port"
        assert config.topic_settings == "cuktech/charger/settings"
        assert config.topic_status == "cuktech/charger/status"


class TestLoadConfig:
    """Test config loading."""

    @patch.dict(os.environ, {
        "CUKTECH_DEVICE_MAC": "AA:BB:CC:DD:EE:FF",
        "CUKTECH_DEVICE_TOKEN": "aabbccddeeff",
    })
    def test_load_from_env(self):
        """Test loading config from environment variables."""
        config = load_config()
        assert config.ble.mac == "AA:BB:CC:DD:EE:FF"
        assert config.ble.token == "aabbccddeeff"

    @patch.dict(os.environ, {
        "CUKTECH_DEVICE_MAC": "AA:BB:CC:DD:EE:FF",
        "CUKTECH_DEVICE_TOKEN": "aabbccddeeff",
        "CUKTECH_LOG_LEVEL": "debug",
    })
    def test_log_level_from_env(self):
        """Test log level from environment variable."""
        config = load_config()
        assert config.server.log_level == "debug"

    @patch.dict(os.environ, {
        "CUKTECH_DEVICE_MAC": "AA:BB:CC:DD:EE:FF",
        "CUKTECH_DEVICE_TOKEN": "aabbccddeeff",
        "CUKTECH_HISTORY_RETENTION_DAYS": "7",
    })
    def test_retention_days_from_env(self):
        """Test history retention days from environment variable."""
        config = load_config()
        assert config.server.history_retention_days == 7


class TestChargeTrackingConfig:
    """充电记录配置加载测试。"""

    @patch.dict(os.environ, {
        "CUKTECH_DEVICE_MAC": "AA:BB:CC:DD:EE:FF",
        "CUKTECH_DEVICE_TOKEN": "aabbccddeeff",
    })
    def test_charge_tracking_defaults(self, tmp_path):
        """空配置时 enabled_ports 默认 [1]，功率阈值默认 0。"""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("", encoding="utf-8")
        with patch.dict(os.environ, {"CUKTECH_CONFIG_PATH": str(cfg_file)}):
            config = load_config()
        ct = config.charge_tracking
        assert ct.enabled_ports == [1]
        assert ct.start_power_w == {1: 0.0, 2: 0.0}
        assert ct.end_power_w == {1: 0.0, 2: 0.0}

    @patch.dict(os.environ, {
        "CUKTECH_DEVICE_MAC": "AA:BB:CC:DD:EE:FF",
        "CUKTECH_DEVICE_TOKEN": "aabbccddeeff",
    })
    def test_charge_tracking_loads_from_yaml(self, tmp_path):
        """YAML 标量阈值表示两口同值，enabled_ports 中的 3（C3）被过滤。"""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "charge_tracking:\n"
            "  enabled_ports: [1, 2, 3]\n"
            "  start_power_w: 2.5\n"
            "  end_power_w: 1.0\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"CUKTECH_CONFIG_PATH": str(cfg_file)}):
            config = load_config()
        ct = config.charge_tracking
        assert ct.enabled_ports == [1, 2]
        assert ct.start_power_w == {1: 2.5, 2: 2.5}
        assert ct.end_power_w == {1: 1.0, 2: 1.0}

    @patch.dict(os.environ, {
        "CUKTECH_DEVICE_MAC": "AA:BB:CC:DD:EE:FF",
        "CUKTECH_DEVICE_TOKEN": "aabbccddeeff",
    })
    def test_charge_tracking_per_port_thresholds(self, tmp_path):
        """YAML 按口独立的阈值能正确加载，未提及的口为 0。"""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "charge_tracking:\n"
            "  start_power_w:\n"
            "    c1: 3.5\n"
            "    c2: 1.2\n"
            "  end_power_w:\n"
            "    c1: 0.8\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"CUKTECH_CONFIG_PATH": str(cfg_file)}):
            config = load_config()
        ct = config.charge_tracking
        assert ct.start_power_w == {1: 3.5, 2: 1.2}
        assert ct.end_power_w == {1: 0.8, 2: 0.0}

    @patch.dict(os.environ, {
        "CUKTECH_DEVICE_MAC": "AA:BB:CC:DD:EE:FF",
        "CUKTECH_DEVICE_TOKEN": "aabbccddeeff",
    })
    def test_charge_tracking_filters_invalid_ports(self, tmp_path):
        """enabled_ports 中的非法端口（0/3/4/5）被过滤，仅保留合法 C 口。"""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "charge_tracking:\n"
            "  enabled_ports: [0, 3, 4, 5, 1]\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"CUKTECH_CONFIG_PATH": str(cfg_file)}):
            config = load_config()
        assert config.charge_tracking.enabled_ports == [1]

    @patch.dict(os.environ, {
        "CUKTECH_DEVICE_MAC": "AA:BB:CC:DD:EE:FF",
        "CUKTECH_DEVICE_TOKEN": "aabbccddeeff",
    })
    def test_charge_tracking_negative_power_becomes_zero(self, tmp_path):
        """负值功率阈值当 0 处理。"""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "charge_tracking:\n"
            "  start_power_w: -1\n"
            "  end_power_w: -2\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"CUKTECH_CONFIG_PATH": str(cfg_file)}):
            config = load_config()
        assert config.charge_tracking.start_power_w == {1: 0.0, 2: 0.0}
        assert config.charge_tracking.end_power_w == {1: 0.0, 2: 0.0}

    @patch.dict(os.environ, {
        "CUKTECH_DEVICE_MAC": "AA:BB:CC:DD:EE:FF",
        "CUKTECH_DEVICE_TOKEN": "aabbccddeeff",
    })
    def test_charge_tracking_empty_ports_falls_back_to_default(self, tmp_path):
        """enabled_ports 为空列表时回退到默认 [1]。"""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "charge_tracking:\n"
            "  enabled_ports: []\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"CUKTECH_CONFIG_PATH": str(cfg_file)}):
            config = load_config()
        assert config.charge_tracking.enabled_ports == [1]
