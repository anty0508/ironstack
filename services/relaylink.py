"""Relay client: connect a Host and Viewer through a public relay server when they
can't reach each other directly (CGNAT, VPN, no port-forward). Pairs with
`relay_server.py` running on a machine with a public IP (e.g. an EC2 box).

Two pieces, both making OUTBOUND connections to the relay (which always work):

  RelayAgent (Host)
    Parks a small pool of "ready" connections at the relay per channel. When the
    relay pairs one with a Viewer (sends {"go":1}), the agent dials the host's OWN
    localhost server (the control server or the screen server, already listening)
    and splices the relay connection to it — then opens a replacement so the pool
    stays full. The existing NetServer / RemoteScreenServer handle the bridged
    connection exactly as if it were a direct LAN connection: no protocol changes.

  dial_via_relay (Viewer)
    Dials the relay, sends the viewer handshake for a room + channel, waits for
    {"ok":1}, and returns a live socket. NetClient / RemoteScreenClient then speak
    their normal protocol over it via their `dialer` / `connect` hooks.

Wire protocol is defined in relay_server.py.
"""

import json
import time
import socket
import threading

from logsetup import get_logger
from services.sockopt import enable_keepalive

RELAY_DEFAULT_PORT = 48920
# How many idle "ready" connections to keep parked per channel. A couple is plenty
# for this app (usually one or two viewers); each is replaced the instant it's used.
POOL = {"control": 3, "screen": 2, "audio": 2}
_HOST_STALE = 60.0   # if no relay heartbeat within this, treat a parked conn as dead


def parse_addr(text, default_port=RELAY_DEFAULT_PORT):
    """'54.254.60.12:48920' or '54.254.60.12' -> ('54.254.60.12', 48920). Returns
    (None, None) if there's no host part."""
    text = (text or "").strip()
    if not text:
        return None, None
    if text.startswith("["):            # bracketed IPv6 literal, optional :port
        host, sep, rest = text[1:].partition("]")
        port = int(rest[1:]) if sep and rest.startswith(":") and rest[1:] else default_port
        return host, port
    if text.count(":") == 1:            # host:port
        host, _, p = text.partition(":")
        try:
            return host.strip(), int(p)
        except ValueError:
            return host.strip(), default_port
    return text, default_port           # bare host (or bare IPv6) -> default port


def _send_line(sock, obj):
    sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))


def _recv_line(sock, timeout=15.0, limit=65536):
    """Read one newline-framed line one byte at a time, so we never consume bytes
    that belong to the spliced stream that follows."""
    sock.settimeout(timeout)
    buf = bytearray()
    while b"\n" not in buf:
        chunk = sock.recv(1)
        if not chunk:
            break
        buf += chunk
        if len(buf) > limit:
            break
    line, _, _ = bytes(buf).partition(b"\n")
    return line


def _pump(src, dst, preload=b""):
    try:
        if preload:
            dst.sendall(preload)
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _splice(a, b, a_to_b=b""):
    t = threading.Thread(target=_pump, args=(a, b, a_to_b), daemon=True)
    t.start()
    _pump(b, a)
    t.join(timeout=2.0)
    for s in (a, b):
        try:
            s.close()
        except OSError:
            pass


# ======================================================================
# Viewer side
# ======================================================================

def dial_via_relay(relay_host, relay_port, room, channel, timeout=15.0, on_phase=None):
    """Dial the relay and return a socket paired to the host's `channel`. Raises
    OSError if the relay is unreachable or no host is registered for the room.

    `on_phase(code)` (optional) reports progress so the UI can distinguish a relay
    failure from a host failure: 'relay_connecting', 'relay_failed',
    'host_connecting', 'host_failed'."""
    def _phase(code):
        if on_phase is not None:
            try:
                on_phase(code)
            except Exception:
                pass

    _phase("relay_connecting")
    try:
        sock = socket.create_connection((relay_host, int(relay_port)), timeout=15)
    except OSError:
        _phase("relay_failed")
        raise
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    # Relay reached; now it must splice us to a host registered for this room.
    _phase("host_connecting")
    try:
        _send_line(sock, {"v": 1, "role": "viewer", "room": room, "channel": channel})
        line = _recv_line(sock, timeout=timeout)
        msg = json.loads(line.decode("utf-8")) if line else {}
    except (OSError, ValueError) as exc:
        try:
            sock.close()
        except OSError:
            pass
        _phase("host_failed")
        raise OSError(f"relay handshake failed: {exc}")
    if not msg.get("ok"):
        try:
            sock.close()
        except OSError:
            pass
        _phase("host_failed")
        raise OSError(msg.get("error", "relay: host not available"))
    # Handshake done -- this socket now carries the long-lived spliced session.
    # socket.create_connection()/_recv_line() leave their timeout set on the
    # socket after returning, so without this a silent stretch longer than that
    # timeout (a slow keepalive tick, a GUI-thread hiccup on the other end) reads
    # as a dead connection and drops an otherwise-healthy session. Blocking mode
    # alone would then miss a truly dead link for minutes (OS TCP defaults), so
    # TCP keepalive bounds that detection to a few seconds instead.
    sock.settimeout(None)
    enable_keepalive(sock)
    return sock


# ======================================================================
# Host side
# ======================================================================

class RelayAgent:
    """Keeps the host reachable through the relay. `channels` maps a channel name
    to the local port of the server that handles it, e.g.
        {"control": 48921, "screen": 48922}
    """

    def __init__(self, relay_host, relay_port, room, channels):
        self.relay_host = relay_host
        self.relay_port = int(relay_port)
        self.room = str(room)
        self.channels = dict(channels)
        self._stop = threading.Event()

    def start(self):
        for channel, localport in self.channels.items():
            for _ in range(POOL.get(channel, 2)):
                self._spawn(channel, localport)
        get_logger().info("relay: agent connecting to %s:%d as room %s",
                          self.relay_host, self.relay_port, self.room)

    def stop(self):
        self._stop.set()

    def _spawn(self, channel, localport):
        threading.Thread(target=self._worker, args=(channel, localport),
                         daemon=True).start()

    def _worker(self, channel, localport):
        """Hold one parked connection at the relay; when it's claimed, bridge it to
        the local server and hand the pool a replacement."""
        backoff = 1.0
        while not self._stop.is_set():
            try:
                sock = socket.create_connection((self.relay_host, self.relay_port),
                                                timeout=10)
            except OSError:
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, 15.0)
                continue
            backoff = 1.0
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                _send_line(sock, {"v": 1, "role": "host",
                                  "room": self.room, "channel": channel})
                leftover = self._await_go(sock)
            except OSError:
                leftover = None
            if leftover is None:
                try:
                    sock.close()
                except OSError:
                    pass
                continue   # parked conn died before use -> reconnect a fresh one
            # Claimed: keep the pool full, then bridge this one and finish.
            self._spawn(channel, localport)
            self._bridge(sock, localport, leftover)
            return

    def _await_go(self, sock):
        """Read heartbeats until the relay says {"go":1}. Returns any bytes already
        read past 'go' (to forward into the splice), or None on death/timeout."""
        sock.settimeout(_HOST_STALE)
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = sock.recv(1024)
            except socket.timeout:
                return None
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                except ValueError:
                    continue
                if msg.get("go"):
                    return buf          # usually b""
                # else {"ping":1} -> stay parked
        return None

    def _bridge(self, relay_sock, localport, viewer_preload):
        try:
            local = socket.create_connection(("127.0.0.1", localport), timeout=10)
        except OSError:
            get_logger().warning("relay: local server on %d unreachable", localport)
            try:
                relay_sock.close()
            except OSError:
                pass
            return
        local.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # Both sockets are about to carry the long-lived spliced session. `local`
        # still has the connect-phase timeout baked in by create_connection(), and
        # `relay_sock` still has the timeout _await_go() set to wait for "go" --
        # left in place, a quiet moment past that timeout during normal use (e.g.
        # a keepalive tick delayed by a busy GUI thread) reads as a dead peer and
        # tears down an otherwise-healthy connection.
        local.settimeout(None)
        relay_sock.settimeout(None)
        # Blocking mode alone would miss a link that goes silently dead (relay
        # box drops off the internet, LAN cable pulled) for minutes at a time
        # (OS TCP defaults); keepalive bounds that to a few seconds instead.
        enable_keepalive(local)
        enable_keepalive(relay_sock)
        _splice(relay_sock, local, a_to_b=viewer_preload)
