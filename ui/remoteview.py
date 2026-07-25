"""Viewer-side remote-desktop window.

Shows the Host's screen (decoded H.264 frames) and forwards mouse/keyboard to the
Host. Owns a RemoteClient; frames arrive on the client's receive thread and are
handed to the GUI thread via a Qt signal. Input events captured over the view are
mapped to normalized (0..1) monitor coords and sent back.

A floating toolbar (RDP/VMware style) sits at the top and can be dragged left/
right. A "View only" toggle stops sending any input to the Host; double-tapping
Ctrl toggles the same thing without reaching for the toolbar.
"""

import time

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from logsetup import get_logger

Qt = QtCore.Qt

_BTN = {Qt.LeftButton: "left", Qt.RightButton: "right", Qt.MiddleButton: "middle"}

# Double-tap Ctrl toggles view-only <-> control. VKs: generic + left/right Ctrl.
_CTRL_VKS = {0x11, 0xA2, 0xA3}
_DOUBLE_TAP_SEC = 0.45   # two Ctrl presses within this window = a double-tap

# Host cursor names (from inputinject.detect_cursor) -> Qt cursor shapes, so the
# viewer's pointer matches the host's (I-beam over text, hand over links, etc.).
_CURSOR_SHAPES = {
    "arrow": Qt.ArrowCursor, "ibeam": Qt.IBeamCursor, "wait": Qt.WaitCursor,
    "appstarting": Qt.BusyCursor, "cross": Qt.CrossCursor, "hand": Qt.PointingHandCursor,
    "sizeall": Qt.SizeAllCursor, "sizenwse": Qt.SizeFDiagCursor,
    "sizenesw": Qt.SizeBDiagCursor, "sizewe": Qt.SizeHorCursor,
    "sizens": Qt.SizeVerCursor, "no": Qt.ForbiddenCursor, "help": Qt.WhatsThisCursor,
}

STYLE = """
* { font-family: 'Segoe UI', sans-serif; color: #e8eaf0; }
#panel { background: #0d0f14; }
#rvbar {
    background: rgba(18,20,26,0.92);
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 11px;
}
#rvgrip { color: #6f7689; font-size: 15px; padding: 0 2px; }
#rvstat { color: #8a90a2; font-size: 11px; }
#rvbar QPushButton {
    background: transparent; border: none; color: #cdd2de;
    font-size: 12px; padding: 4px 10px; border-radius: 7px;
}
#rvbar QPushButton:hover { background: rgba(255,255,255,0.10); color: #ffffff; }
#rvbar QPushButton:checked { background: rgba(242,163,60,0.28); color: #f6c98a; }
#rvcombo {
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12);
    border-radius: 7px; padding: 3px 8px; color: #cdd2de; font-size: 12px;
}
"""


class _ScreenView(QtWidgets.QWidget):
    """Paints the received frame (scaled, letterboxed) and emits input events with
    coordinates normalized to the displayed monitor."""

    input_event = QtCore.Signal(dict)

    def __init__(self):
        super().__init__()
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        # Show the normal pointer: it maps 1:1 to the host cursor, so it doubles
        # as the remote cursor (mss doesn't capture the host's own cursor).
        self._img = None
        self._rect = QtCore.QRect()
        self._status_text = "Connecting…"

    def set_frame(self, qimage):
        self._img = qimage
        self.update()

    def set_status_text(self, text):
        # The centered banner shown until (and between) frames — connection
        # progress, then "Loading host screen…", then it's covered by frames.
        self._status_text = text or ""
        if self._img is None:
            self.update()

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), Qt.black)
        if self._img is None:
            if self._status_text:
                p.setPen(QtGui.QColor("#c9cede"))
                f = p.font()
                f.setPointSize(15)
                p.setFont(f)
                p.drawText(self.rect(), Qt.AlignCenter, self._status_text)
            return
        area = self.rect()
        size = self._img.size().scaled(area.size(), Qt.KeepAspectRatio)
        x = area.x() + (area.width() - size.width()) // 2
        y = area.y() + (area.height() - size.height()) // 2
        self._rect = QtCore.QRect(x, y, size.width(), size.height())
        p.drawImage(self._rect, self._img)

    def _norm(self, pos):
        if self._rect.width() <= 0 or self._rect.height() <= 0:
            return None
        nx = (pos.x() - self._rect.x()) / self._rect.width()
        ny = (pos.y() - self._rect.y()) / self._rect.height()
        if 0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0:
            return nx, ny
        return None

    def mouseMoveEvent(self, e):
        n = self._norm(e.position().toPoint())
        if n:
            self.input_event.emit({"kind": "move", "x": n[0], "y": n[1]})

    def _button(self, e, down):
        n = self._norm(e.position().toPoint())
        b = _BTN.get(e.button())
        if n and b:
            self.input_event.emit({"kind": "button", "button": b, "down": down,
                                   "x": n[0], "y": n[1]})

    def mousePressEvent(self, e):
        self.setFocus()
        self._button(e, True)

    def mouseReleaseEvent(self, e):
        self._button(e, False)

    def wheelEvent(self, e):
        n = self._norm(e.position().toPoint())
        if n:
            self.input_event.emit({
                "kind": "scroll",
                "dy": e.angleDelta().y() // 120,
                "dx": e.angleDelta().x() // 120,
                "x": n[0], "y": n[1]})
    # Keyboard is handled by the global low-level hook (KeyboardHook), not Qt key
    # events — so Ctrl/Alt combos and the Win key are captured, not eaten by Qt.


class _FloatingBar(QtWidgets.QFrame):
    """A floating toolbar that overlays the top of the view and can be dragged
    horizontally (RDP/VMware style). Buttons consume their own clicks, so only the
    grip / empty areas start a drag."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("rvbar")
        self._press_global = None
        self._start_x = 0

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press_global = e.globalPosition().toPoint()
            self._start_x = self.x()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._press_global is not None:
            dx = e.globalPosition().toPoint().x() - self._press_global.x()
            new_x = self._start_x + dx
            maxx = max(0, self.parent().width() - self.width())
            self.move(max(0, min(maxx, new_x)), self.y())
            e.accept()

    def mouseReleaseEvent(self, e):
        self._press_global = None


class RemoteView(QtWidgets.QWidget):
    """Top-level remote-desktop window. `monitors` is the host's monitor list
    (from the control-channel offer); `monitor_id` is the initial selection."""

    _sig_frame = QtCore.Signal(QtGui.QImage)
    _sig_status = QtCore.Signal(str)
    _sig_conn = QtCore.Signal(str)
    _sig_cursor = QtCore.Signal(str)
    _sig_toggle_view = QtCore.Signal()
    closed = QtCore.Signal()

    def __init__(self, host, send_input, connect_screen=None, on_open_transcript=None,
                 on_toggle_mute=None):
        super().__init__()
        self.setWindowTitle("IronStack — Remote")
        # Use the IronStack icon (this is a separate top-level window, so give it
        # the app icon explicitly instead of the default Python/PyInstaller one).
        try:
            from ui.tray import make_app_icon
            self.setWindowIcon(make_app_icon())
        except Exception:
            pass
        self.resize(1100, 680)
        self._host = host
        # Screen params arrive later via start_screen() (after the host offers the
        # media port + token over the control channel).
        self._media_port = None
        self._token = None
        # In relay mode this dials a fresh screen socket through the relay; when
        # None the client connects directly to host:media_port.
        self._connect_screen = connect_screen
        self._monitors = [{"id": 1, "name": "Display 1"}]
        self._monitor_id = 1
        # Input goes over the reliable TCP control channel (supplied by caller);
        # the screen rides its own TCP socket.
        self._send_input_cb = send_input
        self._on_open_transcript = on_open_transcript
        self._on_toggle_mute = on_toggle_mute
        self._client = None
        self._quality = "high"
        self._view_only = False
        self._bar_centered = False
        # double-tap-Ctrl tracking
        self._last_ctrl_time = 0.0
        self._ctrl_is_down = False

        self._build_ui()
        self.setStyleSheet(STYLE)
        self._sig_frame.connect(self._view.set_frame)
        self._sig_status.connect(self._on_status)
        self._sig_conn.connect(self._view.set_status_text)
        self._sig_cursor.connect(self._on_cursor)
        # Toggle is emitted from the keyboard-hook path; hop to the GUI thread.
        self._sig_toggle_view.connect(self._viewonly_btn.toggle)
        self._view.input_event.connect(self._on_input)

        # Global keyboard capture (all keys/combos) while this window is focused.
        from services.keyhook import KeyboardHook
        self._keyhook = KeyboardHook(on_key=self._on_hook_key,
                                     should_capture=self._should_capture_keys)

    def set_connection_status(self, text):
        """Thread-safe: update the big centered banner (control-connection
        progress, shown before any screen frames)."""
        self._sig_conn.emit(text or "")

    def start_screen(self, media_port, token, monitors, monitor_id):
        """Begin (or restart) the screen stream once the host has offered it."""
        self._media_port = media_port
        self._token = token
        self._monitors = monitors or [{"id": 1, "name": "Display 1"}]
        self._monitor_id = monitor_id
        self._populate_monitor_combo()
        self._sig_conn.emit("Loading host screen…")
        self._connect(monitor_id)

    def _should_capture_keys(self):
        # Observe keys whenever the window is focused AND a screen stream exists
        # (both modes) so the double-tap-Ctrl toggle works even in view-only; the
        # per-key decision of send-to-host vs pass-through happens in _on_hook_key.
        # Before the screen connects there's nothing to control, so don't swallow
        # the user's keystrokes while they wait on the connecting banner.
        return self._client is not None and self.isActiveWindow()

    def _on_hook_key(self, vk, down, ext):
        """Return True to swallow the key locally, False to let it pass through."""
        if self._detect_double_ctrl(vk, down):
            return True   # the toggling Ctrl is consumed, not forwarded
        if self._view_only:
            return False  # watching only: let the local machine keep the key
        if self._send_input_cb is not None:
            self._send_input_cb({"kind": "key", "vk": vk, "down": down, "ext": ext})
        return True

    def _detect_double_ctrl(self, vk, down):
        """True when this event completes a quick double-tap of Ctrl. A press of
        any other key resets the sequence, and auto-repeat is ignored."""
        if vk not in _CTRL_VKS:
            if down:
                self._last_ctrl_time = 0.0
                self._ctrl_is_down = False
            return False
        if not down:
            self._ctrl_is_down = False
            return False
        if self._ctrl_is_down:
            return False   # held-key auto-repeat, not a fresh tap
        self._ctrl_is_down = True
        now = time.monotonic()
        if self._last_ctrl_time and (now - self._last_ctrl_time) <= _DOUBLE_TAP_SEC:
            self._last_ctrl_time = 0.0
            self._sig_toggle_view.emit()   # flips View-only on the GUI thread
            return True
        self._last_ctrl_time = now
        return False

    def _connect(self, monitor_id):
        if self._token is None:
            return   # the host hasn't offered the screen yet (start_screen not called)
        from services.remote import RemoteScreenClient
        if self._client is not None:
            self._client.stop()
        self._monitor_id = monitor_id
        self._client = RemoteScreenClient(
            self._host, self._media_port, self._token, monitor_id, self._quality,
            on_frame=self._on_frame, on_status=lambda s: self._sig_status.emit(s),
            on_cursor=lambda c: self._sig_cursor.emit(c),
            connect=self._connect_screen)
        self._client.start()

    @QtCore.Slot(str)
    def _on_cursor(self, name):
        self._view.setCursor(_CURSOR_SHAPES.get(name, Qt.ArrowCursor))

    def _populate_monitor_combo(self):
        self._mon_combo.blockSignals(True)
        self._mon_combo.clear()
        for m in self._monitors:
            self._mon_combo.addItem(
                f"{m.get('name', 'Display')} "
                f"({m.get('width', '?')}x{m.get('height', '?')})", m["id"])
        idx = self._mon_combo.findData(self._monitor_id)
        self._mon_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._mon_combo.blockSignals(False)
        self._mon_combo.setVisible(len(self._monitors) > 1)
        self._reposition_bar()

    def _open_transcript(self):
        if self._on_open_transcript is not None:
            self._on_open_transcript()

    def _toggle_mute(self, muted):
        self._mute_btn.setText("🔇" if muted else "🔊")
        self._mute_btn.setToolTip(
            "Unmute the host's audio" if muted else "Mute the host's audio")
        if self._on_toggle_mute is not None:
            self._on_toggle_mute(muted)

    # ---- layout ----
    def _build_ui(self):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        panel = QtWidgets.QFrame()
        panel.setObjectName("panel")
        outer.addWidget(panel)
        root = QtWidgets.QVBoxLayout(panel)
        root.setContentsMargins(0, 0, 0, 0)

        # The screen view fills the whole window; the toolbar floats over it.
        self._view = _ScreenView()
        root.addWidget(self._view, 1)

        self._bar = _FloatingBar(panel)
        h = QtWidgets.QHBoxLayout(self._bar)
        h.setContentsMargins(8, 4, 8, 4)
        h.setSpacing(4)

        grip = QtWidgets.QLabel("⠿")
        grip.setObjectName("rvgrip")
        grip.setCursor(Qt.SizeAllCursor)
        h.addWidget(grip)

        self._status_lbl = QtWidgets.QLabel("connecting…")
        self._status_lbl.setObjectName("rvstat")
        h.addWidget(self._status_lbl)

        self._transcript_btn = QtWidgets.QPushButton("Transcript")
        self._transcript_btn.setCursor(Qt.PointingHandCursor)
        self._transcript_btn.setToolTip("Open the interview transcript window")
        self._transcript_btn.clicked.connect(self._open_transcript)
        h.addWidget(self._transcript_btn)

        self._mute_btn = QtWidgets.QPushButton("🔊")
        self._mute_btn.setCheckable(True)
        self._mute_btn.setCursor(Qt.PointingHandCursor)
        self._mute_btn.setToolTip("Mute the host's audio")
        self._mute_btn.toggled.connect(self._toggle_mute)
        h.addWidget(self._mute_btn)

        self._viewonly_btn = QtWidgets.QPushButton("View only")
        self._viewonly_btn.setCheckable(True)
        self._viewonly_btn.setCursor(Qt.PointingHandCursor)
        self._viewonly_btn.setToolTip(
            "View only — don't send mouse/keyboard to the host\n"
            "Shortcut: double-tap Ctrl to toggle control on/off")
        self._viewonly_btn.toggled.connect(self._on_view_only)
        h.addWidget(self._viewonly_btn)

        self._quality_combo = QtWidgets.QComboBox()
        self._quality_combo.setObjectName("rvcombo")
        self._quality_combo.setToolTip("Image quality (higher = sharper, more bandwidth)")
        for label, key in (("High", "high"), ("Medium", "medium"), ("Low", "low")):
            self._quality_combo.addItem(label, key)
        self._quality_combo.currentIndexChanged.connect(self._on_quality_changed)
        h.addWidget(self._quality_combo)

        # Monitor picker: always present, populated/shown once the host offers its
        # monitor list (start_screen). Hidden while there's a single display.
        self._mon_combo = QtWidgets.QComboBox()
        self._mon_combo.setObjectName("rvcombo")
        self._mon_combo.currentIndexChanged.connect(self._on_monitor_changed)
        self._mon_combo.hide()
        h.addWidget(self._mon_combo)

        fs = QtWidgets.QPushButton("⛶")
        fs.setCursor(Qt.PointingHandCursor)
        fs.setToolTip("Fullscreen (Esc to exit)")
        fs.clicked.connect(self._toggle_fullscreen)
        h.addWidget(fs)

        close_btn = QtWidgets.QPushButton("✕")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setToolTip("Close remote view")
        close_btn.clicked.connect(self.close)
        h.addWidget(close_btn)

        self._bar.adjustSize()
        self._bar.raise_()

    def _reposition_bar(self):
        self._bar.adjustSize()
        bw = self._bar.width()
        if not self._bar_centered:
            self._bar.move(max(0, (self.width() - bw) // 2), 8)
            self._bar_centered = True
        else:
            maxx = max(0, self.width() - bw)
            self._bar.move(max(0, min(maxx, self._bar.x())), 8)
        self._bar.raise_()

    def showEvent(self, e):
        super().showEvent(e)
        self._reposition_bar()
        self._keyhook.install()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._reposition_bar()

    def _on_frame(self, bgr):
        bgr = np.ascontiguousarray(bgr)
        h, w = bgr.shape[:2]
        img = QtGui.QImage(bgr.data, w, h, 3 * w, QtGui.QImage.Format_BGR888).copy()
        self._sig_frame.emit(img)

    @QtCore.Slot(str)
    def _on_status(self, text):
        mode = " · view only" if self._view_only else ""
        self._status_lbl.setText(text + mode)
        self._reposition_bar()

    def _on_input(self, ev):
        # In view-only mode, never touch the host.
        if self._view_only:
            return
        self._send_input_cb(ev)

    def _on_view_only(self, on):
        self._view_only = on
        self._on_status(self._status_lbl.text().split(" · ")[0])

    def _on_monitor_changed(self, _idx):
        mid = self._mon_combo.currentData()
        if mid is not None and mid != self._monitor_id:
            self._connect(mid)   # reconnect the screen socket for the new monitor

    def _on_quality_changed(self, _idx):
        q = self._quality_combo.currentData()
        if q and q != self._quality:
            self._quality = q
            self._connect(self._monitor_id)   # reconnect to apply the new quality

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape and self.isFullScreen():
            self._toggle_fullscreen()
        else:
            super().keyPressEvent(e)

    def closeEvent(self, e):
        try:
            self._keyhook.uninstall()
        except Exception:
            pass
        try:
            if self._client is not None:
                self._client.stop()
        except Exception:
            get_logger().debug("remoteview: client stop failed", exc_info=True)
        self.closed.emit()
        super().closeEvent(e)
