"""Serial LCD regression tests, isolated by AST with mandatory harmless IO.

No service import, serial port, SSH, hardware write or UI action is reachable.
The unmodified Lcd class is compiled from the candidate source; only its globals
are replaced. Run with python3 -B -m unittest discover -s .../tests -v.
"""
import ast
from collections import deque
from pathlib import Path
import os
import random
import termios
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parent.parent / 'src/proxnap/lcd.py'
HEADER = b'\x53\x05\x00'
SEQUENCE = HEADER + b'\x01' + HEADER + b'\x00' + HEADER + b'\x02'


def forbidden(*_args, **_kwargs):
    raise AssertionError('Production IO or output command reached by parser test')


class FakeSerial:
    def __init__(self):
        self.channels = {}
        self.next_fd = 700
        self.reads = []
        self.closed = []
        self.opened = []
        self.attrs = []
        self.select_error: BaseException | None = None
        self.open_error: BaseException | None = None
        self.close_error: BaseException | None = None
        self.force_not_ready = False
        self.os = SimpleNamespace(read=self.read, write=forbidden, open=self.open,
                                  close=self.close, O_RDWR=os.O_RDWR,
                                  O_NOCTTY=os.O_NOCTTY, O_NONBLOCK=os.O_NONBLOCK)
        self.select = SimpleNamespace(select=self.select_ready)
        self.termios = SimpleNamespace(**{name: getattr(termios, name) for name in
                                          ('CS8', 'CREAD', 'CLOCAL', 'B1200', 'VMIN', 'VTIME', 'TCSANOW')},
                                       tcgetattr=self.get_attrs, tcsetattr=self.set_attrs)

    def allocate(self):
        self.next_fd += 1
        self.channels[self.next_fd] = deque()
        return self.next_fd

    def feed(self, fd, *chunks):
        self.channels[fd].extend(chunks)

    def open(self, port, flags):
        self.opened.append((port, flags))
        if self.open_error is not None:
            raise self.open_error
        return self.allocate()

    def close(self, fd):
        if fd not in self.channels:
            raise AssertionError('unknown fake fd')
        if self.close_error is not None:
            raise self.close_error
        self.closed.append(fd)
        self.channels.pop(fd)

    def select_ready(self, reads, writes, errors, timeout):
        if writes or errors or timeout != 0:
            raise AssertionError('unexpected select contract')
        if self.select_error is not None:
            raise self.select_error
        if any(fd not in self.channels for fd in reads):
            raise AssertionError('non-fixture descriptor')
        return ([] if self.force_not_ready else [fd for fd in reads if self.channels[fd]], [], [])

    def read(self, fd, size):
        if fd not in self.channels or size != 64:
            raise AssertionError('unexpected read descriptor/size')
        self.reads.append((fd, size))
        if not self.channels[fd]:
            raise BlockingIOError()
        data = self.channels[fd].popleft()
        if isinstance(data, BaseException):
            raise data
        if len(data) > size:
            self.channels[fd].appendleft(data[size:])
        return data[:size]

    def get_attrs(self, fd):
        if fd not in self.channels:
            raise AssertionError('non-fixture termios descriptor')
        return [0, 0, 0, 0, 0, 0, [0] * 32]

    def set_attrs(self, fd, action, attrs):
        if fd not in self.channels:
            raise AssertionError('non-fixture termios descriptor')
        self.attrs.append((fd, action, attrs))


def load_lcd(io, source_text=None):
    if io is None:
        raise ValueError('Explicit harmless IO fixture required')
    tree = ast.parse(SOURCE.read_text() if source_text is None else source_text)
    nodes: list[ast.stmt] = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Lcd']
    if len(nodes) != 1:
        raise AssertionError('Must isolate exactly the serial Lcd class')
    namespace = {'os': io.os, 'select': io.select, 'termios': io.termios,
                 'time': SimpleNamespace(sleep=forbidden), 'fit': forbidden}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), namespace)
    return namespace['Lcd']


def oracle(data):
    return [data[i + 3] for i in range(max(0, len(data) - 3)) if data[i:i + 3] == HEADER]


class LcdParserTests(unittest.TestCase):
    def setUp(self):
        self.io = FakeSerial()
        self.Lcd = load_lcd(self.io)
        self.lcd = self.make_lcd()

    def make_lcd(self):
        lcd = self.Lcd('offline-panel')
        lcd.fd = self.io.allocate()
        return lcd

    def drain(self, lcd=None):
        lcd = self.lcd if lcd is None else lcd
        output = []
        calls = 0
        while self.io.channels[lcd.fd]:
            output.extend(lcd.read_buttons())
            self.assertLessEqual(len(lcd._button_buffer), 3)
            calls += 1
            if calls > 10000:
                self.fail('fake stream did not terminate')
        return output

    def test_fixture_required_and_output_calls_forbidden(self):
        with self.assertRaises(ValueError):
            load_lcd(None)
        for fn in (self.io.os.write, forbidden):
            with self.assertRaises(AssertionError):
                fn()

    def test_whole_press_release_and_combination_frames(self):
        self.io.feed(self.lcd.fd, HEADER + b'\x01' + HEADER + b'\x00' + HEADER + b'\x02' + HEADER + b'\x03')
        self.assertEqual(self.drain(), [1, 0, 2, 3])

    def test_every_single_split_for_all_four_ui_masks(self):
        for value in range(4):
            for split in range(1, 4):
                with self.subTest(value=value, split=split):
                    lcd = self.make_lcd()
                    frame = HEADER + bytes([value])
                    self.io.feed(lcd.fd, frame[:split], frame[split:])
                    self.assertEqual(self.drain(lcd), [value])

    def test_all_mask_bytes_and_all_single_splits_preserve_legacy_api(self):
        for value in range(256):
            for split in range(1, 5):
                lcd = self.make_lcd()
                frame = HEADER + bytes([value])
                self.io.feed(lcd.fd, frame[:split], frame[split:])
                self.assertEqual(self.drain(lcd), [value], (value, split))

    def test_byte_by_byte(self):
        self.io.feed(self.lcd.fd, *(bytes([byte]) for byte in SEQUENCE))
        self.assertEqual(self.drain(), [1, 0, 2])

    def test_all_partitions_of_three_frame_sequence(self):
        for cuts in range(1 << (len(SEQUENCE) - 1)):
            lcd = self.make_lcd()
            start = 0
            chunks = []
            for i in range(1, len(SEQUENCE)):
                if cuts & (1 << (i - 1)):
                    chunks.append(SEQUENCE[start:i])
                    start = i
            chunks.append(SEQUENCE[start:])
            self.io.feed(lcd.fd, *chunks)
            self.assertEqual(self.drain(lcd), [1, 0, 2], cuts)

    def test_multiple_frames_and_partial_tail(self):
        self.io.feed(self.lcd.fd, HEADER + b'\x01' + HEADER[:2])
        self.assertEqual(self.drain(), [1])
        self.io.feed(self.lcd.fd, HEADER[2:] + b'\x00')
        self.assertEqual(self.drain(), [0])

    def test_exact_header_waits_for_mask(self):
        self.io.feed(self.lcd.fd, HEADER)
        self.assertEqual(self.drain(), [])
        self.io.feed(self.lcd.fd, b'\x02')
        self.assertEqual(self.drain(), [2])

    def test_noise_before_and_between_frames(self):
        data = b'garbage' + HEADER + b'\x01' + b'\x53\x53\x05x' + HEADER + b'\x00'
        self.io.feed(self.lcd.fd, *(data[i:i + 2] for i in range(0, len(data), 2)))
        self.assertEqual(self.drain(), [1, 0])

    def test_overlapping_headers_keep_original_sliding_scan_semantics(self):
        data = HEADER + HEADER + b'\x01'
        self.assertEqual(oracle(data), [0x53, 1])
        self.io.feed(self.lcd.fd, *(bytes([v]) for v in data))
        self.assertEqual(self.drain(), oracle(data))

    def test_no_duplicate_after_empty_or_unreadable_poll(self):
        self.io.feed(self.lcd.fd, HEADER + b'\x01')
        self.assertEqual(self.drain(), [1])
        self.assertEqual(self.lcd.read_buttons(), [])
        self.io.feed(self.lcd.fd, b'', b'x')
        self.assertEqual(self.drain(), [])

    def test_non_ready_preserves_partial(self):
        self.io.feed(self.lcd.fd, HEADER[:2])
        self.assertEqual(self.drain(), [])
        self.io.force_not_ready = True
        self.assertEqual(self.lcd.read_buttons(), [])
        self.io.force_not_ready = False
        self.io.feed(self.lcd.fd, HEADER[2:] + b'\x01')
        self.assertEqual(self.drain(), [1])

    def test_eagain_preserves_partial(self):
        self.io.feed(self.lcd.fd, HEADER[:2], BlockingIOError(), HEADER[2:] + b'\x02')
        self.assertEqual(self.drain(), [2])

    def test_empty_tty_read_preserves_partial(self):
        self.io.feed(self.lcd.fd, HEADER[:2], b'', HEADER[2:] + b'\x00')
        self.assertEqual(self.drain(), [0])

    def test_large_noise_does_not_grow_retained_buffer(self):
        self.io.feed(self.lcd.fd, b'x' * 100000)
        self.assertEqual(self.drain(), [])
        self.assertLessEqual(len(self.lcd._button_buffer), 3)
        self.io.feed(self.lcd.fd, HEADER + b'\x02')
        self.assertEqual(self.drain(), [2])

    def test_read_error_clears_partial_and_propagates(self):
        self.io.feed(self.lcd.fd, HEADER)
        self.assertEqual(self.drain(), [])
        self.io.feed(self.lcd.fd, OSError('synthetic read failure'))
        with self.assertRaises(OSError):
            self.lcd.read_buttons()
        self.assertEqual(self.lcd._button_buffer, b'')
        self.io.feed(self.lcd.fd, b'\x01')
        self.assertEqual(self.drain(), [])

    def test_select_error_clears_partial_and_propagates(self):
        self.io.feed(self.lcd.fd, HEADER)
        self.assertEqual(self.drain(), [])
        self.io.select_error = OSError('synthetic select failure')
        with self.assertRaises(OSError):
            self.lcd.read_buttons()
        self.assertEqual(self.lcd._button_buffer, b'')

    def test_closed_descriptor_drops_partial_without_io(self):
        self.io.feed(self.lcd.fd, HEADER)
        self.assertEqual(self.drain(), [])
        self.lcd.fd = None
        before = len(self.io.reads)
        self.assertEqual(self.lcd.read_buttons(), [])
        self.assertEqual(self.lcd._button_buffer, b'')
        self.assertEqual(len(self.io.reads), before)

    def test_close_clears_partial(self):
        fd = self.lcd.fd
        self.io.feed(fd, HEADER)
        self.assertEqual(self.drain(), [])
        self.lcd.close()
        self.assertIsNone(self.lcd.fd)
        self.assertEqual(self.lcd._button_buffer, b'')
        self.assertEqual(self.io.closed, [fd])

    def test_close_failure_clears_partial_and_preserves_exception(self):
        self.io.feed(self.lcd.fd, HEADER)
        self.assertEqual(self.drain(), [])
        self.io.close_error = OSError('synthetic close failure')
        with self.assertRaises(OSError):
            self.lcd.close()
        self.assertEqual(self.lcd._button_buffer, b'')

    def test_reopen_cannot_complete_a_frame_from_old_connection(self):
        self.io.feed(self.lcd.fd, HEADER)
        self.assertEqual(self.drain(), [])
        self.lcd.close()
        self.lcd.open()
        self.io.feed(self.lcd.fd, b'\x01')
        self.assertEqual(self.drain(), [])
        self.io.feed(self.lcd.fd, HEADER + b'\x02')
        self.assertEqual(self.drain(), [2])
        self.assertEqual(self.io.attrs[-1][2][4:6], [termios.B1200, termios.B1200])

    def test_open_attempt_clears_old_partial_even_if_open_fails(self):
        self.io.feed(self.lcd.fd, HEADER)
        self.assertEqual(self.drain(), [])
        self.io.open_error = OSError('synthetic open failure')
        with self.assertRaises(OSError):
            self.lcd.open()
        self.assertEqual(self.lcd._button_buffer, b'')

    def test_instances_do_not_share_buffer(self):
        other = self.make_lcd()
        self.io.feed(self.lcd.fd, HEADER)
        self.assertEqual(self.drain(), [])
        self.io.feed(other.fd, b'\x01')
        self.assertEqual(self.drain(other), [])
        self.io.feed(self.lcd.fd, b'\x02')
        self.assertEqual(self.drain(), [2])

    def test_random_chunking_matches_original_full_stream_oracle(self):
        rng = random.Random(0xA78)
        for case in range(64):
            data = bytearray(rng.randbytes(220))
            for _ in range(5):
                offset = rng.randrange(len(data) + 1)
                data[offset:offset] = HEADER + bytes([rng.randrange(256)])
            chunks = []
            start = 0
            while start < len(data):
                end = start + rng.randrange(1, 42)
                chunks.append(bytes(data[start:end]))
                start = end
            lcd = self.make_lcd()
            self.io.feed(lcd.fd, *chunks)
            self.assertEqual(self.drain(lcd), oracle(bytes(data)), case)


if __name__ == '__main__':
    unittest.main()
