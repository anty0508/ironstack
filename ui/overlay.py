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
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

# Window display-affinity modes (SetWindowDisplayAffinity)
WDA_NONE = 0x00
WDA_EXCLUDEFROMCAPTURE = 0x11   # window invisible to captures (Win10 2004 / build 19041+)

# Extended-window-style access for click-through (mouse pass-through). Only the
# hit-test bit is touched — see set_window_click_through.
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020

# Virtual key codes
VK_H = 0x48   # show/hide
VK_C = 0x43   # click-through
VK_S = 0x53   # stealth

# Hotkey ids
HK_TOGGLE = 1
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


def apply_exclude_from_capture(widget, enabled=True, quiet=False):
    """Hide a window from Windows screen capture / screen sharing.

    Uses WDA_EXCLUDEFROMCAPTURE (the window is fully invisible to capture), which
    needs Windows 10 v2004 (build 19041) or newer. Returns True if it was applied.

    There is intentionally no WDA_MONITOR fallback: that mode does not hide the
    window, it renders it as a solid black box in captures, which is more
    conspicuous than the window itself and is what leaks into recordings on
    sessions where real exclusion is unavailable.

    `quiet` downgrades the failure warning to debug — used by the periodic
    re-assertion so it doesn't spam the log every second.

    The feature relies on Desktop Window Manager (GPU) composition; in Remote
    Desktop sessions / some VMs it fails regardless of build, and the window will
    simply be visible in the capture (read answers on the Viewer instead)."""
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

    # Enabling: WDA_EXCLUDEFROMCAPTURE is the only mode that truly hides the
    # window. We deliberately do NOT fall back to WDA_MONITOR: that mode doesn't
    # hide anything, it paints the window as a solid BLACK box in the capture --
    # more conspicuous than the window itself, and exactly what leaks into a
    # recording on Remote Desktop / VM sessions where EXCLUDEFROMCAPTURE fails.
    ok, excl_err = _apply(WDA_EXCLUDEFROMCAPTURE, "WDA_EXCLUDEFROMCAPTURE")
    if ok:
        log.debug("stealth: applied WDA_EXCLUDEFROMCAPTURE (invisible to capture)")
        return True
    if ok is None:
        return False

    (log.debug if quiet else log.warning)(
        "stealth: could NOT hide window from capture "
        "(build=%s, EXCLUDEFROMCAPTURE err=%d, remote_session=%s). "
        "EXCLUDEFROMCAPTURE needs GPU/DWM composition; this usually means a Remote "
        "Desktop session or a VM without it. Screen-sharing will show this window "
        "(read answers on the Viewer instead). No black-box fallback is applied.",
        _windows_build(), excl_err, _is_remote_session())
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


def set_window_click_through(widget, enabled=True):
    """Toggle click-through (mouse pass-through) on a window's EXISTING native
    handle by flipping only the WS_EX_TRANSPARENT extended style. Returns True
    on success.

    This deliberately avoids Qt's Qt.WindowTransparentForInput flag, which does
    two things that break stealth:
      1. setWindowFlags() makes Qt destroy and recreate the native HWND, and the
         new handle resets to WDA_NONE -- silently disabling capture exclusion.
      2. it forces WS_EX_LAYERED on, and on some GPUs a layered window is
         composited on a path where WDA_EXCLUDEFROMCAPTURE no longer hides it, so
         the overlay leaks into the host's own screen capture as a black box on
         the viewer (works on some PCs, fails on others, depending on GPU/driver).

    WS_EX_TRANSPARENT only changes hit-testing, not how the window is composited.
    But writing GWL_EXSTYLE still makes DWM rebuild the window's composition,
    which drops the EFFECTIVE capture exclusion on the (unchanged) handle -- so a
    caller keeping stealth on MUST force-reassert it right after this returns
    (see Overlay._force_reassert_exclusion). Not recreating the handle keeps that
    a cheap, reliable re-assert instead of the fragile layered-window path Qt's
    flag takes."""
    if not sys.platform.startswith("win") or _user32 is None:
        return False
    try:
        hwnd = int(widget.winId())
    except Exception:
        get_logger().debug("click-through: no window handle yet", exc_info=True)
        return False
    try:
        # Get/SetWindowLongPtrW on 64-bit; the plain Long variants on 32-bit
        # (where the Ptr symbols aren't exported).
        get_long = getattr(_user32, "GetWindowLongPtrW", None) or _user32.GetWindowLongW
        set_long = getattr(_user32, "SetWindowLongPtrW", None) or _user32.SetWindowLongW
        get_long.restype = ctypes.c_ssize_t
        get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        set_long.restype = ctypes.c_ssize_t
        set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]

        ex = get_long(hwnd, GWL_EXSTYLE)
        new_ex = (ex | WS_EX_TRANSPARENT) if enabled else (ex & ~WS_EX_TRANSPARENT)
        if new_ex == ex:
            return True
        ctypes.set_last_error(0)
        set_long(hwnd, GWL_EXSTYLE, new_ex)
        err = ctypes.get_last_error()
        if err:
            get_logger().warning("click-through: SetWindowLong failed (err=%d)", err)
            return False
        return True
    except Exception:
        get_logger().exception("click-through: toggling WS_EX_TRANSPARENT failed")
        return False


class WindowVisibilityController(QtCore.QObject):
    """Shared controller for app-wide opacity and capture-exclusion state.

    A QObject so window work marshals onto the GUI thread: `set_capture_excluded`
    can be called from a network thread (a peer toggling stealth), but touching Qt
    widgets or native handles off the GUI thread crashes the process, so the
    request is delivered through a queued signal and applied on the GUI thread."""

    _capture_request = QtCore.Signal(bool)

    def __init__(self):
        super().__init__()
        self.opacity_percent = 95
        self.capture_excluded = False
        self.pinned = True
        self._widgets = []
        self._active = None       # window Ctrl+Alt+H hides/shows (latest registered)
        self._hotkeys = None
        self._start_hotkeys()
        # Any-thread -> GUI-thread hop for stealth changes (see class docstring).
        self._capture_request.connect(self._apply_capture_excluded)
        # Capture-exclusion (stealth) is a per-HWND flag that Windows silently
        # drops whenever Qt recreates the native handle (flag changes, some
        # reconnect/refresh paths). A light watchdog re-asserts it so stealth,
        # once on, can never quietly turn itself off.
        self._watchdog = QtCore.QTimer()
        self._watchdog.setInterval(300)
        self._watchdog.timeout.connect(self._reassert_capture)
        self._watchdog.start()

    def _reassert_capture(self):
        if not self.capture_excluded:
            return
        for widget in list(self._widgets):
            try:
                if widget.isVisible():
                    apply_exclude_from_capture(widget, enabled=True, quiet=True)
            except Exception:
                pass

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
        """Thread-safe: may be called from a network thread (a peer toggling
        stealth). Hops to the GUI thread before touching any windows."""
        self._capture_request.emit(bool(enabled))

    @QtCore.Slot(bool)
    def _apply_capture_excluded(self, enabled):
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


class AIAnswerController(QtCore.QObject):
    """Shared on/off state for AI-generated answers.

    A QObject for the same reason as WindowVisibilityController: a peer (host or
    viewer) can toggle this from a network thread, so the request is delivered
    through a queued signal and applied on the GUI thread before any button
    repaints."""

    _ai_request = QtCore.Signal(bool)

    def __init__(self):
        super().__init__()
        self.ai_enabled = True
        self._widgets = []
        self._ai_request.connect(self._apply_ai_enabled)

    def register(self, widget):
        if widget is None:
            return
        if widget not in self._widgets:
            self._widgets.append(widget)

    def unregister(self, widget):
        try:
            self._widgets.remove(widget)
        except ValueError:
            pass

    def set_ai_enabled(self, enabled):
        """Thread-safe: may be called from a network thread (a peer toggling AI)."""
        self._ai_request.emit(bool(enabled))

    @QtCore.Slot(bool)
    def _apply_ai_enabled(self, enabled):
        self.ai_enabled = bool(enabled)
        get_logger().info("AI answers -> %s", "on" if self.ai_enabled else "off")
        for widget in list(self._widgets):
            try:
                if hasattr(widget, "_refresh_ai_button"):
                    widget._refresh_ai_button()
            except Exception:
                pass


_AI_CONTROLLER = None


def get_ai_answer_controller():
    global _AI_CONTROLLER
    if _AI_CONTROLLER is None:
        _AI_CONTROLLER = AIAnswerController()
    return _AI_CONTROLLER


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
QToolTip {
    background-color: #1c2029;
    color: #e8eaf0;
    border: 1px solid rgba(242,163,60,0.45);
    border-radius: 6px;
    padding: 4px 8px;
    font-size: 12px;
}
#panel {
    background: #12141a;
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 0;
}
#header { border-bottom: 1px solid rgba(255,255,255,0.06); }
#appDot { color: #f2a33c; font-size: 18px; }
#title { font-size: 13px; font-weight: 600; color: #f2f3f7; }
#subtitle { font-size: 11px; color: #8a90a2; }
#sharePanel {
    background: rgba(45,120,255,0.06);
    border: 1px solid rgba(45,120,255,0.28);
    border-radius: 10px;
}
#shareTag { font-size: 10px; font-weight: 700; color: #9fbcff; letter-spacing: 1px; }
#shareEdit {
    background: rgba(18,22,30,0.90);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 8px; color: #e8eaf0; font-size: 13px; padding: 6px;
    selection-background-color: rgba(45,120,255,0.40);
}
#shareEdit:focus { border: 1px solid rgba(45,120,255,0.6); }
QSplitter#bodySplit::handle { background: rgba(255,255,255,0.06); border-radius: 3px; }
QSplitter#bodySplit::handle:horizontal { width: 6px; }
QSplitter#bodySplit::handle:hover { background: rgba(45,120,255,0.55); }
#winbtn {
    background: transparent; border: none; color: #9aa0b2;
    font-size: 15px; font-weight: 600; padding: 0 3px;
}
#winbtn:hover { color: #ffffff; }
#winbtn:pressed { background: transparent; }
#winbtn:focus { outline: none; }
#refreshbtn {
    background: transparent; border: none; color: #9aa0b2;
    font-size: 20px; padding: 0 2px;
}
#refreshbtn:hover { color: #ffffff; }
#refreshbtn:focus { outline: none; }
#langbox {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.10);
    border-radius: 8px; padding: 2px 6px; color: #cdd2de; font-size: 11px;
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
#ububble {
    background: rgba(38, 56, 44, 0.92);
    border: 1px solid rgba(143, 208, 127, 0.22);
    border-radius: 10px;
}
#qtag { font-size: 10px; font-weight: 700; color: #7f8aa3; letter-spacing: 1px; }
#aitag { font-size: 10px; font-weight: 700; color: #f2a33c; letter-spacing: 1px; }
#utag { font-size: 10px; font-weight: 700; color: #8fd07f; letter-spacing: 1px; }
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
    _sig_user = QtCore.Signal(str)        # candidate's own spoken answer (final)
    _sig_user_partial = QtCore.Signal(str)   # candidate's live (streaming) speech
    _sig_user_discard = QtCore.Signal()      # drop the streaming "You" bubble (echo)
    _sig_begin_answer = QtCore.Signal()   # an answer is about to stream
    _sig_delta = QtCore.Signal(str)
    _sig_end = QtCore.Signal(str)
    _sig_close = QtCore.Signal()
    _sig_set_shared = QtCore.Signal(str)   # remote update -> set shared notepad text
    _sig_connected = QtCore.Signal(bool)   # a network connection was established
    _sig_set_language = QtCore.Signal(str, str)   # peer changed language -> reflect it

    end_meeting = QtCore.Signal()   # "End Meeting" clicked -> return to setup
    quit_app = QtCore.Signal()      # quit clicked -> exit the whole app
    language_changed = QtCore.Signal(str, str)   # (deepgram code, display name)
    shared_text_changed = QtCore.Signal(str)     # local user edited the shared notepad
    stealth_toggled = QtCore.Signal(bool)        # user toggled stealth -> sync to peer
    ai_toggled = QtCore.Signal(bool)             # user toggled AI answers -> sync to peer
    refresh_requested = QtCore.Signal()          # user asked to restart transcription

    def __init__(self, start_geometry=None, language_code=DEFAULT_LANGUAGE_CODE,
                 role="solo"):
        super().__init__()
        self._language_code = language_code or DEFAULT_LANGUAGE_CODE
        self._role = role   # "host" | "viewer" | "solo"
        title = {"host": "IronStack — Host",
                 "viewer": "IronStack — Viewer"}.get(role, "IronStack")
        self.setWindowTitle(title)
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
        self._user_body = None         # the live, streaming "You" bubble (or None)
        self._answer_text = ""
        self._click_through = False
        self._suppress_share = False   # True while applying a remote shared-text update
        self._bubbles = []
        self._caption_live = False     # True while real transcription is showing
        self._dots = 0
        self._visibility_controller = get_window_visibility_controller()
        self._pinned = self._visibility_controller.pinned
        self._capture_excluded = self._visibility_controller.capture_excluded
        self._ai_controller = get_ai_answer_controller()

        self._build_ui()
        self.setStyleSheet(STYLE)
        self._visibility_controller.register(self)
        self._visibility_controller.set_opacity_percent(self._visibility_controller.opacity_percent)
        self._ai_controller.register(self)
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
        self._sig_user.connect(self._on_user_answer)
        self._sig_user_partial.connect(self._on_user_partial)
        self._sig_user_discard.connect(self._on_user_discard)
        self._sig_set_shared.connect(self._on_set_shared_text)
        self._sig_connected.connect(self._on_connected)
        self._sig_set_language.connect(self._on_set_language)
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
        h.setSpacing(2)

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

        # No text badge in the header; just recolor the brand dot so a Viewer
        # window stays subtly distinguishable from a Host window side by side.
        if self._role == "viewer":
            dot.setStyleSheet("color: #2d78ff;")
        h.addStretch(1)

        self.language_combo = QtWidgets.QComboBox()
        self.language_combo.setObjectName("langbox")
        self.language_combo.setFixedWidth(82)
        self.language_combo.setFocusPolicy(Qt.NoFocus)
        self.language_combo.setCursor(Qt.PointingHandCursor)
        self.language_combo.setToolTip("Language (transcription + answers) — change anytime")
        for label, code in LANGUAGES:
            self.language_combo.addItem(label, code)
        idx = self.language_combo.findData(self._language_code)
        self.language_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.language_combo.currentIndexChanged.connect(self._on_language_changed)
        h.addWidget(self.language_combo)

        self.share_toggle = QtWidgets.QPushButton("Note")
        self.share_toggle.setObjectName("winbtn")
        self.share_toggle.setFixedWidth(42)
        self.share_toggle.setCursor(Qt.PointingHandCursor)
        self.share_toggle.setFlat(True)
        self.share_toggle.setCheckable(True)
        self.share_toggle.setFocusPolicy(Qt.NoFocus)
        self.share_toggle.setToolTip("Show/hide the shared note (available once connected)")
        self.share_toggle.clicked.connect(lambda: self._toggle_share_panel())
        h.addWidget(self.share_toggle)

        self.stealth_toggle = QtWidgets.QPushButton("Stealth")
        self.stealth_toggle.setObjectName("winbtn")
        self.stealth_toggle.setFixedWidth(52)
        self.stealth_toggle.setCursor(Qt.PointingHandCursor)
        self.stealth_toggle.setFlat(True)
        self.stealth_toggle.setFocusPolicy(Qt.NoFocus)
        self.stealth_toggle.setToolTip("Stealth mode: hidden from remote viewers")
        self.stealth_toggle.clicked.connect(self._toggle_stealth_mode)
        self._refresh_stealth_button()
        h.addWidget(self.stealth_toggle)

        self.ai_toggle = QtWidgets.QPushButton("AI")
        self.ai_toggle.setObjectName("winbtn")
        self.ai_toggle.setFixedWidth(32)
        self.ai_toggle.setCursor(Qt.PointingHandCursor)
        self.ai_toggle.setFlat(True)
        self.ai_toggle.setFocusPolicy(Qt.NoFocus)
        self.ai_toggle.setToolTip("AI answers: generate a suggested answer for each question")
        self.ai_toggle.clicked.connect(self._toggle_ai_mode)
        self._refresh_ai_button()
        h.addWidget(self.ai_toggle)

        self.refresh_btn = QtWidgets.QPushButton("⟳")
        self.refresh_btn.setObjectName("refreshbtn")
        self.refresh_btn.setFixedWidth(30)
        self.refresh_btn.setCursor(Qt.PointingHandCursor)
        self.refresh_btn.setFlat(True)
        self.refresh_btn.setFocusPolicy(Qt.NoFocus)
        self.refresh_btn.setToolTip("Restart Deepgram")
        self.refresh_btn.clicked.connect(self.refresh_requested.emit)
        h.addWidget(self.refresh_btn)

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

        # The meeting area (caption + chat) and the shared-notes panel live in a
        # horizontal splitter so the divider can be dragged. The shared panel is
        # hidden until a network connection is established.
        self.body_split = QtWidgets.QSplitter(Qt.Horizontal)
        self.body_split.setObjectName("bodySplit")
        self.body_split.setChildrenCollapsible(False)

        meeting_side = QtWidgets.QWidget()
        meeting_side.setMinimumWidth(300)   # never let the note panel squeeze it out
        ms = QtWidgets.QVBoxLayout(meeting_side)
        ms.setContentsMargins(0, 0, 0, 0)
        ms.setSpacing(10)

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
        ms.addWidget(cap)

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
        ms.addWidget(self.scroll, 1)

        self.body_split.addWidget(meeting_side)
        self.body_split.addWidget(self._build_share_panel())
        self.body_split.setStretchFactor(0, 7)
        self.body_split.setStretchFactor(1, 3)
        self.share_panel.hide()
        # Re-fit chat bubbles to the left pane whenever the divider is dragged.
        self.body_split.splitterMoved.connect(lambda *_: self._refresh_bubble_widths())
        body.addWidget(self.body_split, 1)

        # --- status / hint ---
        self.status = QtWidgets.QLabel(
            "Ctrl+Alt+H hide · Ctrl+Alt+C click-through"
        )
        self.status.setObjectName("status")
        body.addWidget(self.status)

        # Opacity control lives at the bottom-right now (out of the dense header).
        grip_row = QtWidgets.QHBoxLayout()
        grip_row.addStretch(1)
        op_label = QtWidgets.QLabel("Opacity")
        op_label.setObjectName("status")
        grip_row.addWidget(op_label)
        self.opacity_slider = QtWidgets.QSlider(Qt.Horizontal)
        self.opacity_slider.setFixedWidth(90)
        self.opacity_slider.setRange(30, 100)
        self.opacity_slider.setValue(self._visibility_controller.opacity_percent)
        self.opacity_slider.setToolTip("Opacity")
        self.opacity_slider.valueChanged.connect(
            lambda v: self._visibility_controller.set_opacity_percent(v)
        )
        grip_row.addWidget(self.opacity_slider)
        grip_row.addSpacing(6)
        grip_row.addWidget(QtWidgets.QSizeGrip(panel))
        body.addLayout(grip_row)

    def _build_share_panel(self):
        """Right-side shared text panel: a live, two-way notepad both the local
        user and the connected remote user can type in (like a shared editor).
        Returns the panel widget (kept hidden until connected)."""
        self.share_panel = QtWidgets.QFrame()
        self.share_panel.setObjectName("sharePanel")
        sp = QtWidgets.QVBoxLayout(self.share_panel)
        sp.setContentsMargins(10, 8, 10, 10)
        sp.setSpacing(6)

        self.share_panel.setMinimumWidth(200)
        row = QtWidgets.QHBoxLayout()
        tag = QtWidgets.QLabel("● NOTE  ·  you ⇄ remote")
        tag.setObjectName("shareTag")
        row.addWidget(tag)
        row.addStretch(1)
        close = QtWidgets.QPushButton("×")
        close.setObjectName("winbtn")
        close.setCursor(Qt.PointingHandCursor)
        close.setToolTip("Close the note (reopen with the ‘Note’ button)")
        close.clicked.connect(lambda: self._toggle_share_panel(False))
        row.addWidget(close)
        sp.addLayout(row)

        self.share_edit = QtWidgets.QPlainTextEdit()
        self.share_edit.setObjectName("shareEdit")
        self.share_edit.setPlaceholderText(
            "Shared note — both sides can type here. Text syncs live.")
        self.share_edit.textChanged.connect(self._on_share_text_changed)
        sp.addWidget(self.share_edit, 1)
        self._refresh_share_button()
        return self.share_panel

    def _toggle_share_panel(self, show=None):
        """Show/hide the note panel. When showing, size the split ~70/30 based on
        the splitter's real width so the panel sits beside the meeting area
        rather than on top of it."""
        if show is None:
            show = not self.share_panel.isVisible()
        self.share_panel.setVisible(show)
        if show:
            # Defer sizing to the next event-loop turn so the splitter has its
            # final width (e.g. right after a window resize on connect).
            QtCore.QTimer.singleShot(0, self._apply_split_sizes)
        if hasattr(self, "share_toggle"):
            self.share_toggle.setChecked(show)
            self._refresh_share_button()

    def _apply_split_sizes(self):
        w = self.body_split.width()
        if w <= 0:
            w = self.width()
        self.body_split.setSizes([int(w * 0.7), int(w * 0.3)])
        QtCore.QTimer.singleShot(0, self._refresh_bubble_widths)

    def _refresh_share_button(self):
        on = self.share_panel.isVisible()
        self.share_toggle.setStyleSheet(
            "font-size:12px; font-weight:600; background: rgba(90,150,242,0.22);"
            " color: #9fbcff; border-radius:7px;"
            if on else
            "font-size:12px; font-weight:600; background: transparent;"
            " color: #9aa0b2; border: none;")

    def _on_share_text_changed(self):
        # Local edit -> tell the app to send it over the network. Suppressed while
        # we're applying a remote update (so we don't echo it back).
        if getattr(self, "_suppress_share", False):
            return
        self.shared_text_changed.emit(self.share_edit.toPlainText())

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
        # Size bubbles to the chat's own viewport, NOT the whole window -- once
        # the note panel opens, the window is wider than the left (chat) pane, and
        # window-relative bubbles would spill under the note panel.
        try:
            ref = self.scroll.viewport().width()
        except Exception:
            ref = self.width()
        return max(200, int(ref * 0.92))

    def _refresh_bubble_widths(self):
        w = self._bubble_width()
        for frame in self._bubbles:
            frame.setFixedWidth(w)

    def _add_bubble(self, role):
        is_ai = role == "AI"
        is_user = role == "You"

        # each message sits in a row; a stretch pushes it left (AI) or right
        # (interviewer / the candidate's own speech), like a normal chat app.
        # Newest turns are inserted at the top, so the row is added at index 0.
        row = QtWidgets.QWidget()
        rl = QtWidgets.QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)

        frame = QtWidgets.QFrame()
        frame.setObjectName("abubble" if is_ai else "ububble" if is_user else "qbubble")
        frame.setFixedWidth(self._bubble_width())
        self._bubbles.append(frame)

        v = QtWidgets.QVBoxLayout(frame)
        v.setContentsMargins(14, 10, 14, 12)
        v.setSpacing(4)

        tag = QtWidgets.QLabel(("● " + role) if is_ai else role.upper())
        tag.setObjectName("aitag" if is_ai else "utag" if is_user else "qtag")
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
            rl.addWidget(frame)     # interviewer question / candidate answer -> right

        self.chat_layout.insertWidget(0, row)
        self._scroll_to_top()
        return body, v

    def _scroll_to_top(self):
        bar = self.scroll.verticalScrollBar()
        # Only pull the view back to the top when it is already at (or within a
        # few px of) the top. If the user has scrolled down to read earlier
        # content, leave their position alone instead of yanking it up.
        if bar.value() > bar.minimum() + 4:
            return
        QtCore.QTimer.singleShot(0, lambda: bar.setValue(bar.minimum()))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._refresh_bubble_widths()

    def showEvent(self, e):
        super().showEvent(e)
        # Display affinity is bound to a specific native HWND. Qt destroys and
        # recreates that handle whenever window flags change (click-through,
        # pin fallback), and the new handle resets to WDA_NONE -- silently
        # disabling stealth. show() runs after every such recreation, so we
        # restore click-through (GWL_EXSTYLE) first, then re-assert capture
        # exclusion last so EXCLUDE lands after that ex-style write.
        self._reassert_click_through()
        self._apply_capture_exclusion()
        # Dragging onto a monitor with a different DPI makes Qt recreate the native
        # window on the new screen; that new handle resets to WDA_NONE. Re-assert
        # the instant the screen changes (screenChanged fires as the move crosses),
        # so the window can't flash into a capture mid-drag.
        handle = self.windowHandle()
        if handle is not None and not getattr(self, "_screen_hooked", False):
            self._screen_hooked = True
            handle.screenChanged.connect(lambda _s: self._reassert_exclusion())

    def _reassert_exclusion(self):
        if self._visibility_controller.capture_excluded:
            apply_exclude_from_capture(self, enabled=True, quiet=True)

    def _force_reassert_exclusion(self):
        # A GWL_EXSTYLE change (the click-through toggle) makes DWM rebuild the
        # window's composition and silently drops the EFFECTIVE capture exclusion,
        # even though the HWND is unchanged. Re-applying EXCLUDE is a no-op then
        # (the affinity VALUE hasn't changed, so Windows skips the rebuild — which
        # is also why the 300ms watchdog never rescues it), so force a real
        # WDA_NONE -> WDA_EXCLUDEFROMCAPTURE transition. Same thing that makes
        # toggling the Stealth button off/on clear the black box by hand.
        if not self._visibility_controller.capture_excluded:
            return
        apply_exclude_from_capture(self, enabled=False, quiet=True)
        apply_exclude_from_capture(self, enabled=True, quiet=True)

    def _reassert_click_through(self):
        # WS_EX_TRANSPARENT lives on the native handle, so a handle recreation
        # (cross-DPI move, pin fallback) drops it. Re-assert when click-through
        # is meant to be on, alongside the capture-exclusion re-assert.
        if self._click_through:
            set_window_click_through(self, enabled=True)

    def event(self, e):
        # The native HWND is recreated on some moves (cross-DPI monitors, flag
        # changes); the new handle has no capture-exclusion. Re-assert it the
        # moment the handle changes — the tightest point possible — so stealth
        # can't lapse for even a frame while the window is being moved.
        if e.type() == QtCore.QEvent.Type.WinIdChange:
            # Order matters: restore click-through (a GWL_EXSTYLE write) FIRST,
            # then assert capture exclusion LAST, so EXCLUDE lands after the
            # composition rebuild the ex-style change triggers — on a fresh
            # handle (WDA_NONE) that's a real transition, so it sticks.
            self._reassert_click_through()
            self._reassert_exclusion()
        return super().event(e)

    def moveEvent(self, e):
        super().moveEvent(e)
        # Keep the window excluded from capture WHILE dragging, so a stealth
        # window never flashes into a remote viewer's screen mid-move.
        self._reassert_exclusion()

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

    @QtCore.Slot(str)
    def _on_user_partial(self, text):
        # Live, token-by-token streaming of the candidate's own speech into a
        # "You" bubble (the same immediacy as the interviewer's live caption).
        if not text:
            return
        if self._user_body is None:
            self._user_body, _ = self._add_bubble("You")
            self._user_body.setTextFormat(Qt.PlainText)
        self._user_body.setText(text)
        self._scroll_to_top()

    @QtCore.Slot(str)
    def _on_user_answer(self, text):
        # Finalize the candidate's spoken answer. If a live bubble is streaming,
        # lock it in; otherwise make one. Never triggers an AI answer.
        if self._user_body is not None:
            self._user_body.setText(text)
            self._user_body = None
        else:
            body, _ = self._add_bubble("You")
            body.setTextFormat(Qt.PlainText)
            body.setText(text)
        self._current_body = None

    @QtCore.Slot()
    def _on_user_discard(self):
        # The streaming "You" bubble turned out to be the interviewer's echoed
        # voice; remove it so no duplicate remains.
        if self._user_body is None:
            return
        frame = self._user_body.parentWidget()   # the "ububble" QFrame
        if frame is not None:
            if frame in self._bubbles:
                self._bubbles.remove(frame)
            row = frame.parentWidget()            # the row that holds the frame
            if row is not None:
                self.chat_layout.removeWidget(row)
                row.deleteLater()
        self._user_body = None

    @QtCore.Slot(str)
    def _on_set_shared_text(self, text):
        # Apply a remote update to the shared notepad without echoing it back,
        # keeping the caret where it was so local typing isn't disrupted.
        if text == self.share_edit.toPlainText():
            return
        self._suppress_share = True
        cursor = self.share_edit.textCursor()
        pos = cursor.position()
        self.share_edit.setPlainText(text)
        cursor = self.share_edit.textCursor()
        cursor.setPosition(min(pos, len(text)))
        self.share_edit.setTextCursor(cursor)
        self._suppress_share = False

    @QtCore.Slot(bool)
    def _on_connected(self, connected):
        if connected:
            self.share_toggle.setEnabled(True)
            # Widen the window once so the note panel has room beside the meeting
            # area instead of squeezing it.
            if not getattr(self, "_widened_for_note", False):
                self._widened_for_note = True
                self.resize(self.width() + 340, self.height())
            if not self.share_panel.isVisible():
                self._toggle_share_panel(True)
            self.info("[network] connected — shared note is open")

    @QtCore.Slot(str, str)
    def _on_set_language(self, code, name):
        # Reflect a language change made by the peer on our own picker, WITHOUT
        # re-emitting language_changed (which would bounce it back and loop).
        if not code or code == self._language_code:
            return
        self._language_code = code
        idx = self.language_combo.findData(code)
        if idx >= 0:
            self.language_combo.blockSignals(True)
            self.language_combo.setCurrentIndex(idx)
            self.language_combo.blockSignals(False)
        self.info(f"[language] {name or code}")

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
            (HK_CLICKTHROUGH, MOD_CONTROL | MOD_ALT, VK_C),
        ])
        self.hotkeys.triggered.connect(self._on_hotkey)
        self.hotkeys.start()

    @QtCore.Slot(int)
    def _on_hotkey(self, hid):
        if hid == HK_CLICKTHROUGH:
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
        base = "Ctrl+Alt+H hide · Ctrl+Alt+C click-through · Ctrl+Alt+S stealth"
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
        # Tell the peer so stealth stays in sync across host and viewer.
        self.stealth_toggled.emit(self._capture_excluded)

    def _refresh_ai_button(self):
        on = self._ai_controller.ai_enabled
        self.ai_toggle.setToolTip(
            "AI answers ON — click to stop generating answers" if on
            else "AI answers OFF — click to resume generating answers"
        )
        self.ai_toggle.setStyleSheet(
            "font-size:12px; font-weight:600; background: rgba(90,180,255,0.22);"
            " color: #9fd3ff; border-radius:7px;"
            if on else
            "font-size:12px; font-weight:600; background: transparent;"
            " color: #9aa0b2; border: none;"
        )

    def _toggle_ai_mode(self):
        self._ai_controller.set_ai_enabled(not self._ai_controller.ai_enabled)
        # Tell the peer so the AI on/off state stays in sync across host and viewer.
        self.ai_toggled.emit(self._ai_controller.ai_enabled)

    def _toggle_click_through(self):
        self._click_through = not self._click_through
        if sys.platform.startswith("win"):
            # Flip WS_EX_TRANSPARENT on the existing handle — no HWND recreation
            # and no WS_EX_LAYERED change (see set_window_click_through), unlike
            # Qt.WindowTransparentForInput which does both and leaks the overlay
            # into the host's screen capture as a black box on some GPUs.
            set_window_click_through(self, enabled=self._click_through)
            # The GWL_EXSTYLE write still makes DWM rebuild the composition, which
            # drops the EFFECTIVE capture exclusion on the (unchanged) handle — so
            # the overlay would show as a black box on the viewer until stealth is
            # toggled by hand. Force it back on right away.
            self._force_reassert_exclusion()
        else:
            flags = self.windowFlags()
            if self._click_through:
                flags |= Qt.WindowTransparentForInput
            else:
                flags &= ~Qt.WindowTransparentForInput
            self.setWindowFlags(flags)
            self.show()
        self._refresh_status(True)

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
        try:
            self._ai_controller.unregister(self)
        except Exception:
            pass

    # ---------- public, thread-safe interface ----------

    def info(self, text):
        self._sig_info.emit(str(text))

    def partial(self, text):
        self._sig_partial.emit(text or "")

    def show_question(self, text):
        self._sig_question.emit(text)

    def show_user_answer(self, text):
        self._sig_user.emit(text)

    def user_partial(self, text):
        self._sig_user_partial.emit(text or "")

    def discard_user_answer(self):
        self._sig_user_discard.emit()

    def set_shared_text(self, text):
        self._sig_set_shared.emit(text or "")

    def set_connected(self, connected=True):
        self._sig_connected.emit(bool(connected))

    def set_language(self, code, name=""):
        self._sig_set_language.emit(code or "", name or "")

    def begin_answer(self):
        self._sig_begin_answer.emit()

    def answer_delta(self, delta):
        self._sig_delta.emit(delta)

    def end_answer(self, note=""):
        self._sig_end.emit(note or "")

    def close(self):
        super().close()
