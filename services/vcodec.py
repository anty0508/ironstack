"""H.264 encode/decode for the remote screen stream (PyAV / ffmpeg).

The encoder auto-selects a hardware H.264 encoder when one works (NVENC / QSV /
AMF — low CPU, low latency) and falls back to software libx264 with a
zero-latency preset. Frames come in as BGR uint8 arrays; PyAV's libswscale
handles the scale + pixel-format conversion, so we need no cv2/Pillow.
"""

from fractions import Fraction

import av
import numpy as np

from logsetup import get_logger

_ENCODER_PREFERENCE = ["h264_nvenc", "h264_qsv", "h264_amf", "libx264"]


def _encoder_options(name, fps):
    g = str(max(1, fps * 4))   # keyframe interval; keyframe-on-connect handles start
    if name == "libx264":
        return {"preset": "ultrafast", "tune": "zerolatency", "g": g}
    if name == "h264_nvenc":
        return {"preset": "p1", "tune": "ull", "g": g, "delay": "0"}
    if name == "h264_qsv":
        return {"preset": "veryfast", "g": g}
    if name == "h264_amf":
        return {"usage": "ultralowlatency", "quality": "speed", "g": g}
    return {"g": g}


class H264Encoder:
    """Encode BGR frames to H.264 packet bytes. Bitrate can change live; the
    resolution is fixed for the encoder's lifetime."""

    def __init__(self, width, height, fps=15, bitrate=2_500_000, prefer_hardware=True):
        self.width = int(width) - (int(width) % 2)
        self.height = int(height) - (int(height) % 2)
        self.fps = fps
        self._pts = 0
        self._force_kf = False
        self.ctx = None
        self.name = None

        names = _ENCODER_PREFERENCE if prefer_hardware else ["libx264"]
        black = np.zeros((self.height, self.width, 3), np.uint8)
        last_err = None
        for name in names:
            try:
                probe = self._make_ctx(name, fps, bitrate)
                frame = av.VideoFrame.from_ndarray(black, format="bgr24").reformat(
                    width=self.width, height=self.height, format="yuv420p")
                frame.pts = 0
                frame.time_base = probe.time_base
                list(probe.encode(frame))
                del probe
                self.ctx = self._make_ctx(name, fps, bitrate)   # fresh: emits SPS/PPS
                self.name = name
                break
            except Exception as exc:   # noqa: BLE001
                last_err = exc
                continue
        if self.ctx is None:
            raise RuntimeError(f"no working H.264 encoder available ({last_err})")
        get_logger().info("vcodec: H.264 encoder '%s' %dx%d @%dfps ~%dkbps",
                          self.name, self.width, self.height, fps, int(bitrate) // 1000)

    def _make_ctx(self, name, fps, bitrate):
        ctx = av.CodecContext.create(name, "w")
        ctx.width = self.width
        ctx.height = self.height
        ctx.pix_fmt = "yuv420p"
        ctx.framerate = Fraction(fps, 1)
        ctx.time_base = Fraction(1, fps)
        ctx.bit_rate = int(bitrate)
        ctx.options = _encoder_options(name, fps)
        ctx.open()
        return ctx

    def encode(self, bgr):
        """Encode one BGR frame -> list of H.264 packet bytes (usually one)."""
        frame = av.VideoFrame.from_ndarray(bgr, format="bgr24").reformat(
            width=self.width, height=self.height, format="yuv420p")
        if self._force_kf:
            self._force_kf = False
            try:
                frame.pict_type = av.video.frame.PictureType.I
            except Exception:
                pass
        frame.pts = self._pts
        frame.time_base = self.ctx.time_base
        self._pts += 1
        return [bytes(p) for p in self.ctx.encode(frame)]

    def request_keyframe(self):
        self._force_kf = True

    def set_bitrate(self, bitrate):
        try:
            self.ctx.bit_rate = int(bitrate)
        except Exception:
            pass


class H264Decoder:
    def __init__(self):
        self.ctx = av.CodecContext.create("h264", "r")

    def decode(self, data):
        """Feed one packet; return a list of BGR frames (0 or more)."""
        out = []
        try:
            for frame in self.ctx.decode(av.Packet(data)):
                out.append(frame.to_ndarray(format="bgr24"))
        except Exception:
            get_logger().debug("vcodec: decode error", exc_info=True)
        return out

    def close(self):
        self.ctx = None
