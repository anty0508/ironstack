#!/usr/bin/env python3
"""IronStack relay server.

Run this on a machine with a public IP (e.g. an AWS EC2 box) to connect a Host and
a Viewer that can't reach each other directly — the usual case behind CGNAT, a VPN,
or a router with no port-forward. Both ends make OUTBOUND TCP connections here (which
always work), and the relay pairs them by a shared room ID + channel and splices
their sockets, copying bytes both ways.

It understands nothing about the IronStack protocol — it's a generic byte pipe, so
the same relay carries both the meeting-control channel and the remote-screen
channel. Multiple rooms are multiplexed by the room id.

Reverse-tunnel model:
  - The Host parks a small POOL of idle "ready" connections per (room, channel).
    The relay heartbeats them ({"ping":1}) so NAT keeps the mapping alive.
  - When a Viewer connects for (room, channel), the relay claims one ready Host
    connection, sends it {"go":1}, sends the Viewer {"ok":1}, and splices the two.
    The Host immediately opens a replacement, so the pool stays full.

Handshake — the first line each side sends is newline-framed JSON, then raw bytes:
  Viewer -> relay:  {"v":1,"role":"viewer","room":"<id>","channel":"control|screen"}
  Host   -> relay:  {"v":1,"role":"host","room":"<id>","channel":"control|screen"}
  relay  -> viewer: {"ok":1}            a host was matched; the splice begins
                    {"error":"..."}     no host available; the connection is closed
  relay  -> host:   {"ping":1} ...      idle heartbeats while parked
                    {"go":1}            claimed; the splice begins

Usage on the server:
  python3 relay_server.py                 # listens on 0.0.0.0:48920
  python3 relay_server.py --port 48920

Then open that TCP port in the OS firewall and (on EC2) the Security Group.
No third-party packages — just Python 3.
"""

import sys
import json
import time
import socket
import argparse
import threading
from collections import defaultdict

PING_INTERVAL = 15.0      # seconds between heartbeats to a parked host connection
                          # (keeps a lossy CGNAT/VPN NAT mapping open)
VIEWER_WAIT = 8.0         # how long to wait for a ready host conn before telling
                          # the viewer "host not connected" (< the client's dial
                          # timeout, so the error arrives instead of a timeout)
HANDSHAKE_DEADLINE = 20.0  # absolute cap on reading the first handshake line, so a
                           # client that opens a socket and then dribbles (or never
                           # sends) can't pin a thread + fd forever (slow-loris)
MAX_CONNS = 4096          # soft cap on concurrent connections; past this the relay
                          # drops new ones instead of marching into fd exhaustion.
                          # A host parks ~7-9 conns, so this is ~450 live hosts.
BUFSIZE = 65536

# Sized in serve(); a BoundedSemaphore that gates how many connections may be
# in flight at once. None until serve() runs (e.g. under unit tests).
_conn_gate = None


def _log(*a):
    print("[relay]", *a, flush=True)


def _send_line(sock, obj):
    sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))


def _recv_line(sock, timeout=HANDSHAKE_DEADLINE, limit=65536):
    """Read one newline-framed line WITHOUT over-reading past it (byte at a time),
    so the bytes that follow belong cleanly to the spliced stream. `timeout` is an
    ABSOLUTE deadline across the whole line -- not per-recv -- so a peer that dribbles
    one byte just under the per-recv timeout can't hold the connection open forever."""
    deadline = time.monotonic() + timeout
    buf = bytearray()
    while b"\n" not in buf:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock.settimeout(remaining)
        chunk = sock.recv(1)
        if not chunk:
            break
        buf += chunk
        if len(buf) > limit:
            break
    line, _, _ = bytes(buf).partition(b"\n")
    return line


class _Waiters:
    """Per-(room, channel) queue of parked host connections, with a blocking pop."""

    def __init__(self):
        self._q = defaultdict(list)
        self._cv = threading.Condition()

    def put(self, key, item):
        with self._cv:
            self._q[key].append(item)
            self._cv.notify_all()

    def discard(self, key, item):
        with self._cv:
            lst = self._q.get(key)
            if lst and item in lst:
                lst.remove(item)

    def pop_wait(self, key, timeout):
        with self._cv:
            if not self._cv.wait_for(lambda: bool(self._q.get(key)), timeout=timeout):
                return None
            return self._q[key].pop(0)


_waiters = _Waiters()


def _enable_keepalive(sock, idle=10, interval=3, count=3):
    """Detect a genuinely dead peer within ~idle+interval*count seconds. The only
    dead-link signal a spliced connection has: with the handshake read-timeout
    cleared (see _prime_for_splice), a silent-but-alive direction must NOT be
    mistaken for a dead one, so keepalive does that job instead."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        return
    if sys.platform == "win32":
        try:
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, idle * 1000, interval * 1000))
        except (AttributeError, OSError):
            pass
    else:
        for opt, val in ((getattr(socket, "TCP_KEEPIDLE", None), idle),
                         (getattr(socket, "TCP_KEEPINTVL", None), interval),
                         (getattr(socket, "TCP_KEEPCNT", None), count)):
            if opt is not None:
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, opt, val)
                except OSError:
                    pass


def _prime_for_splice(*socks):
    """Hand both legs of a splice off to blocking mode with keepalive. _recv_line
    left a 20s read-timeout on each socket for the handshake; carried into the
    splice it would fire on any direction that stays quiet for 20s -- e.g. the
    viewer->host leg of audio/screen, which is one-way, so the whole session was
    being torn down every ~20s. Clear it and rely on keepalive for real deaths."""
    for s in socks:
        try:
            s.settimeout(None)
        except OSError:
            pass
        _enable_keepalive(s)


def _pump(src, dst, preload=b""):
    try:
        if preload:
            dst.sendall(preload)
        while True:
            data = src.recv(BUFSIZE)
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


def _splice(a, b, a_to_b=b"", b_to_a=b""):
    """Copy bytes both ways between a and b until either side closes."""
    t = threading.Thread(target=_pump, args=(a, b, a_to_b), daemon=True)
    t.start()
    _pump(b, a, b_to_a)
    t.join(timeout=2.0)
    for s in (a, b):
        try:
            s.close()
        except OSError:
            pass


def _handle_host(sock, room, channel):
    key = (room, channel)
    item = {"sock": sock, "viewer": None, "claimed": threading.Event()}
    # Keepalive on the PARKED socket too: the {"ping":1} below is tiny and, on a
    # peer that dropped off without a RST, buffers silently for a long time before
    # sendall() ever fails -- so without this a dead parked conn (and its fd +
    # thread) can linger for minutes. Keepalive bounds that to a few seconds.
    _enable_keepalive(sock)
    _waiters.put(key, item)
    _log(f"host ready   room={room} channel={channel}")
    # Heartbeat while parked so a CGNAT/VPN mapping stays open; also detects death.
    while not item["claimed"].is_set():
        try:
            _send_line(sock, {"ping": 1})
        except OSError:
            _waiters.discard(key, item)
            try:
                sock.close()
            except OSError:
                pass
            return
        item["claimed"].wait(PING_INTERVAL)
    # Claimed by a viewer: greenlight this connection and splice the two.
    viewer = item["viewer"]
    try:
        _send_line(sock, {"go": 1})
    except OSError:
        try:
            viewer.close()
        except OSError:
            pass
        return
    _log(f"paired       room={room} channel={channel}")
    _prime_for_splice(sock, viewer)
    _splice(sock, viewer)
    _log(f"closed       room={room} channel={channel}")


def _handle_viewer(sock, room, channel):
    key = (room, channel)
    deadline = time.monotonic() + VIEWER_WAIT
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            try:
                _send_line(sock, {"error": "host not connected via relay"})
            except OSError:
                pass
            sock.close()
            _log(f"no host      room={room} channel={channel}")
            return
        item = _waiters.pop_wait(key, remaining)
        if item is None:
            continue
        # Claim it, then let the host connection's thread drive the splice.
        item["viewer"] = sock
        item["claimed"].set()
        try:
            _send_line(sock, {"ok": 1})
        except OSError:
            sock.close()
        return


def _handle(sock):
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        line = _recv_line(sock)
        msg = json.loads(line.decode("utf-8")) if line else {}
        role = msg.get("role")
        room = str(msg.get("room", "")).strip()
        channel = str(msg.get("channel", "control"))
        if not room or channel not in ("control", "screen", "audio", "clip"):
            _send_line(sock, {"error": "bad handshake"})
            sock.close()
            return
        if role == "host":
            _handle_host(sock, room, channel)
        elif role == "viewer":
            _handle_viewer(sock, room, channel)
        else:
            _send_line(sock, {"error": "bad role"})
            sock.close()
    except Exception as exc:   # noqa: BLE001 — a relay must never die on one bad peer
        try:
            sock.close()
        except OSError:
            pass
        _log("session error:", exc)


def _handle_gated(sock):
    """Run _handle, then always free the connection slot back to the gate."""
    try:
        _handle(sock)
    finally:
        if _conn_gate is not None:
            try:
                _conn_gate.release()
            except ValueError:   # released more than acquired -- shouldn't happen
                pass


def serve(host, port, max_conns=MAX_CONNS):
    global _conn_gate
    _conn_gate = threading.BoundedSemaphore(max_conns) if max_conns > 0 else None
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(128)
    _log(f"listening on {host}:{port} (max_conns={max_conns})")
    while True:
        try:
            conn, addr = srv.accept()
        except OSError as exc:
            # A transient accept() failure must NOT take the relay down: fd
            # exhaustion (EMFILE/ENFILE), ECONNABORTED, a momentary ENOBUFS. Log,
            # breathe so we don't spin hot while fds are still scarce, keep serving.
            _log("accept failed:", exc)
            time.sleep(0.1)
            continue
        # Cap in-flight connections so a flood (or a leak elsewhere) can't march the
        # whole box into fd exhaustion -- drop the new one instead of dying.
        if _conn_gate is not None and not _conn_gate.acquire(blocking=False):
            _log("at capacity, dropping", addr)
            try:
                conn.close()
            except OSError:
                pass
            continue
        try:
            threading.Thread(target=_handle_gated, args=(conn,), daemon=True).start()
        except RuntimeError as exc:   # "can't start new thread" -> OS thread limit
            _log("thread start failed:", exc)
            if _conn_gate is not None:
                _conn_gate.release()
            try:
                conn.close()
            except OSError:
                pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="IronStack relay server")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    ap.add_argument("--port", type=int, default=48920, help="TCP port (default 48920)")
    ap.add_argument("--max-conns", type=int, default=MAX_CONNS,
                    help=f"cap on concurrent connections (default {MAX_CONNS}; 0=unlimited)")
    args = ap.parse_args(argv)
    try:
        serve(args.host, args.port, args.max_conns)
    except KeyboardInterrupt:
        _log("shutting down")
        return 0
    except Exception as exc:   # noqa: BLE001 -- last-resort guard; a supervisor
        # (systemd Restart=always) brings us straight back. Better to exit non-zero
        # and be restarted clean than to wedge in a half-broken state.
        _log("fatal:", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
