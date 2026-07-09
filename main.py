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

import config
from config import MIN_TEXT_LENGTH
from logsetup import setup_logging, get_logger
from singleinstance import SingleInstance
from storage import database
from ui.overlay import Overlay, get_window_visibility_controller
from ui.launcher import Launcher, MessageDialog
from services.transcriber import Transcriber
from services.answerer import Answerer
from services.netlink import Broadcaster, NetController, NetClient, apply_message
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


class _MicConsole:
    """The microphone transcriber's 'console'. Live partials stream into the
    candidate's "You" bubble token by token (via the broadcaster's user_partial,
    which also mirrors to Viewers); status lines are dropped."""

    def __init__(self, console):
        self.console = console

    def partial(self, text):
        self.console.user_partial(text)

    def info(self, text):
        pass


class ConnectionApprover(QtCore.QObject):
    """Bridges a viewer's connection request (which arrives on a NetServer
    handshake thread) to an Accept/Reject dialog on the GUI thread, blocking the
    server thread until the host decides."""

    _requested = QtCore.Signal(object)   # carries a pending {peer, event, result} dict

    def __init__(self, parent_window):
        super().__init__()
        self._parent_window = parent_window
        self._requested.connect(self._on_requested)

    def approve(self, peer):
        """Called on a NetServer handshake thread; blocks until the host clicks
        (or a timeout auto-rejects)."""
        pending = {"peer": peer, "event": threading.Event(), "result": False}
        self._requested.emit(pending)
        if not pending["event"].wait(timeout=60):
            return False   # no decision in time -> reject
        return pending["result"]

    @QtCore.Slot(object)
    def _on_requested(self, pending):
        peer = pending["peer"]
        name = peer.get("name", "A viewer")
        ip = peer.get("ip", "?")
        try:
            dlg = MessageDialog(
                "Viewer wants to connect",
                f"“{name}” ({ip}) wants to view this live meeting.\n\n"
                "Allow the connection?",
                parent=self._parent_window,
                confirm_text="Accept", cancel_text="Reject")
            pending["result"] = dlg.exec() == QtWidgets.QDialog.Accepted
            if pending["result"]:
                # A viewer is now connected -> open the shared notepad on the host.
                try:
                    self._parent_window.set_connected(True)
                except Exception:
                    pass
        except Exception:
            get_logger().exception("approval dialog failed")
            pending["result"] = False
        finally:
            pending["event"].set()


def _run_interview(app, tray, system_prompt, meeting_id, start_geometry=None,
                   language_code="en", language_name="English", net=None):
    """Run one interview session against a fresh overlay window.

    Blocks on a nested event loop until the user ends the meeting or quits the
    app, then tears the session down. Returns True to go back to the setup
    window, or False to quit the whole app. `start_geometry` is the setup
    window's geometry, so the overlay opens in the same spot.

    When `net` is hosting, every display update is also streamed to connected
    Viewers via a `Broadcaster` that wraps the overlay.
    """
    log = get_logger()
    log.info("meeting started (meeting_id=%s, language=%s)", meeting_id, language_name)

    overlay = Overlay(start_geometry=start_geometry, language_code=language_code,
                      role="host")
    overlay.show()
    if tray is not None:
        tray.set_window(overlay)

    # Starting an interview automatically makes this PC a Host: open the server +
    # beacon so Viewers can find it. Each viewer that connects is approved by the
    # host through a dialog (ConnectionApprover). If the port is taken (another
    # host already running on this PC) we just run without networking.
    approver = ConnectionApprover(overlay)
    if net is not None:
        host_err = net.start_hosting(on_approve=approver.approve)
        if host_err:
            get_logger().warning("hosting unavailable: %s", host_err)
            overlay.info("[network] hosting unavailable (another host on this PC?)")

    # Everything the transcriber/answerer displays goes through this console. It
    # updates the local overlay and, when hosting, mirrors each call to Viewers.
    console = Broadcaster(overlay, server=net.server if net is not None else None)
    if net is not None:
        net.set_in_meeting(True)
        console.meeting_start(language=language_name)
        # Shared notepad: the host's local edits go out to viewers, and viewers'
        # edits (delivered via the server's on_message) update the host overlay.
        overlay.shared_text_changed.connect(console.share_text)
        # Remote control: a stealth toggle here syncs to every viewer.
        overlay.stealth_toggled.connect(
            lambda val: net.send({"type": "cmd", "action": "stealth", "value": val}))

    answerer = Answerer(console, system_prompt, meeting_id, language=language_name)
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
    transcriber = Transcriber(on_question=question_filter.feed, console=console,
                              language=language_code, source="loopback")

    # Second stream: the candidate's own microphone. Live words stream into the
    # "You" bubble via _MicConsole.partial; each finalized utterance locks the
    # bubble in and is saved. This does NOT trigger an AI answer. Finals go
    # straight through (no merge filter) so each utterance finalizes promptly.
    def on_user_speech(text):
        console.show_user_answer(text)
        try:
            database.add_message(meeting_id, "candidate", text)
        except Exception:
            get_logger().exception("failed to persist candidate line")

    mic_transcriber = Transcriber(on_question=on_user_speech, console=_MicConsole(console),
                                  language=language_code, source="microphone")

    # Refresh button (local or triggered remotely by a viewer): restart both
    # Deepgram streams when transcription silently stops picking up speech.
    def restart_transcription():
        # Show it on the LISTENING caption (console.partial also mirrors it to
        # viewers); the reconnect clears the caption when it comes back.
        console.partial("↻ Restarting Deepgram…")
        transcriber.restart()
        mic_transcriber.restart()

    overlay.refresh_requested.connect(restart_transcription)

    # Apply a language to the live pipeline: reconnect Deepgram in the new
    # language and have the answer model write in it too.
    def apply_language(code, name):
        answerer.set_language(name)
        transcriber.set_language(code)
        mic_transcriber.set_language(code)

    # Local picker change on the host: apply it AND tell the viewers so their
    # pickers reflect it.
    def on_language_changed(code, name):
        get_logger().info("language switched mid-meeting -> %s (%s)", name, code)
        apply_language(code, name)
        overlay.info(f"[language] {name}")
        if net is not None:
            net.send({"type": "cmd", "action": "language", "code": code, "name": name})

    overlay.language_changed.connect(on_language_changed)

    # A viewer changing language (or refresh/stealth) arrives here; apply the
    # language to the host pipeline and reflect it on the host's own picker.
    if net is not None and net.server is not None:
        net.server.on_message = lambda msg: _handle_message(
            overlay, msg, on_refresh=restart_transcription, on_language=apply_language)

    # Each transcriber runs its own asyncio loop on a background thread so the Qt
    # event loop owns the main thread.
    def run_transcriber(t, label):
        # Use a Selector event loop (not the default Proactor) on this thread.
        # The Proactor loop can crash with "Overlapped object still has pending
        # operation at deallocation" when we tear the websocket down at the end
        # of a meeting; the Selector loop has no overlapped I/O and shuts down
        # cleanly. It fully supports the TLS websocket we use.
        loop = asyncio.SelectorEventLoop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(t.run())
        except Exception as exc:
            get_logger().exception("%s transcription stopped", label)
            overlay.info(f"[error] {label} transcription stopped: {exc}")
        finally:
            try:
                loop.close()
            except Exception:
                pass

    threading.Thread(target=run_transcriber, args=(transcriber, "interviewer"),
                     daemon=True).start()
    threading.Thread(target=run_transcriber, args=(mic_transcriber, "microphone"),
                     daemon=True).start()

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

    # Teardown: stop the workers (releasing the audio devices) and the overlay's
    # hotkeys, then close the window before we loop back to the setup screen.
    stop.set()
    transcriber.stop()
    mic_transcriber.stop()
    if net is not None:
        console.meeting_end()
        net.set_in_meeting(False)
        net.stop_hosting()   # hosting lives only for the duration of the meeting
    overlay.shutdown()
    overlay.close()
    app.processEvents()   # let the window actually disappear before setup reopens

    log.info("meeting ended (meeting_id=%s, action=%s)", meeting_id,
             "quit app" if state["quit"] else "back to setup")
    return not state["quit"]


def _handle_message(overlay, msg, on_refresh=None, on_language=None):
    """Dispatch a received network message. Remote-control commands ('cmd') are
    handled here; everything else is a display update replayed onto the overlay.

    - stealth: apply the capture-exclusion state locally (so host and viewers
      stay in sync). Setting it via the controller does not re-emit, so there is
      no feedback loop.
    - refresh: restart transcription (host only; viewers have none).
    - language: reflect the new language on the local picker, and (host only)
      apply it to the live pipeline. Updating the picker is done with signals
      blocked, so it does not bounce back out."""
    if msg.get("type") == "cmd":
        action = msg.get("action")
        if action == "stealth":
            get_window_visibility_controller().set_capture_excluded(bool(msg.get("value")))
        elif action == "refresh" and on_refresh is not None:
            on_refresh()
        elif action == "language":
            code = msg.get("code")
            name = msg.get("name", "")
            overlay.set_language(code, name)
            if on_language is not None:
                on_language(code, name)
        return
    apply_message(overlay, msg)


def _run_viewer(app, tray, net, target, start_geometry=None):
    """Run as a Viewer: open an overlay and drive it from a Host's stream.

    No audio capture or answering happens here -- the overlay is a live mirror of
    the Host's meeting. Blocks until the user ends/quits; returns True to go back
    to setup, False to quit the app.
    """
    host, port = target
    log = get_logger()
    log.info("viewer connecting to %s:%s", host, port)

    overlay = Overlay(start_geometry=start_geometry, role="viewer")
    overlay.show()
    if tray is not None:
        tray.set_window(overlay)
    overlay.info(f"[viewer] connecting to {host}:{port} — waiting for host to accept…")

    # Received messages replay onto the overlay; overlay methods are thread-safe.
    def on_message(msg):
        # When the host ends the meeting, end the viewer session too.
        if msg.get("type") == "meeting_end":
            overlay.info("[host ended the meeting]")
            overlay.end_meeting.emit()
            return
        _handle_message(overlay, msg)

    def on_status(text):
        get_logger().info("viewer: %s", text)   # surface the outcome in the console too
        overlay.info(f"[viewer] {text}")
        if text.startswith("connected"):
            overlay.set_connected(True)   # host accepted -> open the shared notepad
        elif "host closed" in text:
            overlay.end_meeting.emit()    # host disappeared -> end the viewer too

    client = NetClient(
        host, port, name=net.name if net is not None else "viewer",
        on_message=on_message,
        on_status=on_status,
    )
    client.start()
    # Shared notepad: the viewer's local edits go back to the host.
    overlay.shared_text_changed.connect(
        lambda text: client.send({"type": "shared_text", "text": text}))
    # Remote control from the viewer: Stealth syncs both ways; Refresh restarts
    # the host's Deepgram (the viewer has none of its own).
    overlay.stealth_toggled.connect(
        lambda val: client.send({"type": "cmd", "action": "stealth", "value": val}))

    def on_refresh_clicked():
        overlay.partial("↻ Restarting Deepgram…")
        client.send({"type": "cmd", "action": "refresh"})

    overlay.refresh_requested.connect(on_refresh_clicked)
    # Language change on the viewer -> tell the host to switch its Deepgram +
    # answer language (and the host relays it to any other viewers).
    overlay.language_changed.connect(
        lambda code, name: client.send(
            {"type": "cmd", "action": "language", "code": code, "name": name}))

    loop = QtCore.QEventLoop()
    state = {"quit": False}

    def on_quit():
        state["quit"] = True
        loop.quit()

    overlay.end_meeting.connect(loop.quit)
    overlay.quit_app.connect(on_quit)
    if tray is not None:
        tray.set_quit_handler(on_quit)
    loop.exec()

    client.stop()
    overlay.shutdown()
    overlay.close()
    app.processEvents()

    log.info("viewer ended (action=%s)", "quit app" if state["quit"] else "back to setup")
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
    # App-level tooltip style so every tooltip is dark-themed and readable, even
    # on buttons that carry their own inline stylesheet (where an ancestor's
    # QToolTip rule would otherwise not apply).
    app.setStyleSheet(
        "QToolTip { background-color: #1c2029; color: #e8eaf0;"
        " border: 1px solid rgba(242,163,60,0.55); border-radius: 6px;"
        " padding: 4px 8px; font-size: 12px; }"
    )

    # One instance per user: a second launch pings the running one (which brings
    # its window to the front) and exits immediately. IRONSTACK_INSTANCE makes
    # the mutex name unique so a tagged process can run alongside the primary
    # (for local Host<->Viewer testing on one machine).
    mutex_name = "IronStack-" + _username()
    if config.INSTANCE_ID:
        mutex_name += "-" + config.INSTANCE_ID
    single = SingleInstance(mutex_name)
    if not single.is_primary():
        return 0

    log = setup_logging()
    log.info("=== IronStack starting ===")
    _install_qt_message_logging()

    icon = make_app_icon()
    app.setWindowIcon(icon)

    database.init_db()

    # LAN networking (discovery + Host/Viewer streaming). One controller for the
    # whole session, so a host keeps running across the setup <-> meeting loop.
    net = NetController(database)

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
        # Pre-interview setup: pick documents, edit preferences, review meetings,
        # host to / join from the network.
        launcher = Launcher(hide_from_taskbar=tray_available, net=net)
        if tray is not None:
            tray.set_window(launcher)
            tray.set_quit_handler(launcher.reject)   # tray "Quit" == close setup
        if not _run_setup(launcher):
            break   # user quit from the setup window

        start_geometry = launcher.frameGeometry()

        if launcher.mode == "viewer":
            # Join a Host's meeting and mirror it; no local capture/answering.
            if not _run_viewer(app, tray, net, launcher.viewer_target, start_geometry):
                break   # user chose Quit while viewing
            continue

        # Host / solo: open the interview overlay where the setup window sits.
        if not _run_interview(app, tray, launcher.system_prompt,
                              launcher.meeting_id, start_geometry,
                              language_code=launcher.language_code,
                              language_name=launcher.language_name,
                              net=net):
            break   # user chose Quit during the interview

    net.shutdown()
    if tray is not None:
        tray.hide()
    log.info("=== IronStack exiting ===")
    return 0


if __name__ == "__main__":
    main()
