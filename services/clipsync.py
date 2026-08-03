"""Clipboard sync between Host and Viewer(s): copying text, an image, or files
on any connected machine's clipboard makes the same content pasteable on the
others. Always on for the duration of a connection -- no user toggle.

Rides its own dedicated TCP socket per peer (like the screen and audio
streams) so a large file copy never blocks the low-latency control channel
(remote input, stealth/AI toggles, shared notepad). The Host accepts one
connection per viewer; a change from any peer is applied to the Host's own
clipboard AND relayed to every OTHER connected viewer, the same multi-party
fan-out the "cmd" control channel gives stealth.

Framing: [1-byte type][4-byte big-endian length][payload]
  Viewer -> Host:  MT_AUTH   json {token}
  Either way:      MT_TEXT   utf-8 text
                   MT_IMAGE  PNG bytes
                   MT_FILES  json header, NUL, then the files' bytes back to back
                             (header: [{"name": str, "size": int}, ...])
                   MT_PING   empty -- keepalive; the peer just discards it

Unlike the screen/audio streams, this channel is idle by nature (nothing to
send until the clipboard actually changes), so without traffic a NAT/firewall/
relay along the way can quietly kill it for inactivity. MT_PING every few
seconds (see ClipboardSyncController._keepalive) keeps it looking alive, the
same job the control channel's "__ping"/"__pong" does for itself.
"""

import os
import json
import time
import struct
import socket
import hashlib
import tempfile
import threading

from PySide6 import QtCore, QtGui, QtWidgets

from logsetup import get_logger
from services.sockopt import enable_keepalive

MT_AUTH = 1
MT_TEXT = 2
MT_IMAGE = 3
MT_FILES = 4
MT_PING = 5

KEEPALIVE_INTERVAL_MS = 3000   # same cadence as the control channel's __ping

# A connection alive for less than this is treated as a failed attempt (see
# ClipboardClient._run), not a real session -- so a peer that accepts then
# immediately drops can't spin into a zero-delay reconnect storm.
_MIN_STABLE_CONN_S = 2.0

# Clipboard content over this is skipped (logged), never sent -- a huge file
# copy should never stall the link or fill the receiving side's disk.
MAX_CLIP_BYTES = 100 * 1024 * 1024
_HEADER_SLACK = 1 * 1024 * 1024   # room for the MT_FILES json header itself


def _send(sock, mtype, payload):
    sock.sendall(struct.pack(">BI", mtype, len(payload)) + payload)


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(65536, n - len(buf)))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def _recv(sock):
    head = _recv_exact(sock, 5)
    if head is None:
        return None, None
    mtype, length = struct.unpack(">BI", head)
    if length > MAX_CLIP_BYTES + _HEADER_SLACK:
        return None, None   # peer sent something absurd -> drop the connection
    payload = _recv_exact(sock, length) if length else b""
    if payload is None:
        return None, None
    return mtype, payload


def _snapshot_clipboard():
    """Pack the system clipboard's current content for the wire. Returns
    (mtype, payload), or None if it's empty, a folder, or over the size cap."""
    cb = QtWidgets.QApplication.clipboard()
    md = cb.mimeData()
    if md is None:
        return None

    if md.hasUrls():
        entries = []
        total = 0
        for url in md.urls():
            if not url.isLocalFile():
                return None
            path = url.toLocalFile()
            if not os.path.isfile(path):
                return None   # a folder (or other non-file entry): unsupported, skip the copy
            size = os.path.getsize(path)
            total += size
            if total > MAX_CLIP_BYTES:
                get_logger().info("clipboard: skipped files copy (%d bytes > %d cap)",
                                  total, MAX_CLIP_BYTES)
                return None
            entries.append((os.path.basename(path), path, size))
        if not entries:
            return None
        header = json.dumps([{"name": n, "size": s} for n, _, s in entries]).encode("utf-8")
        parts = [header, b"\x00"]
        for _, path, _ in entries:
            with open(path, "rb") as f:
                parts.append(f.read())
        return MT_FILES, b"".join(parts)

    if md.hasImage():
        img = cb.image()
        if not img.isNull():
            buf = QtCore.QBuffer()
            buf.open(QtCore.QIODevice.WriteOnly)
            img.save(buf, "PNG")
            data = bytes(buf.data())
            if len(data) > MAX_CLIP_BYTES:
                get_logger().info("clipboard: skipped image (%d bytes > %d cap)",
                                  len(data), MAX_CLIP_BYTES)
                return None
            return MT_IMAGE, data

    if md.hasText():
        text = md.text()
        if text:
            return MT_TEXT, text.encode("utf-8")

    return None


def _apply_snapshot(mtype, payload, files_dir):
    """Install received clip content on the local clipboard. Returns True on
    success (used to decide whether to keep the loop-guard hash)."""
    cb = QtWidgets.QApplication.clipboard()
    if mtype == MT_TEXT:
        cb.setText(payload.decode("utf-8", errors="replace"))
        return True

    if mtype == MT_IMAGE:
        img = QtGui.QImage()
        if not img.loadFromData(payload, "PNG"):
            return False
        cb.setImage(img)
        return True

    if mtype == MT_FILES:
        try:
            header, blobs = payload.split(b"\x00", 1)
            entries = json.loads(header.decode("utf-8"))
        except Exception:
            get_logger().exception("clipboard: malformed file payload")
            return False
        # A fresh subfolder per paste, so stale files from an earlier one never
        # linger alongside the latest.
        target = tempfile.mkdtemp(prefix="clip_", dir=files_dir)
        urls = []
        offset = 0
        for entry in entries:
            name = os.path.basename(str(entry.get("name", "file"))) or "file"
            size = int(entry.get("size", 0))
            data = blobs[offset:offset + size]
            offset += size
            path = os.path.join(target, name)
            with open(path, "wb") as f:
                f.write(data)
            urls.append(QtCore.QUrl.fromLocalFile(path))
        if not urls:
            return False
        md = QtCore.QMimeData()
        md.setUrls(urls)
        cb.setMimeData(md)
        return True

    return False


class ClipboardSyncController(QtCore.QObject):
    """Watches the local clipboard and relays changes to every connected peer;
    applies whatever a peer sends to the local clipboard. Must be constructed
    on the GUI thread (QClipboard requirement) -- get_clipboard_sync_controller()
    takes care of that since it's first called from Overlay.__init__.

    add_peer/remove_peer are thread-safe (called from accept/connect threads).
    A content-hash guard (`_last_hash`) stops a remote change we just applied
    from being read back off the clipboard and bounced straight back to its
    sender, or looped between host and viewer forever."""

    _new_peer = QtCore.Signal(object)                 # conn -> push current clip to it
    _apply_request = QtCore.Signal(int, bytes, object)  # (mtype, payload, source_conn)

    def __init__(self):
        super().__init__()
        self._conns = set()
        self._lock = threading.Lock()
        self._last_hash = None
        self._files_dir = tempfile.mkdtemp(prefix="ironstack_clip_")
        self._clipboard = QtWidgets.QApplication.clipboard()
        self._clipboard.dataChanged.connect(self._on_local_change)
        self._new_peer.connect(self._push_current_to)
        self._apply_request.connect(self._apply_incoming)
        # This channel is idle by nature (nothing to send until the clipboard
        # changes), unlike the always-flowing screen/audio streams -- so without
        # traffic, a NAT/firewall/relay along the way can quietly kill it for
        # inactivity. A periodic no-op keeps every connection looking alive.
        self._keepalive = QtCore.QTimer(self)
        self._keepalive.setInterval(KEEPALIVE_INTERVAL_MS)
        self._keepalive.timeout.connect(self._send_keepalive)
        self._keepalive.start()

    # --- connection management ---

    def _send_keepalive(self):
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            self._send_to(conn, MT_PING, b"")

    def add_peer(self, conn):
        with self._lock:
            self._conns.add(conn)
        threading.Thread(target=self._recv_loop, args=(conn,), daemon=True).start()
        self._new_peer.emit(conn)   # -> GUI thread: send it whatever's on the clipboard now

    def remove_peer(self, conn):
        with self._lock:
            self._conns.discard(conn)

    # --- outgoing: local clipboard change -> every connected peer ---

    def _on_local_change(self):
        self._broadcast()

    @QtCore.Slot(object)
    def _push_current_to(self, conn):
        snap = _snapshot_clipboard()
        if snap is None:
            return
        mtype, payload = snap
        self._send_to(conn, mtype, payload)

    def _broadcast(self):
        snap = _snapshot_clipboard()
        if snap is None:
            return
        mtype, payload = snap
        h = hashlib.sha1(payload).digest()
        if h == self._last_hash:
            return   # our own remote-applied change bouncing back, or a dup signal
        self._last_hash = h
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            self._send_to(conn, mtype, payload)

    def _send_to(self, conn, mtype, payload):
        try:
            _send(conn, mtype, payload)
        except OSError:
            pass

    # --- incoming: a peer's clipboard change -> local clipboard + other peers ---

    def _recv_loop(self, conn):
        try:
            while True:
                mtype, payload = _recv(conn)
                if mtype is None:
                    break
                if mtype in (MT_TEXT, MT_IMAGE, MT_FILES):
                    self._apply_request.emit(mtype, payload, conn)
        except OSError:
            pass
        finally:
            self.remove_peer(conn)
            try:
                conn.close()
            except OSError:
                pass

    @QtCore.Slot(int, bytes, object)
    def _apply_incoming(self, mtype, payload, source_conn):
        h = hashlib.sha1(payload).digest()
        # Set before touching the clipboard: dataChanged fires on this same GUI
        # thread as a result of applying it, and must see this as "already seen".
        self._last_hash = h
        if not _apply_snapshot(mtype, payload, self._files_dir):
            self._last_hash = None
            return
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            if conn is not source_conn:
                self._send_to(conn, mtype, payload)


_CLIP_CONTROLLER = None


def get_clipboard_sync_controller():
    global _CLIP_CONTROLLER
    if _CLIP_CONTROLLER is None:
        _CLIP_CONTROLLER = ClipboardSyncController()
    return _CLIP_CONTROLLER


# ======================================================================
# Host: accept viewer clip connections
# ======================================================================

class ClipboardServer:
    """TCP listener for clipboard sync. One listening socket; each viewer that
    connects (after a token handshake) becomes a peer of the shared
    ClipboardSyncController."""

    def __init__(self, port, token):
        self.port = int(port)
        self.token = str(token)
        self._sock = None
        self._stop = threading.Event()

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.port))
        sock.listen(1)
        self._sock = sock
        threading.Thread(target=self._accept_loop, daemon=True).start()
        get_logger().info("clipboard: server on TCP %d", self.port)

    def _accept_loop(self):
        self._sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._authenticate, args=(conn, addr),
                             daemon=True).start()

    def _authenticate(self, conn, addr):
        try:
            conn.settimeout(10)
            mtype, auth = _recv(conn)
            if mtype != MT_AUTH or auth is None:
                conn.close()
                return
            if str(json.loads(auth.decode("utf-8")).get("token", "")) != self.token:
                get_logger().info("clipboard: auth rejected from %s", addr[0])
                conn.close()
                return
            conn.settimeout(None)
            enable_keepalive(conn)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            get_logger().info("clipboard: viewer connected from %s", addr[0])
            get_clipboard_sync_controller().add_peer(conn)
        except Exception:
            get_logger().debug("clipboard: session error", exc_info=True)
            try:
                conn.close()
            except OSError:
                pass

    def stop(self):
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass


# ======================================================================
# Viewer: connect to the host's clip server
# ======================================================================

class ClipboardClient:
    """Connects (directly or via a relay dialer), authenticates, then joins the
    shared ClipboardSyncController as a peer. Reconnects on drops (like
    AudioClient) so a transient relay/network hiccup doesn't permanently end
    clipboard sync for the rest of the session."""

    def __init__(self, host, port, token, connect=None):
        self.host = host
        self.port = int(port)
        self.token = str(token)
        self.connect = connect          # () -> connected socket (relay mode)
        self._stop = threading.Event()
        self._sock = None

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def _run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                sock = (self.connect() if self.connect is not None
                        else socket.create_connection((self.host, self.port), timeout=15))
            except OSError:
                get_logger().info("clipboard: connect failed, retrying")
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, 5.0)
                continue
            self._sock = sock
            try:
                # The direct (non-relay) branch above came from create_connection(),
                # which leaves its connect-phase timeout on the socket afterward --
                # left in place, a quiet moment during normal use (keepalive delayed
                # by a busy GUI thread) reads as a dead peer and drops a fine link.
                sock.settimeout(None)
                # Blocking mode alone would miss a link gone silently dead (cable
                # pulled, Wi-Fi drops uncleanly) for minutes at a time (OS TCP
                # defaults); keepalive bounds that to a few seconds instead.
                enable_keepalive(sock)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                _send(sock, MT_AUTH, json.dumps({"token": self.token}).encode("utf-8"))
            except OSError:
                try:
                    sock.close()
                except OSError:
                    pass
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, 5.0)
                continue
            get_logger().info("clipboard: connected to host")
            connected_at = time.monotonic()
            get_clipboard_sync_controller().add_peer(sock)
            # Block here for as long as this connection is alive.
            # ClipboardSyncController's own recv loop closes the socket (so
            # fileno() -> -1) once the peer drops it or it errors out.
            while not self._stop.is_set() and sock.fileno() != -1:
                self._stop.wait(1.0)
            if self._stop.is_set():
                return
            get_logger().info("clipboard: disconnected, reconnecting…")
            if time.monotonic() - connected_at < _MIN_STABLE_CONN_S:
                # Died right away (stale relay pairing, host not ready yet):
                # throttle like a failed connect. Without this, a connection that
                # keeps getting accepted-then-dropped spins into a zero-delay
                # reconnect loop -- each iteration also re-pushes the whole
                # clipboard to the new peer, which floods the GUI thread with
                # snapshot work and reads as a total freeze.
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, 5.0)
            else:
                backoff = 1.0
