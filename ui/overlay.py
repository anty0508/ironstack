"""Floating overlay UI (PySide6).

A frameless, always-on-top, translucent panel that shows the live transcription
caption and a chat-style stream of interviewer questions + generated answers.

It exposes a small interface used by the answerer / transcriber:
    info(text), partial(text), show_question(text), begin_answer(),
    answer_delta(delta), end_answer(note), close()

All public methods are thread-safe: they emit Qt signals, so they can be called
from the transcription / answer worker threads and the actual widget updates run
on the GUI thread.
"""

import re
import html
import threading
import ctypes
import sys
from ctypes import wintypes

from PySide6 import QtCore, QtWidgets

from config import LANGUAGES, DEFAULT_LANGUAGE_CODE
from logsetup import get_logger

Qt = QtCore.Qt

# Win32 hotkey modifiers / messages
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

# Window display-affinity modes (SetWindowDisplayAffinity)
WDA_NONE = 0x00
WDA_MONITOR = 0x01              # content shows as black in captures (Windows 7+)
WDA_EXCLUDEFROMCAPTURE = 0x11   # window invisible to captures (Win10 2004 / build 19041+)

# Virtual key codes
VK_UP = 0x26
VK_DOWN = 0x28
VK_H = 0x48
VK_C = 0x43
VK_P = 0x50
VK_S = 0x53

# Hotkey ids
HK_TOGGLE = 1
HK_OPACITY_UP = 2
HK_OPACITY_DOWN = 3
HK_CLICKTHROUGH = 4
HK_TOGGLE_CAPTURE = 5


# A user32 handle that preserves the Win32 last-error, so failures can be
# diagnosed reliably (ctypes.windll.* does NOT preserve it -> stale codes like 8).
try:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.SetWindowDisplayAffinity.restype = ctypes.c_bool
    _user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
except Exception:
    _user32 = None


def _windows_build():
    try:
        v = sys.getwindowsversion()
        return f"{v.major}.{v.minor}.{v.build}"
    except Exception:
        return "unknown"


def _is_remote_session():
    """True if running in a Remote Desktop session. Display-affinity relies on
    local GPU/DWM composition, which RDP sessions (and some VMs) don't provide,
    so capture-exclusion fails there regardless of Windows version."""
    try:
        return bool(ctypes.windll.user32.GetSystemMetrics(0x1000))  # SM_REMOTESESSION
    except Exception:
        return False


def apply_exclude_from_capture(widget, enabled=True):
    """Hide a window from Windows screen capture / screen sharing.

    Prefers WDA_EXCLUDEFROMCAPTURE (the window is fully invisible to capture),
    which needs Windows 10 v2004 (build 19041) or newer. On older builds that
    flag is rejected, so we fall back to WDA_MONITOR (the window shows up as a
    black box in captures, supported back to Windows 7). Returns True if any
    affinity mode was applied.

    The whole feature relies on Desktop Window Manager (GPU) composition; in
    Remote Desktop sessions / some VMs both modes fail regardless of build."""
    if not sys.platform.startswith("win") or _user32 is None:
        return False

    log = get_logger()
    try:
        hwnd = int(widget.winId())
    except Exception:
        log.debug("stealth: no window handle yet", exc_info=True)
        return False

    def _apply(mode, name):
        # returns (True, 0) on success, (False, err) on failure, (None, 0) on raise
        try:
            ctypes.set_last_error(0)
            ok = bool(_user32.SetWindowDisplayAffinity(hwnd, mode))
            return (True, 0) if ok else (False, ctypes.get_last_error())
        except Exception:
            log.exception("stealth: SetWindowDisplayAffinity(%s) raised", name)
            return None, 0

    if not enabled:
        ok, err = _apply(WDA_NONE, "WDA_NONE")
        if ok is False:
            log.warning("stealth: clearing capture exclusion failed (err=%d, build %s)",
                        err, _windows_build())
        return bool(ok)

    # Enabling: try full invisibility, then the black-box fallback.
    ok, excl_err = _apply(WDA_EXCLUDEFROMCAPTURE, "WDA_EXCLUDEFROMCAPTURE")
    if ok:
        log.debug("stealth: applied WDA_EXCLUDEFROMCAPTURE (invisible to capture)")
        return True
    if ok is None:
        return False

    ok, mon_err = _apply(WDA_MONITOR, "WDA_MONITOR")
    if ok:
        log.warning("stealth: WDA_EXCLUDEFROMCAPTURE rejected (err=%d) on Windows build "
                    "%s; fell back to WDA_MONITOR (window shows as BLACK in captures, "
                    "not invisible).", excl_err, _windows_build())
        return True
    if ok is None:
        return False

    log.warning("stealth: could NOT hide window from capture "
                "(build=%s, EXCLUDEFROMCAPTURE err=%d, MONITOR err=%d, remote_session=%s). "
                "Both modes need GPU/DWM composition; this usually means a Remote Desktop "
                "session or a VM without it. Screen-sharing will show this window.",
                _windows_build(), excl_err, mon_err, _is_remote_session())
    return False


def set_window_topmost(widget, topmost=True):
    """Set or clear the native HWND top-most flag without recreating the window.
    Uses Win32 SetWindowPos to avoid Qt re-creating the native window (which
    causes a brief hide/show "splash")."""
    if not sys.platform.startswith("win"):
        return False
    try:
        hwnd = int(widget.winId())
    except Exception:
        get_logger().debug("pin: no window handle yet", exc_info=True)
        return False
    try:
        user32 = ctypes.windll.user32
        # Define constants
        HWND_TOPMOST = -1
        HWND_NOTOPMOST = -2
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002
        SWP_NOACTIVATE = 0x0010
        flags = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE

        # Set arg/return types for better compatibility
        try:
            user32.SetWindowPos.restype = ctypes.c_bool
            user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.INT, wintypes.INT, wintypes.INT, wintypes.INT, wintypes.UINT]
        except Exception:
            # best-effort; some Python/ctypes versions may not permit resetting
            pass

        # hWndInsertAfter can be a special constant (negative); cast to c_void_p
        h_insert = ctypes.c_void_p(HWND_TOPMOST if topmost else HWND_NOTOPMOST)
        return bool(user32.SetWindowPos(wintypes.HWND(hwnd), h_insert, 0, 0, 0, 0, flags))
    except Exception:
        get_logger().exception("pin: SetWindowPos failed (topmost=%s)", topmost)
        return False


class WindowVisibilityController:
    """Shared controller for app-wide opacity and capture-exclusion state."""

    def __init__(self):
        self.opacity_percent = 95
        self.capture_excluded = False
        self.pinned = True
        self._widgets = []
        self._active = None       # window Ctrl+Alt+H hides/shows (latest registered)
        self._hotkeys = None
        self._start_hotkeys()

    def register(self, widget):
        if widget is None:
            return
        if widget not in self._widgets:
            self._widgets.append(widget)
        # The most recently registered top-level window is the active one (the
        # setup window during setup, the overlay during a meeting), so the
        # global show/hide hotkey acts on whatever is currently on screen.
        self._active = widget
        self._apply_to_widget(widget)

    def unregister(self, widget):
        try:
            self._widgets.remove(widget)
        except ValueError:
            pass
        if self._active is widget:
            self._active = self._widgets[-1] if self._widgets else None

    def toggle_active_visibility(self):
        """Hide/show the active window (Ctrl+Alt+H). Works for both the setup
        window and the interview overlay."""
        w = self._active
        if w is None:
            return
        try:
            if w.isVisible():
                w.hide()
            else:
                w.show()
                w.raise_()
                w.activateWindow()
        except RuntimeError:
            self._active = None   # the window was already destroyed

    def set_opacity_percent(self, value):
        self.opacity_percent = max(30, min(100, int(value)))
        for widget in list(self._widgets):
            try:
                widget.setWindowOpacity(self.opacity_percent / 100.0)
            except Exception:
                pass

    def set_capture_excluded(self, enabled):
        self.capture_excluded = bool(enabled)
        get_logger().info("stealth (screen-capture exclusion) -> %s", self.capture_excluded)
        for widget in list(self._widgets):
            try:
                apply_exclude_from_capture(widget, enabled=self.capture_excluded)
            except Exception:
                pass
            try:
                if hasattr(widget, "_apply_capture_exclusion"):
                    widget._apply_capture_exclusion()
            except Exception:
                pass
            try:
                if hasattr(widget, "_refresh_stealth_button"):
                    widget._refresh_stealth_button()
            except Exception:
                pass

    def set_pinned(self, enabled):
        self.pinned = bool(enabled)
        get_logger().info("pin (always-on-top) -> %s", self.pinned)
        for widget in list(self._widgets):
            try:
                if hasattr(widget, "_apply_pin_state"):
                    widget._apply_pin_state()
            except Exception:
                get_logger().exception("pin: applying state to a window failed")

    def _start_hotkeys(self):
        if self._hotkeys is not None:
            return
        self._hotkeys = GlobalHotkeys([
            (HK_TOGGLE_CAPTURE, MOD_CONTROL | MOD_ALT, VK_S),  # toggle stealth
            (HK_TOGGLE, MOD_CONTROL | MOD_ALT, VK_H),   # show/hide active window
        ])
        self._hotkeys.triggered.connect(self._handle_hotkey)
        self._hotkeys.start()

    def _handle_hotkey(self, hid):
        if hid == HK_TOGGLE_CAPTURE:
            self.set_capture_excluded(not self.capture_excluded)
        elif hid == HK_TOGGLE:
            self.toggle_active_visibility()

    def _apply_to_widget(self, widget):
        try:
            widget.setWindowOpacity(self.opacity_percent / 100.0)
        except Exception:
            pass
        try:
            apply_exclude_from_capture(widget, enabled=self.capture_excluded)
        except Exception:
            pass
        try:
            if hasattr(widget, "_apply_capture_exclusion"):
                widget._apply_capture_exclusion()
        except Exception:
            pass
        try:
            if hasattr(widget, "_apply_pin_state"):
                widget._apply_pin_state()
        except Exception:
            pass


_VISIBILITY_CONTROLLER = None


def get_window_visibility_controller():
    global _VISIBILITY_CONTROLLER
    if _VISIBILITY_CONTROLLER is None:
        _VISIBILITY_CONTROLLER = WindowVisibilityController()
    return _VISIBILITY_CONTROLLER


class GlobalHotkeys(QtCore.QObject):
    """Register system-wide hotkeys via Win32 and emit `triggered(id)` on the
    GUI thread. Runs its own message loop in a daemon thread."""

    triggered = QtCore.Signal(int)

    def __init__(self, bindings):
        super().__init__()
        self._bindings = bindings          # list of (id, modifiers, vk)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread_id = None

    def start(self):
        self._thread.start()

    def stop(self):
        """Ask the listener thread to exit its message loop. Windows ties hotkey
        registrations to the registering thread, so once this thread ends the
        hotkeys are released and the next overlay can register them again."""
        tid = self._thread_id
        if tid:
            try:
                ctypes.windll.user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
            except Exception:
                pass

    def _run(self):
        try:
            self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
            user32 = ctypes.windll.user32
            for hid, mods, vk in self._bindings:
                user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk)

            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == WM_HOTKEY:
                    self.triggered.emit(int(msg.wParam))
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            # Hotkeys are best-effort; the header buttons still work without them.
            get_logger().exception("global hotkeys failed (best-effort, app still works)")


class _Header(QtWidgets.QFrame):
    """Top bar that drags the (frameless) window when clicked."""

    def __init__(self, window):
        super().__init__()
        self.setObjectName("header")
        self._window = window
        self._drag = None

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self._window.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag is not None and (e.buttons() & Qt.LeftButton):
            self._window.move(e.globalPosition().toPoint() - self._drag)
            e.accept()

    def mouseReleaseEvent(self, e):
        self._drag = None


STYLE = """
* { font-family: 'Segoe UI', sans-serif; color: #e8eaf0; }
#panel {
    background: #12141a;
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 0;
}
#header { border-bottom: 1px solid rgba(255,255,255,0.06); }
#appDot { color: #f2a33c; font-size: 14px; }
#title { font-size: 13px; font-weight: 600; color: #f2f3f7; }
#subtitle { font-size: 11px; color: #8a90a2; }
#winbtn {
    background: transparent; border: none; color: #9aa0b2;
    font-size: 15px; font-weight: 600; padding: 0 6px;
}
#winbtn:hover { color: #ffffff; }
#winbtn:pressed { background: transparent; }
#winbtn:focus { outline: none; }
#langbox {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.10);
    border-radius: 8px; padding: 3px 8px; color: #cdd2de; font-size: 11px;
}
#langbox:hover { border: 1px solid rgba(242,163,60,0.45); color: #ffffff; }
#langbox::drop-down { border: none; width: 18px; }
#langbox QAbstractItemView {
    background: #1c2029; border: 1px solid rgba(255,255,255,0.10);
    selection-background-color: rgba(242,163,60,0.30); outline: none;
}
QPushButton:pressed { background: transparent; }
#endbtn {
    background: rgba(242, 163, 60, 0.16);
    border: 1px solid rgba(242, 163, 60, 0.45);
    border-radius: 8px; color: #f6c98a;
    font-size: 11px; font-weight: 700; padding: 4px 12px;
}
#endbtn:hover { background: rgba(242, 163, 60, 0.30); color: #ffffff; }
#captionTag { font-size: 10px; font-weight: 700; color: #f2a33c; letter-spacing: 1px; }
#captionBox {
    background: rgba(242, 163, 60, 0.10);
    border: 1px solid rgba(242, 163, 60, 0.35);
    border-radius: 10px;
}
#captionText { font-size: 14px; color: #f4d9b0; }
QScrollArea { border: none; background: transparent; }
#chat { background: transparent; }
#qbubble {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 10px;
}
#abubble {
    background: rgba(45, 52, 66, 0.92);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 10px;
}
#qtag { font-size: 10px; font-weight: 700; color: #7f8aa3; letter-spacing: 1px; }
#aitag { font-size: 10px; font-weight: 700; color: #f2a33c; letter-spacing: 1px; }
#bubbletext { font-size: 14px; color: #e8eaf0; }
#note { font-size: 11px; color: #6f7689; }
#status { font-size: 11px; color: #6f7689; }
QScrollBar:vertical {
    background: rgba(255,255,255,0.06);
    width: 12px; margin: 2px; border-radius: 6px;
}
QScrollBar::handle:vertical {
    background: rgba(242,163,60,0.55);
    border-radius: 5px; min-height: 32px;
}
QScrollBar::handle:vertical:hover { background: rgba(242,163,60,0.85); }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QSlider::groove:horizontal { height: 4px; background: rgba(255,255,255,0.12); border-radius: 2px; }
QSlider::handle:horizontal {
    width: 12px; height: 12px; margin: -5px 0;
    background: #f2a33c; border-radius: 6px;
}
"""


class Overlay(QtWidgets.QWidget):

    # cross-thread signals -> GUI-thread slots
    _sig_info = QtCore.Signal(str)
    _sig_partial = QtCore.Signal(str)
    _sig_question = QtCore.Signal(str)    # interviewer line (always shown)
    _sig_begin_answer = QtCore.Signal()   # an answer is about to stream
    _sig_delta = QtCore.Signal(str)
    _sig_end = QtCore.Signal(str)
    _sig_close = QtCore.Signal()

    end_meeting = QtCore.Signal()   # "End Meeting" clicked -> return to setup
    quit_app = QtCore.Signal()      # quit clicked -> exit the whole app
    language_changed = QtCore.Signal(str, str)   # (deepgram code, display name)

    def __init__(self, start_geometry=None, language_code=DEFAULT_LANGUAGE_CODE):
        super().__init__()
        self._language_code = language_code or DEFAULT_LANGUAGE_CODE
        self.setWindowTitle("IronStack")
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool                      # keep it out of the taskbar
        )
        # NOTE: deliberately NOT WA_TranslucentBackground. A frameless window
        # with per-pixel alpha becomes a layered window that some GPUs composite
        # on a path WDA_EXCLUDEFROMCAPTURE doesn't cover, so stealth silently
        # fails. We keep the window opaque (see-through comes from
        # setWindowOpacity) with square corners.
        self.setMinimumSize(440, 320)
        self.resize(580, 640)

        self._current_body = None
        self._answer_text = ""
        self._click_through = False
        self._bubbles = []
        self._caption_live = False     # True while real transcription is showing
        self._dots = 0
        self._visibility_controller = get_window_visibility_controller()
        self._pinned = self._visibility_controller.pinned
        self._capture_excluded = self._visibility_controller.capture_excluded

        self._build_ui()
        self.setStyleSheet(STYLE)
        self._visibility_controller.register(self)
        self._visibility_controller.set_opacity_percent(self._visibility_controller.opacity_percent)
        self._place(start_geometry)

        # animate the "listening" dots when no live transcription is showing
        self._anim = QtCore.QTimer(self)
        self._anim.setInterval(400)
        self._anim.timeout.connect(self._tick_caption)
        self._anim.start()

        # wire signals to slots (queued -> always runs on GUI thread)
        self._sig_info.connect(self._on_info)
        self._sig_partial.connect(self._on_partial)
        self._sig_question.connect(self._on_question)
        self._sig_begin_answer.connect(self._on_begin_answer)
        self._sig_delta.connect(self._on_delta)
        self._sig_end.connect(self._on_end)
        self._sig_close.connect(self.close)

        self._init_hotkeys()
        QtCore.QTimer.singleShot(0, lambda: self._apply_capture_exclusion())

    # ---------- layout ----------

    def _build_ui(self):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        panel = QtWidgets.QFrame()
        panel.setObjectName("panel")
        outer.addWidget(panel)

        root = QtWidgets.QVBoxLayout(panel)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- header ---
        header = _Header(self)
        header.setFixedHeight(44)
        h = QtWidgets.QHBoxLayout(header)
        h.setContentsMargins(12, 0, 6, 0)
        h.setSpacing(6)

        # Compact brand: amber dot + name, with a width-capped, elided status
        # line below (see _on_info) so it never overflows into the controls.
        dot = QtWidgets.QLabel("●")
        dot.setObjectName("appDot")
        h.addWidget(dot)
        titles = QtWidgets.QVBoxLayout()
        titles.setSpacing(0)
        title = QtWidgets.QLabel("IronStack")
        title.setObjectName("title")
        self.subtitle = QtWidgets.QLabel("starting…")
        self.subtitle.setObjectName("subtitle")
        self.subtitle.setFixedWidth(132)
        titles.addWidget(title)
        titles.addWidget(self.subtitle)
        h.addLayout(titles)
        h.addStretch(1)

        self.language_combo = QtWidgets.QComboBox()
        self.language_combo.setObjectName("langbox")
        self.language_combo.setFixedWidth(98)
        self.language_combo.setFocusPolicy(Qt.NoFocus)
        self.language_combo.setCursor(Qt.PointingHandCursor)
        self.language_combo.setToolTip("Language (transcription + answers) — change anytime")
        for label, code in LANGUAGES:
            self.language_combo.addItem(label, code)
        idx = self.language_combo.findData(self._language_code)
        self.language_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.language_combo.currentIndexChanged.connect(self._on_language_changed)
        h.addWidget(self.language_combo)

        self.stealth_toggle = QtWidgets.QPushButton("Stealth")
        self.stealth_toggle.setObjectName("winbtn")
        self.stealth_toggle.setFixedWidth(64)
        self.stealth_toggle.setCursor(Qt.PointingHandCursor)
        self.stealth_toggle.setFlat(True)
        self.stealth_toggle.setFocusPolicy(Qt.NoFocus)
        self.stealth_toggle.setToolTip("Stealth mode: hidden from remote viewers")
        self.stealth_toggle.clicked.connect(self._toggle_stealth_mode)
        self._refresh_stealth_button()
        h.addWidget(self.stealth_toggle)

        self.opacity_slider = QtWidgets.QSlider(Qt.Horizontal)
        self.opacity_slider.setFixedWidth(64)
        self.opacity_slider.setRange(30, 100)
        self.opacity_slider.setValue(self._visibility_controller.opacity_percent)
        self.opacity_slider.setToolTip("Opacity")
        self.opacity_slider.valueChanged.connect(
            lambda v: self._visibility_controller.set_opacity_percent(v)
        )
        h.addWidget(self.opacity_slider)

        end_btn = QtWidgets.QPushButton("End")
        end_btn.setObjectName("endbtn")
        end_btn.setCursor(Qt.PointingHandCursor)
        end_btn.setToolTip("End this meeting and return to the setup window")
        end_btn.clicked.connect(self.end_meeting.emit)
        h.addWidget(end_btn)

        self.pin_toggle = QtWidgets.QPushButton("📌")
        self.pin_toggle.setObjectName("winbtn")
        self.pin_toggle.setCursor(Qt.PointingHandCursor)
        self.pin_toggle.setFlat(True)
        self.pin_toggle.setCheckable(True)
        self.pin_toggle.setFocusPolicy(Qt.NoFocus)
        # remove splash/press background via per-widget stylesheet
        self.pin_toggle.setStyleSheet("background: transparent; border: none;")
        self.pin_toggle.clicked.connect(self._toggle_pin)
        h.addWidget(self.pin_toggle)
        self._refresh_pin_button()

        hide_btn = QtWidgets.QPushButton("—")
        hide_btn.setObjectName("winbtn")
        hide_btn.setCursor(Qt.PointingHandCursor)
        hide_btn.setToolTip("Hide (Ctrl+Alt+H to show again)")
        hide_btn.clicked.connect(self.hide)
        h.addWidget(hide_btn)

        close_btn = QtWidgets.QPushButton("×")
        close_btn.setObjectName("winbtn")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setToolTip("Hide")
        close_btn.clicked.connect(self._quit)
        h.addWidget(close_btn)

        root.addWidget(header)

        body = QtWidgets.QVBoxLayout()
        body.setContentsMargins(12, 12, 12, 10)
        body.setSpacing(10)
        root.addLayout(body)

        # --- live caption box ---
        cap = QtWidgets.QFrame()
        cap.setObjectName("captionBox")
        cv = QtWidgets.QVBoxLayout(cap)
        cv.setContentsMargins(12, 8, 12, 10)
        cv.setSpacing(3)
        cap_tag = QtWidgets.QLabel("● LISTENING")
        cap_tag.setObjectName("captionTag")
        self.caption_label = QtWidgets.QLabel("…")
        self.caption_label.setObjectName("captionText")
        self.caption_label.setWordWrap(True)
        cv.addWidget(cap_tag)
        cv.addWidget(self.caption_label)
        body.addWidget(cap)

        # --- chat ---
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        chat = QtWidgets.QWidget()
        chat.setObjectName("chat")
        self.chat_layout = QtWidgets.QVBoxLayout(chat)
        self.chat_layout.setContentsMargins(0, 0, 0, 0)
        self.chat_layout.setSpacing(8)
        self.chat_layout.addStretch(1)
        self.scroll.setWidget(chat)
        body.addWidget(self.scroll, 1)

        # --- status / hint ---
        self.status = QtWidgets.QLabel(
            "Ctrl+Alt+H hide · Ctrl+Alt+C click-through · Ctrl+Alt+↑/↓ opacity"
        )
        self.status.setObjectName("status")
        body.addWidget(self.status)

        grip_row = QtWidgets.QHBoxLayout()
        grip_row.addStretch(1)
        grip_row.addWidget(QtWidgets.QSizeGrip(panel))
        body.addLayout(grip_row)

    def _place(self, anchor):
        # Open where the setup window was, so the interview window doesn't jump
        # across the screen when the meeting starts. Fall back to the top-right
        # corner if we weren't handed a position.
        if anchor is not None:
            center = anchor.center()
            self.move(center.x() - self.width() // 2,
                      center.y() - self.height() // 2)
        else:
            self._place_top_right()

    def _place_top_right(self):
        screen = QtWidgets.QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 24, screen.top() + 24)

    # ---------- bubbles ----------

    def _bubble_width(self):
        return max(220, int(self.width() * 0.78))

    def _add_bubble(self, role):
        is_ai = role == "AI"

        # each message sits in a row; a stretch pushes it left (interviewer)
        # or right (AI), like a normal chat app. Newest turns are inserted at
        # the top, so the row is added at index 0.
        row = QtWidgets.QWidget()
        rl = QtWidgets.QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)

        frame = QtWidgets.QFrame()
        frame.setObjectName("abubble" if is_ai else "qbubble")
        frame.setFixedWidth(self._bubble_width())
        self._bubbles.append(frame)

        v = QtWidgets.QVBoxLayout(frame)
        v.setContentsMargins(14, 10, 14, 12)
        v.setSpacing(4)

        tag = QtWidgets.QLabel(("● " + role) if is_ai else role.upper())
        tag.setObjectName("aitag" if is_ai else "qtag")
        v.addWidget(tag)

        body = QtWidgets.QLabel("")
        body.setObjectName("bubbletext")
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(body)

        if is_ai:
            rl.addWidget(frame)
            rl.addStretch(1)        # AI answer -> left
        else:
            rl.addStretch(1)
            rl.addWidget(frame)     # interviewer question -> right

        self.chat_layout.insertWidget(0, row)
        self._scroll_to_top()
        return body, v

    def _scroll_to_top(self):
        bar = self.scroll.verticalScrollBar()
        QtCore.QTimer.singleShot(0, lambda: bar.setValue(bar.minimum()))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        w = self._bubble_width()
        for frame in self._bubbles:
            frame.setFixedWidth(w)

    def showEvent(self, e):
        super().showEvent(e)
        # Display affinity is bound to a specific native HWND. Qt destroys and
        # recreates that handle whenever window flags change (click-through,
        # pin fallback), and the new handle resets to WDA_NONE -- silently
        # disabling stealth. show() runs after every such recreation, so we
        # re-assert capture exclusion here to keep it sticky.
        self._apply_capture_exclusion()

    # Generous leading + a clear gap after each block, so a long answer is easy
    # to scan and you don't lose your place between lines.
    _P_STYLE = "margin:0 0 0.6em 0; line-height:160%"
    _LI_STYLE = "margin-bottom:0.4em"

    @staticmethod
    def _split_sentences(text):
        """Break a run of prose into individual sentences. Handles Latin
        (. ! ? …) and CJK (。 ！ ？) terminators, and leaves a trailing
        in-progress sentence (not yet ended) intact so streaming looks smooth."""
        parts = re.split(r"(?<=[.!?…])\s+|(?<=[。！？])", text)
        return [p for p in (s.strip() for s in parts) if p]

    @staticmethod
    def _format_answer(text):
        """Render the model's lightly marked-up answer as easy-to-read rich text:
        each sentence on its own spaced line, plus bullet / numbered lists,
        **bold**, and `code`."""

        def inline(s):
            s = html.escape(s)
            s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
            s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
            return s

        parts = []
        list_type = None   # "ul" | "ol" | None

        def close_list():
            nonlocal list_type
            if list_type:
                parts.append(f"</{list_type}>")
                list_type = None

        for raw in text.split("\n"):
            line = raw.strip()
            if not line:
                close_list()
                continue
            m_ul = re.match(r"^[-*•]\s+(.*)$", line)
            m_ol = re.match(r"^\d+[.)]\s+(.*)$", line)
            if m_ul:
                if list_type != "ul":
                    close_list(); parts.append("<ul>"); list_type = "ul"
                parts.append(f'<li style="{Overlay._LI_STYLE}">{inline(m_ul.group(1))}</li>')
            elif m_ol:
                if list_type != "ol":
                    close_list(); parts.append("<ol>"); list_type = "ol"
                parts.append(f'<li style="{Overlay._LI_STYLE}">{inline(m_ol.group(1))}</li>')
            else:
                close_list()
                # One sentence per spaced line keeps even a long, unbroken answer
                # readable (the model often emits a single dense paragraph).
                for sentence in Overlay._split_sentences(line):
                    parts.append(f'<p style="{Overlay._P_STYLE}">{inline(sentence)}</p>')
        close_list()
        return "".join(parts)

    # ---------- GUI-thread slots ----------

    @QtCore.Slot(str)
    def _on_info(self, text):
        # Elide to the subtitle's fixed width so a long status line shows a clean
        # "…" instead of overflowing into / clipping the header controls.
        fm = self.subtitle.fontMetrics()
        self.subtitle.setText(fm.elidedText(text, Qt.TextElideMode.ElideRight, 128))

    @QtCore.Slot(str)
    def _on_partial(self, text):
        if text:
            self._caption_live = True
            self.caption_label.setText(text)
        else:
            # hand the caption back to the animated "listening" dots
            self._caption_live = False

    def _tick_caption(self):
        if self._caption_live:
            return
        self._dots = self._dots % 3 + 1
        self.caption_label.setText("." * self._dots)

    @QtCore.Slot(str)
    def _on_question(self, text):
        # Always show the interviewer line, whether or not it gets an answer.
        body, _ = self._add_bubble("Interviewer")
        body.setTextFormat(Qt.PlainText)
        body.setText(text)
        self._current_body = None
        self._caption_live = False     # resume listening animation

    @QtCore.Slot()
    def _on_begin_answer(self):
        # The answer streams in above the question that was just added, so the
        # AI answer ends up top-left and the question sits below it.
        self._current_body, self._current_layout = self._add_bubble("AI")
        self._current_body.setTextFormat(Qt.RichText)
        self._answer_text = ""

    @QtCore.Slot(str)
    def _on_delta(self, delta):
        if self._current_body is None:
            self._current_body, self._current_layout = self._add_bubble("AI")
            self._current_body.setTextFormat(Qt.RichText)
            self._answer_text = ""
        self._answer_text += delta
        self._current_body.setText(self._format_answer(self._answer_text))
        self._scroll_to_top()

    @QtCore.Slot(str)
    def _on_end(self, note):
        if self._current_body is not None and note:
            lbl = QtWidgets.QLabel(note)
            lbl.setObjectName("note")
            self._current_layout.addWidget(lbl)
        self._current_body = None
        self._current_layout = None
        self._scroll_to_top()

    # ---------- hotkeys ----------

    def _init_hotkeys(self):
        # Ctrl+Alt+H (show/hide) is owned by the shared visibility controller so
        # it works for the setup window too; the rest are overlay-only.
        self.hotkeys = GlobalHotkeys([
            (HK_OPACITY_UP, MOD_CONTROL | MOD_ALT, VK_UP),
            (HK_OPACITY_DOWN, MOD_CONTROL | MOD_ALT, VK_DOWN),
            (HK_CLICKTHROUGH, MOD_CONTROL | MOD_ALT, VK_C),
        ])
        self.hotkeys.triggered.connect(self._on_hotkey)
        self.hotkeys.start()

    @QtCore.Slot(int)
    def _on_hotkey(self, hid):
        if hid == HK_OPACITY_UP:
            self.opacity_slider.setValue(min(100, self.opacity_slider.value() + 5))
        elif hid == HK_OPACITY_DOWN:
            self.opacity_slider.setValue(max(30, self.opacity_slider.value() - 5))
        elif hid == HK_CLICKTHROUGH:
            self._toggle_click_through()

    def _on_language_changed(self, _idx):
        code = self.language_combo.currentData()
        name = self.language_combo.currentText()
        if code == self._language_code:
            return
        self._language_code = code
        self.info(f"[language] switching to {name}…")
        self.language_changed.emit(code, name)

    def _apply_capture_exclusion(self):
        self._capture_excluded = self._visibility_controller.capture_excluded
        applied = apply_exclude_from_capture(self, enabled=self._capture_excluded)
        self._refresh_stealth_button()
        self._refresh_status(applied)

    def _refresh_stealth_button(self):
        on = self._visibility_controller.capture_excluded
        self.stealth_toggle.setText("Stealth")
        self.stealth_toggle.setToolTip(
            "Stealth ON — hidden from screen capture (click to disable)" if on
            else "Stealth OFF — visible in screen capture (click to enable)"
        )
        self.stealth_toggle.setStyleSheet(
            "font-size:12px; font-weight:600; background: rgba(242,163,60,0.22);"
            " color: #f6c98a; border-radius:7px;"
            if on else
            "font-size:12px; font-weight:600; background: transparent;"
            " color: #9aa0b2; border: none;"
        )

    def _refresh_status(self, applied):
        base = "Ctrl+Alt+H hide · Ctrl+Alt+C click-through · Ctrl+Alt+↑/↓ opacity · Ctrl+Alt+S stealth"
        if self._click_through:
            base = "CLICK-THROUGH ON · Ctrl+Alt+C to disable"
        if applied:
            if self._capture_excluded:
                base = f"{base} · remote: hidden"
            else:
                base = f"{base} · remote: visible"
        self.status.setText(base)

    def _toggle_stealth_mode(self):
        self._visibility_controller.set_capture_excluded(
            not self._visibility_controller.capture_excluded
        )
        self._capture_excluded = self._visibility_controller.capture_excluded
        self._apply_capture_exclusion()

    def _toggle_click_through(self):
        self._click_through = not self._click_through
        flags = self.windowFlags()
        if self._click_through:
            flags |= Qt.WindowTransparentForInput
        else:
            flags &= ~Qt.WindowTransparentForInput
        self._refresh_status(True)
        self.setWindowFlags(flags)
        self.show()

    def _apply_pin_state(self):
        self._pinned = self._visibility_controller.pinned
        try:
            set_window_topmost(self, self._pinned)
        except Exception:
            flags = self.windowFlags()
            if self._pinned:
                flags |= Qt.WindowStaysOnTopHint
            else:
                flags &= ~Qt.WindowStaysOnTopHint
            geo = self.geometry()
            self.setWindowFlags(flags)
            self.setGeometry(geo)
            self.show()
        try:
            self.pin_toggle.setChecked(self._pinned)
        except Exception:
            pass
        self._refresh_pin_button()

    def _toggle_pin(self):
        self._visibility_controller.set_pinned(not self._visibility_controller.pinned)

    def _refresh_pin_button(self):
        self.pin_toggle.setToolTip(
            "Unpin — currently always on top" if self._pinned
            else "Pin on top of other windows"
        )
        # Visual differentiation: colored background when pinned, gray when unpinned
        if self._pinned:
            self.pin_toggle.setStyleSheet(
                "background: rgba(242,163,60,0.22); color: #f2a33c; border-radius:6px;"
            )
        else:
            self.pin_toggle.setStyleSheet(
                "background: transparent; color: #9aa0b2; border: none;"
            )

    def _hide_window(self):
        self.hide()

    def closeEvent(self, event):
        event.ignore()
        self._hide_window()

    def _quit(self):
        # Hide the overlay; the tray icon is the only place that should fully quit.
        self._hide_window()

    def shutdown(self):
        """Release this overlay's per-session resources before it closes, so a
        fresh overlay can reclaim them next meeting: stop the global hotkeys
        (Windows ties the registrations to the listener thread) and drop our
        registration with the shared visibility controller."""
        try:
            if getattr(self, "hotkeys", None) is not None:
                self.hotkeys.stop()
        except Exception:
            pass
        try:
            self._visibility_controller.unregister(self)
        except Exception:
            pass

    # ---------- public, thread-safe interface ----------

    def info(self, text):
        self._sig_info.emit(str(text))

    def partial(self, text):
        self._sig_partial.emit(text or "")

    def show_question(self, text):
        self._sig_question.emit(text)

    def begin_answer(self):
        self._sig_begin_answer.emit()

    def answer_delta(self, delta):
        self._sig_delta.emit(delta)

    def end_answer(self, note=""):
        self._sig_end.emit(note or "")

    def close(self):
        super().close()
