"""Real-time single-channel spectral denoiser (numpy only, no external deps).

Cleans the live meeting-audio mix on the Host before Opus encoding: it estimates
the stationary background-noise spectrum during non-speech gaps and subtracts it
every frame (spectral over-subtraction with a spectral floor), so steady hiss,
hum, fan and room noise are pulled down while speech passes through. During pure
noise the per-bin gain collapses toward the floor, which also mutes the gaps
between words -- so the effect is "let speech through, reject the rest".

Runs on the 48 kHz mono stream in 20 ms hops with 50% weighted overlap-add
(sqrt-Hann analysis+synthesis), so it reconstructs cleanly with no frame-edge
clicks. Cost is one hop (~20 ms) of added latency.
"""

import numpy as np

_EPS = 1e-9


class SpectralDenoiser:
    def __init__(self, hop=960, oversub=2.0, floor=0.08,
                 update_snr=1.5, noise_adapt=0.95, warmup=8):
        self.H = int(hop)
        self.N = self.H * 2
        n = np.arange(self.N)
        hann = 0.5 - 0.5 * np.cos(2.0 * np.pi * n / self.N)
        self.win = np.sqrt(hann).astype(np.float32)
        self._in = np.zeros(self.N, dtype=np.float32)
        self._ola = np.zeros(self.N, dtype=np.float32)
        self._noise = None
        self._gain_prev = None
        self._warm = 0
        self.oversub = float(oversub)
        self.floor = float(floor)
        self.update_snr = float(update_snr)
        self.noise_adapt = float(noise_adapt)
        self.warmup = int(warmup)

    def process(self, x):
        x = np.asarray(x, dtype=np.float32)
        if len(x) != self.H:
            fixed = np.zeros(self.H, dtype=np.float32)
            fixed[:min(len(x), self.H)] = x[:self.H]
            x = fixed

        self._in[:self.H] = self._in[self.H:]
        self._in[self.H:] = x

        spec = np.fft.rfft(self._in * self.win)
        mag = np.abs(spec)

        if self._noise is None:
            self._noise = mag.copy()

        snr = (mag.sum() + _EPS) / (self._noise.sum() + _EPS)
        if snr < self.update_snr or self._warm < self.warmup:
            a = self.noise_adapt
            self._noise = a * self._noise + (1.0 - a) * mag
            self._warm += 1

        sub = mag - self.oversub * self._noise
        gain = np.maximum(sub, self.floor * mag) / (mag + _EPS)
        gain = np.convolve(gain, np.ones(3, dtype=np.float32) / 3.0, mode="same")
        if self._gain_prev is not None:
            gain = 0.5 * gain + 0.5 * self._gain_prev
        self._gain_prev = gain

        seg_out = np.fft.irfft(spec * gain, n=self.N).astype(np.float32) * self.win
        self._ola += seg_out
        out = self._ola[:self.H].copy()
        self._ola[:self.H] = self._ola[self.H:]
        self._ola[self.H:] = 0.0
        return out
