"""Copy gestures remain AST-only with mandatory inert final effects."""
from pathlib import Path

import pytest

from test_copy_button import Harness, clicks, packet

SOURCE = Path(__file__).resolve().parents[1] / "src/proxnap/copy_update.py"


def harness(reads, **settings):
    return Harness(SOURCE.read_text(), reads, single_click_status_enabled=True, **settings)


def operations(h):
    return [op for _at, _name, op in h.notices]


def test_single_released_click_only_after_settle_and_never_commands():
    h = harness(clicks(101, 1) + [(102, BlockingIOError()), (103.09, BlockingIOError())])
    assert h.run() == []
    assert operations(h) == ["cancel", "status"]
    assert h.notices[-1][0] >= 103.08
    assert h.effects == [] and h.messages == []
    assert "copy status request sent" in h.logs


@pytest.mark.parametrize("count", [2, 3, 4, 7, 8, 12, 13, 15])
def test_multiclick_and_pending_confirmations_never_show_status(count):
    reads = clicks(101, count)
    settled = reads[-1][0] + 2.1
    reads += [(settled, BlockingIOError())]
    if count >= 4:
        reads += clicks(settled + .3, 1) + [(settled + 2.6, BlockingIOError())]
    h = harness(reads)
    assert h.run() == []
    assert "status" not in operations(h)
    assert operations(h) == ["cancel"] * (count + (count >= 4))


@pytest.mark.parametrize("count,action", [(4, "upgrade"), (8, "reboot"), (13, "poweroff")])
def test_maintenance_confirmation_preserved_with_feature_enabled(count, action):
    reads = clicks(101, count)
    at = reads[-1][0] + 2.1
    h = harness(reads + [(at, BlockingIOError())] + clicks(at + .3, 3))
    assert h.run() == [action]
    assert "status" not in operations(h)


@pytest.mark.parametrize("reads", [
    [(101, packet(101, 1))],
    [(101, packet(101, 1)), (103.1, packet(103.1, 0))],
    [(101, packet(101, 1)), (101.03, packet(101.03, 2)), (101.1, packet(101.1, 0))],
    clicks(101, 1, hold=.005),
    clicks(101, 3, spacing=.06, hold=.04),
    [(101, packet(101, 1)), (101.02, packet(101.02, 1)), (101.1, packet(101.1, 0))],
    [(101, packet(99, 1)), (101.1, packet(99.1, 0))],
    [(101, packet(102, 1)), (101.1, packet(102.1, 0))],
    [(101, packet(101, 1)), (101.1, packet(101.1, 9)), (101.2, packet(101.2, 0))],
    clicks(101, 1) + [(101.5, b"")],
    clicks(101, 1) + [(101.5, OSError("offline disconnect"))],
    [(101, packet(101, 1)), (101.1, packet(101.1, 0)[:-1])],
    clicks(101, 1) + [(101.5, packet(101.5, 0, event_type=0, code=3)),
                      (101.6, packet(101.6, 0, event_type=0, code=0))],
])
def test_hold_noise_repeat_disconnect_partial_and_stale_never_show_status(reads):
    h = harness(reads + [(105, BlockingIOError())])
    h.run()
    assert "status" not in operations(h)


def test_queue_drains_before_settle_and_fragmented_valid_pair_works():
    # Arrival time is late but the second click is already in the input queue.
    reads = clicks(101, 1) + [(104, packet(101.5, 1) + packet(101.6, 0)), (104.1, BlockingIOError())]
    h = harness(reads)
    assert h.run() == []
    assert "status" not in operations(h)
    reads = [(at, chunk) for at, data in clicks(101, 1) for chunk in (data[:7], data[7:])]
    h = harness(reads + [(103.1, BlockingIOError())])
    assert h.run() == []
    assert operations(h) == ["cancel", "status"]


def test_new_press_cancels_popup_before_hold_or_new_sequence_and_failure_is_best_effort():
    for tail, expected in [(clicks(104, 2), []),
                           ([(104, packet(104, 1)), (106.1, packet(106.1, 0))], ["refresh"])]:
        h = harness(clicks(101, 1) + [(103.1, BlockingIOError())] + tail + [(107, BlockingIOError())])
        assert h.run() == expected
        assert operations(h)[:3] == ["cancel", "status", "cancel"]
        assert operations(h).count("status") == 1
    h = harness(clicks(101, 1) + [(103.1, BlockingIOError())] + clicks(104, 4) +
                [(107, BlockingIOError())] + clicks(107.3, 3))
    h.ns["send_status_notice"] = lambda *_args: False
    assert h.run() == ["upgrade"]
    assert "copy status request unavailable" in h.logs


def test_disabled_default_sends_nothing():
    h = Harness(SOURCE.read_text(), clicks(101, 1) + [(103.1, BlockingIOError())])
    assert h.run() == []
    assert h.notices == []
