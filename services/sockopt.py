"""Cross-platform TCP keepalive helper shared by the streaming services.

Used on every long-lived socket (clip/audio/relay) once its handshake is done
and its read timeout has been reset to blocking (see ClipboardClient._run,
AudioClient._run, relaylink.dial_via_relay/_bridge): blocking mode alone means
a link that goes silently dead -- cable pulled, Wi-Fi drops without a clean
FIN/RST -- is only noticed once the OS's own (often multi-minute) TCP defaults
kick in. TCP keepalive bounds that to roughly idle + interval*count seconds.
"""

import socket
import sys

_KEEPALIVE_IDLE = 10      # seconds of silence before the first probe
_KEEPALIVE_INTERVAL = 3   # seconds between probes
_KEEPALIVE_COUNT = 3      # probes before the connection is declared dead


def enable_keepalive(sock, idle=_KEEPALIVE_IDLE, interval=_KEEPALIVE_INTERVAL,
                     count=_KEEPALIVE_COUNT):
    """Best-effort; does nothing on a platform that doesn't expose the knobs."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        return
    if sys.platform == "win32":
        try:
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, idle * 1000, interval * 1000))
        except (AttributeError, OSError):
            pass
    else:
        for opt, val in ((getattr(socket, "TCP_KEEPIDLE", None), idle),
                         (getattr(socket, "TCP_KEEPINTVL", None), interval),
                         (getattr(socket, "TCP_KEEPCNT", None), count)):
            if opt is not None:
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, opt, val)
                except OSError:
                    pass
