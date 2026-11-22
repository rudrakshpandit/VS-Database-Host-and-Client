"""
Automated Test Suite for Windows Auto-Start, API Contract, Client Background Daemon & Memory Benchmarks
Location: tests/test_autostart_and_daemon.py

Verifies:
1. Host Auto-Start Windows Registry & Task Scheduler enable, inspect, disable functionality.
2. /api/system/autostart API endpoint contract (GET & POST) and /api/desktop/lifecycle-policy.
3. Client background daemon (client_daemon.pyw) execution, CLI flags, health heartbeat, and state JSON writing.
4. Memory Benchmarks:
   - client_daemon.pyw private working set < 35 MB (Target: ~15-25 MB).
   - Headless server_sidecar private working set < 85 MB (Target: ~45-70 MB).
"""

import unittest
import os
import sys
import json
import time
import winreg
import socket
import urllib.request
import urllib.error
import subprocess
import ctypes
from ctypes import wintypes
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import server

# Win32 Memory Profiling Structures
class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ('cb', wintypes.DWORD),
        ('PageFaultCount', wintypes.DWORD),
        ('PeakWorkingSetSize', ctypes.c_size_t),
        ('WorkingSetSize', ctypes.c_size_t),
        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
        ('QuotaPagedPoolUsage', ctypes.c_size_t),
        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
        ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
        ('PagefileUsage', ctypes.c_size_t),
        ('PeakPagefileUsage', ctypes.c_size_t),
        ('PrivateUsage', ctypes.c_size_t),
    ]

def get_process_memory_info_mb(pid: int) -> dict:
    """Returns memory metrics (WorkingSet, PrivateUsage, PeakWorkingSet) in Megabytes for a given PID."""
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    h_proc = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h_proc:
        return {"working_set_mb": 0.0, "private_usage_mb": 0.0, "peak_ws_mb": 0.0}
    try:
        pmc = PROCESS_MEMORY_COUNTERS_EX()
        pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(h_proc, ctypes.byref(pmc), pmc.cb)
        if not ok:
            return {"working_set_mb": 0.0, "private_usage_mb": 0.0, "peak_ws_mb": 0.0}
        return {
            "working_set_mb": pmc.WorkingSetSize / (1024.0 * 1024.0),
            "private_usage_mb": pmc.PrivateUsage / (1024.0 * 1024.0),
            "peak_ws_mb": pmc.PeakWorkingSetSize / (1024.0 * 1024.0),
        }
    finally:
        ctypes.windll.kernel32.CloseHandle(h_proc)


class Test01HostAutostartWindowsRegistry(unittest.TestCase):
    """Verifies Windows Registry HKCU\\...\\Run and server autostart functions."""

    def setUp(self):
        # Clean state before each test
        server.set_server_autostart(False)

    def tearDown(self):
        # Restore clean state
        server.set_server_autostart(False)

    def test_01_paths_and_commands(self):
        cmd = server.get_host_server_autostart_cmd()
        self.assertTrue(bool(cmd), "Autostart command must not be empty")
        self.assertTrue("server_sidecar.exe" in cmd or "server.py" in cmd, f"Command must reference sidecar or server.py: {cmd}")
        self.assertTrue("--host-mode" in cmd, f"Command must include --host-mode flag: {cmd}")
        self.assertTrue("--port=8768" in cmd, f"Command must bind to standard host port 8768: {cmd}")

        paths = server.get_windows_autostart_paths()
        self.assertIn("server_cmd", paths)
        self.assertIn("app_cmd", paths)
        self.assertIn("app_dir", paths)

    def test_02_enable_inspect_disable_server_autostart(self):
        run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"

        # 1. Initially disabled
        self.assertFalse(server.is_server_autostart_enabled())
        status_init = server.get_autostart_status()
        self.assertTrue(status_init["ok"])
        self.assertFalse(status_init["enabled"])
        self.assertFalse(status_init["server_autostart"])

        # 2. Enable server autostart
        res_enable = server.set_server_autostart(True)
        self.assertTrue(res_enable["ok"])
        self.assertTrue(res_enable["enabled"])
        self.assertTrue(res_enable["server_autostart"])
        self.assertEqual(res_enable["mode"], "host")
        self.assertTrue(bool(res_enable["path"]))

        # Verify registry entry in HKCU\...\Run
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_READ) as key:
            val, reg_type = winreg.QueryValueEx(key, "VSDatabaseHostServer")
            self.assertEqual(reg_type, winreg.REG_SZ)
            self.assertEqual(val, res_enable["path"])

        # Inspect via server helper functions
        self.assertTrue(server.is_server_autostart_enabled())
        status_active = server.get_autostart_status()
        self.assertTrue(status_active["enabled"])
        self.assertTrue(status_active["server_autostart"])

        # 3. Disable server autostart
        res_disable = server.set_server_autostart(False)
        self.assertTrue(res_disable["ok"])
        self.assertFalse(res_disable["enabled"])
        self.assertFalse(res_disable["server_autostart"])

        # Verify registry entry is removed
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_READ) as key:
            with self.assertRaises(OSError):
                winreg.QueryValueEx(key, "VSDatabaseHostServer")

        self.assertFalse(server.is_server_autostart_enabled())

    def test_03_legacy_vbs_cleanup_on_registration(self):
        run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
        # Seed legacy key
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "VSDatabaseServer", 0, winreg.REG_SZ, "wscript.exe legacy.vbs")

        # Activating new autostart cleans up legacy key
        server.set_server_autostart(True)

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_READ) as key:
            # New key exists
            val, _ = winreg.QueryValueEx(key, "VSDatabaseHostServer")
            self.assertTrue(bool(val))
            # Legacy key removed
            with self.assertRaises(OSError):
                winreg.QueryValueEx(key, "VSDatabaseServer")

    def test_04_task_scheduler_fallback_query(self):
        """Verifies Task Scheduler query utility execution without crashing."""
        cmd = ["schtasks", "/query", "/fo", "LIST"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        self.assertIn(res.returncode, (0, 1), "schtasks query returned unexpected returncode")


class Test02AutostartHttpApiContract(unittest.TestCase):
    """Verifies HTTP API contract for /api/system/autostart and /api/desktop/lifecycle-policy."""

    @classmethod
    def setUpClass(cls):
        cls.test_port = 8794
        server.start_server_background(port=cls.test_port, host="127.0.0.1")
        time.sleep(1.2)

    @classmethod
    def tearDownClass(cls):
        server.stop_server()
        server.set_server_autostart(False)

    def test_01_get_autostart_endpoint_contract(self):
        url = f"http://127.0.0.1:{self.test_port}/api/system/autostart"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode())

            # Validate complete contract schema
            self.assertTrue(data.get("ok"), "Expected ok: True")
            self.assertIn("enabled", data)
            self.assertIsInstance(data["enabled"], bool)
            self.assertEqual(data.get("mode"), "host")
            self.assertIn("path", data)
            self.assertIsInstance(data["path"], str)
            self.assertIn("server_autostart", data)
            self.assertIsInstance(data["server_autostart"], bool)
            self.assertIn("app_autostart", data)
            self.assertIsInstance(data["app_autostart"], bool)

    def test_02_post_autostart_enable_and_disable(self):
        url = f"http://127.0.0.1:{self.test_port}/api/system/autostart"

        # 1. Enable via POST {"enabled": True}
        payload_enable = json.dumps({"enabled": True}).encode("utf-8")
        req_enable = urllib.request.Request(url, data=payload_enable, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req_enable, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode())
            self.assertTrue(data.get("ok"))
            self.assertTrue(data.get("enabled"))
            self.assertTrue(data.get("server_autostart"))
            self.assertEqual(data.get("mode"), "host")

        # Verify registry was actually written
        self.assertTrue(server.is_server_autostart_enabled())

        # 2. Disable via POST {"enabled": False}
        payload_disable = json.dumps({"enabled": False}).encode("utf-8")
        req_disable = urllib.request.Request(url, data=payload_disable, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req_disable, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode())
            self.assertTrue(data.get("ok"))
            self.assertFalse(data.get("enabled"))
            self.assertFalse(data.get("server_autostart"))

        # Verify registry was cleaned
        self.assertFalse(server.is_server_autostart_enabled())

    def test_03_lifecycle_policy_blocks_shutdown_when_autostart_active(self):
        url_auto = f"http://127.0.0.1:{self.test_port}/api/system/autostart"
        url_policy = f"http://127.0.0.1:{self.test_port}/api/desktop/lifecycle-policy"

        # 1. Enable autostart
        payload = json.dumps({"enabled": True}).encode("utf-8")
        req = urllib.request.Request(url_auto, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)

        # Check lifecycle policy -> must NOT terminate
        req_pol = urllib.request.Request(url_policy)
        with urllib.request.urlopen(req_pol, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            pol = json.loads(resp.read().decode())
            self.assertTrue(pol["ok"])
            self.assertTrue(pol["autostart_enabled"])
            self.assertFalse(pol["can_terminate"], "Server shutdown must be blocked when autostart is active")
            self.assertEqual(pol.get("reason"), "autostart_enabled")

        # 2. Disable autostart
        payload_off = json.dumps({"enabled": False}).encode("utf-8")
        req_off = urllib.request.Request(url_auto, data=payload_off, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req_off, timeout=5) as resp:
            self.assertEqual(resp.status, 200)

        # Check lifecycle policy -> can terminate (when no active LAN connections)
        with urllib.request.urlopen(req_pol, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            pol = json.loads(resp.read().decode())
            self.assertFalse(pol["autostart_enabled"])


class Test03ClientBackgroundDaemon(unittest.TestCase):
    """Verifies client_daemon.pyw execution, CLI flags, health heartbeats, and state writing."""

    @classmethod
    def setUpClass(cls):
        cls.daemon_path = PROJECT_ROOT / "client_daemon.pyw"
        cls.test_port = 8795
        server.start_server_background(port=cls.test_port, host="127.0.0.1")
        time.sleep(1.0)

    @classmethod
    def tearDownClass(cls):
        server.stop_server()
        # Clean daemon registry entry
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, "VSDatabaseClientAgent")
        except Exception:
            pass

    def test_01_daemon_file_existence_and_syntax(self):
        self.assertTrue(self.daemon_path.is_file(), f"client_daemon.pyw not found at {self.daemon_path}")
        src_daemon = PROJECT_ROOT / "src-tauri" / "client_daemon.pyw"
        self.assertTrue(src_daemon.is_file(), f"src-tauri/client_daemon.pyw not found at {src_daemon}")

        # Syntax check via py_compile
        import py_compile
        py_compile.compile(str(self.daemon_path), doraise=True)

    def test_02_cli_flags_autostart_enable_disable(self):
        # 1. Enable autostart
        cmd_enable = [sys.executable, str(self.daemon_path), "--autostart-enable"]
        res_enable = subprocess.run(cmd_enable, capture_output=True, text=True, timeout=5)
        self.assertEqual(res_enable.returncode, 0, f"Error enabling daemon autostart: {res_enable.stderr}")
        data_enable = json.loads(res_enable.stdout.strip())
        self.assertTrue(data_enable.get("ok"))
        self.assertTrue(data_enable.get("autostart"))

        # Verify registry entry
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, "VSDatabaseClientAgent")
            self.assertTrue(bool(val))
            self.assertTrue("client_daemon.pyw" in val)

        # 2. Check status flag
        cmd_status = [sys.executable, str(self.daemon_path), "--status"]
        res_status = subprocess.run(cmd_status, capture_output=True, text=True, timeout=5)
        self.assertEqual(res_status.returncode, 0)
        data_status = json.loads(res_status.stdout.strip())
        self.assertTrue(data_status.get("autostart"))

        # 3. Disable autostart
        cmd_disable = [sys.executable, str(self.daemon_path), "--autostart-disable"]
        res_disable = subprocess.run(cmd_disable, capture_output=True, text=True, timeout=5)
        self.assertEqual(res_disable.returncode, 0)
        data_disable = json.loads(res_disable.stdout.strip())
        self.assertFalse(data_disable.get("autostart"))

        # Verify registry deleted
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ) as key:
            with self.assertRaises(OSError):
                winreg.QueryValueEx(key, "VSDatabaseClientAgent")

    def test_03_daemon_health_heartbeat_and_state_json(self):
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        vs_data_dir = Path(appdata) / "VS Database"
        vs_data_dir.mkdir(parents=True, exist_ok=True)

        config_file = vs_data_dir / "client_config.json"
        state_file = vs_data_dir / "client_connection_state.json"

        # Point daemon config to active test server
        test_host = f"http://127.0.0.1:{self.test_port}"
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump({"host_url": test_host}, f, indent=2)

        # Trigger --run-once heartbeat probe
        cmd_run = [sys.executable, str(self.daemon_path), "--run-once"]
        res = subprocess.run(cmd_run, capture_output=True, text=True, timeout=10)
        self.assertEqual(res.returncode, 0, f"Daemon --run-once failed: {res.stderr}")

        # Assert state JSON was written and has expected schema
        self.assertTrue(state_file.is_file(), f"Expected state file at {state_file}")
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)

        self.assertTrue(state.get("connected"), f"Expected connected=True, got: {state}")
        self.assertEqual(state.get("status"), "online")
        self.assertEqual(state.get("host_url"), test_host)
        self.assertIn("last_ping", state)
        self.assertLess(time.time() - state["last_ping"], 10.0, "Heartbeat timestamp is stale")
        self.assertTrue(bool(state.get("host_version")), "Host version should be present")


class Test04MemoryProfilingBenchmarks(unittest.TestCase):
    """
    Benchmarks RAM Working Set and Private Working Set footprint against SLA budgets:
    - client_daemon.pyw: Private Working Set < 35 MB (Target: ~15-25 MB)
    - headless server_sidecar: Private Working Set < 85 MB (Target: ~45-70 MB)
    """

    def test_01_client_daemon_memory_benchmark(self):
        daemon_path = PROJECT_ROOT / "client_daemon.pyw"
        # Run daemon in background
        proc = subprocess.Popen([sys.executable, str(daemon_path)], cwd=str(PROJECT_ROOT))
        try:
            # Let it warm up and complete initial probe
            time.sleep(2.0)
            mem = get_process_memory_info_mb(proc.pid)

            ws_mb = mem["working_set_mb"]
            priv_mb = mem["private_usage_mb"]

            print(f"\n[BENCHMARK] Client Daemon PID {proc.pid}: Working Set = {ws_mb:.2f} MB | Private Usage = {priv_mb:.2f} MB")

            self.assertGreater(ws_mb, 0.0, "Working set must be non-zero")
            self.assertGreater(priv_mb, 0.0, "Private working set must be non-zero")

            # SLA Threshold: < 35 MB Private Working Set
            self.assertLess(priv_mb, 35.0, f"Client daemon private working set {priv_mb:.2f} MB exceeded 35.0 MB limit!")
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_02_headless_server_sidecar_memory_benchmark(self):
        bench_port = 8796
        
        # Test headless server startup memory
        proc = subprocess.Popen(
            [sys.executable, "server.py", "--host-mode", f"--port={bench_port}"],
            cwd=str(PROJECT_ROOT)
        )
        try:
            # Poll /api/desktop/health until server is listening and ready
            ready = False
            t0 = time.time()
            while time.time() - t0 < 15:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{bench_port}/api/desktop/health", timeout=1) as resp:
                        if resp.status == 200:
                            ready = True
                            break
                except Exception:
                    time.sleep(0.4)

            self.assertTrue(ready, "Headless server failed to become ready within 15 seconds")

            # Measure memory in steady idle serving state
            time.sleep(1.0)
            mem = get_process_memory_info_mb(proc.pid)
            ws_mb = mem["working_set_mb"]
            priv_mb = mem["private_usage_mb"]

            print(f"\n[BENCHMARK] Headless Server PID {proc.pid}: Working Set = {ws_mb:.2f} MB | Private Usage = {priv_mb:.2f} MB")

            self.assertGreater(ws_mb, 0.0, "Working set must be non-zero")
            self.assertGreater(priv_mb, 0.0, "Private working set must be non-zero")

            # SLA Threshold: < 85 MB Private Working Set
            self.assertLess(priv_mb, 85.0, f"Headless server private working set {priv_mb:.2f} MB exceeded 85.0 MB limit!")
        finally:
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
