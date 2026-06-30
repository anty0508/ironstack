"""Single-instance guard: only one IronStack runs per user session.

A second launch detects the running instance, asks it to surface its window, and
exits. Built on QLocalServer/QLocalSocket (a named pipe on Windows), which the OS
cleans up automatically if the first instance crashes — so there's no stale lock.
"""

from PySide6 import QtCore, QtNetwork


class SingleInstance(QtCore.QObject):
    # Emitted in the primary instance when another launch pings it.
    activated = QtCore.Signal()

    def __init__(self, key, parent=None):
        super().__init__(parent)
        self._key = key
        self._server = None

    def is_primary(self):
        """True if this is the only instance. If another is already running, ping
        it (so it brings its window forward) and return False — the caller should
        then exit."""
        probe = QtNetwork.QLocalSocket()
        probe.connectToServer(self._key)
        if probe.waitForConnected(300):
            probe.write(b"show")
            probe.flush()
            probe.waitForBytesWritten(300)
            probe.disconnectFromServer()
            return False

        # Nobody answered -> we are the first. Clear any stale endpoint and listen.
        QtNetwork.QLocalServer.removeServer(self._key)
        self._server = QtNetwork.QLocalServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        self._server.listen(self._key)
        return True

    def _on_new_connection(self):
        conn = self._server.nextPendingConnection()
        if conn is not None:
            conn.close()
        self.activated.emit()
