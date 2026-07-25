"""Live meeting audio: on the Host, capture the system output (loopback, i.e. the
interviewer's voice) AND the microphone (the candidate's voice), mix them, and
stream to a Viewer that plays the mix on its own speakers.

It rides its own dedicated TCP socket (like the screen), so audio never competes
with screen frames or control messages. The payload is Opus (mono, 48 kHz, voice-
tuned ~24-40 kbps) — ~10x smaller than raw PCM, so it barely dents the screen
stream's bandwidth even on a slow link at Low quality. Latency is bounded by
drop-oldest buffering: on a slow/lossy link you get brief drop-outs instead of
audio that drifts further and further behind.

Framing: [1-byte type][4-byte big-endian length][payload]
  Viewer -> Host:   MT_AUTH   json {token}
  Host   -> Viewer:  MT_AUDIO  one 20 ms Opus packet
"""

import sys
import json
import time
import ctypes
import struct
import socket
import threading

import numpy as np

from config import CAPTURE_SR
from logsetup import get_logger
from services.audio_utils import find_loopback, find_microphone, resample

AUDIO_SR = 48000                              # streamed sample rate (mono, Opus-native)
FRAME_MS = 20
FRAME_SAMPLES = AUDIO_SR * FRAME_MS // 1000   # 960 samples per frame
CAP_BLOCK = 1024                              # device capture block size
MAX_LAG = AUDIO_SR                            # ~1 s per buffer -> bounds latency
OPUS_BITRATE = 24000                          # ~24-40 kbps VBR (voice)
SNDBUF = 64 * 1024

MT_AUTH = 1      # Viewer -> Host: json {token}
MT_AUDIO = 2     # Host -> Viewer: one Opus packet


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


def _com_init():
    # soundcard (WASAPI) needs a COM apartment on its thread; the GUI thread is
    # STA, so audio threads join the multithreaded apartment.
    if sys.platform == "win32":
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 0)   # 0 = COINIT_MULTITHREADED
        except Exception:
            pass


class _SampleFIFO:
    """Thread-safe float32 sample buffer, hard-capped to bound latency (drops the
    oldest samples when full). read(n) always returns n samples, zero-padded when
    the buffer is short (i.e. brief silence rather than blocking)."""

    def __init__(self, cap):
        self._buf = np.zeros(0, dtype=np.float32)
        self._cap = int(cap)
        self._lock = threading.Lock()

    def write(self, samples):
        if not len(samples):
            return
        with self._lock:
            self._buf = np.concatenate([self._buf, samples.astype(np.float32)])
            if len(self._buf) > self._cap:
                self._buf = self._buf[-self._cap:]   # drop oldest

    def read(self, n):
        with self._lock:
            if len(self._buf) >= n:
                out, self._buf = self._buf[:n], self._buf[n:]
                return out
            out = np.zeros(n, dtype=np.float32)
            out[:len(self._buf)] = self._buf
            self._buf = np.zeros(0, dtype=np.float32)
            return out


# ======================================================================
# Host: capture + mix + send
# ======================================================================

class _Capture:
    """Records one device (loopback or mic) into a FIFO as mono @ AUDIO_SR."""

    def __init__(self, kind):
        self.kind = kind      # "loopback" | "mic"
        self.fifo = _SampleFIFO(MAX_LAG)
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _run(self):
        _com_init()
        try:
            if self.kind == "mic":
                device, channels = find_microphone(), 1
            else:
                device, channels = find_loopback(), 2
            get_logger().info("audio: capturing %s %r", self.kind,
                              getattr(device, "name", device))
            with device.recorder(samplerate=CAPTURE_SR, channels=channels,
                                  blocksize=CAP_BLOCK) as rec:
                while not self._stop.is_set():
                    data = rec.record(numframes=CAP_BLOCK)
                    mono = np.mean(data, axis=1) if data.ndim > 1 else data
                    self.fifo.write(resample(mono, CAPTURE_SR, AUDIO_SR))
        except Exception:
            # A missing mic (or loopback) just means that side is silent; the other
            # still streams. Don't take the session down.
            get_logger().exception("audio: %s capture failed", self.kind)


class AudioServer:
    """TCP audio sender. One listening socket; captures + mixes only while a
    viewer is connected. `token` is validated on connect (rotated per offer)."""

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
        get_logger().info("audio: server on TCP %d", self.port)

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
        loop = mic = None
        try:
            conn.settimeout(10)
            mtype, auth = _recv(conn)
            if mtype != MT_AUTH or auth is None:
                conn.close()
                return
            if str(json.loads(auth.decode("utf-8")).get("token", "")) != self.token:
                get_logger().info("audio: auth rejected from %s", addr[0])
                conn.close()
                return
            conn.settimeout(None)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SNDBUF)
            except OSError:
                pass

            from services.acodec import OpusEncoder
            encoder = OpusEncoder(AUDIO_SR, bitrate=OPUS_BITRATE)
            loop = _Capture("loopback"); loop.start()
            mic = _Capture("mic"); mic.start()
            get_logger().info("audio: streaming (loopback + mic, Opus) to %s", addr[0])

            interval = FRAME_MS / 1000.0
            next_t = time.monotonic()
            while not self._stop.is_set():
                mixed = np.clip(loop.fifo.read(FRAME_SAMPLES) +
                                mic.fifo.read(FRAME_SAMPLES), -1.0, 1.0)
                pcm16 = (mixed * 32767).astype(np.int16)
                try:
                    for pkt in encoder.encode(pcm16):
                        _send(conn, MT_AUDIO, pkt)
                except OSError:
                    break
                next_t += interval
                dt = next_t - time.monotonic()
                if dt > 0:
                    self._stop.wait(dt)
                else:
                    next_t = time.monotonic()   # fell behind -> reset the cadence
        except Exception:
            get_logger().debug("audio: session error", exc_info=True)
        finally:
            if loop is not None:
                loop.stop()
            if mic is not None:
                mic.stop()
            try:
                conn.close()
            except OSError:
                pass
            get_logger().info("audio: session with %s ended", addr[0])

    def stop(self):
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass


# ======================================================================
# Viewer: receive + play
# ======================================================================

class AudioClient:
    """Connects (directly or via a relay dialer), authenticates, and plays the
    received PCM on the default speaker. Reconnects on drops until stop()."""

    def __init__(self, host, port, token, connect=None, on_status=None):
        self.host = host
        self.port = int(port)
        self.token = str(token)
        self.connect = connect          # () -> connected socket (relay mode)
        self.on_status = on_status
        self._stop = threading.Event()
        self._muted = threading.Event()
        self._sock = None
        self._fifo = _SampleFIFO(MAX_LAG)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._play_loop, daemon=True).start()

    def set_muted(self, muted):
        if muted:
            self._muted.set()
        else:
            self._muted.clear()

    def stop(self):
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def _status(self, text):
        if self.on_status:
            try:
                self.on_status(text)
            except Exception:
                pass

    def _run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                sock = (self.connect() if self.connect is not None
                        else socket.create_connection((self.host, self.port), timeout=15))
            except OSError as exc:
                self._status(f"audio reconnecting… ({exc})")
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 5.0)
                continue
            backoff = 1.0
            self._sock = sock
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                _send(sock, MT_AUTH, json.dumps({"token": self.token}).encode("utf-8"))
            except OSError:
                self._close(sock)
                continue
            self._status("audio connected")
            self._recv_loop(sock)
            self._close(sock)
            if self._stop.is_set():
                break
            self._status("audio reconnecting…")
        self._status("audio stopped")

    def _recv_loop(self, sock):
        from services.acodec import OpusDecoder
        decoder = OpusDecoder(AUDIO_SR)
        try:
            sock.settimeout(10)
        except OSError:
            return
        while not self._stop.is_set():
            try:
                mtype, data = _recv(sock)
            except socket.timeout:
                return
            except OSError:
                return
            if mtype is None:
                return
            if mtype != MT_AUDIO:
                continue
            try:
                for samples in decoder.decode(data):
                    self._fifo.write(samples)
            except Exception:
                get_logger().debug("audio: decode failed", exc_info=True)

    def _play_loop(self):
        _com_init()
        try:
            import soundcard as sc
            speaker = sc.default_speaker()
            get_logger().info("audio: playing on %r", getattr(speaker, "name", speaker))
            with speaker.player(samplerate=AUDIO_SR, channels=1,
                                blocksize=FRAME_SAMPLES) as player:
                while not self._stop.is_set():
                    # read() paces itself only when data exists; player.play blocks
                    # ~one frame of real time, so playback stays near real time.
                    # Always drain the FIFO so it doesn't back up; when muted,
                    # play silence so the host's voice never reaches the speaker.
                    frame = self._fifo.read(FRAME_SAMPLES).reshape(-1, 1)
                    if self._muted.is_set():
                        frame = np.zeros_like(frame)
                    player.play(frame)
        except Exception:
            get_logger().exception("audio: playback failed")

    def _close(self, sock):
        try:
            sock.close()
        except OSError:
            pass
