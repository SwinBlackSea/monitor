#!/usr/bin/env python3
"""仅使用 Python 标准库实现的极简多机进程监视器。

设计约束：
- 单进程同时提供页面、API、内存缓存和后台健康检查。
- CPU/内存快照由页面按需触发，同一机器的并发请求合并为一次采集。
- 目录占用只在展开时计算并缓存，不进入 5 秒资源采集周期。
- 目标机只需要 SSH，不安装 Agent，也不保存历史时序数据。
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import gzip
import ipaddress
import json
import os
import posixpath
import random
import re
import shlex
import signal
import stat
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
INDEX_PATH = ROOT / "doc" / "index.html"
DIRECTORY_CACHE_TTL = 600
HOST_STATUS_INTERVAL = 30
HOST_STATUS_STALE_AFTER = 75
HOST_STATUS_RETRY_DELAY = 3
HOST_STATUS_MAX_WORKERS = 8


def now_ms() -> int:
    """返回供 API 使用的当前 Unix 毫秒时间戳。"""
    return int(time.time() * 1000)


def safe_id(value: str) -> str:
    """将机器标签转换为可安全用于 URL 和文件名的稳定标识。"""
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-")
    return value or "host"


def parse_ps(text: str) -> list[dict[str, Any]]:
    """解析固定的 `ps -eo` 列，并补全父进程名称。"""
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.split(None, 10)
        if len(parts) != 11 or not parts[0].isdigit():
            continue
        pid, ppid, user, name, threads, cpu, rss, memory, month, day, rest = parts
        started_parts = rest.rsplit(None, 1)
        started = f"{month} {day} {started_parts[0]}" if started_parts else "-"
        try:
            rows.append({
                "pid": int(pid), "ppid": int(ppid), "user": user, "name": name,
                "threads": int(threads), "cpu": float(cpu), "rss_bytes": int(rss) * 1024,
                "memory": float(memory), "started": started,
            })
        except ValueError:
            continue
    names = {row["pid"]: row["name"] for row in rows}
    for row in rows:
        row["parent_name"] = names.get(row["ppid"], "kernel" if row["ppid"] == 0 else "—")
    return rows


def parse_df(text: str) -> list[dict[str, Any]]:
    """将按字节输出的 `df -PT -B1` 解析为挂载点摘要。"""
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 7 or not parts[2].isdigit():
            continue
        device, fs_type, total, used, available, percent = parts[:6]
        rows.append({
            "device": device, "type": fs_type, "total_bytes": int(total),
            "used_bytes": int(used), "available_bytes": int(available),
            "used_percent": float(percent.rstrip("%")), "mount": " ".join(parts[6:]),
        })
    return rows


def read_local_snapshot() -> dict[str, Any]:
    """在本机采集与 `_read_ssh` 远程采集结构一致的快照。"""
    ps = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,user=,comm=,nlwp=,pcpu=,rss=,pmem=,lstart="],
        capture_output=True, text=True, check=True, timeout=8,
    ).stdout
    df = subprocess.run(
        ["df", "-PT", "-B1", "--exclude-type=tmpfs", "--exclude-type=devtmpfs"],
        capture_output=True, text=True, check=True, timeout=8,
    ).stdout
    load = os.getloadavg()
    processes = parse_ps(ps)
    mem_total = mem_available = 0
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        if key == "MemTotal":
            mem_total = int(value.split()[0]) * 1024
        elif key == "MemAvailable":
            mem_available = int(value.split()[0]) * 1024
    return {
        "processes": processes, "filesystems": parse_df(df),
        "home_path": str(Path.home()),
        "summary": {
            "cpu_percent": round(sum(p["cpu"] for p in processes) / max(1, os.cpu_count() or 1), 1),
            "memory_percent": round((1 - mem_available / max(1, mem_total)) * 100, 1),
            "load_1": round(load[0], 2), "load_5": round(load[1], 2),
            "cores": os.cpu_count() or 1, "process_count": len(processes),
        },
    }


DEMO_NAMES = [
    ("code", "ubuntu", "systemd"), ("node", "ubuntu", "code"), ("postgres", "postgres", "systemd"),
    ("python3", "ubuntu", "systemd"), ("nginx", "www-data", "systemd"),
    ("dockerd", "root", "systemd"), ("redis-server", "redis", "systemd"),
    ("sshd", "root", "systemd"), ("containerd", "root", "systemd"),
    ("chrome", "ubuntu", "systemd"), ("java", "ubuntu", "systemd"),
]

DEMO_DIRECTORY_TREE: dict[str, list[tuple[str, int]]] = {
    "/": [("home", 96_600_000_000), ("var", 41_200_000_000), ("opt", 18_400_000_000)],
    "/home": [("demo-user", 72_800_000_000), ("shared", 23_800_000_000)],
    "/home/demo-user": [
        ("projects", 38_600_000_000), ("Library", 21_400_000_000),
        ("Downloads", 8_700_000_000), (".cache", 4_100_000_000),
    ],
    "/home/demo-user/projects": [
        ("monitor", 12_800_000_000), ("website", 9_400_000_000),
        ("archives", 7_600_000_000), ("playground", 3_900_000_000),
    ],
    "/home/demo-user/projects/monitor": [
        ("node_modules", 6_800_000_000), ("logs", 3_100_000_000),
        ("build", 2_900_000_000),
    ],
    "/home/demo-user/Library": [
        ("Caches", 11_900_000_000), ("Containers", 6_300_000_000),
        ("Application Support", 3_200_000_000),
    ],
    "/var": [("lib", 22_700_000_000), ("log", 12_600_000_000), ("cache", 5_900_000_000)],
    "/var/lib": [("docker", 16_400_000_000), ("postgresql", 6_300_000_000)],
    "/data": [("backups", 188_000_000_000), ("media", 121_000_000_000), ("exports", 65_000_000_000)],
    "/data/backups": [("daily", 94_000_000_000), ("weekly", 67_000_000_000), ("monthly", 27_000_000_000)],
    "/data/backups/daily": [("2026-07-27", 34_000_000_000), ("2026-07-26", 32_000_000_000), ("2026-07-25", 28_000_000_000)],
}


def demo_snapshot(host_index: int, tick: int) -> dict[str, Any]:
    """为演示模式生成可重复但会随轮次变化的进程数据。"""
    rng = random.Random(host_index * 100003 + tick)
    processes = []
    base_pid = 1100 + host_index * 1000
    for index, (name, user, parent) in enumerate(DEMO_NAMES):
        cpu = max(.1, 61 - index * 4.8 + rng.uniform(-4, 4) + host_index)
        memory = max(.1, 20 - index * 1.45 + rng.uniform(-1.2, 1.2))
        processes.append({
            "pid": base_pid + index * 17, "ppid": 1 if parent == "systemd" else base_pid,
            "name": name, "parent_name": parent, "user": user, "threads": 2 + index * 3,
            "cpu": round(cpu, 1), "memory": round(memory, 1),
            "rss_bytes": int(memory * 310 * 1024 * 1024),
            "started": f"Jul 27 {9 + index % 8:02d}:{index * 4 % 60:02d}:00",
        })
    used = 52 + host_index * 7 + rng.uniform(-1, 1)
    return {
        "processes": processes,
        "home_path": "/home/demo-user",
        "filesystems": [
            {"device": "/dev/vda1", "type": "ext4", "total_bytes": 256e9,
             "used_bytes": 256e9 * used / 100, "available_bytes": 256e9 * (1 - used / 100),
             "used_percent": round(used, 1), "mount": "/"},
            {"device": "/dev/vdb1", "type": "xfs", "total_bytes": 1e12,
             "used_bytes": 3.74e11, "available_bytes": 6.26e11,
             "used_percent": 37.4, "mount": "/data"},
        ],
        "summary": {
            "cpu_percent": round(43 + host_index * 6 + rng.uniform(-3, 3), 1),
            "memory_percent": round(58 + host_index * 4 + rng.uniform(-2, 2), 1),
            "load_1": round(2.13 + host_index * .8, 2),
            "load_5": round(1.84 + host_index * .7, 2),
            "cores": 16 if host_index < 2 else 8, "process_count": 238 + host_index * 21,
        },
    }


@dataclass
class Config:
    """保存由命令行参数和环境变量生成的进程级配置。"""

    bind: str
    port: int
    interval: float
    demo: bool
    allow_kill: bool
    username: str | None
    password: str | None
    allow_delete: bool = False
    delete_min_depth: int = 3

    def __post_init__(self) -> None:
        """强制目录清空功能的最小安全层级。"""
        if self.delete_min_depth < 3:
            raise ValueError("目录清空层级必须至少为 3")


class MonitorApp:
    """集中管理机器状态、内存缓存、采集器和危险操作。"""

    def __init__(self, config: Config, hosts_path: Path | None):
        """初始化持久化机器元数据和全部进程内状态。"""
        self.config = config
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.tick = 0
        # 演示模式的操作结果只在本次进程生命周期内有效。
        self.demo_cleared_directories: set[tuple[str, str]] = set()
        self.demo_terminated_processes: set[tuple[str, int]] = set()
        # 目录结果按（机器、挂载点、路径）组成 KV 缓存；后台任务表避免
        # 相同点击重复启动 `du` 遍历。
        self.directory_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
        self.directory_jobs: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.directory_generation: dict[str, int] = {}
        # SSH 连通状态和隧道对端信息与资源采集状态相互独立。
        self.tunnel_peer_cache: dict[tuple[str, int], tuple[float, str | None]] = {}
        self.host_health: dict[str, dict[str, Any]] = {}
        # 资源快照使用单调时钟、机器代次和每机锁，实现按请求触发的
        # single-flight 合并采集。
        self.snapshot_collected_at: dict[str, float] = {}
        self.snapshot_generation: dict[str, int] = {}
        self.snapshot_locks: dict[str, threading.Lock] = {}
        default_hosts_path = (
            ROOT / "hosts.local.json"
            if (ROOT / "hosts.local.json").exists()
            else ROOT / "hosts.json"
        )
        self.hosts_path = hosts_path or default_hosts_path
        self.hosts = self._load_hosts(self.hosts_path)
        self.data: dict[str, dict[str, Any]] = {}
        self.thread: threading.Thread | None = None
        self.health_thread: threading.Thread | None = None

    def _load_hosts(self, path: Path) -> list[dict[str, Any]]:
        """加载演示机器、持久化机器或安全的本机默认配置。"""
        if self.config.demo:
            names = ["开发机", "生产服务器", "数据库", "备份机"]
            return [{"id": f"demo-{i + 1}", "name": name, "address": "demo", "port": 22}
                    for i, name in enumerate(names)]
        if path.exists():
            raw = json.loads(path.read_text())
            hosts = raw["hosts"] if isinstance(raw, dict) else raw
            return [{"id": safe_id(str(h.get("id") or h["name"])), **h} for h in hosts]
        return [{"id": "local", "name": "本机", "address": "127.0.0.1", "port": 22,
                 "local": True}]

    def _persist_hosts(self) -> None:
        """原子替换机器 JSON，避免写入失败破坏原配置。"""
        if self.config.demo:
            return
        allowed = ("id", "name", "address", "user", "port", "local")
        payload = {"hosts": [{key: host[key] for key in allowed if key in host}
                             for host in self.hosts]}
        temporary = self.hosts_path.with_suffix(self.hosts_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(self.hosts_path)

    def _normalize_host(self, values: dict[str, Any], current: dict[str, Any] | None = None
                        ) -> dict[str, Any]:
        """合并并校验机器管理表单允许提交的字段。"""
        host = dict(current or {})
        name = str(values.get("name", host.get("name", ""))).strip()
        address = str(values.get("address", host.get("address", ""))).strip()
        user = str(values.get("user", host.get("user", ""))).strip()
        try:
            port = int(values.get("port", host.get("port", 22)))
        except (TypeError, ValueError):
            raise ValueError("SSH 端口必须是数字") from None
        if not name or len(name) > 40:
            raise ValueError("机器名称长度必须为 1–40 个字符")
        if not address or len(address) > 253 or address.startswith("-") or not re.fullmatch(
                r"[a-zA-Z0-9._:\[\]-]+", address):
            raise ValueError("IP 地址或主机名格式不正确")
        if user and (len(user) > 32 or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.-]*", user)):
            raise ValueError("SSH 用户名格式不正确")
        if not 1 <= port <= 65535:
            raise ValueError("SSH 端口必须在 1–65535 之间")
        host.update({"name": name, "address": address, "port": port})
        if user:
            host["user"] = user
        else:
            host.pop("user", None)
        if current is None:
            host["local"] = bool(values.get("local", False))
        elif "local" in values:
            host["local"] = bool(values["local"])
        elif address != current.get("address"):
            host["local"] = False
        return host

    def start(self) -> None:
        """启动缓存维护线程和独立的 SSH 健康检查线程。"""
        self.thread = threading.Thread(target=self._loop, daemon=True, name="maintenance")
        self.thread.start()
        self.health_thread = threading.Thread(
            target=self._health_loop, daemon=True, name="host-health"
        )
        self.health_thread.start()

    def _loop(self) -> None:
        """周期清理具有严格 10 分钟 TTL 的内存缓存。"""
        while not self.stop.wait(min(60, max(1, self.config.interval))):
            self._prune_memory_caches()

    def close(self) -> None:
        """通知后台循环退出，并短暂等待线程完成清理。"""
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
        if self.health_thread:
            self.health_thread.join(timeout=6)

    def _prune_memory_caches(self) -> None:
        """删除过期目录和隧道结果；资源快照继续保留最后值。"""
        now = time.monotonic()
        with self.lock:
            self.directory_cache = {
                key: value for key, value in self.directory_cache.items()
                if now - value[0] < DIRECTORY_CACHE_TTL
            }
            self.tunnel_peer_cache = {
                key: value for key, value in self.tunnel_peer_cache.items()
                if now - value[0] < DIRECTORY_CACHE_TTL
            }

    def _probe_host(self, host: dict[str, Any]) -> tuple[bool, str | None]:
        """只执行 `ssh ... true`，不采集任何资源信息。"""
        if self.config.demo:
            reachable = host["id"] != "demo-4"
            return reachable, None if reachable else "模拟 SSH 连接失败"
        if host.get("local"):
            return True, None
        destination = f'{host.get("user", "") + "@" if host.get("user") else ""}{host["address"]}'
        result = subprocess.run(
            [
                "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
                "-o", "ConnectionAttempts=1", "-o", "LogLevel=ERROR",
                "-p", str(host.get("port", 22)), destination, "true",
            ],
            capture_output=True, text=True, timeout=4.5,
        )
        if result.returncode == 0:
            return True, None
        detail = result.stderr.strip().splitlines()
        return False, detail[-1] if detail else "SSH 连接失败"

    def _mark_health_checking(self, host_ids: list[str]) -> None:
        """将指定机器标记为检测中，同时保留上一次状态。"""
        with self.lock:
            current_ids = {host["id"] for host in self.hosts}
            for host_id in host_ids:
                if host_id not in current_ids:
                    continue
                previous = self.host_health.get(host_id, {})
                self.host_health[host_id] = {
                    **previous,
                    "status": previous.get("status", "warning"),
                    "checked_at": previous.get("checked_at"),
                    "failures": int(previous.get("failures", 0)),
                    "checking": True,
                    "error": previous.get("error"),
                }

    def _record_health_result(
        self, host_id: str, reachable: bool, error: str | None = None,
        expected_host: dict[str, Any] | None = None,
    ) -> str:
        """更新绿、黄、红状态，同时拒绝旧连接参数产生的迟到结果。"""
        with self.lock:
            current_host = next(
                (host for host in self.hosts if host["id"] == host_id), None
            )
            if current_host is None:
                return "warning"
            connection_keys = ("address", "user", "port", "local")
            if expected_host is not None and any(
                current_host.get(key) != expected_host.get(key)
                for key in connection_keys
            ):
                return "warning"
            previous = self.host_health.get(host_id, {})
            failures = 0 if reachable else int(previous.get("failures", 0)) + 1
            # 首次失败先显示黄色，短暂复查仍失败后才转为红色。
            status = "normal" if reachable else ("warning" if failures == 1 else "offline")
            self.host_health[host_id] = {
                "status": status,
                "checked_at": now_ms(),
                "failures": failures,
                "checking": False,
                "error": None if reachable else error or "SSH 连接失败",
            }
            return status

    def _health_probe_batch(
        self, hosts: list[dict[str, Any]]
    ) -> list[tuple[dict[str, Any], bool, str | None]]:
        """按限定并发数批量检测，避免机器增加时无限创建 SSH。"""
        if not hosts:
            return []
        results: list[tuple[dict[str, Any], bool, str | None]] = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(HOST_STATUS_MAX_WORKERS, len(hosts)),
            thread_name_prefix="health",
        ) as executor:
            futures = {executor.submit(self._probe_host, host): host for host in hosts}
            for future in concurrent.futures.as_completed(futures):
                host = futures[future]
                try:
                    reachable, error = future.result()
                except Exception as exc:
                    reachable, error = False, str(exc)
                results.append((host, reachable, error))
        return results

    def check_host_health(self) -> None:
        """检测全部机器，并在首次失败 3 秒后复查一次。"""
        with self.lock:
            hosts = [dict(host) for host in self.hosts]
        self._mark_health_checking([host["id"] for host in hosts])
        retry_ids: set[str] = set()
        for host, reachable, error in self._health_probe_batch(hosts):
            if self._record_health_result(
                host["id"], reachable, error, expected_host=host
            ) == "warning":
                retry_ids.add(host["id"])
        if not retry_ids or self.stop.wait(HOST_STATUS_RETRY_DELAY):
            return
        with self.lock:
            retry_hosts = [
                dict(host) for host in self.hosts
                if host["id"] in retry_ids
                and self.host_health.get(host["id"], {}).get("failures") == 1
            ]
        self._mark_health_checking([host["id"] for host in retry_hosts])
        for host, reachable, error in self._health_probe_batch(retry_hosts):
            self._record_health_result(
                host["id"], reachable, error, expected_host=host
            )

    def _health_loop(self) -> None:
        """每 30 秒执行一次全部机器的轻量连通检查。"""
        while not self.stop.is_set():
            try:
                self.check_host_health()
            except Exception:
                pass
            if self.stop.wait(HOST_STATUS_INTERVAL):
                return

    def _host_health_info(self, host_id: str, now: int | None = None) -> dict[str, Any]:
        """根据最近一次轻量检测生成机器 Tab 状态点数据。"""
        with self.lock:
            cached = dict(self.host_health.get(host_id, {}))
        checked_at = cached.get("checked_at")
        status = cached.get("status", "warning")
        current = now if now is not None else now_ms()
        if checked_at is None or current - int(checked_at) > HOST_STATUS_STALE_AFTER * 1000:
            status = "warning"
        return {
            "id": host_id,
            "status": status,
            "checked_at": checked_at,
            "checking": bool(cached.get("checking", checked_at is None)),
        }

    def host_status_list(self) -> list[dict[str, Any]]:
        """从内存返回全部 Tab 状态点，不在接口请求中执行 SSH。"""
        with self.lock:
            host_ids = [host["id"] for host in self.hosts]
        current = now_ms()
        return [self._host_health_info(host_id, current) for host_id in host_ids]

    def collect_host(self, host_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """返回有效期内的共享快照，并保证同一机器最多采集一次。

        获取每机锁前后都会检查缓存。这个二次检查构成 single-flight
        边界：快照过期后的第一个请求负责采集，并发请求等待并复用结果。
        机器代次用于阻止终止进程、清空目录、编辑或删除之前启动的旧采集
        覆盖新状态。
        """
        while True:
            with self.lock:
                item = next(
                    ((index, dict(host)) for index, host in enumerate(self.hosts)
                     if host["id"] == host_id),
                    None,
                )
                if item is None:
                    raise KeyError(host_id)
                cached = self.data.get(host_id)
                collected_at = self.snapshot_collected_at.get(host_id)
                if (
                    cached is not None
                    and collected_at is not None
                    and time.monotonic() - collected_at < self.config.interval
                ):
                    return item[1], dict(cached)
                collection_lock = self.snapshot_locks.setdefault(
                    host_id, threading.Lock()
                )

            with collection_lock:
                # 等待采集锁期间，其他请求可能已经刷新完成，因此必须
                # 在锁内再次检查有效期。
                with self.lock:
                    item = next(
                        ((index, dict(host)) for index, host in enumerate(self.hosts)
                         if host["id"] == host_id),
                        None,
                    )
                    if item is None:
                        raise KeyError(host_id)
                    cached = self.data.get(host_id)
                    collected_at = self.snapshot_collected_at.get(host_id)
                    if (
                        cached is not None
                        and collected_at is not None
                        and time.monotonic() - collected_at < self.config.interval
                    ):
                        return item[1], dict(cached)
                    index, host = item
                    generation = self.snapshot_generation.get(host_id, 0)
                    connection = tuple(
                        host.get(key) for key in ("address", "user", "port", "local")
                    )
                    self.tick += 1

                _, snapshot = self._refresh_host(host, index)
                with self.lock:
                    current = next(
                        (dict(item) for item in self.hosts if item["id"] == host_id),
                        None,
                    )
                    current_connection = tuple(
                        current.get(key) for key in ("address", "user", "port", "local")
                    ) if current else None
                    if (
                        current is not None
                        and self.snapshot_generation.get(host_id, 0) == generation
                        and current_connection == connection
                    ):
                        self.data[host_id] = snapshot
                        self.snapshot_collected_at[host_id] = time.monotonic()
                        return current, dict(snapshot)
            # 采集期间机器连接被修改或快照被主动失效：丢弃旧结果，
            # 释放每机锁后重新采集。

    def _invalidate_snapshot(self, host_id: str) -> None:
        """立即让快照过期，并使已经在途的采集结果失效。"""
        with self.lock:
            self.snapshot_generation[host_id] = (
                self.snapshot_generation.get(host_id, 0) + 1
            )
            self.snapshot_collected_at.pop(host_id, None)

    def _refresh_host(self, host: dict[str, Any], index: int) -> tuple[str, dict[str, Any]]:
        """执行采集，并将连接异常转换为可返回的离线快照。"""
        try:
            snapshot = self._collect_host(host, index)
        except Exception as exc:
            with self.lock:
                previous = self.data.get(host["id"], {"processes": [], "filesystems": [], "summary": {}})
            # 采集失败时保留最后一份有效数据，只更新状态、时间和错误信息。
            snapshot = {**previous, "status": "offline", "updated_at": now_ms(), "error": str(exc)}
        return host["id"], snapshot

    def _collect_host(self, host: dict[str, Any], index: int = 0) -> dict[str, Any]:
        """执行一次真实或演示采集，不在此处应用缓存策略。"""
        if self.config.demo:
            snapshot = demo_snapshot(index, self.tick)
            removed = {
                pid for removed_host, pid in self.demo_terminated_processes
                if removed_host == host["id"]
            }
            if removed:
                snapshot["processes"] = [
                    process for process in snapshot["processes"]
                    if process["pid"] not in removed
                ]
                snapshot["summary"]["process_count"] = len(snapshot["processes"])
            status = "offline" if index == 3 else ("warning" if index == 2 else "normal")
        elif host.get("local"):
            snapshot, status = read_local_snapshot(), "normal"
        else:
            snapshot, status = self._read_ssh(host), "normal"
        snapshot.update({"status": status, "updated_at": now_ms(), "error": None})
        return snapshot

    def _read_ssh(self, host: dict[str, Any]) -> dict[str, Any]:
        """通过一次 SSH 往返采集进程、挂载点、负载、内存和 HOME。"""
        destination = f'{host.get("user", "") + "@" if host.get("user") else ""}{host["address"]}'
        # 使用分段标记让远程端无需额外依赖，同时用一个 SSH 进程
        # 一致地返回快照的全部组成部分。
        command = (
            "printf '__PS__\\n'; ps -eo pid=,ppid=,user=,comm=,nlwp=,pcpu=,rss=,pmem=,lstart=; "
            "printf '__DF__\\n'; df -PT -B1 --exclude-type=tmpfs --exclude-type=devtmpfs; "
            "printf '__META__\\n'; cat /proc/loadavg; nproc; "
            "grep -E '^(MemTotal|MemAvailable):' /proc/meminfo; "
            "printf '__HOME__\\n%s\\n' \"$HOME\""
        )
        args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", "-p",
                str(host.get("port", 22)), destination, command]
        result = subprocess.run(args, capture_output=True, text=True, timeout=4.5)
        if result.returncode:
            detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "SSH 连接失败"
            raise ConnectionError(detail)
        ps_text, tail = result.stdout.split("__DF__", 1)
        df_text, meta_with_home = tail.split("__META__", 1)
        meta, home_text = meta_with_home.split("__HOME__", 1)
        processes = parse_ps(ps_text.split("__PS__", 1)[-1])
        lines = [line for line in meta.splitlines() if line.strip()]
        load = [float(x) for x in lines[0].split()[:3]]
        cores = int(lines[1])
        memory = {line.split(":", 1)[0]: int(line.split()[1]) * 1024 for line in lines[2:]}
        total = memory.get("MemTotal", 1)
        available = memory.get("MemAvailable", 0)
        return {
            "processes": processes, "filesystems": parse_df(df_text),
            "home_path": home_text.strip().splitlines()[0] if home_text.strip() else "",
            "summary": {
                "cpu_percent": round(sum(p["cpu"] for p in processes) / max(1, cores), 1),
                "memory_percent": round((1 - available / total) * 100, 1),
                "load_1": load[0], "load_5": load[1], "cores": cores,
                "process_count": len(processes),
            },
        }

    @staticmethod
    def _endpoint_host(value: str) -> str:
        """从 IPv4、主机名或带方括号的 IPv6 端点中提取主机部分。"""
        if value.startswith("[") and "]" in value:
            return value[1:value.index("]")]
        return value.rsplit(":", 1)[0] if ":" in value else value

    @staticmethod
    def _loopback_address(value: str) -> bool:
        """识别回环主机名和地址，包括带方括号的 IPv6。"""
        if value.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(value.strip("[]")).is_loopback
        except ValueError:
            return False

    def _tunnel_peer_address(self, host: dict[str, Any]) -> str | None:
        """尽可能将反向隧道监听端解析为公网 SSH 对端。

        普通远程地址保持不变。只有指向回环地址且标记为非本机的机器
        才参与识别，避免影响正常 SSH 地址的展示逻辑。
        """
        address = str(host.get("address", ""))
        if host.get("local") or not self._loopback_address(address):
            return None
        key = (address, int(host.get("port", 22)))
        now = time.monotonic()
        with self.lock:
            cached = self.tunnel_peer_cache.get(key)
            if cached and now - cached[0] < DIRECTORY_CACHE_TTL:
                return cached[1]
        peer = None
        try:
            listeners = subprocess.run(
                ["sudo", "-n", "ss", "-H", "-ltnp"],
                capture_output=True, text=True, timeout=2,
            )
            established = subprocess.run(
                ["sudo", "-n", "ss", "-H", "-tnp", "state", "established"],
                capture_output=True, text=True, timeout=2,
            )
            if listeners.returncode == 0 and established.returncode == 0:
                port = key[1]
                pids: set[str] = set()
                for line in listeners.stdout.splitlines():
                    parts = line.split()
                    if len(parts) < 5 or not parts[3].endswith(f":{port}"):
                        continue
                    pids.update(re.findall(r"pid=(\d+)", line))
                for line in established.stdout.splitlines():
                    if not any(f"pid={pid}" in line for pid in pids):
                        continue
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    candidate = self._endpoint_host(parts[3])
                    try:
                        parsed = ipaddress.ip_address(candidate)
                    except ValueError:
                        continue
                    if parsed.is_global:
                        peer = candidate
                        break
        except (OSError, subprocess.SubprocessError):
            pass
        with self.lock:
            self.tunnel_peer_cache[key] = (now, peer)
        return peer

    def host_list(self) -> list[dict[str, Any]]:
        """返回机器元数据和缓存摘要，不触发资源采集。"""
        with self.lock:
            pairs = [(dict(host), dict(self.data.get(host["id"], {})))
                     for host in self.hosts]
        return [self._host_info(host, data, resolve_tunnel=False) for host, data in pairs]

    def snapshot_list(self) -> list[dict[str, Any]]:
        """只返回内存中已有的快照，不在此处触发采集。"""
        with self.lock:
            pairs = [(dict(host), dict(data)) for host in self.hosts
                     if (data := self.data.get(host["id"])) is not None]
        return [{
                "host_id": host["id"],
                "status": data.get("status", "offline"),
                "updated_at": data.get("updated_at"),
                "summary": data.get("summary", {}),
                "processes": data.get("processes", []),
                "filesystems": data.get("filesystems", []),
                "tunnel_peer": self._host_info(
                    host, data, resolve_tunnel=False
                ).get("tunnel_peer"),
            } for host, data in pairs]

    def _host_info(self, host: dict[str, Any], data: dict[str, Any],
                   resolve_tunnel: bool = True) -> dict[str, Any]:
        """合并安全机器元数据、缓存资源状态和页面展示地址。"""
        collection_status = data.get("status", "warning")
        connectivity_status = self._host_health_info(host["id"])["status"]
        tunnel_peer = self._tunnel_peer_address(host) if resolve_tunnel else None
        if not resolve_tunnel:
            key = (str(host.get("address", "")), int(host.get("port", 22)))
            with self.lock:
                cached_peer = self.tunnel_peer_cache.get(key)
            if cached_peer and time.monotonic() - cached_peer[0] < DIRECTORY_CACHE_TTL:
                tunnel_peer = cached_peer[1]
        return {
            "id": host["id"], "name": host["name"], "address": host.get("address", ""),
            "user": host.get("user", ""), "port": host.get("port", 22),
            "local": bool(host.get("local")),
            "tunnel_peer": tunnel_peer,
            "home_path": data.get("home_path", ""),
            "status": collection_status,
            "collection_status": collection_status,
            "connectivity_status": connectivity_status,
            "updated_at": data.get("updated_at"),
            "summary": data.get("summary", {}),
        }

    def _host_change_result(self, host: dict[str, Any],
                            snapshot: dict[str, Any],
                            resolve_tunnel: bool = True) -> dict[str, Any]:
        """构造前端统一使用的新增、编辑和快照响应结构。"""
        return {
            "id": host["id"], "name": host["name"], "status": snapshot["status"],
            "host": self._host_info(host, snapshot, resolve_tunnel),
            "snapshot": {
                "processes": snapshot.get("processes", []),
                "filesystems": snapshot.get("filesystems", []),
                "summary": snapshot.get("summary", {}),
                "updated_at": snapshot.get("updated_at"),
            },
        }

    def get_host(self, host_id: str, collect_if_missing: bool = False
                 ) -> tuple[dict[str, Any], dict[str, Any]]:
        """读取机器状态，并可在快照不存在时执行一次采集。"""
        with self.lock:
            host = next((h for h in self.hosts if h["id"] == host_id), None)
            data = self.data.get(host_id)
            if not host:
                raise KeyError(host_id)
            if data:
                return dict(host), dict(data)
        if collect_if_missing:
            return self.collect_host(host_id)
        raise KeyError(host_id)

    def add_host(self, values: dict[str, Any]) -> dict[str, Any]:
        """验证连接成功后再持久化新增机器。"""
        host = self._normalize_host(values)
        with self.lock:
            base = safe_id(str(values.get("id") or host["name"] or host["address"]))
            host_id, suffix = base, 2
            existing = {item["id"] for item in self.hosts}
            while host_id in existing:
                host_id, suffix = f"{base}-{suffix}", suffix + 1
            host["id"] = host_id
            index = len(self.hosts)
        try:
            snapshot = self._collect_host(host, index)
        except Exception as exc:
            raise ConnectionError(f"连接测试失败：{exc}") from exc
        with self.lock:
            self.hosts.append(host)
            self.data[host_id] = snapshot
            try:
                self._persist_hosts()
            except Exception:
                self.hosts.pop()
                self.data.pop(host_id, None)
                raise
            self.snapshot_collected_at[host_id] = time.monotonic()
        return self._host_change_result(host, snapshot)

    def update_host(self, host_id: str, values: dict[str, Any]) -> dict[str, Any]:
        """更新机器，并在提交前验证发生变化的 SSH 连接信息。"""
        with self.lock:
            index = next((i for i, host in enumerate(self.hosts) if host["id"] == host_id), None)
            if index is None:
                raise KeyError(host_id)
            current = dict(self.hosts[index])
        updated = self._normalize_host(values, current)
        connection_changed = any(updated.get(key) != current.get(key)
                                 for key in ("address", "user", "port"))
        snapshot = None
        if connection_changed:
            # 连接测试失败时不修改 JSON 和内存中的当前机器，避免一次
            # 错误编辑破坏原本可用的配置。
            try:
                snapshot = self._collect_host(updated, index)
            except Exception as exc:
                raise ConnectionError(f"连接测试失败：{exc}") from exc
        with self.lock:
            previous_snapshot = self.data.get(host_id)
            previous_collected_at = self.snapshot_collected_at.get(host_id)
            previous_generation = self.snapshot_generation.get(host_id, 0)
            self.hosts[index] = updated
            if snapshot is not None:
                self.snapshot_generation[host_id] = previous_generation + 1
                self.data[host_id] = snapshot
                self.snapshot_collected_at[host_id] = time.monotonic()
            try:
                self._persist_hosts()
            except Exception:
                self.hosts[index] = current
                self.snapshot_generation[host_id] = previous_generation
                if previous_snapshot is None:
                    self.data.pop(host_id, None)
                else:
                    self.data[host_id] = previous_snapshot
                if previous_collected_at is None:
                    self.snapshot_collected_at.pop(host_id, None)
                else:
                    self.snapshot_collected_at[host_id] = previous_collected_at
                raise
            if connection_changed:
                self.host_health.pop(host_id, None)
        with self.lock:
            current_snapshot = self.data.get(host_id, {
                "processes": [], "filesystems": [], "summary": {},
                "status": "warning", "updated_at": None, "error": None,
            })
        if connection_changed:
            self._invalidate_directory_cache(host_id)
        return self._host_change_result(
            updated, current_snapshot, resolve_tunnel=connection_changed
        )

    def test_host(self, values: dict[str, Any]) -> dict[str, Any]:
        """用真实快照测试表单连接信息，但不保存任何配置。"""
        host_id = str(values.get("id", "")).strip()
        with self.lock:
            current = next((dict(host) for host in self.hosts if host["id"] == host_id), None)
            index = next((i for i, host in enumerate(self.hosts) if host["id"] == host_id),
                         len(self.hosts))
        host = self._normalize_host(values, current)
        started = time.monotonic()
        try:
            snapshot = self._collect_host(host, index)
        except Exception as exc:
            raise ConnectionError(f"连接测试失败：{exc}") from exc
        return {
            "ok": True, "status": snapshot["status"],
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "tested_at": snapshot["updated_at"],
        }

    def _validate_directory(self, host_id: str, mount: str, path: str
                            ) -> tuple[dict[str, Any], str, str, int, int]:
        """规范化请求路径，并验证它始终位于指定挂载点内。"""
        host, data = self.get_host(host_id, collect_if_missing=True)
        if "\0" in mount or "\0" in path:
            raise ValueError("目录路径不正确")
        mount = posixpath.normpath(mount)
        path = posixpath.normpath(path)
        if not mount.startswith("/") or not path.startswith("/"):
            raise ValueError("目录路径必须是绝对路径")
        filesystems = {
            posixpath.normpath(item["mount"]): item for item in data.get("filesystems", [])
        }
        filesystem = filesystems.get(mount)
        if filesystem is None:
            raise ValueError("挂载点不存在或尚未采集")
        try:
            # 按路径组件比较，避免把 `/data-old` 误判成 `/data` 的子目录。
            if posixpath.commonpath([mount, path]) != mount:
                raise ValueError("目录不属于指定挂载点")
        except ValueError:
            raise ValueError("目录不属于指定挂载点") from None
        relative = posixpath.relpath(path, mount)
        depth = 0 if relative == "." else len([part for part in relative.split("/") if part])
        return host, mount, path, depth, int(filesystem.get("total_bytes", 0))

    @staticmethod
    def _directory_item(path: str, size: int, depth: int, total: int,
                        delete_enabled: bool, delete_min_depth: int) -> dict[str, Any]:
        """构造包含层级和清空资格的一级子目录数据行。"""
        return {
            "name": posixpath.basename(path.rstrip("/")) or "/",
            "path": path, "size_bytes": size, "depth": depth,
            "percent": round(size / total * 100, 1) if total else 0,
            "can_delete": delete_enabled and depth >= delete_min_depth,
            "delete_min_depth": delete_min_depth,
        }

    @staticmethod
    def _parse_du(output: bytes, target: str) -> list[tuple[str, int]]:
        """解析 NUL 分隔的字节统计，正确处理包含空格的路径。"""
        entries: list[tuple[str, int]] = []
        for record in output.split(b"\0"):
            if not record or b"\t" not in record:
                continue
            size_raw, path_raw = record.split(b"\t", 1)
            try:
                size = int(size_raw)
            except ValueError:
                continue
            item_path = os.fsdecode(path_raw)
            if posixpath.normpath(item_path) != target:
                entries.append((posixpath.normpath(item_path), size))
        return entries

    @staticmethod
    def _remote_directory_error(
        stderr: bytes, path: str, returncode: int
    ) -> tuple[str, int]:
        """将远程 Shell 和 `du` 错误转换为明确的页面提示。"""
        text = os.fsdecode(stderr).strip()
        matches: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for line in text.splitlines():
            match = re.search(
                r"(?:cannot read directory|cannot access) ['‘](.+?)['’]: (.+)$",
                line.strip(),
            )
            if match:
                item = (posixpath.normpath(match.group(1)), match.group(2).strip())
                if item not in seen:
                    seen.add(item)
                    matches.append(item)
        if matches:
            item_path, reason = matches[0]
            return f"无法读取 {item_path}：{reason}", len(matches)
        if returncode == 20:
            return "无法解析远程挂载点", 1
        if returncode == 21:
            return f"目录不存在或无法访问：{path}", 1
        if returncode == 22:
            return f"目标不是目录：{path}", 1
        if returncode == 23:
            return f"目录超出挂载点范围：{path}", 1
        if returncode == 24:
            return f"无法读取目录设备信息：{path}", 1
        if returncode == 25:
            return f"无法读取 {path}：Permission denied", 1
        if text:
            return text.splitlines()[-1].strip(), 1
        return f"无法统计 {path}：远程命令退出码 {returncode}", 1

    @staticmethod
    def _ssh_command(host: dict[str, Any], command: str, timeout: float = 30
                     ) -> subprocess.CompletedProcess[bytes]:
        """通过非交互 SSH 执行一条由服务端固定构造的命令。"""
        destination = f'{host.get("user", "") + "@" if host.get("user") else ""}{host["address"]}'
        return subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", "-p",
             str(host.get("port", 22)), destination, command],
            capture_output=True, timeout=timeout,
        )

    def _resolved_local_directory(self, mount: str, path: str) -> Path:
        """解析本机符号链接，并强制目录操作不越过挂载点边界。"""
        try:
            resolved_mount = Path(mount).resolve(strict=True)
            resolved_path = Path(path).resolve(strict=True)
        except (FileNotFoundError, RuntimeError):
            raise ValueError("目录不存在") from None
        if not resolved_path.is_dir():
            raise ValueError("目标不是目录")
        try:
            if os.path.commonpath([resolved_mount, resolved_path]) != str(resolved_mount):
                raise ValueError("目录解析后超出挂载点")
        except ValueError:
            raise ValueError("目录解析后超出挂载点") from None
        return resolved_path

    def _scan_directories(self, host_id: str, mount: str, path: str) -> dict[str, Any]:
        """只统计一个已展开树节点的一级子目录。

        `du -x` 防止进入子挂载点。本机最多使用 4 个工作线程；远程机器
        在一个 SSH 会话内使用 `xargs -P 4`。全部任务完成后再统一排序，
        避免用户阅读时数据行持续跳动。
        """
        host, mount, path, depth, total = self._validate_directory(host_id, mount, path)
        entries: list[tuple[str, int]] = []
        warning = None

        def children_from_entries(sort: bool = True) -> list[dict[str, Any]]:
            """将已完成的字节统计转换为稳定的 API 目录行。"""
            children = [
                self._directory_item(item_path, size, depth + 1, total,
                                     self.config.allow_delete,
                                     self.config.delete_min_depth)
                for item_path, size in entries
            ]
            if sort:
                children.sort(key=lambda item: (-item["size_bytes"], item["name"].casefold()))
            return children

        if self.config.demo:
            raw_children = [] if (host_id, path) in self.demo_cleared_directories else \
                DEMO_DIRECTORY_TREE.get(path, [])
            for name, size in raw_children:
                child_path = posixpath.join(path, name)
                if (host_id, child_path) in self.demo_cleared_directories:
                    size = 0
                entries.append((child_path, size))
        elif host.get("local"):
            target = self._resolved_local_directory(mount, path)
            with os.scandir(target) as iterator:
                directories = [
                    Path(item.path) for item in iterator
                    if item.is_dir(follow_symlinks=False)
                ]

            def local_size(directory: Path) -> tuple[str, int] | None:
                """统计一个本机一级子目录，并禁止跨文件系统。"""
                result = subprocess.run(
                    ["du", "-0", "-x", "-B1", "-s", "--", str(directory)],
                    capture_output=True, timeout=300,
                )
                parsed = self._parse_du(result.stdout, "")
                if parsed:
                    return parsed[0]
                if result.returncode not in {0, 1}:
                    detail = os.fsdecode(result.stderr).strip() or f"无法统计 {directory}"
                    raise OSError(detail)
                return None

            errors: list[str] = []
            # 每个线程只负责一个互不重叠的一级子目录；4 个线程用于平衡
            # 等待时间和被监控磁盘的元数据 I/O 压力。
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(4, max(1, len(directories))),
                thread_name_prefix="du",
            ) as executor:
                futures = [executor.submit(local_size, directory) for directory in directories]
                for future in concurrent.futures.as_completed(futures):
                    try:
                        item = future.result()
                        if item:
                            entries.append(item)
                    except Exception as exc:
                        errors.append(str(exc))
            if not entries and errors:
                raise OSError(errors[0])
        else:
            root_q, target_q = shlex.quote(mount), shlex.quote(path)
            # 远程执行 `find` 前先解析真实路径并检查边界；所有来自用户
            # 输入的 Shell 参数均由 shlex.quote 保护。
            command = (
                f"root=$(readlink -f -- {root_q}) || exit 20; "
                f"target=$(readlink -f -- {target_q}) || exit 21; "
                '[ -d "$target" ] || exit 22; '
                'if [ "$root" != "/" ]; then case "$target" in "$root"|"$root"/*) ;; *) exit 23;; esac; fi; '
                'target_dev=$(stat -c %d -- "$target") || exit 24; '
                '[ -r "$target" ] && [ -x "$target" ] || exit 25; '
                'find "$target" -xdev -mindepth 1 -maxdepth 1 -type d -print0 '
                "| xargs -0 -r -n 1 -P 4 sh -c '"
                'item=$1; item_dev=$(stat -c %d -- "$item" 2>/dev/null) || exit 0; '
                '[ "$item_dev" = "$0" ] || exit 0; '
                "du -0 -x -B1 -s -- \"$item\"' \"$target_dev\""
            )
            result = self._ssh_command(host, command, timeout=300)
            entries = self._parse_du(result.stdout, "")
            warning = None
            if result.returncode:
                detail, error_count = self._remote_directory_error(
                    result.stderr, path, result.returncode
                )
                if result.returncode == 255:
                    raise ConnectionError(f"SSH 连接失败：{detail}")
                if not entries:
                    raise OSError(detail)
                suffix = f"（另有 {error_count - 1} 处无法读取）" if error_count > 1 else ""
                warning = f"{detail}{suffix}，当前占用结果可能不完整"
        children = children_from_entries()
        result = {
            "host_id": host_id, "mount": mount, "path": path, "depth": depth,
            "delete_enabled": self.config.allow_delete,
            "delete_min_depth": self.config.delete_min_depth,
            "children": children,
        }
        if not self.config.demo and not host.get("local") and warning:
            result["warning"] = warning
        return result

    def list_directories(self, host_id: str, mount: str, path: str) -> dict[str, Any]:
        """返回缓存的目录层级，未命中时启动一个共享后台任务。"""
        host, mount, path, depth, _ = self._validate_directory(host_id, mount, path)
        if self.config.demo:
            return self._scan_directories(host_id, mount, path)
        key = (host_id, mount, path)
        now = time.monotonic()
        with self.lock:
            cached = self.directory_cache.get(key)
            if cached and now - cached[0] < DIRECTORY_CACHE_TTL:
                return cached[1]
            job = self.directory_jobs.get(key)
            if job and job["state"] == "error":
                self.directory_jobs.pop(key, None)
                raise OSError(job["error"])
            if not job:
                generation = self.directory_generation.get(host_id, 0)
                self.directory_jobs[key] = {
                    "state": "running", "generation": generation, "children": [],
                }

                def scan() -> None:
                    """仅在机器代次仍有效时发布当前 KV 任务结果。"""
                    try:
                        result = self._scan_directories(host_id, mount, path)
                    except subprocess.TimeoutExpired:
                        error = "目录统计超过 5 分钟，请稍后重试或先展开更小的挂载点"
                        with self.lock:
                            current = self.directory_jobs.get(key)
                            if current and current["generation"] == generation:
                                current.update({"state": "error", "error": error})
                        return
                    except Exception as exc:
                        with self.lock:
                            current = self.directory_jobs.get(key)
                            if current and current["generation"] == generation:
                                current.update({"state": "error", "error": str(exc)})
                        return
                    with self.lock:
                        current = self.directory_jobs.get(key)
                        if (current and current["generation"] == generation
                                and self.directory_generation.get(host_id, 0) == generation):
                            cached_result = {
                                **result,
                                "cache_expires_at": now_ms() + DIRECTORY_CACHE_TTL * 1000,
                            }
                            self.directory_cache[key] = (time.monotonic(), cached_result)
                        self.directory_jobs.pop(key, None)

                threading.Thread(
                    target=scan, daemon=True,
                    name=f"directory-{safe_id(host_id)}",
                ).start()
            partial_children = list(self.directory_jobs.get(key, {}).get("children", []))
        return {
            "host_id": host_id, "mount": mount, "path": path, "depth": depth,
            "delete_enabled": self.config.allow_delete,
            "delete_min_depth": self.config.delete_min_depth,
            "pending": True,
            "message": "正在后台统计目录占用", "children": partial_children,
        }

    def _invalidate_directory_cache(self, host_id: str) -> None:
        """使一台机器的全部目录结果和在途任务失效。"""
        with self.lock:
            self.directory_generation[host_id] = self.directory_generation.get(host_id, 0) + 1
            self.directory_cache = {
                key: value for key, value in self.directory_cache.items() if key[0] != host_id
            }
            self.directory_jobs = {
                key: value for key, value in self.directory_jobs.items() if key[0] != host_id
            }

    @staticmethod
    def _clear_local_contents(target: Path) -> int:
        """删除同一设备上的全部后代，同时保留目标目录本身。"""
        root_device = target.stat().st_dev
        removed = 0

        def remove_entry(path: Path) -> None:
            """递归删除一个后代节点，同时拒绝进入子挂载点。"""
            nonlocal removed
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                if path.stat().st_dev != root_device:
                    return
                with os.scandir(path) as iterator:
                    for child in iterator:
                        remove_entry(Path(child.path))
                path.rmdir()
            else:
                path.unlink()
            removed += 1

        with os.scandir(target) as iterator:
            for child in iterator:
                remove_entry(Path(child.path))
        return removed

    def clear_directory(self, host_id: str, mount: str, path: str) -> dict[str, Any]:
        """再次执行服务端安全校验后，清空符合条件的目录。"""
        if not self.config.allow_delete:
            raise PermissionError("未启用清空目录功能，请使用 --allow-delete 启动")
        host, mount, path, depth, _ = self._validate_directory(host_id, mount, path)
        if depth < self.config.delete_min_depth:
            raise ValueError(
                f"仅允许清空挂载点下第 {self.config.delete_min_depth} 级及更深目录"
            )
        if self.config.demo:
            self.demo_cleared_directories.add((host_id, path))
            removed = len(DEMO_DIRECTORY_TREE.get(path, []))
        elif host.get("local"):
            target = self._resolved_local_directory(mount, path)
            removed = self._clear_local_contents(target)
        else:
            root_q, target_q = shlex.quote(mount), shlex.quote(path)
            command = (
                f"root=$(readlink -f -- {root_q}) || exit 20; "
                f"target=$(readlink -f -- {target_q}) || exit 21; "
                '[ -d "$target" ] || exit 22; '
                'if [ "$root" != "/" ]; then case "$target" in "$root"|"$root"/*) ;; *) exit 23;; esac; fi; '
                # `-mindepth 1` 是保留目标目录本身的关键约束。
                'find "$target" -xdev -mindepth 1 -depth -print0 -delete'
            )
            result = self._ssh_command(host, command, timeout=60)
            if result.returncode:
                detail = os.fsdecode(result.stderr).strip() or "远程目录清空失败"
                raise OSError(detail)
            removed = len([item for item in result.stdout.split(b"\0") if item])
        self._invalidate_directory_cache(host_id)
        if not self.config.demo:
            self._refresh_host_soon(host_id)
        return {"ok": True, "path": path, "removed": removed, "cleared_at": now_ms()}

    def delete_host(self, host_id: str) -> None:
        """原子删除一台机器，同时保证配置中至少保留一台。"""
        with self.lock:
            index = next((i for i, host in enumerate(self.hosts) if host["id"] == host_id), None)
            if index is None:
                raise KeyError(host_id)
            if len(self.hosts) <= 1:
                raise ValueError("至少需要保留一台机器")
            removed = self.hosts.pop(index)
            data = self.data.pop(host_id, None)
            health = self.host_health.pop(host_id, None)
            collected_at = self.snapshot_collected_at.pop(host_id, None)
            generation = self.snapshot_generation.get(host_id, 0)
            try:
                self._persist_hosts()
            except Exception:
                self.hosts.insert(index, removed)
                if data is not None:
                    self.data[host_id] = data
                if health is not None:
                    self.host_health[host_id] = health
                if collected_at is not None:
                    self.snapshot_collected_at[host_id] = collected_at
                raise
            self.snapshot_generation[host_id] = generation + 1
        self._invalidate_directory_cache(host_id)

    def _remove_cached_process(self, host_id: str, pid: int) -> bool:
        """从当前可见快照中乐观移除已经终止的 PID。"""
        with self.lock:
            data = self.data.get(host_id)
            if not data:
                return False
            processes = data.get("processes", [])
            remaining = [process for process in processes if process["pid"] != pid]
            if len(remaining) == len(processes):
                return False
            data["processes"] = remaining
            summary = data.get("summary", {})
            summary["process_count"] = len(remaining)
            cores = max(1, int(summary.get("cores", 1)))
            summary["cpu_percent"] = round(
                sum(process.get("cpu", 0) for process in remaining) / cores, 1
            )
            data["updated_at"] = now_ms()
            return True

    def _refresh_host_soon(self, host_id: str) -> None:
        """立即使快照失效，稍等远程状态稳定后重新采集。"""
        self._invalidate_snapshot(host_id)

        def collect() -> None:
            """在服务未关闭且机器未删除时执行延迟刷新。"""
            if self.stop.wait(0.35):
                return
            try:
                self.collect_host(host_id)
            except KeyError:
                pass

        threading.Thread(
            target=collect, daemon=True, name=f"refresh-{safe_id(host_id)}"
        ).start()

    def terminate(self, host_id: str, pid: int) -> None:
        """通过功能开关和 PID 安全校验后发送 SIGTERM。"""
        if not self.config.allow_kill:
            raise PermissionError("未启用结束进程功能")
        if pid <= 1 or pid == os.getpid():
            raise ValueError("拒绝结束系统关键进程")
        if self.config.demo:
            if not self._remove_cached_process(host_id, pid):
                raise KeyError(pid)
            self.demo_terminated_processes.add((host_id, pid))
            return
        host, _ = self.get_host(host_id)
        if host.get("local"):
            os.kill(pid, signal.SIGTERM)
        else:
            destination = f'{host.get("user", "") + "@" if host.get("user") else ""}{host["address"]}'
            subprocess.run(["ssh", "-o", "BatchMode=yes", "-p", str(host.get("port", 22)),
                            destination, "kill", "-TERM", "--", str(pid)],
                           check=True, timeout=8, capture_output=True)
        self._remove_cached_process(host_id, pid)
        self._refresh_host_soon(host_id)


def make_handler(app: MonitorApp):
    """将一个 MonitorApp 实例绑定到标准库多线程 HTTP 处理器。"""

    class Handler(BaseHTTPRequestHandler):
        """提供单页面、JSON API 和可选的 Basic Auth 验证。"""

        server_version = "Monitor/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            """输出适合终端直接启动场景的紧凑访问日志。"""
            print(f"{self.address_string()} - {fmt % args}")

        def authenticated(self) -> bool:
            """配置账号后，对每个页面和 API 请求执行身份验证。"""
            if not app.config.username:
                return True
            expected = base64.b64encode(
                f"{app.config.username}:{app.config.password or ''}".encode()
            ).decode()
            if self.headers.get("Authorization") == f"Basic {expected}":
                return True
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("WWW-Authenticate", 'Basic realm="Monitor"')
            self.end_headers()
            return False

        def send_json(self, value: Any, status: int = 200) -> None:
            """序列化紧凑 JSON，并在浏览器支持时压缩较大的响应。"""
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
            compressed = len(body) >= 1024 and "gzip" in self.headers.get(
                "Accept-Encoding", ""
            ).lower()
            if compressed:
                body = gzip.compress(body, compresslevel=5)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            if compressed:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def body(self) -> dict[str, Any]:
            """解码一个小型 JSON 请求体。"""
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self) -> None:
            """处理读取请求；元数据和健康接口不会触发资源采集。"""
            if not self.authenticated():
                return
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path in {"/", "/index.html"}:
                body = INDEX_PATH.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/health":
                self.send_json({
                    "ok": True, "interval": app.config.interval,
                    "allow_kill": app.config.allow_kill,
                    "allow_delete": app.config.allow_delete,
                    "delete_min_depth": app.config.delete_min_depth,
                })
                return
            if path == "/api/hosts":
                self.send_json({
                    "hosts": app.host_list(),
                    "snapshot_interval_seconds": app.config.interval,
                })
                return
            if path == "/api/host-statuses":
                self.send_json({
                    "statuses": app.host_status_list(),
                    "interval_seconds": HOST_STATUS_INTERVAL,
                    "stale_after_seconds": HOST_STATUS_STALE_AFTER,
                })
                return
            if path == "/api/snapshots":
                self.send_json({"snapshots": app.snapshot_list()})
                return
            snapshot_match = re.fullmatch(r"/api/hosts/([^/]+)/snapshot", path)
            if snapshot_match:
                try:
                    host, snapshot = app.collect_host(
                        urllib.parse.unquote(snapshot_match.group(1))
                    )
                    self.send_json(app._host_change_result(host, snapshot))
                except KeyError:
                    self.send_json({"error": "机器不存在"}, 404)
                return
            directory_match = re.fullmatch(r"/api/hosts/([^/]+)/directories", path)
            if directory_match:
                query = urllib.parse.parse_qs(parsed.query)
                try:
                    result = app.list_directories(
                        urllib.parse.unquote(directory_match.group(1)),
                        query.get("mount", [""])[0], query.get("path", [""])[0],
                    )
                    self.send_json(result)
                except KeyError:
                    self.send_json({"error": "机器不存在"}, 404)
                except ValueError as exc:
                    self.send_json({"error": str(exc)}, 400)
                except (OSError, subprocess.SubprocessError) as exc:
                    self.send_json({"error": str(exc)}, 500)
                return
            match = re.fullmatch(r"/api/hosts/([^/]+)/(processes|filesystems)", path)
            if match:
                try:
                    host, data = app.collect_host(urllib.parse.unquote(match.group(1)))
                except KeyError:
                    self.send_json({"error": "机器不存在"}, 404)
                    return
                key = match.group(2)
                self.send_json({
                    "host": {"id": host["id"], "name": host["name"]},
                    "status": data["status"], "updated_at": data["updated_at"],
                    "summary": data["summary"], key: data[key],
                })
                return
            self.send_json({"error": "接口不存在"}, 404)

        def do_PATCH(self) -> None:
            """验证连接后编辑一台机器。"""
            if not self.authenticated():
                return
            match = re.fullmatch(r"/api/hosts/([^/]+)", urllib.parse.urlparse(self.path).path)
            if not match:
                self.send_json({"error": "接口不存在"}, 404)
                return
            try:
                result = app.update_host(urllib.parse.unquote(match.group(1)), self.body())
                self.send_json(result)
            except KeyError:
                self.send_json({"error": "机器不存在"}, 404)
            except (ValueError, ConnectionError) as exc:
                self.send_json({"error": str(exc)}, 400)
            except (OSError, json.JSONDecodeError) as exc:
                self.send_json({"error": f"保存失败：{exc}"}, 500)

        def do_POST(self) -> None:
            """处理连接测试、新增机器和显式开启的危险操作。"""
            if not self.authenticated():
                return
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/hosts/test":
                try:
                    self.send_json(app.test_host(self.body()))
                except (ValueError, ConnectionError) as exc:
                    self.send_json({"error": str(exc)}, 400)
                except json.JSONDecodeError as exc:
                    self.send_json({"error": f"请求格式错误：{exc}"}, 400)
                return
            if path == "/api/hosts":
                try:
                    self.send_json(app.add_host(self.body()), 201)
                except (ValueError, ConnectionError) as exc:
                    self.send_json({"error": str(exc)}, 400)
                except (OSError, json.JSONDecodeError) as exc:
                    self.send_json({"error": f"保存失败：{exc}"}, 500)
                return
            directory_match = re.fullmatch(r"/api/hosts/([^/]+)/directories/clear", path)
            if directory_match:
                try:
                    body = self.body()
                    result = app.clear_directory(
                        urllib.parse.unquote(directory_match.group(1)),
                        str(body.get("mount", "")), str(body.get("path", "")),
                    )
                    self.send_json(result)
                except PermissionError as exc:
                    self.send_json({"error": str(exc)}, 403)
                except KeyError:
                    self.send_json({"error": "机器不存在"}, 404)
                except ValueError as exc:
                    self.send_json({"error": str(exc)}, 400)
                except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
                    self.send_json({"error": str(exc)}, 500)
                return
            match = re.fullmatch(r"/api/hosts/([^/]+)/processes/(\d+)/terminate", path)
            if not match:
                self.send_json({"error": "接口不存在"}, 404)
                return
            try:
                app.terminate(urllib.parse.unquote(match.group(1)), int(match.group(2)))
                self.send_json({"ok": True})
            except PermissionError as exc:
                self.send_json({"error": str(exc)}, 403)
            except (KeyError, ProcessLookupError):
                self.send_json({"error": "进程不存在"}, 404)
            except (ValueError, subprocess.SubprocessError, OSError) as exc:
                self.send_json({"error": str(exc)}, 400)

        def do_DELETE(self) -> None:
            """删除一台已配置机器，同时保留最后一台机器。"""
            if not self.authenticated():
                return
            match = re.fullmatch(r"/api/hosts/([^/]+)", urllib.parse.urlparse(self.path).path)
            if not match:
                self.send_json({"error": "接口不存在"}, 404)
                return
            try:
                app.delete_host(urllib.parse.unquote(match.group(1)))
                self.send_json({"ok": True})
            except KeyError:
                self.send_json({"error": "机器不存在"}, 404)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
            except OSError as exc:
                self.send_json({"error": f"保存失败：{exc}"}, 500)

    return Handler


def parse_args() -> argparse.Namespace:
    """解析保持精简的部署参数和能力开关。"""

    def delete_depth(value: str) -> int:
        """解析并校验可选的 `--allow-delete` 层级参数。"""
        try:
            depth = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("目录清空层级必须是整数") from None
        if depth < 3:
            raise argparse.ArgumentTypeError("目录清空层级必须至少为 3")
        return depth

    parser = argparse.ArgumentParser(description="极简多机器进程监控")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--hosts", type=Path)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--allow-kill", action="store_true")
    parser.add_argument(
        "--allow-delete", nargs="?", type=delete_depth, const=3, metavar="LEVEL",
        help="允许从网页清空挂载点下第 LEVEL 级及更深目录的内容（省略 LEVEL 时为 3）",
    )
    parser.add_argument("--username", default=os.environ.get("MONITOR_USERNAME"))
    parser.add_argument("--password", default=os.environ.get("MONITOR_PASSWORD"))
    return parser.parse_args()


def main() -> None:
    """校验暴露规则、启动后台任务，并持续服务直到被中断。"""
    args = parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit("HTTP 端口必须在 1–65535 之间")
    if bool(args.username) != bool(args.password):
        raise SystemExit("MONITOR_USERNAME 和 MONITOR_PASSWORD 必须同时配置")
    # 内置 HTTP 服务监听回环地址以外的接口时必须启用 Basic Auth；
    # TLS 仍由可信反向代理或私有网络负责。
    if (args.bind not in {"127.0.0.1", "::1", "localhost"}
            and not args.demo and not args.username):
        raise SystemExit("非本机监听必须配置 MONITOR_USERNAME 和 MONITOR_PASSWORD")
    config = Config(
        args.bind, args.port, max(1, args.interval), args.demo,
        args.allow_kill, args.username, args.password,
        args.allow_delete is not None, args.allow_delete or 3,
    )
    app = MonitorApp(config, args.hosts)
    app.start()
    server = ThreadingHTTPServer((args.bind, args.port), make_handler(app))
    print(f"Monitor running at http://{args.bind}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        app.close()


if __name__ == "__main__":
    main()
