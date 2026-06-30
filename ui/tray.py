"""System-tray presence for the app.

The app's windows are tool windows (kept off the taskbar for stealth), so the
tray icon is the user's handle on the running app: left-click toggles the
window in front, and the menu offers Show/Hide and Quit.
"""

from PySide6 import QtCore, QtGui, QtWidgets

import config
from logsetup import get_logger

Qt = QtCore.Qt


def make_app_icon():
    """The app icon: the committed assets/IronStack.ico (the indigo->violet 'IS'
    monogram). The .ico carries every size Windows needs (16-256px), and is the
    single source of truth — the same file is embedded into the built .exe."""
    return QtGui.QIcon(str(config.resource_path("assets", "IronStack.ico")))


class AppTray(QtCore.QObject):
    """Tray icon that controls whichever window is currently in front."""

    def __init__(self, icon, parent=None):
        super().__init__(parent)
        self._window = None
        self._on_quit = None

        self.tray = QtWidgets.QSystemTrayIcon(icon, self)
        self.tray.setToolTip("IronStack")

        # Keep a reference to the menu: QSystemTrayIcon.setContextMenu does not
        # take ownership, so a local would be garbage-collected and take its
        # actions down with it.
        self._menu = QtWidgets.QMenu()
        self._toggle_action = self._menu.addAction("Hide")
        self._toggle_action.triggered.connect(self.toggle_window)
        self._menu.addSeparator()
        quit_action = self._menu.addAction("Quit")
        quit_action.triggered.connect(self._quit)
        self.tray.setContextMenu(self._menu)

        self.tray.activated.connect(self._on_activated)
        self.tray.show()

    def set_window(self, window):
        """Point the tray at the window currently on screen (setup or overlay)."""
        self._window = window
        # The window may not be shown yet (a dialog is shown by exec()); refresh
        # the menu label once the event loop has had a chance to display it.
        QtCore.QTimer.singleShot(0, self._refresh)

    def set_quit_handler(self, fn):
        """Set what 'Quit' does in the current phase (close setup / end app)."""
        self._on_quit = fn

    def toggle_window(self, *_):
        w = self._window
        if w is None:
            return
        try:
            if w.isVisible():
                w.hide()
            else:
                w.show()
                w.raise_()
                w.activateWindow()
            self._refresh()
        except RuntimeError:
            pass   # window's C++ object is gone; nothing to toggle

    def show_window(self, *_):
        """Bring the current window to the front (e.g. when a second launch of
        the app pings this instance)."""
        w = self._window
        if w is None:
            return
        try:
            w.show()
            w.raise_()
            w.activateWindow()
            self._refresh()
        except RuntimeError:
            pass

    def hide(self):
        self.tray.hide()

    def _on_activated(self, reason):
        # Left-click or double-click toggles; right-click opens the menu. Never
        # let an exception escape a Qt slot — in PySide6 that aborts the app.
        try:
            if reason in (QtWidgets.QSystemTrayIcon.Trigger,
                          QtWidgets.QSystemTrayIcon.DoubleClick):
                self.toggle_window()
        except Exception:
            get_logger().exception("tray activation handler failed")

    def _refresh(self):
        try:
            w = self._window
            visible = bool(w is not None and w.isVisible())
            self._toggle_action.setText("Hide" if visible else "Show")
        except RuntimeError:
            pass

    def _quit(self, *_):
        if self._on_quit is not None:
            self._on_quit()
