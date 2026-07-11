"""Remote screen stream over a dedicated TCP socket.

TCP (reliable) means no skipped/garbled frames — the trade-off vs UDP is latency,
which we keep bounded by:
  - drop-at-source: the capture loop sends the CURRENT frame with a blocking send,
    then grabs a FRESH frame — so nothing queues and we always send the latest
    screen (fps naturally adapts to bandwidth),
  - TCP_NODELAY (no Nagle) + a small send buffer (little data in flight),
  - hardware H.264 (low encode latency) + ~720p downscale (small frames).

This socket carries video only (Host -> Viewer). Input rides the reliable netlink
control channel, so keystrokes/clicks are never dropped and are never stuck behind
a video frame.

Wire framing: [4-byte big-endian length][payload].
  Viewer -> Host (first):  JSON auth {token, monitor_id}
  Host   -> Viewer:        H.264 packet bytes
"""

import json
import time
import struct
import socket
import threading

from logsetup import get_logger
from services import screencap
from services.vcodec import H264Encoder, H264Decoder
from services.inputinject import detect_cursor

DEFAULT_FPS = 20
SNDBUF = 512 * 1024      # small send buffer -> bounds in-flight latency
SCREEN_LIVENESS = 10.0   # no frame for this long -> reconnect the screen socket

# media message types (host<->viewer on the screen socket)
MT_AUTH = 1      # Viewer -> Host: json {token, monitor_id, quality}
MT_FRAME = 2     # Host -> Viewer: H.264 packet bytes
MT_CURSOR = 3    # Host -> Viewer: json {cursor: <name>}

# Quality presets the viewer can pick (max dimension, bitrate). Higher = sharper
# but more bandwidth/CPU. Chosen by the viewer and sent in the auth message.
QUALITY = {
    "low":    (1280, 2_000_000),
    "medium": (1600, 4_000_000),
    "high":   (1920, 6_500_000),
}
DEFAULT_QUALITY = "high"


def _fit(w, h, max_dim):
    scale = min(1.0, max_dim / max(w, h))
    return (max(2, int(w * scale)) & ~1, max(2, int(h * scale)) & ~1)


def _send(sock, mtype, payload):
    sock.sendall(struct.pack(">BI", mtype, len(payload)) + payload)


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _recv(sock):
    head = _recv_exact(sock, 5)
    if head is None:
        return None, None
    mtype, length = struct.unpack(">BI", head)
    payload = _recv_exact(sock, length) if length else b""
    if payload is None:
        return None, None
    return mtype, payload


# ======================================================================
# Host
# ======================================================================

class RemoteScreenServer:
    """TCP screen sender. `on_session(geometry)` fires when a viewer's monitor is
    chosen, giving (mon_x, mon_y, mon_w, mon_h) so the host can build an input
    injector for that display. One controlling viewer at a time."""

    def __init__(self, port, token, fps=DEFAULT_FPS, on_session=None):
        self.port = int(port)
        self.token = str(token)
        self.fps = fps
        self.on_session = on_session
        self._sock = None
        self._stop = threading.Event()
        self._enc = None
        self._enc_lock = threading.Lock()

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.port))
        sock.listen(1)
        self._sock = sock
        threading.Thread(target=self._accept_loop, daemon=True).start()
        get_logger().info("remote: screen server on TCP %d", self.port)

    def request_keyframe(self):
        with self._enc_lock:
            if self._enc is not None:
                self._enc.request_keyframe()

    def _accept_loop(self):
        self._sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._session, args=(conn, addr),
                             daemon=True).start()

    def _session(self, conn, addr):
        cap = None
        try:
            conn.settimeout(10)
            mtype, auth = _recv(conn)
            if mtype != MT_AUTH or auth is None:
                conn.close()
                return
            hello = json.loads(auth.decode("utf-8"))
            if str(hello.get("token", "")) != self.token:
                get_logger().info("remote: screen auth rejected from %s", addr[0])
                conn.close()
                return
            conn.settimeout(None)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SNDBUF)
            except OSError:
                pass

            monitor_id = int(hello.get("monitor_id", 1))
            quality = str(hello.get("quality", DEFAULT_QUALITY))
            max_dim, bitrate = QUALITY.get(quality, QUALITY[DEFAULT_QUALITY])
            cap = screencap.ScreenCapturer(monitor_id)
            cap.start()
            enc_w, enc_h = _fit(cap.width, cap.height, max_dim)
            encoder = H264Encoder(enc_w, enc_h, fps=self.fps, bitrate=bitrate)
            encoder.request_keyframe()
            with self._enc_lock:
                self._enc = encoder
            if self.on_session:
                try:
                    self.on_session(cap.geometry())
                except Exception:
                    get_logger().exception("remote: on_session failed")
            get_logger().info("remote: streaming monitor %d (%dx%d -> %dx%d) to %s",
                              monitor_id, cap.width, cap.height, enc_w, enc_h, addr[0])

            interval = 1.0 / self.fps
            last_cursor = None
            while not self._stop.is_set():
                t0 = time.monotonic()
                try:
                    # Send the host's cursor shape when it changes, so the viewer
                    # shows the right pointer (I-beam / hand / wait / ...).
                    cur = detect_cursor()
                    if cur != last_cursor:
                        last_cursor = cur
                        _send(conn, MT_CURSOR, json.dumps({"cursor": cur}).encode("utf-8"))
                    frame = cap.grab()
                    if frame is not None:
                        for pkt in encoder.encode(frame):
                            _send(conn, MT_FRAME, pkt)   # blocking send == drop-at-source
                except OSError:
                    break
                # cap the rate when the network is fast; a slow send already paced us
                dt = interval - (time.monotonic() - t0)
                if dt > 0:
                    self._stop.wait(dt)
        except Exception:
            get_logger().debug("remote: screen session error", exc_info=True)
        finally:
            with self._enc_lock:
                self._enc = None
            if cap is not None:
                cap.stop()
            try:
                conn.close()
            except OSError:
                pass
            get_logger().info("remote: screen session with %s ended", addr[0])

    def stop(self):
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass


# ======================================================================
# Viewer
# ======================================================================

class RemoteScreenClient:
    """TCP screen receiver: connect, authenticate, decode frames -> on_frame."""

    def __init__(self, host, port, token, monitor_id=1, quality=DEFAULT_QUALITY,
                 on_frame=None, on_status=None, on_cursor=None, connect=None):
        self.host = host
        self.port = int(port)
        self.token = str(token)
        self.monitor_id = int(monitor_id)
        self.quality = quality
        self.on_frame = on_frame
        self.on_status = on_status
        self.on_cursor = on_cursor
        # connect() -> a connected socket. When set (relay mode), it's used instead
        # of a direct connection to host:port; the MT_AUTH handshake is unchanged.
        self.connect = connect
        self._sock = None
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _status(self, text):
        if self.on_status:
            try:
                self.on_status(text)
            except Exception:
                pass

    def _run(self):
        """Connect, stream, and on any drop (dead link or >SCREEN_LIVENESS s with no
        frame on a slow network) reconnect automatically, retrying with backoff
        until stop(). The host forces a keyframe on each new session."""
        backoff = 1.0
        while not self._stop.is_set():
            try:
                sock = (self.connect() if self.connect is not None
                        else socket.create_connection((self.host, self.port), timeout=15))
            except OSError as exc:
                self._status(f"remote reconnecting… ({exc})")
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 5.0)
                continue
            backoff = 1.0
            self._sock = sock
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                _send(sock, MT_AUTH, json.dumps(
                    {"token": self.token, "monitor_id": self.monitor_id,
                     "quality": self.quality}).encode("utf-8"))
            except OSError:
                self._close(sock)
                if self._stop.is_set():
                    break
                self._status("remote reconnecting…")
                continue
            self._recv_loop(sock)
            self._close(sock)
            self._sock = None
            if self._stop.is_set():
                break
            self._status("remote reconnecting…")
        self._status("remote disconnected")

    def _recv_loop(self, sock):
        decoder = H264Decoder()
        connected = False
        try:
            sock.settimeout(SCREEN_LIVENESS)
        except OSError:
            return
        while not self._stop.is_set():
            try:
                mtype, data = _recv(sock)
            except socket.timeout:
                get_logger().info("remote: no frame for %.0fs -> reconnecting",
                                  SCREEN_LIVENESS)
                return
            except OSError:
                return
            if mtype is None:
                return
            if mtype == MT_CURSOR:
                if self.on_cursor:
                    try:
                        self.on_cursor(json.loads(data.decode("utf-8")).get("cursor", "arrow"))
                    except Exception:
                        pass
                continue
            if mtype != MT_FRAME:
                continue
            if not connected:
                connected = True
                self._status("remote connected")
            for frame in decoder.decode(data):
                if self.on_frame:
                    try:
                        self.on_frame(frame)
                    except Exception:
                        get_logger().debug("remote: on_frame failed", exc_info=True)

    def _close(self, sock):
        try:
            sock.close()
        except OSError:
            pass

    def stop(self):
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
