"""Low-level keyboard hook for the remote Viewer (Windows).

A plain Qt key handler misses a lot: Qt eats Ctrl/Alt combos as shortcuts, and the
Windows key opens the *local* Start menu before the app sees it. A global
WH_KEYBOARD_LL hook captures EVERY key (Win, Ctrl+C/V, function keys, …) while the
remote window is focused, forwards it to the host, and suppresses local handling.

Alt+Tab is deliberately let through, so the user can always switch away (which
deactivates capture). Ctrl+Alt+Delete cannot be captured or injected by a normal
app — the OS handles it before any hook — so it needs a Windows service (later).
"""

import sys
import ctypes
from ctypes import wintypes

from logsetup import get_logger

_IS_WIN = sys.platform == "win32"

if _IS_WIN:
    WH_KEYBOARD_LL = 13
    WM_KEYDOWN = 0x0100
    WM_SYSKEYDOWN = 0x0104
    LLKHF_EXTENDED = 0x01
    HC_ACTION = 0
    VK_TAB = 0x09
    VK_MENU = 0x12   # Alt

    ULONG_PTR = ctypes.c_size_t

    class _KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("vkCode", wintypes.DWORD),
                    ("scanCode", wintypes.DWORD),
                    ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    # LRESULT is pointer-sized; use LPARAM (c_ssize_t) as the proc return type.
    _HOOKPROC = ctypes.CFUNCTYPE(wintypes.LPARAM, ctypes.c_int,
                                 wintypes.WPARAM, wintypes.LPARAM)
    _user32.SetWindowsHookExW.restype = wintypes.HHOOK
    _user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC,
                                          wintypes.HINSTANCE, wintypes.DWORD]
    _user32.CallNextHookEx.restype = wintypes.LPARAM
    _user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int,
                                       wintypes.WPARAM, wintypes.LPARAM]
    # Return is pointer-sized; without this ctypes truncates the module handle to
    # 32 bits on 64-bit Windows and SetWindowsHookEx then fails.
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]


class KeyboardHook:
    """Install/remove a global low-level keyboard hook.

    `on_key(vk, down, extended)` handles a key and returns True to swallow it
    locally (it went to the host) or False to let it pass through to the local
    machine (e.g. view-only mode). `should_capture()` returns True only when the
    remote window is focused (so local typing works everywhere else)."""

    def __init__(self, on_key, should_capture):
        self.on_key = on_key
        self.should_capture = should_capture
        self._hook = None
        self._proc = None

    def install(self):
        if not _IS_WIN or self._hook is not None:
            return
        self._proc = _HOOKPROC(self._callback)   # keep a ref so it isn't GC'd
        self._hook = _user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._proc, _kernel32.GetModuleHandleW(None), 0)
        if not self._hook:
            get_logger().warning("keyhook: SetWindowsHookEx failed")

    def uninstall(self):
        if self._hook:
            try:
                _user32.UnhookWindowsHookEx(self._hook)
            except Exception:
                pass
        self._hook = None
        self._proc = None

    def _callback(self, n_code, w_param, l_param):
        try:
            if n_code == HC_ACTION and self.should_capture():
                kb = ctypes.cast(l_param, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                vk = int(kb.vkCode)
                down = w_param in (WM_KEYDOWN, WM_SYSKEYDOWN)
                ext = bool(kb.flags & LLKHF_EXTENDED)
                # Let local Alt+Tab through so the user can escape capture.
                alt_down = _user32.GetAsyncKeyState(VK_MENU) & 0x8000
                if vk == VK_TAB and alt_down:
                    return _user32.CallNextHookEx(self._hook, n_code, w_param, l_param)
                # on_key decides: True -> swallow locally (sent to host or consumed
                # as a shortcut), False -> let it fall through to the local machine.
                if self.on_key(vk, down, ext):
                    return 1
        except Exception:
            get_logger().debug("keyhook: callback error", exc_info=True)
        return _user32.CallNextHookEx(self._hook, n_code, w_param, l_param)
