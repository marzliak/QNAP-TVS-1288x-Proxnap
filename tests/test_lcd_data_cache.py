"""AST-only LCD data-cache tests. No daemon, UART or real collector execution."""
import ast
import builtins
import io
import json
from dataclasses import dataclass
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "src/proxnap/lcd.py"
SOURCE_OVERRIDE = None


def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected production IO or effect")


class DataCacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.module = ModuleType("offline_lcd_data_cache")
        sys.modules[self.module.__name__] = self.module
        self.addCleanup(sys.modules.pop, self.module.__name__, None)
        self.ns = self.module.__dict__
        self.ns.update({
            "__builtins__": dict(vars(builtins), open=forbidden, __import__=forbidden),
            "dataclass": dataclass, "LCD_WIDTH": 16, "LCD_MESSAGE_LOCK": "mock://unused-lock",
            "threading": SimpleNamespace(Lock=threading.Lock),
            "socket": SimpleNamespace(gethostname=lambda: "mockhost"),
            "time": SimpleNamespace(monotonic=lambda: self.now, sleep=forbidden),
            "json": json, "STATUS_STATE_FILE": "mock://status-state",
            "subprocess": SimpleNamespace(run=forbidden, Popen=forbidden),
            "open": forbidden, "Lcd": forbidden,
            "running": True,
        })
        names = {"Settings", "LcdPage", "LiveSnapshot", "PageCache", "home_lines", "heartbeat_char",
                 "home_frame_index", "ascii_safe", "to_lcd", "fmt_home_load", "bps_to_mbps", "ram_pct",
                 "page_worker", "critical_status_page", "short_status_reason"}
        tree = ast.parse(SOURCE_OVERRIDE if SOURCE_OVERRIDE is not None else SOURCE.read_text())
        nodes: list[ast.stmt] = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names]
        self.assertEqual({n.name for n in nodes if isinstance(n, (ast.FunctionDef, ast.ClassDef))}, names)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<lcd-data-only>", "exec"), self.ns)
        self.cache = self.ns["PageCache"]()
        self.settings = self.ns["Settings"]()
        self.good = self.ns["LiveSnapshot"](hostname="MOCK", load_1=1.25, load_5=2.5,
                                               ram_used_gb=32, ram_total_gb=64, ram_pct=50,
                                               vm_count=2, lxc_count=3, uptime_short="1d",
                                               net_rx_bps=1_000_000, net_tx_bps=2_000_000)
        self.pages = [self.ns["LcdPage"]("Status", "Mock", "Mock", "data")]

    def test_initial_cache_is_unknown_not_zero_snapshot(self):
        self.assertIsNone(self.cache.get_snapshot())

    def test_error_pages_invalidate_previous_snapshot(self):
        self.cache.set(self.pages, self.good)
        self.now += 2
        self.cache.set([self.ns["LcdPage"]("Status", "Error", "LCD data error", "mock error")])
        self.assertIsNone(self.cache.get_snapshot())

    def test_cache_age_boundary_and_expiration(self):
        self.cache.set(self.pages, self.good)
        self.now += 30
        self.assertIs(self.cache.get_snapshot(), self.good)
        self.now += 0.01
        self.assertIsNone(self.cache.get_snapshot())

    def test_backward_clock_does_not_make_snapshot_fresh(self):
        self.cache.set(self.pages, self.good)
        self.now -= 1
        self.assertIsNone(self.cache.get_snapshot())

    def test_valid_custom_refresh_age_and_invalid_age_refusal(self):
        self.cache.set(self.pages, self.good, max_age=80)
        self.now += 79
        self.assertIs(self.cache.get_snapshot(), self.good)
        self.now += 2
        self.assertIsNone(self.cache.get_snapshot())
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.cache.set(self.pages, self.good, max_age=value)

    def test_unknown_home_has_no_fabricated_numeric_measurements(self):
        for frame in range(3):
            with self.subTest(frame=frame):
                line1, line2, actual = self.ns["home_lines"](None, self.now, self.settings, manual_frame=frame)
                self.assertEqual(actual, frame)
                self.assertEqual(len(line1), 16)
                self.assertEqual(len(line2), 16)
                self.assertIn("unknown", line1.lower())
                self.assertNotIn("0%", line1 + line2)
                self.assertNotIn("0/0", line1 + line2)

    def test_fresh_home_output_preserves_existing_numeric_layout(self):
        line1, line2, frame = self.ns["home_lines"](self.good, self.now, self.settings, manual_frame=0)
        self.assertEqual(frame, 0)
        self.assertEqual(line1.rstrip(), "LOAD 1.25/2.50")
        self.assertIn("RAM 50% 32/64", line2)
        self.assertEqual(len(line1), 16)
        self.assertEqual(len(line2), 16)

    def worker(self, fail=False):
        self.settings.data_refresh_seconds = 40
        self.ns["load_settings"] = lambda path: self.settings if path == "mock://config" else forbidden()
        self.ns["all_net_stats"] = lambda: "mock-network-data"
        self.ns["cpu_stat"] = lambda: "mock-cpu-data"
        self.ns["build_pages"] = lambda *args: self.pages

        def snapshot(*args):
            if fail:
                raise OSError("mock collection failure")
            return self.good

        def sleep(seconds):
            self.assertGreater(seconds, 0)
            self.ns["running"] = False

        self.ns["build_live_snapshot"] = snapshot
        self.ns["time"].sleep = sleep
        self.ns["page_worker"]("mock://config", self.cache)

    def test_worker_uses_configured_refresh_for_freshness(self):
        self.worker()
        self.now += 60
        self.assertIs(self.cache.get_snapshot(), self.good)
        self.now += 21
        self.assertIsNone(self.cache.get_snapshot())

    def test_worker_failure_removes_old_home_data(self):
        self.cache.set(self.pages, self.good)
        self.worker(fail=True)
        self.assertEqual(self.cache.get()[0].title, "Error")
        self.assertIsNone(self.cache.get_snapshot())

    def test_recovery_after_failure_returns_only_new_snapshot(self):
        self.cache.set(self.pages, self.good)
        self.cache.set(self.pages)
        self.assertIsNone(self.cache.get_snapshot())
        self.now += 5
        recovered = self.ns["LiveSnapshot"](hostname="RECOVERED", load_1=2)
        self.cache.set(self.pages, recovered)
        self.assertIs(self.cache.get_snapshot(), recovered)

    def status_page(self, payload):
        def status_open(path, mode, encoding):
            self.assertEqual((path, mode, encoding), ("mock://status-state", "r", "utf-8"))
            if isinstance(payload, OSError):
                raise payload
            return io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
        self.ns["open"] = status_open
        self.ns["time"].time = lambda: self.now
        return self.ns["critical_status_page"]()

    def healthy_status(self, **changes):
        return {"status": "ok", "timestamp": self.now.__int__(), "check_seconds": 30,
                "reasons": [], "warnings": [], "unavailable": []} | changes

    def test_status_healthy_remains_the_existing_ok_message(self):
        self.assertEqual(self.status_page(self.healthy_status()), ("System Check", "All running OK"))

    def test_status_warning_is_not_reported_as_all_ok(self):
        title, detail = self.status_page(self.healthy_status(status="warning", warnings=["reboot required"]))
        self.assertEqual(title, "Warning")
        self.assertIn("reboot", detail)

    def test_status_missing_or_unknown_never_renders_ok(self):
        for payload in (OSError("mock unavailable"), {}, self.healthy_status(status="unknown", unavailable=["sensor missing"])):
            with self.subTest(payload=payload):
                self.assertNotEqual(self.status_page(payload)[1], "All running OK")

    def test_status_stale_future_and_recovery(self):
        for state in ("ok", "critical"):
            for stamp in (1, 2000):
                payload = self.healthy_status(status=state, timestamp=stamp,
                                              reasons=["hot"] if state == "critical" else [])
                self.assertNotEqual(self.status_page(payload)[1], "All running OK")
        self.assertEqual(self.status_page(self.healthy_status())[1], "All running OK")

    def test_status_invalid_json_or_nonobject_never_renders_ok(self):
        for payload in ("{", "null", [], ["critical"], 7, "true"):
            with self.subTest(payload=payload):
                self.assertNotEqual(self.status_page(payload)[1], "All running OK")

    def test_status_malformed_fields_and_nonfinite_clock_data_deny_ok(self):
        for changes in ({"timestamp": True}, {"timestamp": "1000"}, {"timestamp": float("nan")},
                        {"check_seconds": 0}, {"check_seconds": float("inf")},
                        {"check_seconds": float("nan")}, {"warnings": "none"},
                        {"reasons": [False]}, {"unavailable": "none"}, {"status": "future-state"}):
            with self.subTest(changes=changes):
                self.assertNotEqual(self.status_page(self.healthy_status(**changes))[1], "All running OK")

    def test_status_fresh_critical_survives_partial_unavailability(self):
        payload = self.healthy_status(status="critical", reasons=["temperature 95C"], unavailable=["one sensor missing"])
        title, detail = self.status_page(payload)
        self.assertEqual(title, "Critical")
        self.assertIn("95C", detail)

    def test_status_incoherent_or_incomplete_known_state_does_not_render_ok(self):
        for changes in ({"reasons": ["fault"]}, {"warnings": ["pending"]},
                        {"unavailable": ["missing"]}, {"status": "warning"},
                        {"status": "critical"}):
            with self.subTest(changes=changes):
                self.assertNotEqual(self.status_page(self.healthy_status(**changes))[1], "All running OK")


if __name__ == "__main__":
    unittest.main()
