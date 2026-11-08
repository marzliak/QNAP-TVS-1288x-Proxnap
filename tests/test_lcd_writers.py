"""Cross-writer lifecycle tests: AST-only message functions and scratch flocks.

No production imports, serial access, service commands or action functions.
Every LCD operation and sleep is an explicit inert assertion callback.
"""
import ast
import fcntl
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[1]


class WriterLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="proxnap-writers-", dir=os.environ["TMPDIR"])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "lcd-message.lock"
        self.calls = []
        self.logs = []
        self.fail_at = None
        self.settings = SimpleNamespace(lcd_enabled=True, lcd_lock_file=str(self.path),
                                        lcd_port="FORBIDDEN_REAL_SERIAL", lcd_message_seconds=8,
                                        lcd_off_after_message=True)

    def assert_owned(self, operation):
        with open(self.path, "a") as competitor:
            with self.assertRaises(BlockingIOError, msg=operation):
                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.calls.append(operation)
        if self.fail_at == operation:
            raise OSError("injected harmless LCD failure")

    def load_writer(self, name):
        test = self

        class MockLcd:
            def __init__(self, port):
                test.assertEqual(port, "FORBIDDEN_REAL_SERIAL")
                test.assert_owned("construct")

            def open(self):
                test.assert_owned("open")

            def stop_clock(self):
                test.assert_owned("clock")

            def backlight(self, on):
                test.assert_owned("light_on" if on else "light_off")

            def write_lines(self, line1, line2):
                test.assertEqual((line1, line2), ("MOCK LINE ONE", "MOCK LINE TWO"))
                test.assert_owned("lines")

            def clear(self):
                test.assert_owned("clear")

            def close(self):
                test.assert_owned("close")

        def scratch_open(path, *args, **kwargs):
            resolved = Path(path).resolve()
            self.assertTrue(resolved.is_relative_to(self.root))
            return open(resolved, *args, **kwargs)

        def scratch_makedirs(path, **kwargs):
            resolved = Path(path).resolve()
            self.assertTrue(resolved.is_relative_to(self.root))
            resolved.mkdir(parents=True, exist_ok=True)

        namespace = {"Settings": SimpleNamespace, "Lcd": MockLcd, "open": scratch_open,
                     "fcntl": fcntl,
                     "os": SimpleNamespace(path=SimpleNamespace(dirname=os.path.dirname),
                                           makedirs=scratch_makedirs),
                     "time": SimpleNamespace(sleep=lambda seconds: self.assert_owned("sleep")),
                     "log": lambda settings, message: self.logs.append(message)}
        tree = ast.parse((SOURCE / name).read_text())
        nodes: list[ast.stmt] = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "lcd_message"]
        self.assertEqual(len(nodes), 1)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE / name), "exec"), namespace)
        return namespace["lcd_message"], namespace

    def assert_released(self):
        with open(self.path, "a") as competitor:
            fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(competitor, fcntl.LOCK_UN)

    def test_copy_message_and_final_delay_keep_same_lock(self):
        writer, _ = self.load_writer("src/proxnap/copy_update.py")
        writer(self.settings, "MOCK LINE ONE", "MOCK LINE TWO", final=True)
        self.assertEqual(self.calls, ["construct", "open", "clock", "light_on", "lines",
                                      "sleep", "clear", "light_off", "close"])
        self.assertFalse(self.logs)
        inode = self.path.stat().st_ino
        self.assert_released()
        self.assertEqual(self.path.stat().st_ino, inode)

    def test_power_writer_holds_message_lock_through_close(self):
        writer, _ = self.load_writer("src/proxnap/power_button.py")
        writer(self.settings, "MOCK LINE ONE", "MOCK LINE TWO")
        self.assertEqual(self.calls, ["construct", "open", "clock", "light_on", "lines", "close"])
        self.assert_released()

    def test_copy_write_failure_closes_and_releases(self):
        writer, _ = self.load_writer("src/proxnap/copy_update.py")
        self.fail_at = "lines"
        writer(self.settings, "MOCK LINE ONE", "MOCK LINE TWO", final=True)
        self.assertEqual(self.calls[-1], "close")
        self.assertNotIn("sleep", self.calls)
        self.assertEqual(len(self.logs), 1)
        self.assert_released()

    def test_power_write_failure_closes_and_releases(self):
        writer, _ = self.load_writer("src/proxnap/power_button.py")
        self.fail_at = "lines"
        writer(self.settings, "MOCK LINE ONE", "MOCK LINE TWO")
        self.assertEqual(self.calls[-1], "close")
        self.assertEqual(len(self.logs), 1)
        self.assert_released()

    def test_lock_creation_error_never_opens_serial(self):
        for name in ["src/proxnap/copy_update.py", "src/proxnap/power_button.py"]:
            with self.subTest(writer=name):
                writer, namespace = self.load_writer(name)

                def refuse(*args, **kwargs):
                    raise PermissionError("mock filesystem refusal")

                namespace["os"].makedirs = refuse
                writer(self.settings, "MOCK LINE ONE", "MOCK LINE TWO")
                self.assertFalse(self.calls)
        self.assertEqual(len(self.logs), 2)

    def test_disabled_writers_do_not_touch_lock_or_serial(self):
        self.settings.lcd_enabled = False
        for name in ["src/proxnap/copy_update.py", "src/proxnap/power_button.py"]:
            writer, _ = self.load_writer(name)
            writer(self.settings, "MOCK LINE ONE", "MOCK LINE TWO")
        self.assertFalse(self.calls)
        self.assertFalse(self.path.exists())

    def test_all_shipped_cooperators_configure_identical_lock_path(self):
        values = []
        for name, key in [("configs/lcd.conf", "lcd_message_lock"),
                          ("configs/copy-button.conf", "lcd_lock_file"),
                          ("configs/power-button.conf", "lcd_lock_file")]:
            config = {}
            for line in (SOURCE / name).read_text().splitlines():
                body = line.split("#", 1)[0].strip()
                if "=" in body:
                    k, v = body.split("=", 1)
                    config[k.strip()] = v.strip()
            values.append(config[key])
        self.assertEqual(values, ["/run/proxnap-lcd-message.lock"] * 3)


if __name__ == "__main__":
    unittest.main()
