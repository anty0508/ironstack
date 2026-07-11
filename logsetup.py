"""Application logging. Logs to the console (stderr) only — no log file is
written.

Call setup_logging() once at startup; use get_logger() everywhere else.
setup_logging() also installs global hooks so *uncaught* exceptions — on the
main thread and on worker threads — always reach the log instead of vanishing.
"""

import logging
import sys
import threading
import faulthandler

_NAME = "ironstack"
_configured = False


def setup_logging():
    """Configure the app logger to write to the console and install global
    exception hooks. Idempotent, and never raises — logging must not stop the
    app from starting."""
    global _configured
    logger = logging.getLogger(_NAME)
    if _configured:
        return logger

    try:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(threadName)s: %(message)s")
        )
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.propagate = False
    except Exception:
        pass

    # Dump a C-level traceback on a hard crash (segfault / access violation /
    # abort) — e.g. a Qt call from the wrong thread — instead of the process just
    # vanishing. Writes to the console (visible in the -Debug build).
    try:
        if sys.stderr is not None:
            faulthandler.enable()
    except Exception:
        pass

    _install_excepthooks(logger)
    _configured = True
    return logger


def _install_excepthooks(logger):
    """Route uncaught exceptions (main thread + worker threads) to the log."""

    def main_hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logger.critical("UNCAUGHT exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = main_hook

    def thread_hook(args):
        if issubclass(args.exc_type, SystemExit):
            return
        name = args.thread.name if args.thread else "?"
        logger.error("UNCAUGHT exception in thread %s", name,
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    try:
        threading.excepthook = thread_hook   # Python 3.8+
    except Exception:
        pass


def get_logger():
    """The shared app logger (call setup_logging() once at startup first)."""
    return logging.getLogger(_NAME)
