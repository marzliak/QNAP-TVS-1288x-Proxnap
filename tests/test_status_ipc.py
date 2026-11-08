"""Pure IPC utility only: unique abstract sockets, no daemons or real actions."""
from collections import deque
import os
import socket
import struct
import time
from types import SimpleNamespace
import uuid

import pytest

from proxnap import status_ipc as ipc


def endpoint():
    return "@proxnap-offline-" + uuid.uuid4().hex


@pytest.mark.skipif(os.geteuid() != 0, reason="real UID0 abstract transport requires root; inert credential cases run everywhere")
def test_real_abstract_kernel_credentials_sender_replay_cancel_and_cleanup():
    name = endpoint()
    receiver = ipc.StatusReceiver()
    try:
        receiver.configure(name, time.monotonic())
        assert receiver.channel is not None
        assert receiver.channel.getsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED) == 1
        assert not receiver.channel.getblocking()
        assert ipc.send_status_notice(name, "status")
        assert receiver.poll(True) == "status"
        payload = ipc._PACKET.pack(ipc._MAGIC, b"S", time.monotonic_ns())
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
            sender.sendto(payload, ipc._address(name))
            assert receiver.poll(True) == "status"
            sender.sendto(payload, ipc._address(name))
            assert receiver.poll(True) is None
        assert ipc.send_status_notice(name, "cancel")
        assert receiver.poll(True) == "cancel"
        assert ipc.send_status_notice(name, "status")
        assert receiver.poll(False) is None
        assert receiver.poll(True) is None
        old = receiver.channel
        receiver.configure(endpoint(), time.monotonic())
        assert old.fileno() == -1
        assert not ipc.send_status_notice(name, "status")
    finally:
        receiver.close()
    assert receiver.channel is None and receiver.high_water_ns == 0
    assert not ipc.send_status_notice(name, "status")


def test_bind_collision_backoff_recovery_and_reenable(capsys):
    name = endpoint()
    first, second = ipc.StatusReceiver(), ipc.StatusReceiver()
    try:
        first.configure(name, 10)
        second.configure(name, 10)
        assert second.channel is None and second.next_retry == 15
        first.close()
        second.configure(name, 14.9)
        assert second.channel is None
        second.configure(name, 15)
        assert second.channel is not None
        stamp = time.monotonic_ns()
        second.close()
        second.configure(name, 16)
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
            sender.sendto(ipc._PACKET.pack(ipc._MAGIC, b"S", stamp), ipc._address(name))
        assert second.poll(True) is None
    finally:
        first.close()
        second.close()
    assert capsys.readouterr().out.count("unavailable") == 1


@pytest.mark.parametrize("name", ["/run/not-allowed", "relative", "@", "@a\0b", "@" + "x" * 107, "@é"])
def test_invalid_names_never_create_files_or_abort(name):
    assert not ipc.send_status_notice(name, "status")
    receiver = ipc.StatusReceiver()
    try:
        receiver.configure(name, 0)
        assert receiver.channel is None and receiver.next_retry == 5
    finally:
        receiver.close()


def test_missing_receiver_bad_operation_and_full_queue_are_best_effort(monkeypatch):
    assert not ipc.send_status_notice(endpoint(), "status")
    assert not ipc.send_status_notice(endpoint(), "reboot")
    class FullQueue:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def setblocking(self, enabled):
            assert not enabled
        def sendto(self, payload, address):
            assert len(payload) == ipc._PACKET.size and address.startswith('\0proxnap-offline-')
            raise BlockingIOError('offline full datagram queue')
    monkeypatch.setattr(ipc, 'socket', SimpleNamespace(AF_UNIX=socket.AF_UNIX,
                        SOCK_DGRAM=socket.SOCK_DGRAM, socket=lambda *args: FullQueue()))
    assert not ipc.send_status_notice(endpoint(), 'status')


class Inbox:
    def __init__(self, messages):
        self.messages = deque(messages)
        self.reads = 0
        self.closed = False

    def recvmsg(self, size, ancillary_size):
        assert size == ipc._PACKET.size and ancillary_size == socket.CMSG_SPACE(12)
        self.reads += 1
        if not self.messages:
            raise BlockingIOError()
        item = self.messages.popleft()
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        self.closed = True


def frame(stamp=19_000_000_000, *, uid=0, magic=b"PLS1", operation=b"S", flags=0, credential=True):
    ancillary = [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, struct.pack("3i", 123, uid, 123))] if credential else []
    return ipc._PACKET.pack(magic, operation, stamp), ancillary, flags, None


def reader(monkeypatch, messages):
    monkeypatch.setattr(ipc, "time", SimpleNamespace(monotonic_ns=lambda: 20_000_000_000, monotonic=lambda: 20.0))
    receiver = ipc.StatusReceiver()
    receiver.channel = Inbox(messages)
    receiver.name = endpoint()
    receiver.started_ns = receiver.high_water_ns = 10_000_000_000
    return receiver


@pytest.mark.parametrize("message", [
    frame(uid=1000), frame(credential=False), frame(magic=b"BAD!"), frame(operation=b"X"),
    frame(stamp=20_000_000_001), frame(stamp=17_999_999_999), frame(stamp=10_000_000_000),
    frame(flags=socket.MSG_TRUNC), frame(flags=socket.MSG_CTRUNC),
    (b"x", frame()[1], 0, None),
    (frame()[0], [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, b"x")], 0, None),
    (frame()[0], frame()[1] * 2, 0, None),
])
def test_rejects_bad_unauthenticated_future_stale_prestart_and_truncated(monkeypatch, message):
    receiver = reader(monkeypatch, [message])
    assert receiver.poll(True) is None
    assert receiver.high_water_ns == 10_000_000_000
    receiver.close()


def test_ttl_boundary_last_operation_wins_and_replays_do_not_trigger_twice(monkeypatch):
    receiver = reader(monkeypatch, [frame(stamp=18_000_000_000), frame(operation=b"C"), frame()])
    assert receiver.poll(True) == "cancel"  # equal timestamp status is a replay
    receiver.channel.messages.append(frame())
    assert receiver.poll(True) is None
    receiver.close()


def test_bounded_drain_invalidates_backlog_and_no_ownership_does_not_defer(monkeypatch):
    receiver = reader(monkeypatch, [frame()] * 100)
    channel = receiver.channel
    assert receiver.poll(True) is None
    assert channel.reads == ipc._DRAIN_LIMIT and len(channel.messages) == 84
    assert receiver.high_water_ns == 20_000_000_000
    while channel.messages:
        assert receiver.poll(False) in {None, "cancel"}
    channel.messages.append(frame())
    assert receiver.poll(True) is None
    receiver.close()


def test_unauthenticated_flood_cannot_cancel_but_authenticated_cancel_survives_budget(monkeypatch):
    receiver = reader(monkeypatch, [frame(uid=1000)] * 32)
    assert receiver.poll(True) is None
    receiver.close()
    receiver = reader(monkeypatch, [frame(operation=b"C")] + [frame(uid=1000)] * 32)
    assert receiver.poll(True) == 'cancel'
    receiver.close()


def test_receive_error_closes_and_backs_off(monkeypatch):
    receiver = reader(monkeypatch, [OSError("offline receive failure")])
    old, name = receiver.channel, receiver.name
    assert receiver.poll(True) == "cancel"
    assert old.closed and receiver.channel is None
    assert receiver.name == name and receiver.next_retry == 25
    receiver.configure(name, 24)
    assert receiver.channel is None
