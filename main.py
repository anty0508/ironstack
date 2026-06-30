import sys
import ctypes
import queue
import asyncio
import threading
import getpass

# Windows COM apartment: claim STA for the main thread BEFORE importing anything
# that touches COM. Qt's QApplication calls OleInitialize(), which requires a
# single-threaded apartment (STA). But `soundcard` (pulled in transitively via
# `transcriber`) initializes COM as multithreaded (MTA) at import time. Whichever
# runs first wins the apartment for this thread, and if soundcard wins, Qt fails
# to start with:
#   OleInitialize() failed: "COM error 0x80010106: Cannot change thread mode
#   after it is set."
# Claiming STA here first makes soundcard's import-time call detect the existing
# apartment and continue harmlessly, while Qt gets the STA it needs. The audio
# capture thread sets up its own MTA apartment (see transcriber._capture_worker).
if sys.platform == "win32":
    ctypes.windll.ole32.CoInitialize(None)

from PySide6 import QtCore, QtWidgets

from config import MIN_TEXT_LENGTH
from logsetup import setup_logging, get_logger
from singleinstance import SingleInstance
from storage import database
from ui.overlay import Overlay
from ui.launcher import Launcher
from services.transcriber import Transcriber
from services.answerer import Answerer
from ui.tray import AppTray, make_app_icon


def _username():
    try:
        return getpass.getuser()
    except Exception:
        return "user"


def _install_qt_message_logging():
    """Route Qt's own warnings/errors into the log (layout issues, paint
    warnings, native-window complaints, etc.)."""
    def handler(mode, ctx, message):
        log = get_logger()
        if mode in (QtCore.QtMsgType.QtCriticalMsg, QtCore.QtMsgType.QtFatalMsg):
            log.error("Qt: %s", message)
        elif mode == QtCore.QtMsgType.QtWarningMsg:
            log.warning("Qt: %s", message)
        else:
            log.debug("Qt: %s", message)
    QtCore.qInstallMessageHandler(handler)


class QuestionFilter:
    """Merge very short transcript fragments into the next one before
    enqueuing them for an answer."""

    def __init__(self, enqueue):
        self.enqueue = enqueue
        self.pending = ""

    def feed(self, text: str):
        text = " ".join(text.split()).strip()
        if not text:
            return

        if self.pending:
            text = f"{self.pending} {text}".strip()
            self.pending = ""

        if len(text) < MIN_TEXT_LENGTH:
            self.pending = text
            return

        self.enqueue(text)


def _run_interview(app, tray, system_prompt, meeting_id, start_geometry=None,
                   language_code="en", language_name="English"):
    """Run one interview session against a fresh overlay window.

    Blocks on a nested event loop until the user ends the meeting or quits the
    app, then tears the session down. Returns True to go back to the setup
    window, or False to quit the whole app. `start_geometry` is the setup
    window's geometry, so the overlay opens in the same spot.
    """
    log = get_logger()
    log.info("meeting started (meeting_id=%s, language=%s)", meeting_id, language_name)

    overlay = Overlay(start_geometry=start_geometry, language_code=language_code)
    overlay.show()
    if tray is not None:
        tray.set_window(overlay)

    answerer = Answerer(overlay, system_prompt, meeting_id, language=language_name)
    answer_queue = queue.Queue()
    stop = threading.Event()

    # Answer in a worker thread so generating an answer never blocks the
    # audio capture / transcription loop. The worker owns each Q/A block, so
    # answers are produced one at a time and never overlap.
    def answer_worker():
        while not stop.is_set():
            try:
                question = answer_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                answerer.answer(question)
            except Exception as exc:
                get_logger().exception("answer failed")
                overlay.info(f"[error] answer failed: {exc}")

    threading.Thread(target=answer_worker, daemon=True).start()

    question_filter = QuestionFilter(answer_queue.put)
    transcriber = Transcriber(on_question=question_filter.feed, console=overlay,
                              language=language_code)

    # Live language switch from the overlay's picker: reconnect Deepgram in the
    # new language and have the answer model write in it too.
    def on_language_changed(code, name):
        get_logger().info("language switched mid-meeting -> %s (%s)", name, code)
        answerer.set_language(name)
        transcriber.set_language(code)
        overlay.info(f"[language] {name}")

    overlay.language_changed.connect(on_language_changed)

    # Transcription runs its own asyncio loop on a background thread so the Qt
    # event loop owns the main thread.
    def run_transcriber():
        # Use a Selector event loop (not the default Proactor) on this thread.
        # The Proactor loop can crash with "Overlapped object still has pending
        # operation at deallocation" when we tear the websocket down at the end
        # of a meeting; the Selector loop has no overlapped I/O and shuts down
        # cleanly. It fully supports the TLS websocket we use.
        loop = asyncio.SelectorEventLoop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(transcriber.run())
        except Exception as exc:
            get_logger().exception("transcription stopped")
            overlay.info(f"[error] transcription stopped: {exc}")
        finally:
            try:
                loop.close()
            except Exception:
                pass

    threading.Thread(target=run_transcriber, daemon=True).start()

    overlay.info("[ready] listening to system audio — answers appear automatically")

    # Wait here until the overlay asks to end the meeting or quit the app. This
    # nested loop lets "End Meeting" return control to the setup window without
    # tearing down the whole application.
    loop = QtCore.QEventLoop()
    state = {"quit": False}

    def on_quit():
        state["quit"] = True
        loop.quit()

    overlay.end_meeting.connect(loop.quit)
    overlay.quit_app.connect(on_quit)
    if tray is not None:
        tray.set_quit_handler(on_quit)   # tray "Quit" during a meeting == quit app
    loop.exec()

    # Teardown: stop the workers (releasing the audio device) and the overlay's
    # hotkeys, then close the window before we loop back to the setup screen.
    stop.set()
    transcriber.stop()
    overlay.shutdown()
    overlay.close()
    app.processEvents()   # let the window actually disappear before setup reopens

    log.info("meeting ended (meeting_id=%s, action=%s)", meeting_id,
             "quit app" if state["quit"] else "back to setup")
    return not state["quit"]


def _run_setup(launcher):
    """Show the setup window and wait for the user to start an interview or quit.

    Uses a nested event loop on the dialog's `finished` signal instead of
    QDialog.exec(): exec() ends the instant the window is hidden, so hiding the
    window to the tray would look like quitting. With our own loop, only "Start
    interview" (accept) or Quit/close (reject) ends setup — hiding to the tray
    leaves it running. Returns True if the user started an interview.
    """
    loop = QtCore.QEventLoop()
    result = {"code": QtWidgets.QDialog.Rejected}

    def on_finished(code):
        result["code"] = code
        loop.quit()

    get_logger().info("setup window shown")
    launcher.finished.connect(on_finished)
    launcher.show()
    launcher.raise_()
    launcher.activateWindow()
    loop.exec()
    accepted = result["code"] == QtWidgets.QDialog.Accepted
    get_logger().info("setup closed: %s", "start interview" if accepted else "quit/close")
    return accepted


def main():
    app = QtWidgets.QApplication(sys.argv)
    # The setup/interview loop below drives the window lifecycle explicitly, so
    # don't let Qt auto-quit when a window closes between the two phases (or when
    # the window is hidden to the tray).
    app.setQuitOnLastWindowClosed(False)

    # One instance per user: a second launch pings the running one (which brings
    # its window to the front) and exits immediately.
    single = SingleInstance("IronStack-" + _username())
    if not single.is_primary():
        return 0

    log = setup_logging()
    log.info("=== IronStack starting ===")
    _install_qt_message_logging()

    icon = make_app_icon()
    app.setWindowIcon(icon)

    database.init_db()

    # The windows are kept off the taskbar; the tray icon is how the user gets
    # to the app. Only hide from the taskbar if a tray is actually available,
    # so the windows can never become unreachable.
    tray_available = QtWidgets.QSystemTrayIcon.isSystemTrayAvailable()
    tray = AppTray(icon) if tray_available else None
    log.info("system tray available: %s", tray_available)
    if tray is not None:
        single.activated.connect(tray.show_window)
    single.activated.connect(
        lambda: get_logger().info("second launch detected -> surfacing window"))

    # Setup -> interview -> setup -> ... The launcher (main window) and the
    # interview overlay take turns: "End Meeting" returns here to show setup
    # again; the loop ends only when the user quits from either screen.
    while True:
        # Pre-interview setup: pick documents, edit preferences, review meetings.
        launcher = Launcher(hide_from_taskbar=tray_available)
        if tray is not None:
            tray.set_window(launcher)
            tray.set_quit_handler(launcher.reject)   # tray "Quit" == close setup
        if not _run_setup(launcher):
            break   # user quit from the setup window

        # Open the interview overlay where the setup window currently sits.
        start_geometry = launcher.frameGeometry()
        if not _run_interview(app, tray, launcher.system_prompt,
                              launcher.meeting_id, start_geometry,
                              language_code=launcher.language_code,
                              language_name=launcher.language_name):
            break   # user chose Quit during the interview

    if tray is not None:
        tray.hide()
    log.info("=== IronStack exiting ===")
    return 0


if __name__ == "__main__":
    main()
