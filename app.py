#!/usr/bin/env python3
"""
Local macOS disk I/O monitor.

The monitor uses proc_pid_rusage(2), exposed by libproc, to read per-process
disk I/O counters without requiring sudo. It serves a local web UI and writes
periodic TXT snapshots for later comparison.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import posixpath
import signal
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
DEFAULT_LOG_ROOT = Path(
    os.environ.get("DISK_IO_MONITOR_LOG_DIR", str(ROOT / "logs"))
).expanduser()


def now_ts() -> float:
    return time.time()


def iso_time(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts or now_ts()).strftime("%Y-%m-%d %H:%M:%S")


def file_time(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts or now_ts()).strftime("%Y%m%d-%H%M%S")


def format_bytes(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(max(0, value))
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


def format_duration_seconds(seconds: float) -> str:
    safe = max(0, int(round(seconds)))
    minutes, sec = divmod(safe, 60)
    hours, minute = divmod(minutes, 60)
    if hours:
        if minute == 0 and sec == 0:
            return f"{safe} 秒（{hours} 小时）"
        return f"{safe} 秒（{hours} 小时 {minute} 分钟）"
    if minute:
        if sec == 0:
            return f"{safe} 秒（{minute} 分钟）"
        return f"{safe} 秒（{minute} 分钟 {sec} 秒）"
    return f"{safe} 秒"


def parse_clock(value: object, fallback: str) -> str:
    text = str(value or "").strip()
    try:
        datetime.strptime(text, "%H:%M")
        return text
    except ValueError:
        return fallback


def clock_parts(clock: str) -> Tuple[int, int]:
    hour, minute = clock.split(":", 1)
    return int(hour), int(minute)


def next_daily_report_at(after_ts: float, clock: str) -> float:
    hour, minute = clock_parts(clock)
    current = datetime.fromtimestamp(after_ts)
    boundary = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if after_ts < boundary.timestamp():
        return boundary.timestamp()
    return (boundary + timedelta(days=1)).timestamp()


def next_weekly_report_at(after_ts: float, weekday: int, clock: str) -> float:
    hour, minute = clock_parts(clock)
    current = datetime.fromtimestamp(after_ts)
    week_start = current.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=current.weekday()
    )
    boundary = week_start + timedelta(days=weekday, hours=hour, minutes=minute)
    if after_ts < boundary.timestamp():
        return boundary.timestamp()
    return (boundary + timedelta(days=7)).timestamp()


def weekday_name(weekday: int) -> str:
    names = ["一", "二", "三", "四", "五", "六", "日"]
    return names[min(6, max(0, weekday))]


@dataclass
class ProcessSample:
    pid: int
    name: str
    app_name: str
    path: str
    start_abstime: int
    read_bytes: int
    write_bytes: int
    sampled_at: float

    @property
    def pid_key(self) -> str:
        return f"{self.pid}:{self.start_abstime}"

    @property
    def app_key(self) -> str:
        return f"{self.app_name}\x1f{self.path}"


class RUsageInfoV2(ctypes.Structure):
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
        ("ri_child_user_time", ctypes.c_uint64),
        ("ri_child_system_time", ctypes.c_uint64),
        ("ri_child_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_child_interrupt_wkups", ctypes.c_uint64),
        ("ri_child_pageins", ctypes.c_uint64),
        ("ri_child_elapsed_abstime", ctypes.c_uint64),
        ("ri_diskio_bytesread", ctypes.c_uint64),
        ("ri_diskio_byteswritten", ctypes.c_uint64),
    ]


class MacProcessReader:
    def __init__(self) -> None:
        if os.uname().sysname != "Darwin":
            raise RuntimeError("This monitor currently supports macOS only.")

        self.libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        self.libproc.proc_pid_rusage.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        self.libproc.proc_pid_rusage.restype = ctypes.c_int

    def iter_processes(self) -> Iterable[ProcessSample]:
        timestamp = now_ts()
        try:
            output = subprocess.check_output(
                ["ps", "-axo", "pid=,comm="],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return []

        samples: List[ProcessSample] = []
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if not parts or not parts[0].isdigit():
                continue

            pid = int(parts[0])
            if pid == os.getpid():
                continue
            command_path = parts[1] if len(parts) > 1 else f"pid-{pid}"
            info = RUsageInfoV2()
            if self.libproc.proc_pid_rusage(pid, 2, ctypes.byref(info)) != 0:
                continue

            name = process_name(command_path)
            app = app_display_name(command_path, name)
            samples.append(
                ProcessSample(
                    pid=pid,
                    name=name,
                    app_name=app,
                    path=command_path,
                    start_abstime=int(info.ri_proc_start_abstime),
                    read_bytes=int(info.ri_diskio_bytesread),
                    write_bytes=int(info.ri_diskio_byteswritten),
                    sampled_at=timestamp,
                )
            )
        return samples


def process_name(path: str) -> str:
    if path.startswith("<") and path.endswith(">"):
        return path
    base = os.path.basename(path.rstrip("/"))
    return base or path or "unknown"


def app_display_name(path: str, fallback: str) -> str:
    for piece in Path(path).parts:
        if piece.endswith(".app"):
            return piece[:-4]
    return fallback


class DiskIOMonitor:
    def __init__(self, sample_interval: float, log_interval: float) -> None:
        self.reader = MacProcessReader()
        self.sample_interval = max(1.0, sample_interval)
        self.log_interval = max(60.0, log_interval)
        self.log_enabled = True
        self.started_at = now_ts()
        self.last_sample_at = 0.0
        self.next_log_at = self.started_at + self.log_interval
        self.last_log_at = self.started_at
        self.status = "starting"
        self.error = ""
        config_path = os.environ.get("DISK_IO_MONITOR_CONFIG_PATH")
        self.config_path = Path(config_path).expanduser() if config_path else None
        self.config = self._load_config()
        self.log_root = self._configured_log_root()
        self.monitor_during_sleep = bool(self.config.get("monitor_during_sleep", False))
        self.alert_enabled = bool(self.config.get("alert_enabled", False))
        self.alert_window_seconds = min(
            24 * 3600.0,
            max(60.0, float(self.config.get("alert_window_seconds", 600))),
        )
        self.alert_threshold_bytes = max(
            0,
            int(float(self.config.get("alert_threshold_bytes", 10 * 1024**3))),
        )
        self.daily_report_enabled = bool(self.config.get("daily_report_enabled", False))
        self.daily_report_time = parse_clock(self.config.get("daily_report_time"), "23:55")
        self.weekly_report_enabled = bool(self.config.get("weekly_report_enabled", False))
        self.weekly_report_day = min(6, max(0, int(self.config.get("weekly_report_day", 6))))
        self.weekly_report_time = parse_clock(self.config.get("weekly_report_time"), "23:55")
        self.hidden_items = self._normalize_hidden_items(self.config.get("hidden_items", []))
        self.sleep_paused = False
        self.sleep_pause_remaining: Optional[float] = None

        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None

        self.baselines: Dict[str, Tuple[int, int, str]] = {}
        self.last_values: Dict[str, Tuple[int, int, float]] = {}
        self.current_rates: Dict[str, Tuple[int, int]] = {}
        self.current: Dict[str, ProcessSample] = {}
        self.completed: Dict[str, Dict[str, object]] = {}
        self.last_log_session: Dict[str, Tuple[int, int]] = {}
        self.last_log_totals = (0, 0)
        self.final_log_written = False
        self.sample_history: List[Dict[str, float]] = []
        self.alert_messages: List[Dict[str, object]] = []
        self.alert_last_trigger_at = 0.0
        self.alert_counter = 0
        self.daily_period_start = self.started_at
        self.weekly_period_start = self.started_at
        self.next_daily_report_at = next_daily_report_at(
            self.started_at,
            self.daily_report_time,
        )
        self.next_weekly_report_at = next_weekly_report_at(
            self.started_at,
            self.weekly_report_day,
            self.weekly_report_time,
        )
        self.daily_period_session: Dict[str, Tuple[int, int]] = {}
        self.weekly_period_session: Dict[str, Tuple[int, int]] = {}
        self.daily_period_totals = (0, 0)
        self.weekly_period_totals = (0, 0)

        self.log_root.mkdir(parents=True, exist_ok=True)

    def _load_config(self) -> Dict[str, object]:
        if not self.config_path or not self.config_path.exists():
            return {}
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
            return config if isinstance(config, dict) else {}
        except Exception:
            return {}

    def _configured_log_root(self) -> Path:
        path = str(self.config.get("log_directory", "")).strip()
        return Path(path).expanduser() if path else DEFAULT_LOG_ROOT

    def _save_config_locked(self) -> None:
        if not self.config_path:
            return
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "log_directory": str(self.log_root),
            "monitor_during_sleep": self.monitor_during_sleep,
            "alert_enabled": self.alert_enabled,
            "alert_window_seconds": self.alert_window_seconds,
            "alert_threshold_bytes": self.alert_threshold_bytes,
            "daily_report_enabled": self.daily_report_enabled,
            "daily_report_time": self.daily_report_time,
            "weekly_report_enabled": self.weekly_report_enabled,
            "weekly_report_day": self.weekly_report_day,
            "weekly_report_time": self.weekly_report_time,
            "hidden_items": self.hidden_items,
        }
        self.config_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _normalize_hidden_items(self, value: object) -> List[Dict[str, str]]:
        if not isinstance(value, list):
            return []
        items = []
        seen = set()
        for raw in value:
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("key", "")).strip()
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "key": key,
                    "app": str(raw.get("app", "")).strip()[:240],
                    "path": str(raw.get("path", "")).strip()[:2000],
                }
            )
        return items[:500]

    def start(self) -> None:
        self.sample_once()
        self.thread = threading.Thread(target=self._run, name="disk-io-monitor", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            began = now_ts()
            self.sample_once()
            if not self.sleep_paused and self.log_enabled and now_ts() >= self.next_log_at:
                self.write_log(reason="scheduled")
                self.next_log_at = now_ts() + self.log_interval
            if not self.sleep_paused:
                with self.lock:
                    self._check_reports_locked(now_ts())
            elapsed = now_ts() - began
            self.stop_event.wait(max(0.2, self.sample_interval - elapsed))

    def sample_once(self) -> None:
        try:
            samples = list(self.reader.iter_processes())
            sample_map = {sample.pid_key: sample for sample in samples}
            with self.lock:
                self._archive_exited(sample_map)
                for sample in samples:
                    self.baselines.setdefault(
                        sample.pid_key,
                        (sample.read_bytes, sample.write_bytes, sample.app_key),
                    )
                    prev = self.last_values.get(sample.pid_key)
                    if prev:
                        prev_read, prev_write, prev_time = prev
                        dt = max(0.001, sample.sampled_at - prev_time)
                        self.current_rates[sample.pid_key] = (
                            max(0, int((sample.read_bytes - prev_read) / dt)),
                            max(0, int((sample.write_bytes - prev_write) / dt)),
                        )
                    else:
                        self.current_rates[sample.pid_key] = (0, 0)
                    self.last_values[sample.pid_key] = (
                        sample.read_bytes,
                        sample.write_bytes,
                        sample.sampled_at,
                    )
                    self.current[sample.pid_key] = sample
                self.last_sample_at = now_ts()
                self.status = "running"
                self.error = ""
                self._record_history_and_alerts_locked()
        except Exception as exc:
            with self.lock:
                self.status = "error"
                self.error = str(exc)

    def _archive_exited(self, sample_map: Dict[str, ProcessSample]) -> None:
        gone_keys = [pid_key for pid_key in self.current if pid_key not in sample_map]
        for pid_key in gone_keys:
            sample = self.current.pop(pid_key)
            base_read, base_write, app_key = self.baselines.get(
                pid_key,
                (sample.read_bytes, sample.write_bytes, sample.app_key),
            )
            read_delta = max(0, sample.read_bytes - base_read)
            write_delta = max(0, sample.write_bytes - base_write)
            bucket = self.completed.setdefault(
                app_key,
                {
                    "app": sample.app_name,
                    "path": sample.path,
                    "read": 0,
                    "write": 0,
                    "completed_count": 0,
                    "last_seen": sample.sampled_at,
                },
            )
            bucket["read"] = int(bucket["read"]) + read_delta
            bucket["write"] = int(bucket["write"]) + write_delta
            bucket["completed_count"] = int(bucket["completed_count"]) + 1
            bucket["last_seen"] = max(float(bucket["last_seen"]), sample.sampled_at)
            self.baselines.pop(pid_key, None)
            self.last_values.pop(pid_key, None)
            self.current_rates.pop(pid_key, None)

    def set_settings(
        self,
        sample_interval: Optional[float] = None,
        log_interval_minutes: Optional[float] = None,
        log_enabled: Optional[bool] = None,
        log_directory: Optional[str] = None,
        monitor_during_sleep: Optional[bool] = None,
        alert_enabled: Optional[bool] = None,
        alert_window_minutes: Optional[float] = None,
        alert_threshold_gb: Optional[float] = None,
        daily_report_enabled: Optional[bool] = None,
        daily_report_time: Optional[str] = None,
        weekly_report_enabled: Optional[bool] = None,
        weekly_report_day: Optional[int] = None,
        weekly_report_time: Optional[str] = None,
        hidden_items: Optional[object] = None,
    ) -> None:
        with self.lock:
            if sample_interval is not None:
                self.sample_interval = min(300.0, max(1.0, float(sample_interval)))
            if log_interval_minutes is not None:
                self.log_interval = min(24 * 3600.0, max(60.0, float(log_interval_minutes) * 60))
            if log_enabled is not None:
                self.log_enabled = bool(log_enabled)
            if log_directory is not None:
                new_root = Path(str(log_directory)).expanduser()
                if not str(new_root).strip():
                    raise ValueError("日志文件夹路径不能为空。")
                new_root.mkdir(parents=True, exist_ok=True)
                if not new_root.is_dir():
                    raise ValueError("日志路径不是文件夹。")
                self.log_root = new_root
                self._save_config_locked()
            if monitor_during_sleep is not None:
                self.monitor_during_sleep = bool(monitor_during_sleep)
                self._save_config_locked()
            changed_persistent_settings = False
            old_daily_enabled = self.daily_report_enabled
            old_daily_time = self.daily_report_time
            old_weekly_enabled = self.weekly_report_enabled
            old_weekly_day = self.weekly_report_day
            old_weekly_time = self.weekly_report_time
            if alert_enabled is not None:
                self.alert_enabled = bool(alert_enabled)
                changed_persistent_settings = True
            if alert_window_minutes is not None:
                self.alert_window_seconds = min(
                    24 * 3600.0,
                    max(60.0, float(alert_window_minutes) * 60),
                )
                changed_persistent_settings = True
            if alert_threshold_gb is not None:
                self.alert_threshold_bytes = max(
                    0,
                    int(float(alert_threshold_gb) * 1024**3),
                )
                changed_persistent_settings = True
            if daily_report_enabled is not None:
                self.daily_report_enabled = bool(daily_report_enabled)
                changed_persistent_settings = True
            if daily_report_time is not None:
                self.daily_report_time = parse_clock(daily_report_time, self.daily_report_time)
                changed_persistent_settings = True
            if weekly_report_enabled is not None:
                self.weekly_report_enabled = bool(weekly_report_enabled)
                changed_persistent_settings = True
            if weekly_report_day is not None:
                self.weekly_report_day = min(6, max(0, int(weekly_report_day)))
                changed_persistent_settings = True
            if weekly_report_time is not None:
                self.weekly_report_time = parse_clock(weekly_report_time, self.weekly_report_time)
                changed_persistent_settings = True
            if hidden_items is not None:
                self.hidden_items = self._normalize_hidden_items(hidden_items)
                changed_persistent_settings = True
            daily_changed = (
                self.daily_report_enabled != old_daily_enabled
                or self.daily_report_time != old_daily_time
            )
            weekly_changed = (
                self.weekly_report_enabled != old_weekly_enabled
                or self.weekly_report_day != old_weekly_day
                or self.weekly_report_time != old_weekly_time
            )
            if daily_changed and self.daily_report_enabled:
                self._reset_report_period_locked("daily", now_ts())
            if weekly_changed and self.weekly_report_enabled:
                self._reset_report_period_locked("weekly", now_ts())
            if changed_persistent_settings:
                self._save_config_locked()

    def power_event(self, event: str) -> None:
        with self.lock:
            if event == "sleep":
                if not self.monitor_during_sleep and not self.sleep_paused:
                    self.sleep_pause_remaining = max(0.0, self.next_log_at - now_ts())
                    self.sleep_paused = True
            elif event == "wake":
                if self.sleep_paused:
                    remaining = self.sleep_pause_remaining
                    if remaining is None:
                        remaining = self.log_interval
                    self.next_log_at = now_ts() + max(0.0, remaining)
                    self.sleep_paused = False
                    self.sleep_pause_remaining = None
            else:
                raise ValueError("未知电源事件。")

    def delete_alert(self, alert_id: str) -> bool:
        with self.lock:
            before = len(self.alert_messages)
            self.alert_messages = [
                alert for alert in self.alert_messages if str(alert.get("id")) != alert_id
            ]
            return len(self.alert_messages) != before

    def _record_history_and_alerts_locked(self) -> None:
        _, totals = self._aggregate_locked()
        timestamp = now_ts()
        snapshot = {
            "at": timestamp,
            "read": float(totals["session_read_bytes"]),
            "write": float(totals["session_write_bytes"]),
        }
        self.sample_history.append(snapshot)
        retention_seconds = max(8 * 24 * 3600.0, self.alert_window_seconds * 2)
        cutoff = timestamp - retention_seconds
        self.sample_history = [item for item in self.sample_history if item["at"] >= cutoff]

        if not self.alert_enabled or self.alert_threshold_bytes <= 0:
            return

        window_start_at = timestamp - self.alert_window_seconds
        start = self.sample_history[0]
        for item in self.sample_history:
            if item["at"] >= window_start_at:
                start = item
                break

        read_delta = max(0, int(snapshot["read"] - start["read"]))
        write_delta = max(0, int(snapshot["write"] - start["write"]))
        total_delta = read_delta + write_delta
        if total_delta < self.alert_threshold_bytes:
            return
        if timestamp - self.alert_last_trigger_at < self.alert_window_seconds:
            return

        self.alert_last_trigger_at = timestamp
        self.alert_counter += 1
        title = "磁盘读写达到提醒阈值"
        body = (
            f"{format_duration_seconds(timestamp - start['at'])} 内读写合计 "
            f"{format_bytes(total_delta)}，已达到阈值 {format_bytes(self.alert_threshold_bytes)}。"
        )
        self.alert_messages.insert(
            0,
            {
                "id": f"alert-{int(timestamp)}-{self.alert_counter}",
                "created_at": timestamp,
                "created_text": iso_time(timestamp),
                "title": title,
                "body": body,
                "window_start_at": start["at"],
                "window_start_text": iso_time(start["at"]),
                "window_end_at": timestamp,
                "window_end_text": iso_time(timestamp),
                "duration_seconds": int(timestamp - start["at"]),
                "read_bytes": read_delta,
                "write_bytes": write_delta,
                "total_bytes": total_delta,
                "threshold_bytes": self.alert_threshold_bytes,
            },
        )
        self.alert_messages = self.alert_messages[:200]

    def _check_reports_locked(self, timestamp: float) -> None:
        if self.daily_report_enabled and timestamp >= self.next_daily_report_at:
            self.write_report_locked("daily", timestamp, self.next_daily_report_at)

        if self.weekly_report_enabled and timestamp >= self.next_weekly_report_at:
            self.write_report_locked("weekly", timestamp, self.next_weekly_report_at)

    def _current_session_snapshot_locked(
        self,
        rows: List[Dict[str, object]],
    ) -> Dict[str, Tuple[int, int]]:
        return {
            str(row["app_key"]): (
                int(row["session_read_bytes"]),
                int(row["session_write_bytes"]),
            )
            for row in rows
        }

    def _current_total_snapshot(self, totals: Dict[str, int]) -> Tuple[int, int]:
        return (
            int(totals["session_read_bytes"]),
            int(totals["session_write_bytes"]),
        )

    def _reset_report_period_locked(self, kind: str, timestamp: float) -> None:
        rows, totals = self._aggregate_locked()
        current_session = self._current_session_snapshot_locked(rows)
        current_totals = self._current_total_snapshot(totals)

        if kind == "daily":
            self.daily_period_start = timestamp
            self.daily_period_session = dict(current_session)
            self.daily_period_totals = current_totals
            self.next_daily_report_at = next_daily_report_at(timestamp, self.daily_report_time)
        elif kind == "weekly":
            self.weekly_period_start = timestamp
            self.weekly_period_session = dict(current_session)
            self.weekly_period_totals = current_totals
            self.next_weekly_report_at = next_weekly_report_at(
                timestamp,
                self.weekly_report_day,
                self.weekly_report_time,
            )
        else:
            raise ValueError("未知报告类型。")

    def state(self) -> Dict[str, object]:
        with self.lock:
            rows, totals = self._aggregate_locked()
            return {
                "now": now_ts(),
                "now_text": iso_time(),
                "started_at": self.started_at,
                "started_text": iso_time(self.started_at),
                "uptime_seconds": max(0, now_ts() - self.started_at),
                "status": self.status,
                "error": self.error,
                "sample_interval_seconds": self.sample_interval,
                "log_interval_seconds": self.log_interval,
                "log_enabled": self.log_enabled,
                "next_log_at": self.next_log_at,
                "next_log_in_seconds": max(0, self.next_log_at - now_ts()),
                "last_log_at": self.last_log_at,
                "last_log_text": iso_time(self.last_log_at),
                "last_sample_at": self.last_sample_at,
                "last_sample_text": iso_time(self.last_sample_at) if self.last_sample_at else "",
                "log_directory": str(self.log_root),
                "monitor_during_sleep": self.monitor_during_sleep,
                "sleep_paused": self.sleep_paused,
                "alert_enabled": self.alert_enabled,
                "alert_window_seconds": self.alert_window_seconds,
                "alert_threshold_bytes": self.alert_threshold_bytes,
                "alert_threshold_gb": self.alert_threshold_bytes / 1024**3,
                "alerts": list(self.alert_messages),
                "daily_report_enabled": self.daily_report_enabled,
                "daily_report_time": self.daily_report_time,
                "daily_period_start_at": self.daily_period_start,
                "daily_period_start_text": iso_time(self.daily_period_start),
                "next_daily_report_at": self.next_daily_report_at,
                "next_daily_report_text": iso_time(self.next_daily_report_at),
                "next_daily_report_in_seconds": max(0, self.next_daily_report_at - now_ts()),
                "weekly_report_enabled": self.weekly_report_enabled,
                "weekly_report_day": self.weekly_report_day,
                "weekly_report_time": self.weekly_report_time,
                "weekly_period_start_at": self.weekly_period_start,
                "weekly_period_start_text": iso_time(self.weekly_period_start),
                "next_weekly_report_at": self.next_weekly_report_at,
                "next_weekly_report_text": iso_time(self.next_weekly_report_at),
                "next_weekly_report_in_seconds": max(0, self.next_weekly_report_at - now_ts()),
                "hidden_items": list(self.hidden_items),
                "running_processes": len(self.current),
                "tracked_apps": len(rows),
                "totals": totals,
                "rows": rows,
            }

    def _aggregate_locked(self) -> Tuple[List[Dict[str, object]], Dict[str, int]]:
        apps: Dict[str, Dict[str, object]] = {}
        for app_key, bucket in self.completed.items():
            apps[app_key] = {
                "app": bucket["app"],
                "path": bucket["path"],
                "pids": [],
                "process_count": 0,
                "completed_count": int(bucket["completed_count"]),
                "session_read_bytes": int(bucket["read"]),
                "session_write_bytes": int(bucket["write"]),
                "lifetime_read_bytes": 0,
                "lifetime_write_bytes": 0,
                "read_rate_bps": 0,
                "write_rate_bps": 0,
                "last_seen": float(bucket["last_seen"]),
            }

        for pid_key, sample in self.current.items():
            base_read, base_write, app_key = self.baselines.get(
                pid_key,
                (sample.read_bytes, sample.write_bytes, sample.app_key),
            )
            row = apps.setdefault(
                app_key,
                {
                    "app": sample.app_name,
                    "path": sample.path,
                    "pids": [],
                    "process_count": 0,
                    "completed_count": 0,
                    "session_read_bytes": 0,
                    "session_write_bytes": 0,
                    "lifetime_read_bytes": 0,
                    "lifetime_write_bytes": 0,
                    "read_rate_bps": 0,
                    "write_rate_bps": 0,
                    "last_seen": sample.sampled_at,
                },
            )
            row["pids"].append(sample.pid)
            row["process_count"] = int(row["process_count"]) + 1
            row["session_read_bytes"] = int(row["session_read_bytes"]) + max(
                0, sample.read_bytes - base_read
            )
            row["session_write_bytes"] = int(row["session_write_bytes"]) + max(
                0, sample.write_bytes - base_write
            )
            row["lifetime_read_bytes"] = int(row["lifetime_read_bytes"]) + sample.read_bytes
            row["lifetime_write_bytes"] = int(row["lifetime_write_bytes"]) + sample.write_bytes
            row["last_seen"] = max(float(row["last_seen"]), sample.sampled_at)

            read_rate, write_rate = self.current_rates.get(pid_key, (0, 0))
            row["read_rate_bps"] = int(row["read_rate_bps"]) + read_rate
            row["write_rate_bps"] = int(row["write_rate_bps"]) + write_rate

        rows = []
        for app_key, row in apps.items():
            row["app_key"] = app_key
            row["pids"] = sorted(row["pids"])
            row["session_total_bytes"] = int(row["session_read_bytes"]) + int(
                row["session_write_bytes"]
            )
            row["lifetime_total_bytes"] = int(row["lifetime_read_bytes"]) + int(
                row["lifetime_write_bytes"]
            )
            rows.append(row)

        rows.sort(key=lambda item: int(item["session_write_bytes"]), reverse=True)
        totals = {
            "session_read_bytes": sum(int(row["session_read_bytes"]) for row in rows),
            "session_write_bytes": sum(int(row["session_write_bytes"]) for row in rows),
            "session_total_bytes": sum(int(row["session_total_bytes"]) for row in rows),
            "lifetime_read_bytes": sum(int(row["lifetime_read_bytes"]) for row in rows),
            "lifetime_write_bytes": sum(int(row["lifetime_write_bytes"]) for row in rows),
            "read_rate_bps": sum(int(row["read_rate_bps"]) for row in rows),
            "write_rate_bps": sum(int(row["write_rate_bps"]) for row in rows),
        }
        return rows, totals

    def write_log(self, reason: str = "manual") -> Path:
        with self.lock:
            log_time = now_ts()
            interval_start = self.last_log_at or self.started_at
            rows, totals = self._aggregate_locked()
            current_session = {
                str(row["app_key"]): (
                    int(row["session_read_bytes"]),
                    int(row["session_write_bytes"]),
                )
                for row in rows
            }
            previous_session = dict(self.last_log_session)
            previous_totals = self.last_log_totals

            lines = self._format_log_lines(
                rows=rows,
                totals=totals,
                current_session=current_session,
                previous_session=previous_session,
                previous_totals=previous_totals,
                reason=reason,
                interval_start=interval_start,
                interval_end=log_time,
            )
            self.log_root.mkdir(parents=True, exist_ok=True)
            path = self.log_root / f"disk-io-{file_time(log_time)}.txt"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            self.last_log_session = current_session
            self.last_log_totals = (
                int(totals["session_read_bytes"]),
                int(totals["session_write_bytes"]),
            )
            self.last_log_at = log_time
            return path

    def write_final_log(self, reason: str = "shutdown") -> Optional[Path]:
        with self.lock:
            if self.final_log_written:
                return None
            self.final_log_written = True
        return self.write_log(reason=reason)

    def _format_log_lines(
        self,
        rows: List[Dict[str, object]],
        totals: Dict[str, int],
        current_session: Dict[str, Tuple[int, int]],
        previous_session: Dict[str, Tuple[int, int]],
        previous_totals: Tuple[int, int],
        reason: str,
        interval_start: float,
        interval_end: float,
    ) -> List[str]:
        interval_read = max(0, int(totals["session_read_bytes"]) - previous_totals[0])
        interval_write = max(0, int(totals["session_write_bytes"]) - previous_totals[1])
        interval_seconds = max(0, interval_end - interval_start)
        lines = [
            "硬盘读写监控 TXT 日志",
            f"记录时间: {iso_time(interval_end)}",
            f"记录原因: {reason}",
            "",
            "监控启动:",
            iso_time(self.started_at),
            "",
            "本次统计区间:",
            iso_time(interval_start),
            "~",
            iso_time(interval_end),
            "",
            "统计时长:",
            format_duration_seconds(interval_seconds),
            "",
            f"采样间隔: {self.sample_interval:.0f} 秒",
            "",
            "自监控启动后的总量:",
            f"  读取: {format_bytes(totals['session_read_bytes'])}",
            f"  写入: {format_bytes(totals['session_write_bytes'])}",
            f"  合计: {format_bytes(totals['session_total_bytes'])}",
            "",
            "较上一次日志新增:",
            f"  读取: {format_bytes(interval_read)}",
            f"  写入: {format_bytes(interval_write)}",
            f"  合计: {format_bytes(interval_read + interval_write)}",
            "",
            "进程/应用排名，按较上一次日志新增写入排序:",
            (
                "应用/进程\t完整应用/进程名\tPID\t本段写入\t本段读取\t"
                "启动后写入\t启动后读取\t当前写速\t当前读速\t完整可执行文件路径"
            ),
        ]

        ranked = []
        for row in rows:
            key = str(row["app_key"])
            prev_read, prev_write = previous_session.get(key, (0, 0))
            current_read, current_write = current_session.get(key, (0, 0))
            row_copy = dict(row)
            row_copy["interval_read"] = max(0, current_read - prev_read)
            row_copy["interval_write"] = max(0, current_write - prev_write)
            ranked.append(row_copy)
        ranked.sort(key=lambda item: int(item["interval_write"]), reverse=True)

        for row in ranked:
            pids = ",".join(str(pid) for pid in row["pids"]) or "-"
            lines.append(
                "\t".join(
                    [
                        str(row["app"]),
                        str(row["app"]),
                        pids,
                        format_bytes(int(row["interval_write"])),
                        format_bytes(int(row["interval_read"])),
                        format_bytes(int(row["session_write_bytes"])),
                        format_bytes(int(row["session_read_bytes"])),
                        f"{format_bytes(int(row['write_rate_bps']))}/s",
                        f"{format_bytes(int(row['read_rate_bps']))}/s",
                        str(row["path"]),
                    ]
                )
            )
        return lines

    def write_report_locked(
        self,
        kind: str,
        generated_at: float,
        period_end: Optional[float] = None,
    ) -> Path:
        period_end = generated_at if period_end is None else period_end
        rows, totals = self._aggregate_locked()
        current_session = self._current_session_snapshot_locked(rows)
        current_totals = self._current_total_snapshot(totals)

        if kind == "daily":
            title = "硬盘读写监控 日报"
            period_label = f"日报周期（每日 {self.daily_report_time} 作为周期边界）"
            period_start = self.daily_period_start
            previous_session = dict(self.daily_period_session)
            previous_totals = self.daily_period_totals
            filename_prefix = "disk-io-daily"
        elif kind == "weekly":
            title = "硬盘读写监控 周报"
            period_label = (
                f"周报周期（每周{weekday_name(self.weekly_report_day)} "
                f"{self.weekly_report_time} 作为周期边界）"
            )
            period_start = self.weekly_period_start
            previous_session = dict(self.weekly_period_session)
            previous_totals = self.weekly_period_totals
            filename_prefix = "disk-io-weekly"
        else:
            raise ValueError("未知报告类型。")

        interval_read = max(0, int(totals["session_read_bytes"]) - previous_totals[0])
        interval_write = max(0, int(totals["session_write_bytes"]) - previous_totals[1])
        lines = [
            title,
            f"生成时间: {iso_time(generated_at)}",
            f"统计口径: {period_label}",
            "",
            "监控启动:",
            iso_time(self.started_at),
            "",
            "本次报告统计区间:",
            iso_time(period_start),
            "~",
            iso_time(period_end),
            "",
            "统计时长:",
            format_duration_seconds(period_end - period_start),
            "",
            "本报告区间新增:",
            f"  读取: {format_bytes(interval_read)}",
            f"  写入: {format_bytes(interval_write)}",
            f"  合计: {format_bytes(interval_read + interval_write)}",
            "",
            "自监控启动后的总量:",
            f"  读取: {format_bytes(totals['session_read_bytes'])}",
            f"  写入: {format_bytes(totals['session_write_bytes'])}",
            f"  合计: {format_bytes(totals['session_total_bytes'])}",
            "",
            "应用/进程明细，按本报告区间新增写入排序:",
            (
                "应用/进程\t完整应用/进程名\tPID\t本报告区间写入\t本报告区间读取\t"
                "启动后写入\t启动后读取\t当前写速\t当前读速\t完整可执行文件路径"
            ),
        ]

        ranked = []
        for row in rows:
            key = str(row["app_key"])
            prev_read, prev_write = previous_session.get(key, (0, 0))
            current_read, current_write = current_session.get(key, (0, 0))
            row_copy = dict(row)
            row_copy["interval_read"] = max(0, current_read - prev_read)
            row_copy["interval_write"] = max(0, current_write - prev_write)
            ranked.append(row_copy)
        ranked.sort(key=lambda item: int(item["interval_write"]), reverse=True)

        for row in ranked:
            pids = ",".join(str(pid) for pid in row["pids"]) or "-"
            lines.append(
                "\t".join(
                    [
                        str(row["app"]),
                        str(row["app"]),
                        pids,
                        format_bytes(int(row["interval_write"])),
                        format_bytes(int(row["interval_read"])),
                        format_bytes(int(row["session_write_bytes"])),
                        format_bytes(int(row["session_read_bytes"])),
                        f"{format_bytes(int(row['write_rate_bps']))}/s",
                        f"{format_bytes(int(row['read_rate_bps']))}/s",
                        str(row["path"]),
                    ]
                )
            )

        self.log_root.mkdir(parents=True, exist_ok=True)
        path = self.log_root / f"{filename_prefix}-{file_time(period_end)}.txt"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if kind == "daily":
            self.daily_period_start = period_end
            self.daily_period_session = current_session
            self.daily_period_totals = current_totals
            self.next_daily_report_at = next_daily_report_at(period_end, self.daily_report_time)
        else:
            self.weekly_period_start = period_end
            self.weekly_period_session = current_session
            self.weekly_period_totals = current_totals
            self.next_weekly_report_at = next_weekly_report_at(
                period_end,
                self.weekly_report_day,
                self.weekly_report_time,
            )
        return path

    def list_logs(self) -> List[Dict[str, object]]:
        self.log_root.mkdir(parents=True, exist_ok=True)
        logs = []
        for path in sorted(self.log_root.glob("*.txt"), reverse=True):
            stat = path.stat()
            logs.append(
                {
                    "name": path.name,
                    "size": stat.st_size,
                    "modified_at": stat.st_mtime,
                    "modified_text": iso_time(stat.st_mtime),
                }
            )
        return logs

    def resolve_log_file(self, name: str) -> Optional[Path]:
        log_root = self.log_root.resolve()
        path = (log_root / name).resolve()
        if log_root not in path.parents or not path.exists() or path.suffix != ".txt":
            return None
        return path


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:
        return


class RequestHandler(SimpleHTTPRequestHandler):
    monitor: DiskIOMonitor

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/state":
            self.send_json(self.monitor.state())
            return
        if parsed.path == "/api/logs":
            self.send_json({"logs": self.monitor.list_logs()})
            return
        if parsed.path.startswith("/logs/"):
            self.serve_log(parsed.path)
            return
        if parsed.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/settings":
            payload = self.read_json_body()
            try:
                self.monitor.set_settings(
                    sample_interval=payload.get("sample_interval_seconds"),
                    log_interval_minutes=payload.get("log_interval_minutes"),
                    log_enabled=payload.get("log_enabled"),
                    log_directory=payload.get("log_directory"),
                    monitor_during_sleep=payload.get("monitor_during_sleep"),
                    alert_enabled=payload.get("alert_enabled"),
                    alert_window_minutes=payload.get("alert_window_minutes"),
                    alert_threshold_gb=payload.get("alert_threshold_gb"),
                    daily_report_enabled=payload.get("daily_report_enabled"),
                    daily_report_time=payload.get("daily_report_time"),
                    weekly_report_enabled=payload.get("weekly_report_enabled"),
                    weekly_report_day=payload.get("weekly_report_day"),
                    weekly_report_time=payload.get("weekly_report_time"),
                    hidden_items=payload.get("hidden_items"),
                )
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self.send_json({"ok": True, "state": self.monitor.state()})
            return
        if parsed.path == "/api/alerts/delete":
            payload = self.read_json_body()
            self.monitor.delete_alert(str(payload.get("id", "")))
            self.send_json({"ok": True, "state": self.monitor.state()})
            return
        if parsed.path == "/api/power-event":
            payload = self.read_json_body()
            try:
                self.monitor.power_event(str(payload.get("event", "")))
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self.send_json({"ok": True, "state": self.monitor.state()})
            return
        if parsed.path == "/api/snapshot":
            path = self.monitor.write_log(reason="manual")
            self.send_json({"ok": True, "log": path.name, "logs": self.monitor.list_logs()})
            return
        if parsed.path == "/api/shutdown-snapshot":
            path = self.monitor.write_final_log(reason="shutdown")
            self.send_json(
                {
                    "ok": True,
                    "log": path.name if path else "",
                    "already_written": path is None,
                }
            )
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def translate_path(self, path: str) -> str:
        path = urllib.parse.urlparse(path).path
        path = posixpath.normpath(urllib.parse.unquote(path))
        pieces = [piece for piece in path.split("/") if piece and piece not in (".", "..")]
        resolved = WEB_ROOT
        for piece in pieces:
            resolved = resolved / piece
        return str(resolved)

    def serve_log(self, request_path: str) -> None:
        name = posixpath.basename(urllib.parse.unquote(request_path))
        path = self.monitor.resolve_log_file(name)
        if path is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = path.read_bytes()
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'inline; filename="{path.name}"')
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def read_json_body(self) -> Dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            parsed = json.loads(raw.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}

    def send_json(
        self,
        payload: Dict[str, object],
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="macOS disk I/O monitor with local web UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--sample-interval", type=float, default=5)
    parser.add_argument("--log-interval-minutes", type=float, default=30)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    monitor = DiskIOMonitor(
        sample_interval=args.sample_interval,
        log_interval=args.log_interval_minutes * 60,
    )
    monitor.start()
    RequestHandler.monitor = monitor
    server = QuietThreadingHTTPServer((args.host, args.port), RequestHandler)

    url = f"http://{args.host}:{args.port}/"
    print(f"Disk I/O Monitor running at {url}")
    print(f"TXT logs will be saved in: {monitor.log_root}")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    def handle_shutdown_signal(signum, _frame) -> None:
        try:
            monitor.write_final_log(reason=f"signal-{signum}")
        except Exception:
            pass
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)

    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        print("\nStopping...")
    finally:
        monitor.stop()
        server.server_close()


if __name__ == "__main__":
    main()
