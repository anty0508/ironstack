"""Inject mouse/keyboard events on the Host via Win32 SendInput (ctypes).

The Viewer sends events with the pointer position **normalized (0..1)** inside the
shared monitor; this maps that to the monitor's rectangle in the Windows virtual
desktop and issues absolute SendInput calls, so it works correctly with multiple
monitors and any resolution.

Limitation: a non-elevated process cannot inject into elevated/UAC windows
(Windows UIPI). Controlling those needs a background service (a later phase).
"""

import sys
import ctypes
from ctypes import wintypes

from logsetup import get_logger

_IS_WIN = sys.platform == "win32"

if _IS_WIN:
    ULONG_PTR = ctypes.c_size_t

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    _INPUT_MOUSE = 0
    _INPUT_KEYBOARD = 1

    _MOUSEEVENTF_MOVE = 0x0001
    _MOUSEEVENTF_ABSOLUTE = 0x8000
    _MOUSEEVENTF_VIRTUALDESK = 0x4000
    _MOUSE_BTN = {
        "left":   (0x0002, 0x0004),   # down, up
        "right":  (0x0008, 0x0010),
        "middle": (0x0020, 0x0040),
    }
    _MOUSEEVENTF_WHEEL = 0x0800
    _MOUSEEVENTF_HWHEEL = 0x1000

    _KEYEVENTF_KEYUP = 0x0002
    _KEYEVENTF_EXTENDEDKEY = 0x0001

    _SM_XVIRTUALSCREEN = 76
    _SM_YVIRTUALSCREEN = 77
    _SM_CXVIRTUALSCREEN = 78
    _SM_CYVIRTUALSCREEN = 79

    _user32 = ctypes.windll.user32

    class _CURSORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT)]

    _user32.GetCursorInfo.argtypes = [ctypes.POINTER(_CURSORINFO)]
    _user32.GetCursorInfo.restype = wintypes.BOOL
    _user32.LoadCursorW.restype = wintypes.HANDLE

    # Standard cursor resource ids -> a name the viewer maps to a Qt cursor.
    _CURSOR_IDS = {
        32512: "arrow", 32513: "ibeam", 32514: "wait", 32515: "cross",
        32646: "sizeall", 32642: "sizenwse", 32643: "sizenesw",
        32644: "sizewe", 32645: "sizens", 32649: "hand",
        32650: "appstarting", 32651: "help", 32648: "no",
    }
    _std_cursor_map = None


def detect_cursor():
    """Return the Host's current cursor type as a name (e.g. 'ibeam', 'hand',
    'wait', 'arrow'). Custom/app cursors fall back to 'arrow'."""
    if not _IS_WIN:
        return "arrow"
    global _std_cursor_map
    if _std_cursor_map is None:
        _std_cursor_map = {}
        for idc, name in _CURSOR_IDS.items():
            h = _user32.LoadCursorW(None, idc)
            if h:
                _std_cursor_map[int(h)] = name
    ci = _CURSORINFO()
    ci.cbSize = ctypes.sizeof(_CURSORINFO)
    if not _user32.GetCursorInfo(ctypes.byref(ci)):
        return "arrow"
    return _std_cursor_map.get(int(ci.hCursor) if ci.hCursor else 0, "arrow")


class InputInjector:
    """Applies input events to the Host, mapping normalized monitor coords to
    absolute virtual-desktop coordinates."""

    def __init__(self, mon_x, mon_y, mon_w, mon_h):
        self.mon_x, self.mon_y = mon_x, mon_y
        self.mon_w, self.mon_h = max(1, mon_w), max(1, mon_h)
        if _IS_WIN:
            self.v_x = _user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
            self.v_y = _user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
            self.v_w = max(1, _user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN))
            self.v_h = max(1, _user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN))

    # --- helpers ---
    def _abs(self, nx, ny):
        """Normalized (0..1) monitor coords -> 0..65535 virtual-desktop coords."""
        px = self.mon_x + max(0.0, min(1.0, nx)) * self.mon_w
        py = self.mon_y + max(0.0, min(1.0, ny)) * self.mon_h
        ax = int((px - self.v_x) / self.v_w * 65535)
        ay = int((py - self.v_y) / self.v_h * 65535)
        return ax, ay

    def _send_mouse(self, flags, ax=0, ay=0, data=0):
        inp = _INPUT(type=_INPUT_MOUSE, u=_INPUTUNION(mi=_MOUSEINPUT(
            dx=ax, dy=ay, mouseData=data,
            dwFlags=flags | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK,
            time=0, dwExtraInfo=0)))
        _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))

    def _send_key(self, vk, up, extended=False):
        flags = 0
        if up:
            flags |= _KEYEVENTF_KEYUP
        if extended:
            flags |= _KEYEVENTF_EXTENDEDKEY
        inp = _INPUT(type=_INPUT_KEYBOARD, u=_INPUTUNION(ki=_KEYBDINPUT(
            wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)))
        _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))

    # --- public: apply one event dict from the viewer ---
    def apply(self, ev):
        if not _IS_WIN:
            return
        try:
            kind = ev.get("kind")
            if kind == "move":
                ax, ay = self._abs(ev["x"], ev["y"])
                self._send_mouse(_MOUSEEVENTF_MOVE, ax, ay)
            elif kind == "button":
                ax, ay = self._abs(ev["x"], ev["y"])
                down_flag, up_flag = _MOUSE_BTN.get(ev.get("button", "left"),
                                                    _MOUSE_BTN["left"])
                self._send_mouse(_MOUSEEVENTF_MOVE, ax, ay)   # position first
                self._send_mouse(down_flag if ev.get("down") else up_flag, ax, ay)
            elif kind == "scroll":
                ax, ay = self._abs(ev["x"], ev["y"])
                dy = int(ev.get("dy", 0))
                dx = int(ev.get("dx", 0))
                if dy:
                    self._send_mouse(_MOUSEEVENTF_WHEEL, ax, ay, data=dy * 120)
                if dx:
                    self._send_mouse(_MOUSEEVENTF_HWHEEL, ax, ay, data=dx * 120)
            elif kind == "key":
                vk = int(ev.get("vk", 0))
                if vk:
                    self._send_key(vk, up=not ev.get("down"),
                                   extended=bool(ev.get("ext")))
        except Exception:
            get_logger().debug("inputinject: apply failed for %r", ev, exc_info=True)
