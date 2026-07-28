import json
import gzip
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from monitor import Config, MonitorApp, make_handler, parse_args, parse_df, parse_ps


class ParserTests(unittest.TestCase):
    def test_process_parent_is_resolved(self):
        text = (
            "1 0 root systemd 1 0.1 1000 0.1 Mon Jul 27 08:00:00 2026\n"
            "42 1 web nginx 4 12.5 2048 1.2 Mon Jul 27 09:00:00 2026\n"
        )
        rows = parse_ps(text)
        self.assertEqual(rows[1]["parent_name"], "systemd")
        self.assertEqual(rows[1]["ppid"], 1)

    def test_filesystems(self):
        rows = parse_df("/dev/vda1 ext4 1000 750 250 75% /\n")
        self.assertEqual(rows[0]["used_percent"], 75)
        self.assertEqual(rows[0]["mount"], "/")

    def test_allow_delete_depth_argument(self):
        with mock.patch.object(sys, "argv", ["monitor.py", "--allow-delete"]):
            self.assertEqual(parse_args().allow_delete, 3)
        with mock.patch.object(
            sys, "argv", ["monitor.py", "--allow-delete", "5"]
        ):
            self.assertEqual(parse_args().allow_delete, 5)
        with mock.patch.object(
            sys, "argv", ["monitor.py", "--allow-delete", "2"]
        ), mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                parse_args()


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = MonitorApp(Config("127.0.0.1", 0, 60, True, True, None, None, True), None)
        cls.app.start()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.app.close()

    def request(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, json.load(response)

    def test_hosts_and_processes(self):
        status, data = self.request("/api/hosts")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["hosts"]), 4)
        _, payload = self.request("/api/hosts/demo-1/processes")
        self.assertGreater(len(payload["processes"]), 5)
        self.assertIn("parent_name", payload["processes"][0])
        _, selected = self.request("/api/hosts/demo-1/snapshot")
        self.assertEqual(selected["host"]["home_path"], "/home/demo-user")

    def test_rename(self):
        _, data = self.request("/api/hosts/demo-2", "PATCH", {"name": "应用服务器"})
        self.assertEqual(data["name"], "应用服务器")

    def test_terminate_demo_process(self):
        _, payload = self.request("/api/hosts/demo-3/processes")
        pid = payload["processes"][0]["pid"]
        status, data = self.request(f"/api/hosts/demo-3/processes/{pid}/terminate", "POST", {})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        _, after = self.request("/api/hosts/demo-3/processes")
        self.assertNotIn(pid, [row["pid"] for row in after["processes"]])

    def test_health(self):
        status, payload = self.request("/api/health")
        self.assertEqual((status, payload["ok"]), (200, True))
        self.assertEqual(payload["delete_min_depth"], 3)

    def test_host_statuses_are_small_cached_results(self):
        status, payload = self.request("/api/host-statuses")
        self.assertEqual(status, 200)
        self.assertEqual(
            (payload["interval_seconds"], payload["stale_after_seconds"]), (30, 75)
        )
        self.assertEqual(len(payload["statuses"]), 4)
        self.assertTrue(all(
            set(item) == {"id", "status", "checked_at", "checking"}
            for item in payload["statuses"]
        ))

    def test_selected_snapshot_is_gzip_compressed(self):
        request = urllib.request.Request(
            self.base + "/api/hosts/demo-2/snapshot",
            headers={"Accept-Encoding": "gzip"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.headers.get("Content-Encoding"), "gzip")
            payload = json.loads(gzip.decompress(response.read()))
        self.assertEqual(payload["host"]["id"], "demo-2")
        self.assertIn("processes", payload["snapshot"])

    def test_host_crud(self):
        status, created = self.request(
            "/api/hosts", "POST",
            {"name": "新服务器", "address": "10.0.0.8", "user": "monitor", "port": 22},
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["host"]["address"], "10.0.0.8")
        self.assertIn("processes", created["snapshot"])
        host_id = created["id"]
        _, updated = self.request(
            f"/api/hosts/{host_id}", "PATCH",
            {"name": "新服务器 2", "address": "10.0.0.9", "user": "monitor", "port": 2222},
        )
        self.assertEqual(updated["name"], "新服务器 2")
        status, deleted = self.request(f"/api/hosts/{host_id}", "DELETE")
        self.assertEqual((status, deleted["ok"]), (200, True))

    def test_host_connection_test_does_not_save(self):
        before = len(self.app.host_list())
        status, result = self.request(
            "/api/hosts/test", "POST",
            {"name": "仅测试", "address": "10.0.0.20", "user": "monitor", "port": 22},
        )
        self.assertEqual((status, result["ok"]), (200, True))
        self.assertEqual(len(self.app.host_list()), before)

    def test_directory_tree_and_clear(self):
        _, root = self.request(
            "/api/hosts/demo-1/directories?mount=%2F&path=%2F"
        )
        self.assertEqual(root["children"][0]["path"], "/home")
        _, level_two = self.request(
            "/api/hosts/demo-1/directories?mount=%2F&path=%2Fhome%2Fdemo-user"
        )
        projects = next(item for item in level_two["children"] if item["name"] == "projects")
        self.assertTrue(projects["can_delete"])
        self.assertEqual(projects["delete_min_depth"], 3)
        status, result = self.request(
            "/api/hosts/demo-1/directories/clear", "POST",
            {"mount": "/", "path": "/home/demo-user/projects"},
        )
        self.assertEqual((status, result["ok"]), (200, True))
        _, cleared = self.request(
            "/api/hosts/demo-1/directories?mount=%2F&path=%2Fhome%2Fdemo-user%2Fprojects"
        )
        self.assertEqual(cleared["children"], [])


class CollectorTests(unittest.TestCase):
    def test_only_selected_host_is_collected_and_cached(self):
        app = MonitorApp(Config("127.0.0.1", 0, 5, True, False, None, None), None)
        app.hosts = [{"id": f"h{i}", "name": f"H{i}", "address": "demo"} for i in range(4)]
        calls = []

        def selected_snapshot(host, index):
            calls.append(host["id"])
            return {"processes": [], "filesystems": [], "summary": {},
                    "status": "normal", "updated_at": 1, "error": None}

        app._collect_host = selected_snapshot
        app.start()
        time.sleep(0.03)
        self.assertEqual((calls, app.data), ([], {}))
        app.collect_host("h2")
        self.assertEqual(calls, ["h2"])
        self.assertEqual(list(app.data), ["h2"])
        app.collect_host("h2")
        self.assertEqual(calls, ["h2", "h2"])
        self.assertEqual(list(app.data), ["h2"])
        app.close()

    def test_reverse_tunnel_peer_is_detected_without_touching_normal_ssh(self):
        app = MonitorApp(Config("127.0.0.1", 0, 5, False, False, None, None), None)
        listener = (
            "LISTEN 0 128 127.0.0.1:2222 0.0.0.0:* "
            'users:(("sshd",pid=712015,fd=8))\n'
        )
        connection = (
            "0 0 10.8.0.7:22 8.8.8.8:50960 "
            'users:(("sshd",pid=712015,fd=4))\n'
        )
        responses = [
            subprocess.CompletedProcess([], 0, listener, ""),
            subprocess.CompletedProcess([], 0, connection, ""),
        ]
        with mock.patch("monitor.subprocess.run", side_effect=responses) as run:
            peer = app._tunnel_peer_address({
                "id": "tunnel", "address": "127.0.0.1", "port": 2222, "local": False,
            })
            self.assertEqual(peer, "8.8.8.8")
            self.assertEqual(run.call_count, 2)
        with mock.patch("monitor.subprocess.run") as run:
            self.assertIsNone(app._tunnel_peer_address({
                "id": "normal", "address": "10.0.0.8", "port": 22, "local": False,
            }))
            run.assert_not_called()


class HostHealthTests(unittest.TestCase):
    def make_app(self):
        app = MonitorApp(
            Config("127.0.0.1", 0, 5, False, False, None, None), None
        )
        app.hosts = [{
            "id": "remote", "name": "Remote", "address": "10.0.0.8",
            "user": "monitor", "port": 22, "local": False,
        }]
        return app

    def test_failure_transitions_then_success_and_stale_yellow(self):
        app = self.make_app()
        self.assertEqual(app.host_status_list()[0]["status"], "warning")
        self.assertEqual(
            app._record_health_result("remote", False, "temporary"), "warning"
        )
        self.assertEqual(
            app._record_health_result("remote", False, "still down"), "offline"
        )
        self.assertEqual(app.host_status_list()[0]["status"], "offline")
        self.assertEqual(app._record_health_result("remote", True), "normal")
        self.assertEqual(app.host_status_list()[0]["status"], "normal")

        app.host_health["remote"]["checked_at"] = int(time.time() * 1000) - 76_000
        stale = app.host_status_list()[0]
        self.assertEqual((stale["status"], stale["checking"]), ("warning", False))

    def test_failed_probe_is_retried_before_red(self):
        app = self.make_app()
        with mock.patch.object(
            app, "_probe_host",
            side_effect=[(False, "first"), (False, "second")],
        ) as probe, mock.patch("monitor.HOST_STATUS_RETRY_DELAY", 0):
            app.check_host_health()
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(app.host_status_list()[0]["status"], "offline")

    def test_remote_probe_runs_only_ssh_true(self):
        app = self.make_app()
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch("monitor.subprocess.run", return_value=completed) as run:
            self.assertEqual(app._probe_host(app.hosts[0]), (True, None))
        command = run.call_args.args[0]
        self.assertEqual(command[-1], "true")
        self.assertIn("BatchMode=yes", command)
        self.assertIn("ConnectionAttempts=1", command)

    def test_local_probe_does_not_open_ssh(self):
        app = self.make_app()
        with mock.patch("monitor.subprocess.run") as run:
            self.assertEqual(
                app._probe_host({
                    "id": "local", "address": "127.0.0.1", "local": True,
                }),
                (True, None),
            )
        run.assert_not_called()

    def test_late_result_for_old_connection_is_ignored(self):
        app = self.make_app()
        old_host = dict(app.hosts[0])
        app.hosts[0]["address"] = "10.0.0.9"
        status = app._record_health_result(
            "remote", True, expected_host=old_host
        )
        self.assertEqual(status, "warning")
        self.assertEqual(app.host_status_list()[0]["status"], "warning")


class PersistenceTests(unittest.TestCase):
    def test_default_hosts_prefers_ignored_local_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            committed = root / "hosts.json"
            private = root / "hosts.local.json"
            committed.write_text(json.dumps({
                "hosts": [{
                    "id": "public", "name": "脱敏默认", "address": "127.0.0.1",
                    "port": 22, "local": True,
                }]
            }))
            private.write_text(json.dumps({
                "hosts": [{
                    "id": "private", "name": "本机私有配置", "address": "127.0.0.1",
                    "port": 22, "local": True,
                }]
            }))

            with mock.patch("monitor.ROOT", root):
                app = MonitorApp(
                    Config("127.0.0.1", 0, 60, False, False, None, None), None
                )
            self.assertEqual(app.hosts_path, private)
            self.assertEqual(app.hosts[0]["id"], "private")

            private.unlink()
            with mock.patch("monitor.ROOT", root):
                app = MonitorApp(
                    Config("127.0.0.1", 0, 60, False, False, None, None), None
                )
            self.assertEqual(app.hosts_path, committed)
            self.assertEqual(app.hosts[0]["id"], "public")

    def test_hosts_are_persisted_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            hosts_path = Path(directory) / "hosts.json"
            app = MonitorApp(Config("127.0.0.1", 0, 60, False, False, None, None), hosts_path)
            created = app.add_host(
                {"name": "本机副本", "address": "localhost", "port": 22, "local": True}
            )
            app.update_host(created["id"], {"name": "持久化节点"})
            saved = json.loads(hosts_path.read_text())
            self.assertEqual(len(saved["hosts"]), 2)
            self.assertEqual(saved["hosts"][1]["name"], "持久化节点")
            app.delete_host(created["id"])
            self.assertEqual(len(json.loads(hosts_path.read_text())["hosts"]), 1)

    def test_invalid_port_is_rejected(self):
        app = MonitorApp(Config("127.0.0.1", 0, 60, True, False, None, None), None)
        with self.assertRaisesRegex(ValueError, "1–65535"):
            app.add_host({"name": "错误节点", "address": "10.0.0.2", "port": 70000})

    def test_last_host_cannot_be_deleted(self):
        app = MonitorApp(Config("127.0.0.1", 0, 60, True, False, None, None), None)
        app.hosts = app.hosts[:1]
        with self.assertRaisesRegex(ValueError, "至少需要保留一台机器"):
            app.delete_host(app.hosts[0]["id"])

    def test_loopback_frontend_host_uses_ssh(self):
        app = MonitorApp(Config("127.0.0.1", 0, 60, False, False, None, None), None)
        host = app._normalize_host({
            "name": "11", "address": "127.0.0.1", "user": "demo-user", "port": 2222,
        })
        self.assertFalse(host["local"])
        self.assertEqual((host["user"], host["port"]), ("demo-user", 2222))


class DirectorySafetyTests(unittest.TestCase):
    def make_app(self, mount: Path, allow_delete=True, delete_min_depth=3):
        app = MonitorApp(
            Config(
                "127.0.0.1", 0, 60, False, False, None, None,
                allow_delete, delete_min_depth,
            ),
            mount / "hosts.json",
        )
        app.data["local"] = {
            "processes": [], "summary": {}, "status": "normal", "updated_at": 1,
            "filesystems": [{
                "mount": str(mount), "device": "/dev/test", "type": "ext4",
                "total_bytes": 1_000_000, "used_bytes": 500_000,
                "available_bytes": 500_000, "used_percent": 50.0,
            }],
        }
        app.stop.set()
        return app

    def wait_directories(self, app, mount, path):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = app.list_directories("local", str(mount), str(path))
            if not result.get("pending"):
                return result
            time.sleep(0.02)
        self.fail("目录异步统计未完成")

    def make_remote_app(self):
        app = MonitorApp(
            Config("127.0.0.1", 0, 60, False, False, None, None), None
        )
        app.hosts = [{
            "id": "remote", "name": "Remote", "address": "127.0.0.1",
            "user": "demo-user", "port": 2222, "local": False,
        }]
        app.data["remote"] = {
            "processes": [], "summary": {}, "status": "normal", "updated_at": 1,
            "filesystems": [{
                "mount": "/", "device": "/dev/sda2", "type": "ext4",
                "total_bytes": 1_000_000, "used_bytes": 500_000,
                "available_bytes": 500_000, "used_percent": 50.0,
            }],
        }
        return app

    def test_remote_partial_results_keep_specific_permission_warning(self):
        app = self.make_remote_app()
        completed = subprocess.CompletedProcess(
            [], 123, b"4096\t/visible\0",
            b"du: cannot read directory '/home/lighthouse': Permission denied\n",
        )
        with mock.patch.object(app, "_ssh_command", return_value=completed) as ssh:
            result = app._scan_directories("remote", "/", "/")

        self.assertEqual(result["children"][0]["path"], "/visible")
        self.assertEqual(
            result["warning"],
            "无法读取 /home/lighthouse：Permission denied，当前占用结果可能不完整",
        )
        command = ssh.call_args.args[1]
        self.assertIn('target_dev=$(stat -c %d -- "$target")', command)
        self.assertIn('[ "$item_dev" = "$0" ] || exit 0', command)

    def test_remote_unreadable_target_and_ssh_failure_are_distinct(self):
        app = self.make_remote_app()
        unreadable = subprocess.CompletedProcess([], 25, b"", b"")
        with mock.patch.object(app, "_ssh_command", return_value=unreadable):
            with self.assertRaisesRegex(
                OSError, "无法读取 /root：Permission denied"
            ):
                app._scan_directories("remote", "/", "/root")

        disconnected = subprocess.CompletedProcess(
            [], 255, b"", b"ssh: connect to host 127.0.0.1: Connection timed out\n"
        )
        with mock.patch.object(app, "_ssh_command", return_value=disconnected):
            with self.assertRaisesRegex(ConnectionError, "SSH 连接失败.*timed out"):
                app._scan_directories("remote", "/", "/")

    def test_real_directory_tree_and_clear_preserves_target(self):
        with tempfile.TemporaryDirectory() as directory:
            mount = Path(directory)
            target = mount / "one" / "two" / "three"
            target.mkdir(parents=True)
            (target / "file.bin").write_bytes(b"x" * 4096)
            nested = target / "nested"
            nested.mkdir()
            (nested / "inside.txt").write_text("inside")
            outside = mount / "outside.txt"
            outside.write_text("keep")
            (target / "outside-link").symlink_to(outside)
            app = self.make_app(mount)

            first = app.list_directories("local", str(mount), str(mount))
            self.assertTrue(first["pending"])
            root = self.wait_directories(app, mount, mount)
            self.assertEqual(root["children"][0]["name"], "one")
            self.assertTrue(app.directory_cache)
            with self.assertRaisesRegex(ValueError, "第 3 级"):
                app.clear_directory("local", str(mount), str(mount / "one" / "two"))

            result = app.clear_directory("local", str(mount), str(target))
            self.assertTrue(result["ok"])
            self.assertEqual(app.directory_cache, {})
            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])
            self.assertEqual(outside.read_text(), "keep")

    def test_configurable_clear_depth_preserves_target_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            mount = Path(directory)
            level_three = mount / "one" / "two" / "three"
            target = level_three / "four"
            target.mkdir(parents=True)
            (target / "keep-directory-delete-contents.txt").write_text("clear me")
            app = self.make_app(mount, delete_min_depth=4)

            with self.assertRaisesRegex(ValueError, "第 4 级"):
                app.clear_directory("local", str(mount), str(level_three))
            result = app.clear_directory("local", str(mount), str(target))

            self.assertTrue(result["ok"])
            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])

    def test_directory_scan_returns_immediately_and_is_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            mount = Path(directory)
            (mount / "one").mkdir()
            app = self.make_app(mount)
            original = app._scan_directories

            def slow_scan(*args):
                time.sleep(0.12)
                return original(*args)

            app._scan_directories = slow_scan
            started = time.monotonic()
            first = app.list_directories("local", str(mount), str(mount))
            self.assertTrue(first["pending"])
            self.assertLess(time.monotonic() - started, 0.05)
            result = self.wait_directories(app, mount, mount)
            self.assertEqual(result["children"][0]["name"], "one")
            cached = app.list_directories("local", str(mount), str(mount))
            self.assertFalse(cached.get("pending", False))
            app.directory_cache = {
                key: (time.monotonic() - 601, value[1])
                for key, value in app.directory_cache.items()
            }
            app.tunnel_peer_cache[("127.0.0.1", 2222)] = (
                time.monotonic() - 601, "8.8.8.8",
            )
            app._prune_memory_caches()
            self.assertEqual((app.directory_cache, app.tunnel_peer_cache), ({}, {}))

    def test_directory_cannot_escape_mount_or_delete_when_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            mount = Path(directory)
            (mount / "one" / "two" / "three").mkdir(parents=True)
            app = self.make_app(mount, allow_delete=False)
            with self.assertRaisesRegex(PermissionError, "--allow-delete"):
                app.clear_directory(
                    "local", str(mount), str(mount / "one" / "two" / "three")
                )
            with self.assertRaisesRegex(ValueError, "不属于指定挂载点"):
                app.list_directories("local", str(mount), str(mount.parent))


if __name__ == "__main__":
    unittest.main()
