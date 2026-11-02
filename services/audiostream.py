"""Live meeting audio between Host and Viewer.

Routing (Host side) — two modes, nothing else:

  Mic OFF:
    host mic + host out  →  client out
    host mic + host out  →  Deepgram

  Mic ON (Viewer mic connected + CABLE inject live):
    client mic  →  host mic (CABLE)  →  Deepgram "You"  (no client voice)
    host out    →  client out + Deepgram
    (meeting apps must use CABLE Output as mic; speaker = real headphones)

`viewer_mic_active` switches the return mix and which mic Deepgram captures
(real host mic vs CABLE Output). `bump_capture_epoch` reopens loopback after
Windows default playback is restored off CABLE Input.

Framing: [1-byte type][4-byte big-endian length][payload]
  Viewer -> Host:   MT_AUTH   json {token}
  Host   -> Viewer:  MT_AUDIO  one 20 ms Opus packet (meeting audio)
  Viewer -> Host:   MT_AUDIO  one 20 ms Opus packet (mic, on the mic socket)
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
from services.audio_utils import (
    chosen_output_name, find_loopback, find_microphone, find_mic_inject_speaker,
    resample,
)
from services.sockopt import enable_keepalive
from services.vbcable import ensure_installed
from services.win_audio_defaults import ensure_real_playback_default

AUDIO_SR = 48000                              # streamed sample rate (mono, Opus-native)
FRAME_MS = 20
FRAME_SAMPLES = AUDIO_SR * FRAME_MS // 1000   # 960 samples per frame
CAP_BLOCK = 1024                              # device capture block size
MAX_LAG = AUDIO_SR                            # ~1 s per buffer -> bounds latency
OPUS_BITRATE = 48000                          # ~48-64 kbps VBR (voice + system mix)
SNDBUF = 64 * 1024
AUDIO_LIVENESS = 10.0   # no packet for this long -> reconnect the audio socket
DENOISE = True          # spectral noise suppression on the mix before encoding
PREROLL_MS = 120        # playback jitter buffer: buffered ahead before/after any underrun
PREROLL_SAMPLES = AUDIO_SR * PREROLL_MS // 1000
RESYNC_LAG_S = 0.1      # sender pacing: only resync the clock after falling this far behind

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
    the buffer is short (i.e. brief silence rather than blocking).

    With `preroll` > 0, read() withholds audio (returns silence) until at least
    `preroll` samples have accumulated, and re-arms that gate after any underrun
    instead of alternately feeding one silent frame and one real frame forever.
    This is the jitter buffer: it trades a small fixed delay for absorbing the
    normal jitter in packet arrival so brief hiccups don't surface as audible
    stutter."""

    def __init__(self, cap, preroll=0):
        self._buf = np.zeros(0, dtype=np.float32)
        self._cap = int(cap)
        self._preroll = int(preroll)
        self._primed = self._preroll == 0
        self._lock = threading.Lock()

    def write(self, samples):
        if not len(samples):
            return
        with self._lock:
            self._buf = np.concatenate([self._buf, samples.astype(np.float32)])
            if len(self._buf) > self._cap:
                self._buf = self._buf[-self._cap:]   # drop oldest
            if not self._primed and len(self._buf) >= self._preroll:
                self._primed = True

    def read(self, n):
        with self._lock:
            if not self._primed:
                return np.zeros(n, dtype=np.float32)
            if len(self._buf) >= n:
                out, self._buf = self._buf[:n], self._buf[n:]
                return out
            out = np.zeros(n, dtype=np.float32)
            out[:len(self._buf)] = self._buf
            self._buf = np.zeros(0, dtype=np.float32)
            self._primed = self._preroll == 0   # ran dry -> rebuffer before resuming
            return out


# ======================================================================
# Host: capture + mix + send
# ======================================================================

# Single switch: True only while Viewer mic is actually injecting into CABLE.
# Drives return mix and mic-Transcriber device (real mic vs CABLE Output).
_viewer_mic_active = threading.Event()
# Bumped after restoring real Windows playback so loopback/Deepgram reopen on
# the headphones instead of staying bound to a silent/wrong endpoint.
_capture_epoch = 0
_capture_epoch_lock = threading.Lock()


def set_viewer_mic_active(active):
    if active:
        _viewer_mic_active.set()
    else:
        _viewer_mic_active.clear()


def viewer_mic_active():
    return _viewer_mic_active.is_set()


def capture_epoch():
    with _capture_epoch_lock:
        return _capture_epoch


def bump_capture_epoch(reason=""):
    """Force AudioServer + Transcriber captures to reopen their devices."""
    global _capture_epoch
    with _capture_epoch_lock:
        _capture_epoch += 1
        n = _capture_epoch
    get_logger().info("audio: capture epoch -> %s (%s)", n, reason or "refresh")


def mix_for_viewer(loop_frame, mic_frame):
    """Return PCM for client out. Mic ON → host out only; Mic OFF → host out + host mic."""
    if viewer_mic_active():
        return loop_frame
    return loop_frame + mic_frame


class _Capture:
    """Records one device (loopback or mic) into a FIFO as mono @ AUDIO_SR.

    Reopens when `capture_epoch` bumps (e.g. after restoring default playback
    off CABLE) so client-out host sound tracks the real speaker.
    """

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
        while not self._stop.is_set():
            epoch = capture_epoch()
            try:
                if self.kind == "mic":
                    # Always the real host mic for the return mix; discarded
                    # while viewer_mic_active (mix_for_viewer sends loopback only).
                    device, channels = find_microphone(), 1
                else:
                    device, channels = find_loopback(), 2
                get_logger().info("audio: capturing %s %r (epoch %s)", self.kind,
                                  getattr(device, "name", device), epoch)
                with device.recorder(samplerate=CAPTURE_SR, channels=channels,
                                      blocksize=CAP_BLOCK) as rec:
                    while not self._stop.is_set() and capture_epoch() == epoch:
                        data = rec.record(numframes=CAP_BLOCK)
                        mono = np.mean(data, axis=1) if data.ndim > 1 else data
                        self.fifo.write(resample(mono, CAPTURE_SR, AUDIO_SR))
            except Exception:
                # A missing mic (or loopback) just means that side is silent; the
                # other still streams. Don't take the session down.
                get_logger().exception("audio: %s capture failed", self.kind)
                if self._stop.wait(0.5):
                    break


class _TokenServer:
    """TCP listener shared by AudioServer/MicServer: accept loop, token
    handshake, and keepalive/NODELAY setup are identical between the two --
    only what happens with the authed connection differs, via the `_run(conn,
    addr)` hook each subclass implements."""

    def __init__(self, port, token, label):
        self.port = int(port)
        self.token = str(token)
        self.label = label
        self._sock = None
        self._stop = threading.Event()

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.port))
        sock.listen(1)
        self._sock = sock
        threading.Thread(target=self._accept_loop, daemon=True).start()
        get_logger().info("%s: server on TCP %d", self.label, self.port)

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
        try:
            conn.settimeout(10)
            mtype, auth = _recv(conn)
            if mtype != MT_AUTH or auth is None:
                conn.close()
                return
            if str(json.loads(auth.decode("utf-8")).get("token", "")) != self.token:
                get_logger().info("%s: auth rejected from %s", self.label, addr[0])
                conn.close()
                return
            conn.settimeout(None)
            enable_keepalive(conn)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._run(conn, addr)
        except Exception:
            get_logger().exception("%s: session error", self.label)
        finally:
            try:
                conn.close()
            except OSError:
                pass
            get_logger().info("%s: session with %s ended", self.label, addr[0])

    def stop(self):
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass


class AudioServer(_TokenServer):
    """TCP audio sender. One listening socket; captures + mixes only while a
    viewer is connected. `token` is validated on connect (rotated per offer)."""

    def __init__(self, port, token):
        super().__init__(port, token, label="audio")

    def _run(self, conn, addr):
        loop = mic = None
        try:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SNDBUF)
        except OSError:
            pass

        from services.acodec import OpusEncoder
        encoder = OpusEncoder(AUDIO_SR, bitrate=OPUS_BITRATE)
        denoiser = None
        if DENOISE:
            from services.denoise import SpectralDenoiser
            denoiser = SpectralDenoiser(FRAME_SAMPLES)
        try:
            # Always capture both; mix_for_viewer picks what the client hears.
            loop = _Capture("loopback"); loop.start()
            mic = _Capture("mic"); mic.start()
            get_logger().info("audio: streaming to %s", addr[0])

            interval = FRAME_MS / 1000.0
            next_t = time.monotonic()
            last_mic_mode = None
            while not self._stop.is_set():
                # Drain both FIFOs every frame so the mic buffer never piles up
                # while mic mode sends loopback only.
                loop_frame = loop.fifo.read(FRAME_SAMPLES)
                mic_frame = mic.fifo.read(FRAME_SAMPLES)
                mic_on = viewer_mic_active()
                if mic_on != last_mic_mode:
                    get_logger().info(
                        "audio: client out = %s",
                        "host out only (mic on)" if mic_on else "host out + host mic")
                    last_mic_mode = mic_on
                frame = mix_for_viewer(loop_frame, mic_frame)
                if denoiser is not None:
                    frame = denoiser.process(frame)
                mixed = np.tanh(frame)
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
                elif dt < -RESYNC_LAG_S:
                    next_t = time.monotonic()   # fell too far behind -> resync the cadence
                # else: a small overrun -> next_t stays put, so the loop sends the
                # next packet sooner and catches back up instead of adding jitter
        finally:
            if loop is not None:
                loop.stop()
            if mic is not None:
                mic.stop()


# ======================================================================
# Viewer: receive + play
# ======================================================================

class _TokenClient:
    """Dialer shared by AudioClient/MicClient: connect (directly or via a
    relay dialer), authenticate with `token`, and reconnect with backoff on
    drop until stop(). Only what happens with the live socket differs, via
    the `_session(sock)` hook each subclass implements."""

    def __init__(self, host, port, token, connect=None, on_status=None, label="link"):
        self.host = host
        self.port = int(port)
        self.token = str(token)
        self.connect = connect          # () -> connected socket (relay mode)
        self.on_status = on_status
        self.label = label
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
                self._status(f"{self.label} reconnecting… ({exc})")
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 5.0)
                continue
            backoff = 1.0
            self._sock = sock
            try:
                # The direct (non-relay) branch above came from create_connection(),
                # which leaves its connect-phase timeout on the socket afterward --
                # left in place, a quiet moment during normal use reads as a dead
                # peer and drops a fine link.
                sock.settimeout(None)
                # Blocking mode alone would miss a link gone silently dead (cable
                # pulled, Wi-Fi drops uncleanly) for minutes at a time (OS TCP
                # defaults); keepalive bounds that to a few seconds instead.
                enable_keepalive(sock)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                _send(sock, MT_AUTH, json.dumps({"token": self.token}).encode("utf-8"))
            except OSError:
                self._close(sock)
                continue
            self._status(f"{self.label} connected")
            self._session(sock)
            self._close(sock)
            if self._stop.is_set():
                break
            self._status(f"{self.label} reconnecting…")
        self._status(f"{self.label} stopped")

    def _close(self, sock):
        try:
            sock.close()
        except OSError:
            pass


class AudioClient(_TokenClient):
    """Connects (directly or via a relay dialer), authenticates, and plays the
    received PCM on the default speaker. Reconnects on drops until stop()."""

    def __init__(self, host, port, token, connect=None, on_status=None):
        super().__init__(host, port, token, connect, on_status, label="audio")
        self._fifo = _SampleFIFO(MAX_LAG, preroll=PREROLL_SAMPLES)

    def start(self):
        super().start()
        threading.Thread(target=self._play_loop, daemon=True).start()

    def _session(self, sock):
        from services.acodec import OpusDecoder
        decoder = OpusDecoder(AUDIO_SR)
        try:
            sock.settimeout(AUDIO_LIVENESS)
        except OSError:
            return
        while not self._stop.is_set():
            try:
                mtype, data = _recv(sock)
            except socket.timeout:
                get_logger().info("audio: no packet for %.0fs -> reconnecting",
                                  AUDIO_LIVENESS)
                return
            except OSError:
                get_logger().info("audio: socket error -> reconnecting")
                return
            if mtype is None:
                get_logger().info("audio: host closed the connection -> reconnecting")
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
                    frame = self._fifo.read(FRAME_SAMPLES).reshape(-1, 1)
                    player.play(frame)
        except Exception:
            get_logger().exception("audio: playback failed")


# ======================================================================
# Mic relay: Viewer's microphone -> Host's audio input (reverse of the audio
# stream above). The Host still listens (like AudioServer) and the Viewer still
# dials in (like AudioClient) -- only who captures and who plays swaps sides.
# Playback targets a virtual-cable Input endpoint so the stream appears on the
# Host's microphone path (meeting apps pick the cable's Output as their mic).
# Reuses the same framing, FIFO/pre-roll, Opus codec and _Capture helper.
# ======================================================================

def _mic_playback_loop(fifo, should_run):
    """Runs on its own thread: drains `fifo` into the Host's virtual-mic inject
    speaker while `should_run()` is true. `fifo` is pre-rolled, so this absorbs
    the same jitter as the main audio stream's playback (see _SampleFIFO).

    Ensures VB-CABLE is present (installs the bundled official pack if needed)
    so the Viewer's mic appears on CABLE Output for meeting apps.

    Only marks viewer-mic mode active once inject is actually playing — otherwise
    the return mix mutes the host mic while CABLE is still down and the Viewer
    hears silence. Also restores a real Windows default playback device so
    meeting/interviewer audio stays on headphones (loopback), not CABLE Input.
    """
    _com_init()
    armed = False
    try:
        if not ensure_installed():
            get_logger().error(
                "mic: cannot inject — VB-CABLE not available "
                "(reboot after install if prompted)")
            return
        # VB-CABLE install often switches Windows default render to CABLE Input;
        # meeting apps then play the interviewer into the cable and headset
        # loopback (what we send to the Viewer) goes silent.
        ensure_real_playback_default(chosen_output_name())
        bump_capture_epoch("mic-on: restored real playback default")
        speaker = find_mic_inject_speaker()
        get_logger().info(
            "mic: injecting viewer mic into host input via %r "
            "(set meeting mic to CABLE Output; meeting speaker = headphones)",
            getattr(speaker, "name", speaker))
        with speaker.player(samplerate=AUDIO_SR, channels=1,
                            blocksize=FRAME_SAMPLES) as player:
            set_viewer_mic_active(True)
            armed = True
            get_logger().info(
                "mic: inject live — client mic→host mic→Deepgram; "
                "host out→client out + Deepgram")
            while should_run():
                frame = fifo.read(FRAME_SAMPLES).reshape(-1, 1)
                player.play(frame)
    except Exception:
        get_logger().exception("mic: input inject failed")
    finally:
        if armed:
            set_viewer_mic_active(False)
            bump_capture_epoch("mic-off: back to host mic + host out mix")


class MicServer(_TokenServer):
    """TCP listener on the Host: accepts the Viewer's mic connection, decodes the
    incoming Opus stream, and injects it into the Host's audio input via a
    virtual cable. One mic session at a time, like AudioServer."""

    def __init__(self, port, token):
        super().__init__(port, token, label="mic")

    def _run(self, conn, addr):
        local_stop = threading.Event()
        try:
            from services.acodec import OpusDecoder
            decoder = OpusDecoder(AUDIO_SR)
            fifo = _SampleFIFO(MAX_LAG, preroll=PREROLL_SAMPLES)
            # viewer_mic_active is set inside _mic_playback_loop only after
            # inject is actually open — not here (that muted return audio while
            # CABLE install/default restore was still running).
            threading.Thread(
                target=_mic_playback_loop,
                args=(fifo, lambda: not local_stop.is_set() and not self._stop.is_set()),
                daemon=True).start()

            get_logger().info("mic: streaming from viewer %s", addr[0])
            try:
                conn.settimeout(AUDIO_LIVENESS)
            except OSError:
                return
            while not self._stop.is_set():
                try:
                    mtype, data = _recv(conn)
                except socket.timeout:
                    get_logger().info("mic: no packet for %.0fs -> closing", AUDIO_LIVENESS)
                    break
                except OSError:
                    break
                if mtype is None:
                    break
                if mtype != MT_AUDIO:
                    continue
                try:
                    for samples in decoder.decode(data):
                        # Inject only — Deepgram "You" captures CABLE Output
                        # (client mic → host mic → text), not this PCM fork.
                        fifo.write(samples)
                except Exception:
                    get_logger().debug("mic: decode failed", exc_info=True)
        finally:
            local_stop.set()
            set_viewer_mic_active(False)


class MicClient(_TokenClient):
    """Viewer side: captures this PC's microphone, Opus-encodes it, and streams it
    to the Host (mirror of AudioClient, but sending instead of receiving).
    Connects (directly or via a relay dialer), authenticates, and reconnects on
    drops until stop()."""

    def __init__(self, host, port, token, connect=None, on_status=None):
        super().__init__(host, port, token, connect, on_status, label="mic")

    def _session(self, sock):
        try:
            from services.acodec import OpusEncoder
            encoder = OpusEncoder(AUDIO_SR, bitrate=OPUS_BITRATE)
            cap = _Capture("mic")
            cap.start()
            try:
                interval = FRAME_MS / 1000.0
                next_t = time.monotonic()
                while not self._stop.is_set():
                    pcm = cap.fifo.read(FRAME_SAMPLES)
                    pcm16 = (np.clip(pcm, -1.0, 1.0) * 32767).astype(np.int16)
                    for pkt in encoder.encode(pcm16):
                        _send(sock, MT_AUDIO, pkt)
                    next_t += interval
                    dt = next_t - time.monotonic()
                    if dt > 0:
                        self._stop.wait(dt)
                    elif dt < -RESYNC_LAG_S:
                        next_t = time.monotonic()
            finally:
                cap.stop()
        except OSError:
            pass
        except Exception:
            get_logger().exception("mic: capture failed")
