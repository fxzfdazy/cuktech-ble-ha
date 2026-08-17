"""CUKTECH BLE data server for Home Assistant integration.

BLE data is published to MQTT for real-time updates in Home Assistant.
"""
import asyncio
import re
import sys
import warnings
warnings.filterwarnings('ignore', message='.*default MTU.*')
import gzip
import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from aiohttp import web

from config import load_config, LOG_LEVELS, parse_power_thresholds
from state import ChargerState, PORT_BITS, PORT_NAMES, PORT_DEFAULT, VALID_PIIDS, PIID_RANGES, PROTOCOL_SWITCH_BITS
from ble_manager import BLEManager, set_status_cache_invalidator
from history import PortHistory
try:
    from xiaomi_cloud import XiaomiCloudLoginError, QrCodeXiaomiCloudClient
except ImportError:
    XiaomiCloudLoginError = Exception
    QrCodeXiaomiCloudClient = None
from bemfa_client import BemfaClient, MSG_ON, MSG_OFF

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_LOGGER = logging.getLogger("cuktech_server")


_sse_log = logging.getLogger("cuktech_sse")


# ── Request size limit (prevent DoS via large payloads) ──
MAX_REQUEST_BODY_SIZE = 1024 * 1024  # 1 MB


class SSEEmitter:
    """SSE event broadcaster — push events to all connected browser clients."""

    MAX_QUEUE_SIZE = 128

    def __init__(self):
        self._clients: set[asyncio.Queue] = set()
        self._pending_status: dict[int, str] = {}  # id(queue) -> latest status payload
        self._lock = threading.Lock()

    def add_client(self, queue: asyncio.Queue):
        with self._lock:
            self._clients.add(queue)
        _sse_log.info("SSE client connected (total: %d)", len(self._clients))

    def remove_client(self, queue: asyncio.Queue):
        with self._lock:
            self._clients.discard(queue)
            self._pending_status.pop(id(queue), None)
        _sse_log.info("SSE client disconnected (total: %d)", len(self._clients))

    def _put_or_drop(self, q: asyncio.Queue, payload: str):
        """Put payload in queue; drop oldest if full."""
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            _sse_log.warning("SSE queue full, dropping oldest event (qsize=%s)", q.qsize())
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    def emit(self, event_type: str, data: dict):
        """Broadcast event to all connected clients.
        Status events are kept as 'latest only' — stale statuses are replaced,
        never enqueued alongside newer statuses.
        """
        payload = json.dumps({"type": event_type, **data}, ensure_ascii=False)
        is_status = event_type == "status"
        with self._lock:
            for q in self._clients:
                qid = id(q)
                if is_status:
                    # Status: just update pending, don't enqueue (replaces stale status)
                    self._pending_status[qid] = payload
                else:
                    # Non-status: flush pending status first, then enqueue
                    status = self._pending_status.pop(qid, None)
                    if status:
                        self._put_or_drop(q, status)
                    self._put_or_drop(q, payload)

    def flush_status(self, q: asyncio.Queue) -> str | None:
        """Return and clear the latest pending status for a queue (if any).
        Called by SSE handler before each queue read to ensure the newest status
        is sent ahead of all other events.
        """
        with self._lock:
            return self._pending_status.pop(id(q), None)


class Server:
    def __init__(self):
        self.config = load_config()
        self.state = ChargerState()
        self.ble = BLEManager(self.config.ble.mac, self.config.ble.token, self.state, self.config)
        self.mqtt_client = None
        self._mqtt_connect_time = 0.0
        self._mqtt_disconnect_count = 0
        self._mqtt_publish_failures = 0
        self.loop = None
        self._start_lock = asyncio.Lock()
        self._status_cache_bytes = None
        self._status_cache_valid = False
        self._chart_cache: OrderedDict = OrderedDict()
        self._chart_cache_ttl = 10
        self._chart_cache_max = 10
        self.sse = SSEEmitter()
        self._xiaomi_sessions: dict[str, tuple[Any, asyncio.TimerHandle | None]] = {}  # session_id -> (client, timer)
        self._start_time = time.time()
        self.history = PortHistory(
            db_path=self.config.server.history_db_path,
            retention_days=self.config.server.history_retention_days,
        )
        self.bemfa: BemfaClient | None = None
        effective_level = self.config.server.log_level
        logging.getLogger().setLevel(LOG_LEVELS.get(effective_level, logging.INFO))
        env_var = os.environ.get("CUKTECH_LOG_LEVEL")
        source = "环境变量 CUKTECH_LOG_LEVEL" if env_var else "config.yaml"
        _LOGGER.info("日志级别: %s (来源: %s)", effective_level, source)

    def mqtt_publish(self, topic, payload, retain=False):
        """Publish to all enabled MQTT clients (multiplex)."""
        # HA MQTT
        if self.mqtt_client and self.mqtt_client.is_connected():
            try:
                self.mqtt_client.publish(topic, json.dumps(payload, ensure_ascii=False), retain=retain)
            except Exception:
                self._mqtt_publish_failures += 1
        # Bemfa
        if self.bemfa and self.bemfa.is_connected:
            self._bemfa_publish(topic, payload)

    def _bemfa_publish(self, topic, payload):
        """Map HA MQTT topics to Bemfa device states."""
        topic_prefix = self.config.mqtt.topic_prefix
        # Port state: {prefix}/port/{port_name}
        if topic.startswith(f"{topic_prefix}/port/"):
            port_name = topic.split("/")[-1]
            entity_map = {
                "c1": "cuktech_c1",
                "c2": "cuktech_c2",
                "c3": "cuktech_c3",
                "a": "cuktech_usb_a",
            }
            entity_id = entity_map.get(port_name)
            if entity_id and isinstance(payload, dict):
                state = MSG_ON if payload.get("active") else MSG_OFF
                self.bemfa.publish_state(entity_id, state)
        # BLE status: {prefix}/status
        elif topic == f"{topic_prefix}/status":
            if isinstance(payload, dict):
                connected = payload.get("connected", False)
                self.bemfa.publish_state("cuktech_ble", MSG_ON if connected else MSG_OFF)

    async def setup_bemfa(self):
        """Initialize Bemfa client if enabled."""
        if not self.config.bemfa.enabled or not self.config.bemfa.uid:
            _LOGGER.info("Bemfa disabled or UID not configured")
            return

        # Cleanup old client if exists
        if self.bemfa:
            await self.bemfa.stop()

        self.bemfa = BemfaClient(self.config.bemfa.uid, modified=self.config.bemfa.modified)

        # Register devices with custom display names
        self.bemfa.add_device("cuktech_c1", self.config.bemfa.name_c1)
        self.bemfa.add_device("cuktech_c2", self.config.bemfa.name_c2)
        self.bemfa.add_device("cuktech_c3", self.config.bemfa.name_c3)
        self.bemfa.add_device("cuktech_usb_a", self.config.bemfa.name_a)
        self.bemfa.add_device("cuktech_ble", self.config.bemfa.name_ble)

        # Register command callbacks
        def _port_cmd(port, on):
            _LOGGER.info("Bemfa command: %s %s", port, "on" if on else "off")
            try:
                self.loop.call_soon_threadsafe(
                    self.ble.cmd_queue.put_nowait,
                    ("port", (port, "on" if on else "off"), None))
                return True
            except Exception as e:
                _LOGGER.error("Bemfa port cmd failed: %s", e)
                return False

        def _ble_cmd(on):
            _LOGGER.info("Bemfa BLE command: %s", "on" if on else "off")
            try:
                if on:
                    asyncio.run_coroutine_threadsafe(self.ble.start(), self.loop)
                else:
                    asyncio.run_coroutine_threadsafe(self.ble.request_stop(), self.loop)
                return True
            except Exception as e:
                _LOGGER.error("Bemfa BLE cmd failed: %s", e)
                return False

        self.bemfa.on_command("cuktech_c1", lambda on: _port_cmd("c1", on))
        self.bemfa.on_command("cuktech_c2", lambda on: _port_cmd("c2", on))
        self.bemfa.on_command("cuktech_c3", lambda on: _port_cmd("c3", on))
        self.bemfa.on_command("cuktech_usb_a", lambda on: _port_cmd("a", on))
        self.bemfa.on_command("cuktech_ble", _ble_cmd)

        await self.bemfa.start()

        # Clear modified flag after successful topic registration
        if self.config.bemfa.modified:
            self._clear_bemfa_modified()

    def _config_path(self) -> Path:
        return Path(os.environ.get("CUKTECH_CONFIG_PATH", str(Path(__file__).parent / "config.yaml")))

    def _clear_bemfa_modified(self):
        """Set bemfa.modified=false in config.yaml after topic re-registration."""
        try:
            import yaml
            config_path = self._config_path()
            cfg = {}
            if config_path.exists():
                with open(config_path) as f:
                    cfg = yaml.safe_load(f) or {}
            if cfg.get("bemfa", {}).get("modified"):
                cfg["bemfa"]["modified"] = False
                # Atomic write: write to temp file then rename
                tmp_path = config_path.with_suffix(".tmp")
                with open(tmp_path, "w") as f:
                    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
                os.replace(tmp_path, config_path)
                _LOGGER.info("Bemfa modified flag cleared")
        except Exception:
            _LOGGER.warning("Failed to clear bemfa modified flag", exc_info=True)

    async def handle_health(self, request):
        """GET /api/health - health check endpoint."""
        ble_connected = self.ble.ctrl is not None and self.state.connected
        mqtt_connected = self.mqtt_client is not None and self.mqtt_client.is_connected()
        bemfa_connected = self.bemfa is not None and self.bemfa.is_connected
        all_healthy = bool(ble_connected or self.config.server.port > 0)
        return web.json_response({
            "ok": True,
            "healthy": all_healthy,
            "components": {
                "ble": ble_connected,
                "mqtt": mqtt_connected,
                "bemfa": bemfa_connected,
            },
            "uptime_ms": int((time.time() - getattr(self, '_start_time', time.time())) * 1000),
        })

    async def handle_bemfa(self, request):
        """GET /api/bemfa - get Bemfa status."""
        enabled = self.bemfa is not None
        connected = self.bemfa is not None and self.bemfa.is_connected
        uid = self.config.bemfa.uid
        uid_display = f"{uid[:4]}****" if len(uid) > 4 else uid
        return web.json_response({
            "enabled": enabled,
            "connected": connected,
            "uid": uid_display,
            "configured": bool(uid),
        })

    async def handle_ble_events(self, request):
        """GET /api/ble-events — 返回 BLE 连接事件日志。"""
        return web.json_response({
            "ok": True,
            "events": self.ble.get_ble_events(),
        })

    async def setup_mqtt(self):
        if not self.config.mqtt.enabled:
            _LOGGER.info("MQTT disabled, running in standalone web server mode")
            return

        import paho.mqtt.client as mqtt
        self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if self.config.mqtt.username:
            self.mqtt_client.username_pw_set(self.config.mqtt.username, self.config.mqtt.password)
        self.mqtt_client.reconnect_delay_set(min_delay=1, max_delay=60)

        def on_connect(client, userdata, flags, rc, properties=None):
            _LOGGER.info("MQTT connected (rc=%s)", rc)
            s = get_server()
            s._mqtt_connect_time = time.time()
            if s.ble:
                s.ble.set_mqtt_publisher(s.mqtt_publish)
            s.setup_mqtt_subscriptions()

        def on_disconnect(client, userdata, flags, rc, properties=None):
            _LOGGER.warning("MQTT disconnected (rc=%s)", rc)
            get_server()._mqtt_disconnect_count += 1

        self.mqtt_client.on_connect = on_connect
        self.mqtt_client.on_disconnect = on_disconnect

        self.mqtt_client.will_set(
            self.config.topic_status,
            json.dumps({"connected": False}),
            retain=True, qos=1
        )

        for attempt in range(3):
            try:
                self.mqtt_client.connect(self.config.mqtt.host, self.config.mqtt.port, self.config.mqtt.keepalive)
                self.mqtt_client.loop_start()
                _LOGGER.info("MQTT connecting to %s:%s", self.config.mqtt.host, self.config.mqtt.port)
                break
            except Exception:
                _LOGGER.error("MQTT connection failed (attempt %d/3): %s:%s",
                              attempt + 1, self.config.mqtt.host, self.config.mqtt.port)
                if attempt < 2:
                    await asyncio.sleep(3)
                else:
                    self.mqtt_client = None

        if self.mqtt_client:
            self.ble.set_mqtt_publisher(self.mqtt_publish)

    def setup_mqtt_subscriptions(self):
        if not self.mqtt_client:
            return
        server = self

        def on_mqtt_message(client, userdata, msg):
            try:
                payload = json.loads(msg.payload)
                if server.loop is None:
                    return
                if msg.topic == f"{server.config.mqtt.topic_prefix}/set":
                    piid = payload.get("piid")
                    value = payload.get("value")
                    if piid is None or value is None:
                        _LOGGER.warning("Invalid set command: missing piid or value")
                        return
                    try:
                        piid_int = int(piid)
                        value_int = int(value)
                    except (ValueError, TypeError):
                        _LOGGER.warning("Invalid set command: piid/value must be integers")
                        return
                    if piid_int not in VALID_PIIDS:
                        _LOGGER.warning("Invalid set command: piid %d not valid", piid_int)
                        return
                    min_val, max_val = PIID_RANGES[piid_int]
                    if not (min_val <= value_int <= max_val):
                        _LOGGER.warning("Invalid set command: value %d out of range [%d, %d]", value_int, min_val, max_val)
                        return
                    server.loop.call_soon_threadsafe(
                        server.ble.cmd_queue.put_nowait,
                        ("set", (piid_int, value_int), None))
                    _LOGGER.info("MQTT set command: piid=%d value=%d", piid_int, value_int)
                elif msg.topic == f"{server.config.mqtt.topic_prefix}/port":
                    port = payload.get("port")
                    action = payload.get("action")
                    if not port or action not in ("on", "off"):
                        _LOGGER.warning("Invalid port command: port=%s action=%s", port, action)
                        return
                    if port not in PORT_BITS and port != "all":
                        _LOGGER.warning("Invalid port command: unknown port %s", port)
                        return
                    server.loop.call_soon_threadsafe(
                        server.ble.cmd_queue.put_nowait,
                        ("port", (port, action), None))
                    _LOGGER.info("MQTT port command: port=%s action=%s", port, action)
                elif msg.topic == f"{server.config.mqtt.topic_prefix}/ble":
                    enabled = payload.get("enabled")
                    if enabled is None or not isinstance(enabled, bool):
                        _LOGGER.warning("Invalid BLE command: 'enabled' must be a bool")
                        return
                    _LOGGER.info("MQTT BLE command: %s", "enable" if enabled else "disable")
                    try:
                        if enabled:
                            asyncio.run_coroutine_threadsafe(server.ble.start(), server.loop)
                        else:
                            asyncio.run_coroutine_threadsafe(server.ble.request_stop(), server.loop)
                    except Exception as e:
                        _LOGGER.error("MQTT BLE cmd failed: %s", e)
            except (json.JSONDecodeError, ValueError, TypeError) as e:
                _LOGGER.error("MQTT cmd parse error: %s", e)
            except Exception as e:
                _LOGGER.error("MQTT cmd error: %s", e)

        self.mqtt_client.on_message = on_mqtt_message
        self.mqtt_client.subscribe(f"{self.config.mqtt.topic_prefix}/set")
        self.mqtt_client.subscribe(f"{self.config.mqtt.topic_prefix}/port")
        # /ble: HA "BLE连接" switch — enable/disable BLE connection
        self.mqtt_client.subscribe(f"{self.config.mqtt.topic_prefix}/ble")

        _LOGGER.info("MQTT subscriptions ready")

    def invalidate_status_cache(self):
        self._status_cache_valid = False

    def mqtt_quality(self) -> dict:
        """Return MQTT connection quality metrics."""
        if not self.mqtt_client:
            return {"score": 0, "uptime": 0, "disconnects": 0, "publish_failures": 0}
        connected = self.mqtt_client.is_connected()
        if not connected:
            return {"score": 0, "uptime": 0, "disconnects": self._mqtt_disconnect_count,
                    "publish_failures": self._mqtt_publish_failures}
        uptime = int(time.time() - self._mqtt_connect_time) if self._mqtt_connect_time else 0
        # Disconnect penalty: each disconnect costs 15 points
        dc_score = max(0, 100 - self._mqtt_disconnect_count * 15)
        # Publish failure penalty
        pf_score = max(0, 100 - self._mqtt_publish_failures * 5)
        score = round(dc_score * 0.6 + pf_score * 0.4)
        return {"score": score, "uptime": uptime, "disconnects": self._mqtt_disconnect_count,
                "publish_failures": self._mqtt_publish_failures}

    async def handle_status(self, request):
        if self._status_cache_valid and self._status_cache_bytes:
            return web.Response(
                body=self._status_cache_bytes,
                content_type="application/json",
            )
        data = await self.state.to_dict()
        data["mqtt_connected"] = self.mqtt_client is not None and self.mqtt_client.is_connected()
        self._status_cache_bytes = await asyncio.to_thread(
            lambda: json.dumps(data, ensure_ascii=False).encode()
        )
        self._status_cache_valid = True
        return web.Response(
            body=self._status_cache_bytes,
            content_type="application/json",
        )

    async def handle_set(self, request):
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        piid = data.get("piid")
        value = data.get("value")
        if piid is None or value is None:
            return web.json_response({"ok": False, "error": "missing piid or value"}, status=400)
        try:
            piid_int = int(piid)
            value_int = int(value)
        except (ValueError, TypeError):
            return web.json_response({"ok": False, "error": "piid and value must be integers"}, status=400)
        if piid_int not in VALID_PIIDS:
            return web.json_response({"ok": False, "error": f"invalid piid: {piid_int}"}, status=400)
        min_val, max_val = PIID_RANGES[piid_int]
        if not (min_val <= value_int <= max_val):
            return web.json_response({"ok": False, "error": f"value must be between {min_val} and {max_val}"}, status=400)
        result = await self.ble.send_command("set", (piid_int, value_int))
        self.invalidate_status_cache()
        return web.json_response(result)

    async def handle_port(self, request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        port = body.get("port", "").lower()
        action = body.get("action", "").lower()
        if action not in ("on", "off"):
            return web.json_response({"ok": False, "error": "action must be on/off"}, status=400)
        if port not in PORT_BITS and port != "all":
            return web.json_response({"ok": False, "error": f"unknown port: {port}"}, status=400)
        result = await self.ble.send_command("port", (port, action))
        self.invalidate_status_cache()
        return web.json_response(result)

    async def handle_protocol(self, request):
        """处理协议开关 (PIID 21)。

        请求体:
          {"port": "c1", "protocol": "pd"}           # toggle
          {"port": "c1", "protocol": "pd", "action": "on"}   # 显式开关
          {"switches": {"c1": {"pd": true, ...}}}     # 批量设置
          {"value": 50532111}                         # 直接写原始值
        """
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        try:
            state = self.ble.state
            if "value" in body:
                new_val = int(body["value"])
                if not (0 <= new_val <= 0xFFFFFFFF):
                    return web.json_response({"ok": False, "error": "value out of range (0-0xFFFFFFFF)"}, status=400)
            elif "switches" in body:
                new_val = ChargerState.encode_protocol_extend(body["switches"])
            elif "port" in body and "protocol" in body:
                port = body["port"].lower()
                proto = body["protocol"].lower()
                action = body.get("action", "toggle")

                # 加锁确保 read-modify-write-send 原子性，防止竞态
                async with state.lock:
                    current_val = state.protocol_extend
                    switches = dict(state.protocol_switches)

                    if port not in switches or proto not in switches[port]:
                        return web.json_response({"ok": False, "error": f"unknown {port}.{proto}"}, status=400)

                    cur = switches[port][proto]
                    if action == "toggle":
                        new_state = not cur
                    elif action == "on":
                        new_state = True
                    elif action == "off":
                        new_state = False
                    else:
                        return web.json_response({"ok": False, "error": f"invalid action: {action}"}, status=400)

                    _LOGGER.info("Protocol switch: %s.%s %s->%s (current 0x%08X)",
                                 port, proto, cur, new_state, current_val)

                    # 构建新的开关状态
                    new_switches = dict(switches)
                    new_switches[port] = dict(switches[port])
                    new_switches[port][proto] = new_state

                    new_val = ChargerState.encode_protocol_extend(new_switches)

                # 锁外发送 SET (send_command 内部也有 async 操作)
                result = await self.ble.send_command("set", (21, new_val))
                # 同步本地状态，确保后续 GET 读到最新值
                if result and result.get("ok"):
                    await state.update_protocol_extend(new_val)
                    if hasattr(self, 'sse'):
                        self.sse.emit("protocol", {"switches": state.protocol_switches,
                                                    "protocol_extend": new_val})
                self.invalidate_status_cache()
                return web.json_response(result)
            else:
                return web.json_response({"ok": False, "error": "missing port/protocol or value"}, status=400)

            # 批量/原始值路径：锁外发送
            result = await self.ble.send_command("set", (21, new_val))
            if result and result.get("ok"):
                await state.update_protocol_extend(new_val)
                if hasattr(self, 'sse'):
                    self.sse.emit("protocol", {"switches": state.protocol_switches,
                                               "protocol_extend": new_val})
            return web.json_response(result)
        except Exception as e:
            _LOGGER.error("Protocol switch error: %s", e)
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def handle_enable(self, request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        enabled = body.get("enabled", True)
        if enabled:
            async with self._start_lock:
                if self.ble.is_running:
                    return web.json_response({"ok": True, "enabled": True, "note": "already running"})
                app_ = request.app
                if "ble_task" in app_:
                    old = app_["ble_task"]
                    if old and not old.done():
                        old.cancel()
                        try:
                            await old
                        except asyncio.CancelledError:
                            pass
                app_["ble_task"] = asyncio.create_task(self.ble.start())
        else:
            async with self._start_lock:
                await self.ble.request_stop()
                app_ = request.app
                if "ble_task" in app_ and app_["ble_task"] and not app_["ble_task"].done():
                    try:
                        await asyncio.wait_for(app_["ble_task"], timeout=10)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                # _disconnect() (在 start() 的 finally 中) 已进行完整的 Bleak 清理，
                # 不需要额外调用 _force_disconnect_bluetooth()（仅用于错误恢复/关机）
            for piid in range(1, 5):
                await self.state.update_port(piid, PORT_DEFAULT)
            if self.mqtt_client and self.mqtt_client.is_connected():
                for piid, pname in PORT_NAMES.items():
                    self.mqtt_publish(f"{self.config.topic_port}/{pname}", PORT_DEFAULT)
                self.mqtt_publish(self.config.topic_status, {"connected": False}, retain=True)
            else:
                _LOGGER.warning("MQTT not connected, port data not cleared via MQTT")
        self.invalidate_status_cache()
        return web.json_response({"ok": True, "enabled": enabled})

    async def handle_log_level(self, request):
        """Get or set log level."""
        if request.method == "GET":
            current = logging.getLogger().level
            level_name = logging.getLevelName(current).lower()
            return web.json_response({
                "level": level_name,
                "available": list(LOG_LEVELS.keys()),
            })

        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        level = data.get("level", "").lower()
        if level not in LOG_LEVELS:
            return web.json_response({"ok": False, "error": f"invalid level: {level}"}, status=400)

        logging.getLogger().setLevel(LOG_LEVELS[level])
        # Persist to config.yaml so the change survives restart
        try:
            import yaml
            config_path = self._config_path()
            cfg = {}
            if config_path.exists():
                with open(config_path) as f:
                    cfg = yaml.safe_load(f) or {}
            if "server" not in cfg:
                cfg["server"] = {}
            cfg["server"]["log_level"] = level
            # Atomic write: write to temp file then rename
            tmp_path = config_path.with_suffix(".tmp")
            with open(tmp_path, "w") as f:
                yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
            os.replace(tmp_path, config_path)
        except Exception:
            _LOGGER.warning("Failed to persist log level to config.yaml", exc_info=True)
        _LOGGER.info("Log level changed to %s", level)
        return web.json_response({"ok": True, "level": level})

    async def handle_chart(self, request):
        """Get chart-ready data for all ports with caching and ETag."""
        try:
            hours = min(float(request.query.get("hours", 1)), 720)
        except (ValueError, TypeError):
            return web.json_response({"ok": False, "error": "invalid hours parameter"}, status=400)
        try:
            interval = max(int(request.query.get("interval", 30)), 5)
        except (ValueError, TypeError):
            return web.json_response({"ok": False, "error": "invalid interval parameter"}, status=400)
        cache_key = f"{hours}:{interval}"

        # Check cache
        now = time.time()
        entry = self._chart_cache.get(cache_key)
        if entry:
            cached_time, cached_etag, cached_body, _ = entry
            if now - cached_time < self._chart_cache_ttl:
                if_none_match = request.headers.get("If-None-Match")
                if if_none_match == cached_etag:
                    return web.Response(status=304)
                return web.Response(
                    body=cached_body,
                    content_type="application/json",
                    headers={"ETag": cached_etag},
                )

        # Generate data in thread pool (strftime + loops + json.dumps + sha256 are all CPU-bound)
        now_ts = time.time()
        start_ts = now_ts - hours * 3600
        aligned_start = (int(start_ts) // interval) * interval
        use_date = hours > 12
        aligned_now = (int(now_ts) // interval) * interval
        epochs = list(range(aligned_start, aligned_now, interval))
        raw_rows = self.history.query_history_multi(1, 4, hours, interval)

        def _build_chart(epochs_, labels, raw_rows_):
            port_data = {p: {} for p in range(1, 5)}
            for row in raw_rows_:
                port_data[row["port"]][int(row["bucket"])] = (
                    row["power"], row["voltage"], row["current"]
                )

            n = len(labels)
            power = [[0.0] * n for _ in range(5)]
            voltage = [[0.0] * n for _ in range(4)]
            current = [[0.0] * n for _ in range(4)]

            for i, epoch in enumerate(epochs_):
                total = 0.0
                for port in range(1, 5):
                    entry_ = port_data[port].get(epoch)
                    if entry_ is not None:
                        p, v, c = entry_
                        power[port - 1][i] = round(p, 1)
                        voltage[port - 1][i] = round(v, 2)
                        current[port - 1][i] = round(c, 2)
                        total += p
                power[4][i] = round(total, 1)

            port_names = ["C1", "C2", "C3", "A"]
            result = {
                "ok": True,
                "labels": labels,
                "datasets": {
                    "power": [{"label": port_names[p], "data": power[p]} for p in range(4)]
                           + [{"label": "Total", "data": power[4]}],
                    "voltage": [{"label": port_names[p], "data": voltage[p]} for p in range(4)],
                    "current": [{"label": port_names[p], "data": current[p]} for p in range(4)],
                },
            }
            body = json.dumps(result, ensure_ascii=False).encode()
            etag = hashlib.sha256(body).hexdigest()
            return body, etag

        if use_date:
            all_labels = [time.strftime('%m-%d %H:%M', time.localtime(t)) for t in epochs]
        else:
            all_labels = [time.strftime('%H:%M', time.localtime(t)) for t in epochs]

        body, etag = await asyncio.to_thread(_build_chart, epochs, all_labels, raw_rows)

        # Update cache: OrderedDict O(1) eviction
        self._chart_cache[cache_key] = (now, etag, body, now)
        if len(self._chart_cache) > self._chart_cache_max:
            self._chart_cache.popitem(last=False)

        return web.Response(
            body=body,
            content_type="application/json",
            headers={"ETag": etag},
        )

    async def handle_statistics(self, request):
        """Get port statistics."""
        try:
            port = int(request.match_info.get("port", 1))
        except ValueError:
            return web.json_response({"ok": False, "error": "invalid port"}, status=400)
        hours = min(float(request.query.get("hours", 24)), 720)

        if port not in range(1, 5):
            return web.json_response({"ok": False, "error": "invalid port"}, status=400)

        stats = self.history.get_statistics(port, int(hours))
        return web.json_response({"ok": True, "data": stats})

    async def handle_export(self, request):
        """Export port history as CSV."""
        try:
            port = int(request.match_info.get("port", 1))
        except ValueError:
            return web.json_response({"ok": False, "error": "invalid port"}, status=400)
        hours = min(float(request.query.get("hours", 24)), 720)

        if port not in range(1, 5):
            return web.json_response({"ok": False, "error": "invalid port"}, status=400)

        csv_data = self.history.export_csv(port, int(hours))
        return web.Response(
            body=csv_data,
            content_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=port_{port}_history.csv"},
        )

    MOBILE_UA = re.compile(r'Android|iPhone|iPod|webOS|BlackBerry|Windows Phone', re.I)

    async def handle_index(self, request):
        ua = request.headers.get('User-Agent', '')
        path = '/phone.html' if self.MOBILE_UA.search(ua) else '/index.html'
        entry = _static_cache.get(path)
        if entry:
            etag = entry.get("etag")
            if etag and request.headers.get("If-None-Match") == etag:
                return web.Response(status=304, headers={
                    "ETag": etag,
                    "Cache-Control": "no-cache",
                })
            accept_gzip = request.headers.get("Accept-Encoding", "").find("gzip") != -1
            if accept_gzip and entry["gzipped"]:
                body = entry["gzipped"]
                headers = {
                    "Content-Type": "text/html",
                    "Content-Encoding": "gzip",
                    "Content-Length": str(len(body)),
                    "Cache-Control": "no-cache",
                }
            else:
                body = entry["raw"]
                headers = {
                    "Content-Type": "text/html",
                    "Content-Length": str(len(body)),
                    "Cache-Control": "no-cache",
                }
            if etag:
                headers["ETag"] = etag
            return web.Response(body=body, headers=headers)
        return web.FileResponse(WEB_DIR / path.lstrip('/'))

    # ── Charge Session API ──

    async def handle_sessions(self, request):
        """GET /api/sessions?port=c1&limit=5

        按端口查询最近若干次充电会话，port 必填。
        """
        port_str = request.query.get("port", "")
        port_map = {"c1": 1, "c2": 2, "c3": 3, "a": 4}
        port = port_map.get(port_str)
        if port is None:
            return web.json_response(
                {"ok": False, "error": "缺少或非法的 port 参数，可选值 c1/c2/c3/a"},
                status=400,
            )

        try:
            limit = int(request.query.get("limit", "5"))
        except (ValueError, TypeError):
            limit = 5
        if limit < 1:
            limit = 5
        limit = min(limit, 50)

        loop = asyncio.get_running_loop()
        sessions, total = await loop.run_in_executor(
            None, self.history.get_sessions, port, limit)

        # 合并该端口的活跃会话实时数据
        now = time.time()
        live = self.ble.get_live_session_data()
        ld = live.get(port)
        live_sid = None
        if ld:
            live_sid = ld.get("session_id")
            start_time = ld.get("start_time") or now
            dur_sec = max(1, int(now - start_time))
            dur_h = dur_sec / 3600.0
            avg_p = round(ld["session_wh"] / dur_h, 1) if dur_h > 0 else 0
            port_state = self.ble.state.ports.get(port)
            avg_v = round(port_state.voltage, 2) if port_state else 0
            avg_i = round(port_state.current, 2) if port_state else 0
            matched = False
            for s in sessions:
                if s.get("id") == live_sid:
                    # 用实时数据覆盖数据库里的旧值
                    s["total_wh"] = ld["session_wh"]
                    s["peak_power_w"] = max(s.get("peak_power_w", 0), ld["max_power"])
                    s["avg_power_w"] = avg_p
                    s["voltage"] = avg_v
                    s["current"] = avg_i
                    s["duration_sec"] = dur_sec
                    s["is_active"] = True
                    matched = True
                    break
            if not matched and live_sid:
                # 活跃会话未落入结果（total_wh=0 被过滤），补到列表头部
                sessions.insert(0, {
                    "id": live_sid, "port": port, "start_time": start_time,
                    "end_time": None, "total_wh": ld["session_wh"],
                    "avg_power_w": avg_p, "peak_power_w": ld["max_power"],
                    "voltage": avg_v, "current": avg_i,
                    "duration_sec": dur_sec,
                    "protocol": port_state.protocol if port_state else "",
                    "is_active": True,
                })
                total += 1

        # 非活跃会话标记为未充电中
        for s in sessions:
            if s.get("id") != live_sid:
                s["is_active"] = False

        return web.json_response({
            "sessions": sessions,
            "total": total,
        })

    async def handle_session_points(self, request):
        """GET /api/sessions/{id}/points

        明细点已在写入时按采样间隔降采样；返回前再按窗口与点数上限裁剪，
        防止长时充电（如连插数天）的会话把接口响应与前端图表拖垮。
        """
        sid = int(request.match_info["id"])
        loop = asyncio.get_running_loop()
        points = await loop.run_in_executor(
            None, self.history.get_session_points, sid)
        return web.json_response({"points": trim_session_points(points)})

    async def handle_sessions_clear(self, request):
        """POST /api/sessions/clear — 清空全部充电会话及其明细点。"""
        loop = asyncio.get_running_loop()
        deleted = await loop.run_in_executor(
            None, self.history.clear_sessions)
        return web.json_response({"ok": True, "deleted": deleted})

    async def handle_get_charge_tracking(self, request):
        """GET /api/charge_tracking — 返回充电记录配置。"""
        ct = self.ble.config.charge_tracking
        return web.json_response({
            "enabled_ports": list(ct.enabled_ports),
            "start_power_w": {"c1": ct.start_power_w.get(1, 0.0), "c2": ct.start_power_w.get(2, 0.0)},
            "end_power_w": {"c1": ct.end_power_w.get(1, 0.0), "c2": ct.end_power_w.get(2, 0.0)},
            "end_power_duration_sec": ct.end_power_duration_sec,
            "unplug_grace_sec": ct.unplug_grace_sec,
            "point_interval_sec": ct.point_interval_sec,
        })

    async def handle_set_charge_tracking(self, request):
        """POST /api/charge_tracking — 更新充电记录配置并持久化到 config.yaml。"""
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        # 校验 enabled_ports：仅允许 1/2
        raw_ports = data.get("enabled_ports")
        if raw_ports is None:
            return web.json_response(
                {"ok": False, "error": "缺少 enabled_ports"}, status=400)
        if not isinstance(raw_ports, list):
            raw_ports = [raw_ports]
        enabled_ports = []
        for p in raw_ports:
            try:
                port = int(p)
            except (ValueError, TypeError):
                return web.json_response(
                    {"ok": False, "error": f"非法端口值: {p}"}, status=400)
            if port not in (1, 2):
                return web.json_response(
                    {"ok": False, "error": f"仅支持 C1/C2，收到 {port}"}, status=400)
            if port not in enabled_ports:
                enabled_ports.append(port)

        # 校验功率阈值：按端口独立，标量表示两口同值；必须为数字且非负
        try:
            start_power_w = parse_power_thresholds(data.get("start_power_w", 0))
        except ValueError as e:
            return web.json_response(
                {"ok": False, "error": f"start_power_w: {e}"}, status=400)
        try:
            end_power_w = parse_power_thresholds(data.get("end_power_w", 0))
        except ValueError as e:
            return web.json_response(
                {"ok": False, "error": f"end_power_w: {e}"}, status=400)

        # 校验采样间隔：最小 1 秒
        try:
            point_interval_sec = int(data.get("point_interval_sec", 60))
        except (ValueError, TypeError):
            return web.json_response(
                {"ok": False, "error": "point_interval_sec 必须为整数"}, status=400)
        if point_interval_sec < 1:
            return web.json_response(
                {"ok": False, "error": "point_interval_sec 不能小于 1"}, status=400)

        # 校验截止判定持续时长：最小 1 秒，未传时保持当前值
        try:
            end_power_duration_sec = int(
                data.get("end_power_duration_sec", self.ble.config.charge_tracking.end_power_duration_sec))
        except (ValueError, TypeError):
            return web.json_response(
                {"ok": False, "error": "end_power_duration_sec 必须为整数"}, status=400)
        if end_power_duration_sec < 1:
            return web.json_response(
                {"ok": False, "error": "end_power_duration_sec 不能小于 1"}, status=400)

        # 校验拔线容错时长：0=关闭，未传时保持当前值
        try:
            unplug_grace_sec = int(
                data.get("unplug_grace_sec", self.ble.config.charge_tracking.unplug_grace_sec))
        except (ValueError, TypeError):
            return web.json_response(
                {"ok": False, "error": "unplug_grace_sec 必须为整数"}, status=400)
        if unplug_grace_sec < 0:
            return web.json_response(
                {"ok": False, "error": "unplug_grace_sec 不能为负"}, status=400)

        # 更新内存配置（已存在的活跃会话不打断，新插拔按新配置执行）
        ct = self.ble.config.charge_tracking
        ct.enabled_ports = enabled_ports
        ct.start_power_w = start_power_w
        ct.end_power_w = end_power_w
        ct.end_power_duration_sec = end_power_duration_sec
        ct.unplug_grace_sec = unplug_grace_sec
        ct.point_interval_sec = point_interval_sec

        # 实时更新 history 的采样间隔与检测器的持续时长
        if self.ble._history:
            self.ble._history.set_point_interval(point_interval_sec)
        for pdet in self.ble._power_detectors.values():
            pdet.duration_sec = end_power_duration_sec

        # 持久化到 config.yaml
        ok, err = self._persist_charge_tracking()
        start_map = {"c1": ct.start_power_w.get(1, 0.0), "c2": ct.start_power_w.get(2, 0.0)}
        end_map = {"c1": ct.end_power_w.get(1, 0.0), "c2": ct.end_power_w.get(2, 0.0)}
        if not ok:
            return web.json_response({
                "ok": False,
                "error": f"配置已生效但持久化失败: {err}",
                "enabled_ports": list(ct.enabled_ports),
                "start_power_w": start_map,
                "end_power_w": end_map,
                "end_power_duration_sec": ct.end_power_duration_sec,
                "unplug_grace_sec": ct.unplug_grace_sec,
                "point_interval_sec": ct.point_interval_sec,
            }, status=500)

        _LOGGER.info("充电记录配置已更新: enabled_ports=%s start_power_w=%s end_power_w=%s end_dur=%ss grace=%ss",
                     enabled_ports, start_map, end_map, ct.end_power_duration_sec, ct.unplug_grace_sec)
        return web.json_response({
            "ok": True,
            "enabled_ports": list(ct.enabled_ports),
            "start_power_w": start_map,
            "end_power_w": end_map,
            "end_power_duration_sec": ct.end_power_duration_sec,
            "unplug_grace_sec": ct.unplug_grace_sec,
            "point_interval_sec": ct.point_interval_sec,
        })

    def _persist_charge_tracking(self):
        """把充电记录配置写回 config.yaml，返回 (ok, error)。"""
        try:
            import yaml
            config_path = self._config_path()
            cfg = {}
            if config_path.exists():
                with open(config_path) as f:
                    cfg = yaml.safe_load(f) or {}
            ct = self.ble.config.charge_tracking
            cfg["charge_tracking"] = {
                "enabled_ports": list(ct.enabled_ports),
                "start_power_w": {"c1": ct.start_power_w.get(1, 0.0), "c2": ct.start_power_w.get(2, 0.0)},
                "end_power_w": {"c1": ct.end_power_w.get(1, 0.0), "c2": ct.end_power_w.get(2, 0.0)},
                "end_power_duration_sec": ct.end_power_duration_sec,
                "unplug_grace_sec": ct.unplug_grace_sec,
                "point_interval_sec": ct.point_interval_sec,
            }
            # 原子写入：先写临时文件再重命名
            tmp_path = config_path.with_suffix(".tmp")
            with open(tmp_path, "w") as f:
                yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
            os.replace(tmp_path, config_path)
            return True, None
        except Exception as e:
            _LOGGER.warning("持久化充电记录配置失败", exc_info=True)
            return False, str(e)

    async def handle_sse(self, request):
        """GET /api/events — Server-Sent Events stream."""
        response = web.StreamResponse(
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )
        response.content_type = "text/event-stream"
        await response.prepare(request)

        queue: asyncio.Queue = asyncio.Queue(maxsize=SSEEmitter.MAX_QUEUE_SIZE)
        self.sse.add_client(queue)

        # Send full state on connect so client can initialize
        try:
            full_state = await self.state.to_dict()
            full_state["type"] = "init"
            full_state["mqtt_connected"] = self.mqtt_client is not None and self.mqtt_client.is_connected()
            await response.write(
                f"data: {json.dumps(full_state, ensure_ascii=False)}\n\n".encode()
            )
        except Exception as e:
            _sse_log.error("Failed to send SSE init event: %s", e)

        try:
            while True:
                # Send latest pending status before each event (stale status never delivered)
                status_msg = self.sse.flush_status(queue)
                if status_msg:
                    await response.write(f"data: {status_msg}\n\n".encode())
                # Keepalive every 15s + event wait
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    await response.write(f"data: {msg}\n\n".encode())
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
        except asyncio.CancelledError:
            _sse_log.debug("SSE handler cancelled")
        except (ConnectionError, OSError, RuntimeError) as e:
            _sse_log.debug("SSE client disconnected (%s: %s)", type(e).__name__, e)
        except asyncio.TimeoutError:
            _sse_log.debug("SSE keepalive timeout, closing connection")
        except Exception as e:
            _sse_log.warning("SSE handler error: %s: %s", type(e).__name__, e)
        finally:
            self.sse.remove_client(queue)
            _sse_log.info("SSE client cleaned up (total: %d)", len(self.sse._clients))
        return response

    # ── Configuration API ──

    @staticmethod
    def _mask(val):
        """Mask sensitive value: show first 4 + **** + last 4."""
        if not val or len(val) <= 8:
            return "****"
        return val[:4] + "****" + val[-4:]

    async def handle_config_get(self, request):
        """GET /api/config — return current config with masked sensitive fields."""
        cfg = self.config
        return web.json_response({
            "ok": True,
            "config": {
                "ble": {
                    "mac": cfg.ble.mac,
                    "token": self._mask(cfg.ble.token),
                    "ble_key": self._mask(cfg.ble.ble_key),
                    "scan_timeout": cfg.ble.scan_timeout,
                },
                "mqtt": {
                    "enabled": cfg.mqtt.enabled,
                    "host": cfg.mqtt.host,
                    "port": cfg.mqtt.port,
                    "username": cfg.mqtt.username,
                    "password": self._mask(cfg.mqtt.password),
                    "topic_prefix": cfg.mqtt.topic_prefix,
                },
                "bemfa": {
                    "enabled": cfg.bemfa.enabled,
                    "uid": self._mask(cfg.bemfa.uid),
                    "name_c1": cfg.bemfa.name_c1,
                    "name_c2": cfg.bemfa.name_c2,
                    "name_c3": cfg.bemfa.name_c3,
                    "name_a": cfg.bemfa.name_a,
                    "name_ble": cfg.bemfa.name_ble,
                },
                "server": {
                    "port": cfg.server.port,
                    "log_level": cfg.server.log_level,
                    "history_retention_days": cfg.server.history_retention_days,
                },
            },
        })

    async def handle_config_save(self, request):
        """POST /api/config — save config to config.yaml and restart."""
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        config_data = data.get("config", {})
        config_path = self._config_path()

        try:
            import yaml
            # Load existing config to preserve unknown fields
            existing = {}
            if config_path.exists():
                with open(config_path) as f:
                    existing = yaml.safe_load(f) or {}

            # Merge — skip masked placeholder values (****)
            SENSITIVE_KEYS = {"token", "ble_key", "password", "uid"}
            for section in ("ble", "mqtt", "bemfa", "server"):
                if section in config_data:
                    if section not in existing:
                        existing[section] = {}
                    for k, v in config_data[section].items():
                        if k in SENSITIVE_KEYS and v and "****" in str(v):
                            continue  # Skip masked values, keep original
                        existing[section][k] = v

            # Validate server.log_level value (prevent config corruption from frontend)
            if "server" in existing and "log_level" in existing["server"]:
                level = existing["server"]["log_level"]
                if level not in LOG_LEVELS:
                    _LOGGER.warning(
                        "Ignoring invalid log_level '%s' in config save, fallback to 'info'",
                        level,
                    )
                    existing["server"]["log_level"] = "info"

            # Detect bemfa name changes → set modified flag for topic re-registration
            if "bemfa" in config_data:
                bemfa_existing = existing.get("bemfa", {})
                NAME_KEYS = {"name_c1", "name_c2", "name_c3", "name_a", "name_ble"}
                old_names = {}
                if config_path.exists():
                    with open(config_path) as f:
                        old_cfg = yaml.safe_load(f) or {}
                        old_names = {k: old_cfg.get("bemfa", {}).get(k) for k in NAME_KEYS}
                new_names = {k: config_data["bemfa"].get(k) for k in NAME_KEYS}
                if old_names != new_names:
                    bemfa_existing["modified"] = True

            # Atomic write: write to temp file then rename
            tmp_path = config_path.with_suffix(".tmp")
            with open(tmp_path, "w") as f:
                yaml.dump(existing, f, allow_unicode=True, default_flow_style=False)
            os.replace(tmp_path, config_path)

            _LOGGER.info("Config saved to %s, restarting...", config_path)

            # Schedule restart after response (use asyncio.sleep instead of deprecated call_later+ensure_future)
            loop = asyncio.get_running_loop()
            loop.create_task(self._delayed_restart())

            return web.json_response({"ok": True, "message": "配置已保存，服务将在 1 秒后重启"})
        except ImportError:
            return web.json_response({"ok": False, "error": "yaml module not installed"}, status=500)
        except Exception as e:
            _LOGGER.error("Config save failed: %s", e)
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def _delayed_restart(self):
        """Delay 1 second then restart."""
        await asyncio.sleep(1.0)
        await self._restart()

    async def _restart(self):
        """Gracefully restart the server by re-exec-ing the process."""
        _LOGGER.info("Restarting server...")
        s = get_server()
        try:
            await asyncio.wait_for(s.ble.request_stop(), timeout=5.0)
        except Exception:
            pass
        if s.mqtt_client:
            s.mqtt_client.loop_stop()
            s.mqtt_client.disconnect()
        if s.bemfa:
            try:
                await asyncio.wait_for(s.bemfa.stop(), timeout=3.0)
            except Exception:
                pass
        s.history.close()
        # Re-exec: replace current process with fresh server
        os.execv(sys.executable, [sys.executable, str(Path(__file__).parent / "ha_server.py")])

    # ── Xiaomi Cloud API ──

    def _cleanup_xiaomi_session(self, session_id: str):
        """5 分钟超时清理：移除过期 Xiaomi session，释放相关资源。"""
        entry = self._xiaomi_sessions.pop(session_id, None)
        if entry:
            client, timer = entry
            if timer and not timer.cancelled():
                timer.cancel()
            _LOGGER.info("Xiaomi session %s timed out (5 min), cleaned up", session_id[:8])

    async def handle_xiaomi_login(self, request):
        """POST /api/xiaomi/login — start QR code login, return QR URL."""
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        server = data.get("server", "cn").strip()
        if server not in ("cn", "de", "us", "ru", "tw", "sg", "in", "i2"):
            return web.json_response({"ok": False, "error": "无效的服务器区域"}, status=400)

        try:
            import secrets as _secrets
            loop = asyncio.get_running_loop()

            def _start():
                client = QrCodeXiaomiCloudClient(server)
                return client.start_qr_login(), client

            result, client = await loop.run_in_executor(None, _start)
            session_id = _secrets.token_hex(16)
            # 5 分钟超时：启动延时清理，session 完成（beaconkey 获取）时取消
            timer = loop.call_later(300, self._cleanup_xiaomi_session, session_id)
            self._xiaomi_sessions[session_id] = (client, timer)

            return web.json_response({
                "ok": True,
                "qr_image": result["qr_image"],
                "login_url": result["login_url"],
                "session_id": session_id,
            })
        except XiaomiCloudLoginError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        except Exception as e:
            _LOGGER.error("Xiaomi QR start failed: %s", e)
            return web.json_response({"ok": False, "error": f"启动失败: {e}"}, status=500)

    async def handle_xiaomi_qr_complete(self, request):
        """POST /api/xiaomi/qr/complete — long-poll for QR scan, return devices."""
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        session_id = data.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"ok": False, "error": "缺少 session_id"}, status=400)

        entry = self._xiaomi_sessions.get(session_id)
        if not entry:
            return web.json_response({"ok": False, "error": "会话已过期，请重新获取二维码"}, status=400)
        client, _timer = entry

        try:
            loop = asyncio.get_running_loop()

            def _complete():
                client.complete_qr_login()
                return client.get_devices()

            devices = await loop.run_in_executor(None, _complete)
            # QR 扫码已完成，最耗时的阶段已过，取消超时定时器
            if _timer and not _timer.cancelled():
                _timer.cancel()
            self._xiaomi_sessions[session_id] = (client, None)  # 清除定时器引用，保留 client 供 beaconkey 使用

            cuktech_devices = [d for d in devices if "njcuk" in d.model or "fitting" in d.model]
            all_devices = [{"did": d.did, "mac": d.mac, "token": d.token,
                            "name": d.name, "model": d.model} for d in devices]

            return web.json_response({
                "ok": True,
                "devices": all_devices,
                "cuktech": [{"did": d.did, "mac": d.mac, "token": d.token,
                             "name": d.name, "model": d.model} for d in cuktech_devices],
            })
        except XiaomiCloudLoginError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        except Exception as e:
            _LOGGER.error("Xiaomi QR complete failed: %s", e)
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def handle_xiaomi_beaconkey(self, request):
        """POST /api/xiaomi/beaconkey — get BLE key for a device."""
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        session_id = data.get("session_id", "").strip()
        did = data.get("did", "").strip()

        if not session_id or not did:
            return web.json_response({"ok": False, "error": "参数不完整"}, status=400)

        entry = self._xiaomi_sessions.get(session_id)
        if not entry:
            return web.json_response({"ok": False, "error": "会话已过期，请重新扫码"}, status=400)
        client, _timer = entry

        try:
            loop = asyncio.get_running_loop()

            def _get_key():
                return client.get_beaconkey(did)

            ble_key = await loop.run_in_executor(None, _get_key)
            # Session completed — cancel timeout timer and clean up
            if _timer and not _timer.cancelled():
                _timer.cancel()
            self._xiaomi_sessions.pop(session_id, None)

            if ble_key:
                return web.json_response({"ok": True, "ble_key": ble_key})
            return web.json_response({"ok": False, "error": "未找到 BLE Key"}, status=404)
        except XiaomiCloudLoginError as e:
            if _timer and not _timer.cancelled():
                _timer.cancel()
            self._xiaomi_sessions.pop(session_id, None)
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        except Exception as e:
            _LOGGER.error("Get beaconkey failed: %s", e)
            if _timer and not _timer.cancelled():
                _timer.cancel()
            self._xiaomi_sessions.pop(session_id, None)
            return web.json_response({"ok": False, "error": str(e)}, status=500)


WEB_DIR = Path(__file__).parent / "web"
_server = None

# 会话明细点返回上限：时间窗最多一天（超过则只取最近一天滚动），
# 窗口内点数再超上限时等间隔抽稀，保证接口响应与前端图表恒定轻量
SESSION_POINTS_MAX_SPAN_SEC = 86400
SESSION_POINTS_MAX_COUNT = 500


def trim_session_points(points: list) -> list:
    """按一天滚动窗口裁剪并抽稀会话明细点，保留首尾点。"""
    if not points:
        return points
    last_ts = points[-1]["timestamp"]
    if last_ts - points[0]["timestamp"] > SESSION_POINTS_MAX_SPAN_SEC:
        cutoff = last_ts - SESSION_POINTS_MAX_SPAN_SEC
        points = [p for p in points if p["timestamp"] >= cutoff]
    if len(points) <= SESSION_POINTS_MAX_COUNT:
        return points
    step = (len(points) - 1) / (SESSION_POINTS_MAX_COUNT - 1)
    picked = sorted({round(i * step) for i in range(SESSION_POINTS_MAX_COUNT)})
    if picked[-1] != len(points) - 1:
        picked[-1] = len(points) - 1
    return [points[i] for i in picked]


# ── 静态文件缓存（启动时预加载 + 预压缩） ──
_static_cache = {}
_GZIP_TYPES = (".js", ".css", ".html", ".svg", ".json", ".txt")

# 匹配 HTML 里引用的 /static/*.js|.css 链接（含相对路径和已有 ?v= 查询）
_FINGERPRINT_RE = re.compile(
    r'(?P<pre>(?:src|href)=")(?P<path>/?static/[^"]*?\.(?:js|css))(?P<rest>[^"]*")'
)


def _fingerprint_html(raw: bytes) -> bytes:
    """把 HTML 里静态资源链接统一带上 ?v=<内容哈希>，改动静态文件后无需手动递增版本号。

    只在启动加载 HTML 时算一次 md5，运行期请求零额外计算。
    """
    text = raw.decode("utf-8")

    def repl(m):
        entry = _static_cache.get("/" + m.group("path").lstrip("/"))
        if entry is None:
            return m.group(0)
        return f'{m.group("pre")}{m.group("path")}?v={entry["fingerprint"]}"'

    return _FINGERPRINT_RE.sub(repl, text).encode("utf-8")


def _cache_static_files():
    """递归扫描 static 目录，预加载并预压缩所有文件到内存。"""
    static_dir = WEB_DIR / "static"
    if not static_dir.is_dir():
        return
    for fpath in static_dir.rglob("*"):
        if not fpath.is_file():
            continue
        rel = str(fpath.relative_to(static_dir))
        key = f"/static/{rel}"
        raw = fpath.read_bytes()
        ext = fpath.suffix.lower()
        # 按后缀推断 content-type
        mime_map = {
            ".js": "application/javascript",
            ".css": "text/css",
            ".html": "text/html",
            ".png": "image/png",
            ".ico": "image/x-icon",
            ".svg": "image/svg+xml",
            ".json": "application/json",
            ".txt": "text/plain",
            ".woff": "font/woff",
            ".woff2": "font/woff2",
            ".webp": "image/webp",
        }
        ct = mime_map.get(ext, "application/octet-stream")
        # 预计算 gzip 版本
        gzipped = None
        if ext in _GZIP_TYPES and len(raw) >= 1024:
            compressed = gzip.compress(raw)
            if len(compressed) < len(raw):
                gzipped = compressed
        _static_cache[key] = {
            "raw": raw,
            "gzipped": gzipped,
            "content_type": ct,
            # 内容哈希，供 HTML 静态资源自动版本化引用
            "fingerprint": hashlib.md5(raw).hexdigest()[:8],
        }
    # 根目录 HTML 文件也加入缓存（附内容 ETag，配合 no-cache 实现未变时 304）
    for html_name in ("index.html", "phone.html", "config.html"):
        html_path = WEB_DIR / html_name
        if html_path.is_file():
            raw = _fingerprint_html(html_path.read_bytes())
            compressed = gzip.compress(raw)
            gzipped = compressed if len(compressed) < len(raw) else None
            _static_cache[f"/{html_name}"] = {
                "raw": raw,
                "gzipped": gzipped,
                "content_type": "text/html",
                "etag": f'"{hashlib.md5(raw).hexdigest()}"',
            }


async def handle_cached_static(request):
    """从内存缓存响应静态文件，避免磁盘 I/O 和运行时 gzip。

    HTML 页面用 no-cache + ETag：页面内容更新（含引用资源的 ?v= 变化）能立即被浏览器拿到；
    静态资源保持 immutable 长缓存，更新靠引用处的 ?v= 递增。
    """
    entry = _static_cache.get(request.path)
    if entry is None:
        raise web.HTTPNotFound()
    is_html = entry["content_type"] == "text/html"
    if is_html and entry.get("etag"):
        if request.headers.get("If-None-Match") == entry["etag"]:
            return web.Response(status=304, headers={
                "ETag": entry["etag"],
                "Cache-Control": "no-cache",
            })
    accept_gzip = request.headers.get("Accept-Encoding", "").find("gzip") != -1
    if accept_gzip and entry["gzipped"]:
        body = entry["gzipped"]
        headers = {
            "Content-Type": entry["content_type"],
            "Content-Encoding": "gzip",
            "Content-Length": str(len(body)),
            "Cache-Control": "no-cache" if is_html else "public, max-age=604800, immutable",
        }
    else:
        body = entry["raw"]
        headers = {
            "Content-Type": entry["content_type"],
            "Content-Length": str(len(body)),
            "Cache-Control": "no-cache" if is_html else "public, max-age=604800, immutable",
        }
    if is_html and entry.get("etag"):
        headers["ETag"] = entry["etag"]
    return web.Response(body=body, headers=headers)


def get_server():
    """获取全局 Server 单例 (惰性初始化)。"""
    global _server
    if _server is None:
        _server = Server()
    return _server


def reset_server():
    """重置全局 Server 单例 (仅用于测试)。"""
    global _server
    _server = None


@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        response = web.Response()
    else:
        response = await handler(request)
    origin = request.headers.get("Origin", "")
    s = get_server()
    allowed_origins = {
        f"http://localhost:{s.config.server.port}",
        f"http://127.0.0.1:{s.config.server.port}",
    }
    if origin and origin in allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


@web.middleware
async def request_size_limit_middleware(request, handler):
    """Reject requests with body exceeding MAX_REQUEST_BODY_SIZE."""
    if request.method in ("POST", "PUT", "PATCH"):
        content_length = request.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > MAX_REQUEST_BODY_SIZE:
                    return web.json_response(
                        {"ok": False, "error": "Request body too large"},
                        status=413,
                    )
            except ValueError:
                pass
    return await handler(request)


@web.middleware
async def request_timeout_middleware(request, handler):
    """Apply a per-request timeout (30s for API, 120s for SSE)."""
    if request.path == "/api/events":
        timeout = 120.0
    elif request.path.startswith("/api/"):
        timeout = 30.0
    else:
        # Static files / HTML — no timeout
        return await handler(request)
    try:
        return await asyncio.wait_for(handler(request), timeout=timeout)
    except asyncio.TimeoutError:
        _LOGGER.warning("Request timeout: %s %s", request.method, request.path)
        return web.json_response(
            {"ok": False, "error": "Request timeout"},
            status=504,
        )


@web.middleware
async def gzip_middleware(request, handler):
    response = await handler(request)
    # 已预压缩的静态文件（Content-Encoding 已设置）跳过
    if response.headers.get("Content-Encoding") == "gzip":
        return response
    if not hasattr(response, 'body'):
        return response
    if request.headers.get("Accept-Encoding", "").find("gzip") == -1:
        return response
    if response.content_length is not None and response.content_length < 1024:
        return response
    if response.content_type and "text" not in response.content_type and "json" not in response.content_type:
        return response
    body = response.body
    if isinstance(body, bytes) and len(body) >= 1024:
        compressed = await asyncio.to_thread(gzip.compress, body)
        if len(compressed) < len(body):
            response.body = compressed
            response.headers["Content-Encoding"] = "gzip"
            response.headers["Content-Length"] = str(len(compressed))
    return response


@web.middleware
async def cache_middleware(request, handler):
    response = await handler(request)
    # 已由 handle_cached_static / handle_index 设置缓存头的文件跳过
    if response.headers.get("Cache-Control"):
        return response
    if request.path.startswith("/static/"):
        if request.path.endswith((".js", ".css", ".png", ".ico", ".woff", ".woff2")):
            if os.environ.get("CUKTECH_ENV") == "development":
                response.headers["Cache-Control"] = "no-cache"
            else:
                response.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return response


app = web.Application(middlewares=[
    request_size_limit_middleware,
    cors_middleware,
    gzip_middleware,
    cache_middleware,
    request_timeout_middleware,
])
app.router.add_get("/", lambda r: get_server().handle_index(r))
app.router.add_get("/phone.html", handle_cached_static)
app.router.add_get("/config.html", handle_cached_static)
app.router.add_get("/static/{tail:.*}", handle_cached_static)
app.router.add_get("/api/health", lambda r: get_server().handle_health(r))
app.router.add_get("/api/status", lambda r: get_server().handle_status(r))
app.router.add_post("/api/set", lambda r: get_server().handle_set(r))
app.router.add_post("/api/port", lambda r: get_server().handle_port(r))
app.router.add_post("/api/enable", lambda r: get_server().handle_enable(r))
app.router.add_post("/api/protocol", lambda r: get_server().handle_protocol(r))
app.router.add_get("/api/log-level", lambda r: get_server().handle_log_level(r))
app.router.add_post("/api/log-level", lambda r: get_server().handle_log_level(r))
app.router.add_get("/api/chart", lambda r: get_server().handle_chart(r))
app.router.add_get("/api/statistics/{port}", lambda r: get_server().handle_statistics(r))
app.router.add_get("/api/export/{port}", lambda r: get_server().handle_export(r))
app.router.add_get("/api/bemfa", lambda r: get_server().handle_bemfa(r))
app.router.add_get("/api/ble-events", lambda r: get_server().handle_ble_events(r))
app.router.add_get("/api/sessions", lambda r: get_server().handle_sessions(r))
app.router.add_get("/api/sessions/{id}/points", lambda r: get_server().handle_session_points(r))
app.router.add_post("/api/sessions/clear", lambda r: get_server().handle_sessions_clear(r))
app.router.add_get("/api/charge_tracking", lambda r: get_server().handle_get_charge_tracking(r))
app.router.add_post("/api/charge_tracking", lambda r: get_server().handle_set_charge_tracking(r))
app.router.add_get("/api/events", lambda r: get_server().handle_sse(r))
app.router.add_get("/api/config", lambda r: get_server().handle_config_get(r))
app.router.add_post("/api/config", lambda r: get_server().handle_config_save(r))
app.router.add_post("/api/xiaomi/login", lambda r: get_server().handle_xiaomi_login(r))
app.router.add_post("/api/xiaomi/qr/complete", lambda r: get_server().handle_xiaomi_qr_complete(r))
app.router.add_post("/api/xiaomi/beaconkey", lambda r: get_server().handle_xiaomi_beaconkey(r))


async def on_startup(app_):
    _cache_static_files()
    _LOGGER.info("Static files cached: %d files (%.1f KB raw / %.1f KB gzip)",
                 len(_static_cache),
                 sum(len(v["raw"]) for v in _static_cache.values()) / 1024,
                 sum(len(v["gzipped"] or v["raw"]) for v in _static_cache.values()) / 1024)
    s = get_server()
    async with s._start_lock:
        s.loop = asyncio.get_running_loop()
        set_status_cache_invalidator(s.invalidate_status_cache)
        s.ble.set_sse_emitter(s.sse)
        s.ble.set_quality_provider(lambda: {
            "ble": s.ble.connection_quality(),
            "mqtt": s.mqtt_quality(),
            "bemfa": s.bemfa.quality() if s.bemfa else {"score": 0, "uptime": 0, "ping_lost": 0, "reconnect_count": 0},
        })
        s.history.connect()
        s.history.set_point_interval(s.ble.config.charge_tracking.point_interval_sec)
        s.ble.set_history(s.history)
        await s.setup_mqtt()
        if s.mqtt_client:
            s.setup_mqtt_subscriptions()
        if s.config.bemfa.enabled:
            await s.setup_bemfa()
        app_["ble_task"] = asyncio.create_task(s.ble.start())


async def on_shutdown(app_):
    _LOGGER.info("Shutting down...")
    s = get_server()
    # Close active sessions first
    s.ble._close_active_sessions()
    # BLE disconnect with timeout
    try:
        await asyncio.wait_for(s.ble.request_stop(), timeout=5.0)
    except asyncio.TimeoutError:
        _LOGGER.warning("BLE stop timed out, forcing disconnect")
        await asyncio.wait_for(s.ble._disconnect(), timeout=3.0)
    except Exception as e:
        _LOGGER.error("BLE stop error: %s", e)
    if s.bemfa:
        try:
            await asyncio.wait_for(s.bemfa.stop(), timeout=3.0)
        except Exception:
            pass
    ble_task = app_.get("ble_task")
    if ble_task:
        ble_task.cancel()
        try:
            await ble_task
        except asyncio.CancelledError:
            pass
    if s.mqtt_client:
        s.mqtt_client.loop_stop()
        s.mqtt_client.disconnect()
    s.history.close()


app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    s = get_server()
    web.run_app(app, host="0.0.0.0", port=s.config.server.port)
