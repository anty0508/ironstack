"""Screen capture for the remote-view feature.

Enumerates the host's monitors and grabs full-monitor frames as BGR numpy arrays
(the format the H.264 encoder wants).

Capture backend:
  - `dxcam` (DXGI Desktop Duplication) — GPU-accelerated, low CPU, only used when
    there is a SINGLE monitor (output index 0 is then unambiguous).
  - `mss` — simple and correct for multi-monitor; also the fallback if dxcam
    isn't available or fails.

Monitor geometry (x/y/size, needed to map remote input) always comes from mss's
enumeration, independent of the capture backend.
"""

import numpy as np

from logsetup import get_logger


def enumerate_monitors():
    """List the host's monitors: [{id, name, x, y, width, height, primary}].

    `id` is the mss monitor index (1-based); index 0 (the union of all screens)
    is intentionally excluded so a viewer picks a single physical display."""
    import mss
    out = []
    with mss.mss() as sct:
        for i, m in enumerate(sct.monitors[1:], start=1):
            out.append({
                "id": i,
                "name": f"Display {i}",
                "x": m["left"],
                "y": m["top"],
                "width": m["width"],
                "height": m["height"],
                "primary": m["left"] == 0 and m["top"] == 0,
            })
    if not out:
        out.append({"id": 1, "name": "Display 1", "x": 0, "y": 0,
                    "width": 1920, "height": 1080, "primary": True})
    return out


class ScreenCapturer:
    """Grabs full-monitor frames. Create and use from a single thread."""

    def __init__(self, monitor_id=1):
        self.monitor_id = monitor_id
        self.width = 0
        self.height = 0
        self._geom = (0, 0, 1920, 1080)
        self._backend = None
        self._dx = None
        self._sct = None
        self._mon = None
        self._last = None

    def start(self):
        mons = enumerate_monitors()
        g = next((m for m in mons if m["id"] == self.monitor_id), mons[0])
        self._geom = (g["x"], g["y"], g["width"], g["height"])

        # dxcam only for a single, unambiguous monitor; mss otherwise/fallback.
        if len(mons) == 1 and self._start_dxcam():
            self._backend = "dxcam"
        else:
            self._start_mss()
            self._backend = "mss"
        get_logger().info("screencap: monitor %d via %s (%dx%d at %d,%d)",
                          self.monitor_id, self._backend, self.width, self.height,
                          self._geom[0], self._geom[1])

    def _start_dxcam(self):
        try:
            import time
            import dxcam
            cam = dxcam.create(output_idx=max(0, self.monitor_id - 1),
                               output_color="BGR")
            if cam is None:
                return False
            frame = cam.grab()
            if frame is None:
                time.sleep(0.06)
                frame = cam.grab()
            if frame is None or frame.ndim != 3:
                try:
                    cam.release()
                except Exception:
                    pass
                return False
            self._dx = cam
            self.height, self.width = frame.shape[0], frame.shape[1]
            self._last = np.ascontiguousarray(frame)
            return True
        except Exception:
            get_logger().debug("screencap: dxcam unavailable, using mss", exc_info=True)
            return False

    def _start_mss(self):
        import mss
        self._sct = mss.mss()
        mons = self._sct.monitors
        idx = self.monitor_id if 1 <= self.monitor_id < len(mons) else 1
        self._mon = mons[idx]
        self.width = self._mon["width"]
        self.height = self._mon["height"]

    def grab(self):
        """Return the current frame as a contiguous BGR uint8 array (H, W, 3),
        or None on failure."""
        if self._backend == "dxcam":
            frame = self._dx.grab()          # None when nothing changed
            if frame is None:
                return self._last            # reuse last -> cheap P-frame
            self._last = np.ascontiguousarray(frame)
            return self._last
        try:
            shot = self._sct.grab(self._mon)   # BGRA buffer
        except Exception:
            get_logger().debug("screencap: grab failed", exc_info=True)
            return None
        return np.ascontiguousarray(np.asarray(shot)[:, :, :3])

    def geometry(self):
        """(x, y, width, height) of the captured monitor in the virtual desktop."""
        return self._geom

    def stop(self):
        if self._dx is not None:
            try:
                self._dx.release()
            except Exception:
                pass
            self._dx = None
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None
