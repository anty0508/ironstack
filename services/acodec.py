"""Opus audio codec (PyAV / libopus) for the live meeting-audio stream.

Mono, voice-tuned, so the stream is ~24-40 kbps instead of ~384 kbps raw PCM — a
~10x cut, so audio stops eating into the screen stream's bandwidth on a slow link.
The encoder buffers input through an AudioFifo and emits fixed 20 ms Opus frames;
a fresh encoder/decoder pair per connection keeps the two ends in sync.
"""

from fractions import Fraction

import numpy as np
import av
from av.audio.frame import AudioFrame
from av.audio.fifo import AudioFifo


class OpusEncoder:
    def __init__(self, rate, bitrate=24000):
        self.rate = int(rate)
        self.ctx = av.CodecContext.create("libopus", "w")
        self.ctx.sample_rate = self.rate
        self.ctx.format = "s16"
        self.ctx.layout = "mono"
        self.ctx.bit_rate = int(bitrate)
        self.fifo = AudioFifo()
        self._pts = 0
        self._fs = self.rate * 20 // 1000   # 20 ms frame

    def encode(self, pcm_s16_mono):
        """pcm_s16_mono: 1-D int16 ndarray -> list[bytes] of Opus packets."""
        frame = AudioFrame.from_ndarray(
            np.ascontiguousarray(pcm_s16_mono.reshape(1, -1)),
            format="s16", layout="mono")
        frame.sample_rate = self.rate
        frame.time_base = Fraction(1, self.rate)
        frame.pts = self._pts
        self._pts += int(pcm_s16_mono.shape[0])
        self.fifo.write(frame)

        out = []
        fs = self.ctx.frame_size or self._fs
        while True:
            chunk = self.fifo.read(fs)
            if chunk is None:
                break
            for pkt in self.ctx.encode(chunk):
                out.append(bytes(pkt))
        return out


class OpusDecoder:
    def __init__(self, rate):
        self.rate = int(rate)
        self.ctx = av.CodecContext.create("libopus", "r")
        self.ctx.sample_rate = self.rate
        self.ctx.format = "s16"
        self.ctx.layout = "mono"

    def decode(self, data):
        """Opus packet bytes -> list of 1-D float32 ndarrays in [-1, 1]."""
        out = []
        for frame in self.ctx.decode(av.Packet(data)):
            arr = frame.to_ndarray()
            if arr.dtype == np.int16:
                samples = arr.astype(np.float32) / 32768.0
            else:
                samples = arr.astype(np.float32)
            out.append(samples.reshape(-1))
        return out
