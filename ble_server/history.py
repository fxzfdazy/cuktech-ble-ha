"""CUKTECH BLE Server - SQLite history storage for port data."""
import asyncio
import csv
import io
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

_LOGGER = logging.getLogger("cuktech_history")

DEFAULT_RETENTION_DAYS = 2
DEFAULT_DB_PATH = "port_history.db"


class PortHistory:
    """SQLite-based port history storage."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH, retention_days: int = DEFAULT_RETENTION_DAYS):
        self.db_path = db_path
        self.retention_days = retention_days
        self._conn: Optional[sqlite3.Connection] = None
        self._db_lock = threading.Lock()  # 保护所有读写操作
        self._last_cleanup = 0
        self._last_wal_checkpoint = 0
        # charge_points 写入节流：key=session_id，value=上次写入时间戳
        self._last_point_ts: dict = {}
        # port_history 写入节流：key=port，value=上次写入时间戳
        self._last_port_ts: dict = {}
        # 降采样间隔（秒），可由 set_point_interval 修改
        self._point_interval = 30
        # 内存写入缓冲：port_history 与 charge_points 先进内存，后台线程批量落盘
        self._flush_interval = 5
        self._pending_port: list = []
        self._pending_points: list = []
        self._flush_lock = threading.Lock()
        self._flush_stop = threading.Event()
        self._flush_thread: Optional[threading.Thread] = None

    def connect(self):
        """Open database connection and create tables."""
        db_dir = Path(self.db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA wal_autocheckpoint=1000")  # checkpoint every 1000 pages
        self._create_tables()
        self._cleanup_old_data()
        # 启动后台批量落盘线程
        self._flush_stop.clear()
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True, name="history_flush")
        self._flush_thread.start()
        _LOGGER.info("History database connected: %s", self.db_path)

    def close(self):
        """Close database connection with checkpoint and graceful shutdown."""
        # 先停止 flush 线程并落盘剩余缓冲
        self._flush_stop.set()
        if self._flush_thread:
            self._flush_thread.join(timeout=10)
            self._flush_thread = None
        self._flush()
        if self._conn:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            _LOGGER.info("History database closed")

    def _create_tables(self):
        """Create database tables and run migrations."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS port_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                port INTEGER NOT NULL,
                voltage REAL,
                current REAL,
                power REAL,
                active INTEGER,
                protocol TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_port_history_port ON port_history(port);
            CREATE INDEX IF NOT EXISTS idx_port_history_timestamp ON port_history(timestamp);
            CREATE INDEX IF NOT EXISTS idx_port_history_port_time ON port_history(port, timestamp);

            CREATE TABLE IF NOT EXISTS charge_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                port INTEGER NOT NULL,
                start_time REAL NOT NULL,
                end_time REAL,
                total_wh REAL DEFAULT 0,
                avg_power_w REAL DEFAULT 0,
                peak_power_w REAL DEFAULT 0,
                avg_voltage REAL DEFAULT 0,
                avg_current REAL DEFAULT 0,
                duration_sec INTEGER DEFAULT 0,
                protocol TEXT DEFAULT '',
                created_at REAL DEFAULT (strftime('%s','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_charge_sessions_port ON charge_sessions(port);
            CREATE INDEX IF NOT EXISTS idx_charge_sessions_start ON charge_sessions(start_time);

            CREATE TABLE IF NOT EXISTS charge_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                timestamp REAL NOT NULL,
                voltage REAL,
                current REAL,
                power REAL,
                protocol TEXT DEFAULT '',
                FOREIGN KEY (session_id) REFERENCES charge_sessions(id)
            );
            CREATE INDEX IF NOT EXISTS idx_charge_points_session ON charge_points(session_id);
            CREATE INDEX IF NOT EXISTS idx_charge_points_timestamp ON charge_points(timestamp);
        """)
        # 迁移：旧库 charge_points 缺 protocol 列时补上
        try:
            self._conn.execute("SELECT protocol FROM charge_points LIMIT 1")
        except sqlite3.OperationalError:
            self._conn.execute("ALTER TABLE charge_points ADD COLUMN protocol TEXT DEFAULT ''")
            self._conn.commit()
        self._conn.commit()

    def _checkpoint_wal(self):
        """Run WAL checkpoint if enough pages have accumulated."""
        try:
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass

    def set_point_interval(self, sec: int):
        """设置 charge_points 降采样间隔（秒）。"""
        if sec >= 1:
            self._point_interval = sec

    def _flush_loop(self):
        """后台定时把内存缓冲批量写入数据库。"""
        while not self._flush_stop.is_set():
            if self._flush_stop.wait(self._flush_interval):
                break
            self._flush()

    def _flush(self):
        """把内存缓冲批量写入数据库（单次 commit）。"""
        with self._flush_lock:
            if not self._pending_port and not self._pending_points:
                return
            port_rows = self._pending_port[:]
            point_rows = self._pending_points[:]
            self._pending_port.clear()
            self._pending_points.clear()
        if not self._conn:
            return
        with self._db_lock:
            try:
                if port_rows:
                    self._conn.executemany(
                        """INSERT INTO port_history (timestamp, port, voltage, current, power, active, protocol)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""", port_rows)
                if point_rows:
                    self._conn.executemany(
                        """INSERT INTO charge_points (session_id, timestamp, voltage, current, power, protocol)
                           VALUES (?, ?, ?, ?, ?, ?)""", point_rows)
                self._conn.commit()
                now = time.time()
                if now - self._last_cleanup > 3600:
                    self._cleanup_old_data()
                    self._last_cleanup = now
                if now - self._last_wal_checkpoint > 300:
                    self._checkpoint_wal()
                    self._last_wal_checkpoint = now
            except Exception as e:
                _LOGGER.error("Flush failed: %s", e)

    def _flush_before_read(self):
        """查询前调用，确保内存缓冲已落盘，读到最新数据。"""
        self._flush()

    def _cleanup_old_data(self):
        """Remove data older than retention period."""
        cutoff = time.time() - (self.retention_days * 86400)
        # 清理超过保留期的端口实时历史
        self._conn.execute("DELETE FROM port_history WHERE timestamp < ?", (cutoff,))
        # 清理已结束且超过保留期的会话明细点
        self._conn.execute(
            """DELETE FROM charge_points WHERE session_id IN
               (SELECT id FROM charge_sessions WHERE end_time IS NOT NULL AND end_time < ?)""",
            (cutoff,))
        self._conn.commit()

    # ── Port History (功率图表 / 端口统计 / CSV 导出) ──

    def record_port_data(self, port: int, data: dict):
        """记录端口实时数据（按采样间隔降采样，写入内存缓冲批量落盘）。"""
        if not self._conn:
            return
        now = time.time()
        last = self._last_port_ts.get(port, 0)
        if now - last < self._point_interval:
            return
        self._last_port_ts[port] = now
        row = (
            now, port,
            data.get("voltage"), data.get("current"), data.get("power"),
            1 if data.get("active") else 0, data.get("protocol"),
        )
        with self._flush_lock:
            self._pending_port.append(row)

    def query_history(
        self,
        port: int,
        hours: int = 24,
        interval: Optional[int] = None
    ) -> list[dict]:
        """查询端口历史，可选降采样聚合。"""
        if not self._conn:
            return []
        self._flush_before_read()

        cutoff = time.time() - (hours * 3600)

        if interval:
            rows = self._conn.execute(
                """SELECT
                    (CAST(timestamp / ? AS INTEGER) * ?) as bucket,
                    AVG(voltage) as voltage,
                    AVG(current) as current,
                    AVG(power) as power,
                    MAX(active) as active,
                    COUNT(*) as samples
                FROM port_history
                WHERE port = ? AND timestamp >= ?
                GROUP BY bucket
                ORDER BY bucket""",
                (interval, interval, port, cutoff)
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT timestamp, voltage, current, power, active, protocol
                FROM port_history
                WHERE port = ? AND timestamp >= ?
                ORDER BY timestamp""",
                (port, cutoff)
            ).fetchall()

        return [dict(row) for row in rows]

    def get_statistics(self, port: int, hours: int = 24) -> dict:
        """获取端口统计摘要。"""
        if not self._conn:
            return {}
        self._flush_before_read()

        cutoff = time.time() - (hours * 3600)
        row = self._conn.execute(
            """SELECT
                COUNT(*) as samples,
                MIN(timestamp) as first_seen,
                MAX(timestamp) as last_seen,
                AVG(voltage) as avg_voltage,
                MAX(voltage) as max_voltage,
                MIN(voltage) as min_voltage,
                AVG(current) as avg_current,
                MAX(current) as max_current,
                AVG(power) as avg_power,
                MAX(power) as max_power,
                SUM(CASE WHEN active = 1 THEN 1 ELSE 0 END) as active_count,
                COALESCE(
                    (SELECT SUM(p.power * (p.timestamp - p.prev_ts)) / 3600.0
                     FROM (
                         SELECT timestamp, power,
                                LAG(timestamp) OVER (ORDER BY timestamp) as prev_ts
                         FROM port_history
                         WHERE port = ? AND timestamp >= ? AND active = 1
                     ) p
                     WHERE p.prev_ts IS NOT NULL),
                0) as energy_wh
            FROM port_history
            WHERE port = ? AND timestamp >= ?""",
            (port, cutoff, port, cutoff)
        ).fetchone()

        if not row or row["samples"] == 0:
            return {"port": port, "hours": hours, "samples": 0}

        return {
            "port": port,
            "hours": hours,
            "samples": row["samples"],
            "first_seen": datetime.fromtimestamp(row["first_seen"]).isoformat() if row["first_seen"] else None,
            "last_seen": datetime.fromtimestamp(row["last_seen"]).isoformat() if row["last_seen"] else None,
            "voltage": {
                "avg": round(row["avg_voltage"], 2) if row["avg_voltage"] else None,
                "min": round(row["min_voltage"], 2) if row["min_voltage"] else None,
                "max": round(row["max_voltage"], 2) if row["max_voltage"] else None,
            },
            "current": {
                "avg": round(row["avg_current"], 2) if row["avg_current"] else None,
                "max": round(row["max_current"], 2) if row["max_current"] else None,
            },
            "power": {
                "avg": round(row["avg_power"], 2) if row["avg_power"] else None,
                "max": round(row["max_power"], 2) if row["max_power"] else None,
                "total_wh": round(row["energy_wh"], 2) if row["energy_wh"] is not None else 0,
            },
            "active_ratio": round(row["active_count"] / row["samples"], 2) if row["samples"] > 0 else 0,
        }

    def export_csv(self, port: int, hours: int = 24) -> str:
        """导出端口历史为 CSV 字符串。"""
        if not self._conn:
            return ""
        self._flush_before_read()

        cutoff = time.time() - (hours * 3600)
        rows = self._conn.execute(
            """SELECT timestamp, voltage, current, power, active, protocol
            FROM port_history
            WHERE port = ? AND timestamp >= ?
            ORDER BY timestamp""",
            (port, cutoff)
        ).fetchall()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["timestamp", "datetime", "voltage", "current", "power", "active", "protocol"])

        for row in rows:
            writer.writerow([
                row["timestamp"],
                datetime.fromtimestamp(row["timestamp"]).strftime("%Y-%m-%d %H:%M:%S"),
                row["voltage"],
                row["current"],
                row["power"],
                "yes" if row["active"] else "no",
                row["protocol"],
            ])

        return output.getvalue()

    def query_history_multi(self, start_port: int, end_port: int, hours: float, interval: int) -> list[dict]:
        """单次查询多端口历史（用于功率图表）。"""
        if not self._conn:
            return []
        self._flush_before_read()

        cutoff = time.time() - (hours * 3600)
        rows = self._conn.execute(
            """SELECT port,
                (CAST(timestamp / ? AS INTEGER) * ?) as bucket,
                AVG(voltage) as voltage,
                AVG(current) as current,
                AVG(power) as power,
                COUNT(*) as samples
            FROM port_history
            WHERE port >= ? AND port <= ? AND timestamp >= ?
            GROUP BY port, bucket
            ORDER BY port, bucket""",
            (interval, interval, start_port, end_port, cutoff)
        ).fetchall()

        return [dict(row) for row in rows]

    # ── Charge Session Management ──

    def start_session(self, port: int, protocol: str = "") -> int:
        """Start a new charge session, return session_id."""
        if not self._conn:
            return 0
        with self._db_lock:
            try:
                cursor = self._conn.execute(
                    """INSERT INTO charge_sessions (port, start_time, protocol)
                       VALUES (?, ?, ?)""",
                    (port, time.time(), protocol),
                )
                self._conn.commit()
                return cursor.lastrowid
            except Exception as e:
                _LOGGER.error("Failed to start session: %s", e)
                return 0

    def record_charge_point(self, session_id: int, voltage: float,
                            current: float, power: float, protocol: str = ""):
        """Record a single data point for a charge session."""
        if not self._conn or not session_id:
            return
        # 降采样：同一会话距上次写入不足 _point_interval 秒则跳过
        last = self._last_point_ts.get(session_id, 0)
        now = time.time()
        if now - last < self._point_interval:
            return
        self._last_point_ts[session_id] = now
        row = (session_id, now, voltage, current, power, protocol)
        with self._flush_lock:
            self._pending_points.append(row)

    def clear_point_throttle(self, session_id: int):
        """清除某会话的降采样节流记录，供会话开始/结束时调用。"""
        self._last_point_ts.pop(session_id, None)

    def end_session(self, session_id: int, total_wh: float, peak_power_w: float,
                    avg_voltage: float, avg_current: float, duration_sec: int):
        """End a charge session with final stats."""
        if not self._conn or not session_id:
            return
        avg_power = total_wh / (duration_sec / 3600.0) if duration_sec > 0 else 0
        with self._db_lock:
            try:
                # 标签按采样点协议众数回写（仅结束时算一次，后续纯读取）
                # COALESCE：没有任何带协议的点时保留开始时写入的值
                top_proto = self._conn.execute(
                    """SELECT protocol FROM charge_points
                       WHERE session_id = ? AND protocol != ''
                       GROUP BY protocol ORDER BY COUNT(*) DESC LIMIT 1""",
                    (session_id,),
                ).fetchone()
                self._conn.execute(
                    """UPDATE charge_sessions SET
                       end_time = ?, total_wh = ?, avg_power_w = ?,
                       peak_power_w = ?, avg_voltage = ?, avg_current = ?,
                       duration_sec = ?, protocol = COALESCE(?, protocol)
                       WHERE id = ?""",
                    (time.time(), round(total_wh, 4), round(avg_power, 2),
                     round(peak_power_w, 2), round(avg_voltage, 2),
                     round(avg_current, 2), duration_sec,
                     top_proto[0] if top_proto else None, session_id),
                )
                self._conn.commit()
            except Exception as e:
                _LOGGER.error("Failed to end session: %s", e)

    def delete_session(self, session_id: int):
        """Delete a session and its points (for 0Wh sessions)."""
        if not self._conn or not session_id:
            return
        with self._db_lock:
            try:
                self._conn.execute("DELETE FROM charge_points WHERE session_id = ?", (session_id,))
                self._conn.execute("DELETE FROM charge_sessions WHERE id = ?", (session_id,))
                self._conn.commit()
            except Exception as e:
                _LOGGER.error("Failed to delete session: %s", e)

    def get_sessions(self, port: int, limit: int = 5) -> tuple:
        """查询某端口最近 limit 条会话，按开始时间倒序返回。

        Args:
            port: 端口号（1/2/3）
            limit: 最多返回条数
        """
        if not self._conn:
            return [], 0
        self._flush_before_read()
        rows = self._conn.execute(
            """SELECT id, port, start_time, end_time, total_wh, avg_power_w,
                      peak_power_w, avg_voltage, avg_current, duration_sec, protocol
               FROM charge_sessions
               WHERE port = ? AND total_wh > 0
               ORDER BY start_time DESC
               LIMIT ?""",
            (port, limit),
        ).fetchall()
        total = len(rows)
        return [dict(row) for row in rows], total

    def prune_sessions(self, port: int, keep: int = 5):
        """删除某端口超出 keep 条的旧会话及其明细点，保留最近 keep 条。"""
        if not self._conn:
            return
        self._flush_before_read()
        with self._db_lock:
            try:
                # 按 start_time 倒序，跳过最近 keep 条，取需删除的旧会话 id
                rows = self._conn.execute(
                    """SELECT id FROM charge_sessions
                       WHERE port = ?
                       ORDER BY start_time DESC
                       LIMIT -1 OFFSET ?""",
                    (port, keep)
                ).fetchall()
                if not rows:
                    return
                ids = [r["id"] for r in rows]
                placeholders = ",".join("?" * len(ids))
                self._conn.execute(f"DELETE FROM charge_points WHERE session_id IN ({placeholders})", ids)
                self._conn.execute(f"DELETE FROM charge_sessions WHERE id IN ({placeholders})", ids)
                self._conn.commit()
            except Exception as e:
                _LOGGER.error("Failed to prune sessions: %s", e)

    def clear_sessions(self) -> int:
        """清空已结束的充电会话及其明细点，返回被删除的会话条数。

        进行中的会话（end_time 为空）保留：内存态仍持有其 session_id，
        删掉后结束时 UPDATE 落空，本次充电的最终统计会整体丢失。
        """
        if not self._conn:
            return 0
        self._flush_before_read()
        with self._db_lock:
            try:
                active_ids = [r["id"] for r in self._conn.execute(
                    "SELECT id FROM charge_sessions WHERE end_time IS NULL"
                ).fetchall()]
                if active_ids:
                    placeholders = ",".join("?" * len(active_ids))
                    count = self._conn.execute(
                        f"SELECT COUNT(*) FROM charge_sessions WHERE id NOT IN ({placeholders})",
                        active_ids).fetchone()[0]
                    self._conn.execute(
                        f"DELETE FROM charge_points WHERE session_id NOT IN ({placeholders})",
                        active_ids)
                    self._conn.execute(
                        f"DELETE FROM charge_sessions WHERE id NOT IN ({placeholders})",
                        active_ids)
                else:
                    # 删除前统计会话条数，作为返回值
                    count = self._conn.execute(
                        "SELECT COUNT(*) FROM charge_sessions").fetchone()[0]
                    self._conn.execute("DELETE FROM charge_points")
                    self._conn.execute("DELETE FROM charge_sessions")
                self._conn.commit()
                return count
            except Exception as e:
                _LOGGER.error("Failed to clear sessions: %s", e)
                return 0

    def get_session_points(self, session_id: int) -> list[dict]:
        """Get all data points for a charge session."""
        if not self._conn:
            return []
        self._flush_before_read()
        rows = self._conn.execute(
            """SELECT timestamp, voltage, current, power, protocol
               FROM charge_points WHERE session_id = ?
               ORDER BY timestamp""",
            (session_id,),
        ).fetchall()
        return [dict(row) for row in rows]
