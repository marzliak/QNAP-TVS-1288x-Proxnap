"""Information-only Copy/LCD IPC; no filesystem paths or action dispatch.

Linux abstract datagrams carry only a fixed operation and monotonic timestamp.
Importing this utility opens nothing. The existing LCD loop owns the receiver.
"""
import socket
import struct
import time


_PACKET = struct.Struct("!4scQ")
_CREDENTIALS = struct.Struct("3i")
_MAGIC = b"PLS1"
_MAX_AGE_NS = 2_000_000_000
_DRAIN_LIMIT = 16


def _address(name: str) -> str:
    if not isinstance(name, str) or not name.startswith("@"):
        raise ValueError("abstract socket name required")
    if not 1 <= len(name[1:].encode("ascii")) <= 106 or "\0" in name:
        raise ValueError("invalid abstract socket name")
    return "\0" + name[1:]


def send_status_notice(name: str, operation: str) -> bool:
    """Nonblocking best effort; never fall back to an action or external writer."""
    try:
        address = _address(name)
        if operation not in {"status", "cancel"}:
            return False
        payload = _PACKET.pack(_MAGIC, b"S" if operation == "status" else b"C", time.monotonic_ns())
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
            channel.setblocking(False)
            return channel.sendto(payload, address) == len(payload)
    except (OSError, ValueError):
        return False


class StatusReceiver:
    """Bounded UID0-only inbox; no deferred requests across ownership barriers."""

    def __init__(self):
        self.channel = None
        self.name = None
        self.started_ns = 0
        self.high_water_ns = 0
        self.next_retry = 0.0

    def close(self):
        if self.channel is not None:
            self.channel.close()
        self.channel = None
        self.name = None
        self.started_ns = 0
        self.high_water_ns = 0
        self.next_retry = 0.0

    def configure(self, name: str, now: float):
        if name != self.name:
            self.close()
            self.name = name
        if self.channel is not None or now < self.next_retry:
            return
        channel = None
        try:
            address = _address(name)
            channel = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            channel.setblocking(False)
            channel.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            channel.bind(address)
            self.started_ns = time.monotonic_ns()
            self.high_water_ns = self.started_ns
            self.channel = channel
            print("LCD Copy status socket ready", flush=True)
        except (OSError, ValueError):
            if channel is not None:
                channel.close()
            self.next_retry = now + 5.0
            print("LCD Copy status socket unavailable; retry in 5s", flush=True)

    def poll(self, accept: bool) -> str | None:
        if self.channel is None:
            return None
        result = None
        for _ in range(_DRAIN_LIMIT):
            try:
                payload, ancillary, flags, _peer = self.channel.recvmsg(
                    _PACKET.size, socket.CMSG_SPACE(_CREDENTIALS.size))
            except BlockingIOError:
                if not accept:
                    self.high_water_ns = max(self.high_water_ns, time.monotonic_ns())
                return result if accept else None
            except OSError:
                # A failed FD is replaced only by the controlled configure retry.
                name = self.name
                self.close()
                self.name = name
                self.next_retry = time.monotonic() + 5.0
                return "cancel"
            if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(payload) != _PACKET.size:
                continue
            credentials = [data for level, kind, data in ancillary
                           if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS]
            if len(credentials) != 1 or len(credentials[0]) != _CREDENTIALS.size:
                continue
            _pid, uid, _gid = _CREDENTIALS.unpack(credentials[0])
            if uid != 0:
                continue
            magic, operation, stamp = _PACKET.unpack(payload)
            now_ns = time.monotonic_ns()
            if (magic != _MAGIC or operation not in {b"S", b"C"}
                    or stamp <= max(self.started_ns, self.high_water_ns)
                    or stamp > now_ns or now_ns - stamp > _MAX_AGE_NS):
                continue
            self.high_water_ns = stamp
            if accept:
                result = "status" if operation == b"S" else "cancel"
        # Do not display a status with a possible cancel still behind the budget.
        # Only an authenticated cancel may affect an existing popup. Invalidate
        # the remaining captured backlog, including while hidden.
        self.high_water_ns = max(self.high_water_ns, time.monotonic_ns())
        return "cancel" if result == "cancel" else None
