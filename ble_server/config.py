"""CUKTECH BLE Server - Configuration management.

Supports YAML config file and environment variables.
YAML file takes precedence over environment variables.
"""
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def _load_yaml_config():
    """Load config from YAML file if exists."""
    config_path = Path(os.environ.get("CUKTECH_CONFIG_PATH", str(Path(__file__).parent / "config.yaml")))
    if not config_path.exists():
        config_path = Path.cwd() / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        import yaml
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        return {}


@dataclass
class BLEConfig:
    mac: str = ""
    token: str = ""
    ble_key: str = ""
    scan_timeout: int = 10

    def __post_init__(self):
        if not self.mac or self.mac == "XX:XX:XX:XX:XX:XX":
            _LOGGER.warning("CUKTECH_DEVICE_MAC 未配置，BLE 连接不可用。可通过 config.html 配置或设置环境变量")
        if not self.token:
            _LOGGER.warning("CUKTECH_DEVICE_TOKEN 未配置，BLE 连接不可用。可通过 config.html 配置或设置环境变量")


@dataclass
class MQTTConfig:
    enabled: bool = False
    host: str = "localhost"
    port: int = 1883
    username: str = ""
    password: str = ""
    keepalive: int = 60
    topic_prefix: str = "cuktech/charger"


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = field(default_factory=lambda: 18199 if sys.platform == "win32" else 8199)
    command_timeout: float = 10.0
    settings_refresh_interval: float = 10.0
    log_level: str = "info"
    history_retention_days: int = 2
    history_db_path: str = "port_history.db"
    reconnect_base_delay: float = 1.0
    reconnect_max_delay: float = 300.0


@dataclass
class BemfaConfig:
    enabled: bool = False
    uid: str = ""
    name_c1: str = "C口1开关"
    name_c2: str = "C口2开关"
    name_c3: str = "C口3开关"
    name_a: str = "USB-A开关"
    name_ble: str = "蓝牙开关"
    modified: bool = False  # set true when names change, triggers topic delete+recreate on next start


@dataclass
class ChargeTrackingConfig:
    enabled_ports: list = field(default_factory=lambda: [1])  # 记录充电的 C 口列表，仅支持 1/2
    # 起始功率阈值(W)按端口独立配置，键为端口号 1/2；0=按插拔触发，>0=功率超过该值开始记录
    start_power_w: dict = field(default_factory=lambda: {1: 0.0, 2: 0.0})
    # 截止功率阈值(W)按端口独立配置，键为端口号 1/2；0=按插拔触发，>0=功率低于该值持续 end_power_duration_sec 秒结束
    end_power_w: dict = field(default_factory=lambda: {1: 0.0, 2: 0.0})
    end_power_duration_sec: int = 30  # 截止判定的持续时长(秒)，功率持续低于阈值该秒数才会话结束
    unplug_grace_sec: int = 0  # 拔线容错(秒)，0=拔出立即结束；>0=拔出后等待该秒数，期间重新插上则延续同一会话
    point_interval_sec: int = 30  # 采样间隔(秒)，影响功率图表与充电曲线精度，容量统计不受影响


@dataclass
class Config:
    ble: BLEConfig = field(default_factory=BLEConfig)
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    bemfa: BemfaConfig = field(default_factory=BemfaConfig)
    charge_tracking: ChargeTrackingConfig = field(default_factory=ChargeTrackingConfig)

    @property
    def topic_port(self):
        return f"{self.mqtt.topic_prefix}/port"

    @property
    def topic_settings(self):
        return f"{self.mqtt.topic_prefix}/settings"

    @property
    def topic_status(self):
        return f"{self.mqtt.topic_prefix}/status"

    @property
    def topic_charge_event(self):
        return f"{self.mqtt.topic_prefix}/charge_event"


def _to_nonneg_float(v):
    """转非负浮点，非法或负值抛 ValueError。"""
    try:
        f = float(v)
    except (ValueError, TypeError):
        raise ValueError(f"功率阈值必须为数字: {v}")
    if f < 0:
        raise ValueError("功率阈值不能为负")
    return f


def parse_power_thresholds(raw):
    """解析按端口的功率阈值，返回 {1: v, 2: v}。
    标量表示两口同值；dict 支持 c1/c2、1/2、"1"/"2" 键，未提及的口为 0。"""
    result = {1: 0.0, 2: 0.0}
    if isinstance(raw, dict):
        for port in (1, 2):
            if f"c{port}" in raw:
                v = raw[f"c{port}"]
            elif port in raw:
                v = raw[port]
            elif str(port) in raw:
                v = raw[str(port)]
            else:
                continue
            result[port] = _to_nonneg_float(v)
        return result
    v = _to_nonneg_float(raw)
    return {1: v, 2: v}


def load_config() -> Config:
    """Load config from YAML file, then override with environment variables."""
    ycfg = _load_yaml_config()

    ble_cfg = ycfg.get("ble", {})
    mqtt_cfg = ycfg.get("mqtt", {})
    server_cfg = ycfg.get("server", {})

    ble = BLEConfig(
        mac=os.environ.get("CUKTECH_DEVICE_MAC", ble_cfg.get("mac", "")),
        token=os.environ.get("CUKTECH_DEVICE_TOKEN", ble_cfg.get("token", "")),
        ble_key=os.environ.get("CUKTECH_DEVICE_BLE_KEY", ble_cfg.get("ble_key", "")),
        scan_timeout=ble_cfg.get("scan_timeout", 10),
    )

    try:
        mqtt_port = int(os.environ.get("MQTT_PORT", mqtt_cfg.get("port", 1883)))
    except (ValueError, TypeError) as e:
        raise ValueError(f"MQTT port must be an integer: {e}")

    _mqtt_enabled_env = os.environ.get("MQTT_ENABLED", "").lower()
    mqtt_enabled = _mqtt_enabled_env in ("1", "true", "yes") or mqtt_cfg.get("enabled", False)

    mqtt = MQTTConfig(
        enabled=mqtt_enabled,
        host=os.environ.get("MQTT_HOST", mqtt_cfg.get("host", "localhost")),
        port=mqtt_port,
        username=os.environ.get("MQTT_USER", mqtt_cfg.get("username", "")),
        password=os.environ.get("MQTT_PASS", mqtt_cfg.get("password", "")),
        keepalive=mqtt_cfg.get("keepalive", 60),
        topic_prefix=os.environ.get("MQTT_TOPIC_PREFIX", mqtt_cfg.get("topic_prefix", "cuktech/charger")),
    )

    try:
        history_retention = int(os.environ.get("CUKTECH_HISTORY_RETENTION_DAYS", server_cfg.get("history_retention_days", 2)))
    except (ValueError, TypeError) as e:
        raise ValueError(f"History retention days must be an integer: {e}")
    try:
        reconnect_base_delay = float(server_cfg.get("reconnect_base_delay", 1.0))
    except (ValueError, TypeError) as e:
        raise ValueError(f"Reconnect base delay must be a number: {e}")
    try:
        reconnect_max_delay = float(server_cfg.get("reconnect_max_delay", 300.0))
    except (ValueError, TypeError) as e:
        raise ValueError(f"Reconnect max delay must be a number: {e}")

    server = ServerConfig(
        host=server_cfg.get("host", "0.0.0.0"),
        port=int(os.environ.get("CUKTECH_SERVER_PORT", server_cfg.get("port", 18199 if sys.platform == "win32" else 8199))),
        command_timeout=server_cfg.get("command_timeout", 10.0),
        settings_refresh_interval=server_cfg.get("settings_refresh_interval", 60.0),
        log_level=os.environ.get("CUKTECH_LOG_LEVEL", server_cfg.get("log_level", "info")),
        history_retention_days=history_retention,
        history_db_path=os.environ.get("CUKTECH_HISTORY_DB_PATH", server_cfg.get("history_db_path", "port_history.db")),
        reconnect_base_delay=reconnect_base_delay,
        reconnect_max_delay=reconnect_max_delay,
    )

    bemfa_cfg = ycfg.get("bemfa", {})
    bemfa_enabled_env = os.environ.get("BEMFA_ENABLED", "").lower()
    bemfa_enabled = bemfa_enabled_env in ("1", "true", "yes") or bemfa_cfg.get("enabled", False)
    bemfa = BemfaConfig(
        enabled=bemfa_enabled,
        uid=os.environ.get("BEMFA_UID", bemfa_cfg.get("uid", "")),
        name_c1=bemfa_cfg.get("name_c1", "C口1开关"),
        name_c2=bemfa_cfg.get("name_c2", "C口2开关"),
        name_c3=bemfa_cfg.get("name_c3", "C口3开关"),
        name_a=bemfa_cfg.get("name_a", "USB-A开关"),
        name_ble=bemfa_cfg.get("name_ble", "蓝牙开关"),
        modified=bemfa_cfg.get("modified", False),
    )

    charge_cfg = ycfg.get("charge_tracking", {}) or {}

    # 解析 enabled_ports：仅保留 1/2，过滤掉非法值（如 0、3、4、字符串等）
    raw_ports = charge_cfg.get("enabled_ports", [1])
    if not isinstance(raw_ports, list):
        raw_ports = [raw_ports]
    enabled_ports = []
    for p in raw_ports:
        try:
            port = int(p)
        except (ValueError, TypeError):
            continue
        if port in (1, 2) and port not in enabled_ports:
            enabled_ports.append(port)
    if not enabled_ports:
        enabled_ports = [1]

    # 解析功率阈值：按端口独立，标量写法表示两口同值，非法/负值当 0 处理
    try:
        start_power_w = parse_power_thresholds(charge_cfg.get("start_power_w", 0.0))
    except ValueError:
        start_power_w = {1: 0.0, 2: 0.0}
    try:
        end_power_w = parse_power_thresholds(charge_cfg.get("end_power_w", 0.0))
    except ValueError:
        end_power_w = {1: 0.0, 2: 0.0}

    # 解析 point_interval_sec：最小 1，默认 30
    try:
        point_interval_sec = int(charge_cfg.get("point_interval_sec", 30))
    except (ValueError, TypeError):
        point_interval_sec = 30
    if point_interval_sec < 1:
        point_interval_sec = 30

    # 解析 end_power_duration_sec：最小 1，默认 30
    try:
        end_power_duration_sec = int(charge_cfg.get("end_power_duration_sec", 30))
    except (ValueError, TypeError):
        end_power_duration_sec = 30
    if end_power_duration_sec < 1:
        end_power_duration_sec = 30

    # 解析 unplug_grace_sec：0=关闭容错，默认 0；非法或负值回落 0
    try:
        unplug_grace_sec = int(charge_cfg.get("unplug_grace_sec", 0))
    except (ValueError, TypeError):
        unplug_grace_sec = 0
    if unplug_grace_sec < 0:
        unplug_grace_sec = 0

    charge_tracking = ChargeTrackingConfig(
        enabled_ports=enabled_ports,
        start_power_w=start_power_w,
        end_power_w=end_power_w,
        end_power_duration_sec=end_power_duration_sec,
        unplug_grace_sec=unplug_grace_sec,
        point_interval_sec=point_interval_sec,
    )

    return Config(ble=ble, mqtt=mqtt, server=server, bemfa=bemfa, charge_tracking=charge_tracking)
