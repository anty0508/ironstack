import re
import sys
import time
import ctypes
import queue
import asyncio
import threading
import getpass
from collections import deque

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


class EchoGuard:
    """The interviewer's voice plays out of the speakers and bleeds into the
    mic, so the mic stream transcribes it too -- producing a duplicate "You"
    bubble on the viewers. Remember what the interviewer stream said recently
    and treat a mic utterance that echoes it as a duplicate to drop.

    A mic phrase is considered an echo when almost all of its words appear in a
    single recent interviewer utterance; a genuine answer that only reuses a few
    of the question's keywords stays below that bar and is kept."""

    _WORD = re.compile(r"\w+", re.UNICODE)

    def __init__(self, window_s=8.0, containment=0.8):
        self._window = window_s
        self._containment = containment
        self._recent = deque()          # (monotonic_ts, frozenset(tokens))
        self._lock = threading.Lock()

    @classmethod
    def _tokens(cls, text):
        return cls._WORD.findall((text or "").lower())

    def _prune(self, now):
        cutoff = now - self._window
        while self._recent and self._recent[0][0] < cutoff:
            self._recent.popleft()

    def note(self, text):
        """Record something the interviewer stream just said (interim or final)."""
        toks = frozenset(self._tokens(text))
        if not toks:
            return
        now = time.monotonic()
        with self._lock:
            self._recent.append((now, toks))
            self._prune(now)

    def is_echo(self, text):
        toks = self._tokens(text)
        if len(toks) < 2:
            return False   # too short to tell an echo from a real one-word reply
        tokset = set(toks)
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            for _, itoks in self._recent:
                if len(tokset & itoks) / len(tokset) >= self._containment:
                    return True
        return False


class _EchoTap:
    """Wraps the interviewer transcriber's console so its live text also feeds
    the EchoGuard, while every call still reaches the real console unchanged."""

    def __init__(self, console, guard):
        self._console = console
        self._guard = guard

    def partial(self, text):
        self._guard.note(text)
        self._console.partial(text)

    def __getattr__(self, name):
        return getattr(self._console, name)


class _MicConsole:
    """The microphone transcriber's 'console'. Live partials stream into the
    candidate's "You" bubble token by token (via the broadcaster's user_partial,
    which also mirrors to Viewers); status lines are dropped. Partials that echo
    the interviewer's voice are suppressed so no ghost bubble streams in."""

    def __init__(self, console, guard=None):
        self.console = console
        self.guard = guard

    def partial(self, text):
        if self.guard is not None and self.guard.is_echo(text):
            return
        self.console.user_partial(text)

    def info(self, text):
        pass


class ConnectionApprover(QtCore.QObject):
    """Bridges a viewer's connection request (which arrives on a NetServer
    handshake thread) to an Accept/Reject dialog on the GUI thread, blocking the
    server thread until the host decides."""

    _requested = QtCore.Signal(object)   # carries a pending {peer, event, result} dict

    def __init__(self, on_accepted=None):
        super().__init__()
        self._on_accepted = on_accepted
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
            # Parent to whatever window is active now (setup or interview), since
            # the server accepts connections for the whole app run.
            dlg = MessageDialog(
                "Viewer wants to connect",
                f"“{name}” ({ip}) wants to connect to this machine.\n\n"
                "Allow the connection?",
                parent=QtWidgets.QApplication.activeWindow(),
                confirm_text="Accept", cancel_text="Reject")
            pending["result"] = dlg.exec() == QtWidgets.QDialog.Accepted
            if pending["result"] and self._on_accepted is not None:
                try:
                    self._on_accepted()
                except Exception:
                    pass
        except Exception:
            get_logger().exception("approval dialog failed")
            pending["result"] = False
        finally:
            pending["event"].set()


class HostSession:
    """Owns the host's network servers for the whole app run.

    The control + media sockets open at app START (not per-interview), so this PC
    is discoverable/reachable and can be remote-controlled from launch. An
    interview just registers its overlay/console here so transcript data flows to
    already-connected viewers; ending it clears that registration but leaves the
    sockets open."""

    def __init__(self, net):
        self.net = net
        self.approver = ConnectionApprover(on_accepted=self._on_viewer_accepted)
        self.overlay = None
        self.console = None
        self.on_refresh = None
        self.on_language = None
        self._remote = None            # RemoteScreenServer (TCP)
        self.remote_injector = None    # InputInjector for the shared monitor
        self._audio = None             # AudioServer (TCP) — live meeting audio

    # --- lifecycle ---
    def start(self):
        """Open the control server + beacon (+ UPnP) and the TCP screen server."""
        err = self.net.start_hosting(on_approve=self.approver.approve)
        if err:
            get_logger().warning("hosting unavailable at startup: %s", err)
            return
        if self.net.server is not None:
            self.net.server.on_message = self._dispatch
        self._start_remote()
        self._start_audio()
        # If relay reachability is enabled, park outbound connections at the relay
        # so viewers can reach this host by its ID over the internet (CGNAT-proof).
        self.net.start_relay()

    def _start_remote(self):
        if self._remote is not None:
            return
        try:
            import secrets
            from services.remote import RemoteScreenServer
            from services.inputinject import InputInjector

            def on_session(geometry):
                # Viewer chose a monitor -> injector for it, so input (arriving
                # over the reliable TCP control channel) maps to that display.
                self.remote_injector = InputInjector(*geometry)

            srv = RemoteScreenServer(self.net.media_port, secrets.token_hex(8),
                                     on_session=on_session)
            srv.start()
            self._remote = srv
            self.net.map_extra_port(self.net.media_port, protocol="TCP")
        except Exception:
            get_logger().exception("remote: screen server failed to start")

    def _start_audio(self):
        if self._audio is not None:
            return
        try:
            import secrets
            from services.audiostream import AudioServer
            srv = AudioServer(self.net.audio_port, secrets.token_hex(8))
            srv.start()
            self._audio = srv
            self.net.map_extra_port(self.net.audio_port, protocol="TCP")
        except Exception:
            get_logger().exception("audio: server failed to start")

    def stop(self):
        if self._remote is not None:
            self._remote.stop()
        if self._audio is not None:
            self._audio.stop()
        self.net.stop_hosting()

    # --- interview registration ---
    def begin_interview(self, overlay, console, on_refresh, on_language):
        self.overlay = overlay
        self.console = console
        self.on_refresh = on_refresh
        self.on_language = on_language

    def end_interview(self):
        self.overlay = self.console = self.on_refresh = self.on_language = None

    # --- callbacks ---
    def _on_viewer_accepted(self):
        if self.overlay is not None:
            try:
                self.overlay.set_connected(True)   # open the shared notepad
            except Exception:
                pass

    def _offer_remote(self):
        # A viewer asked to view/control -> rotate the token and offer the media
        # port + monitor list over the control channel; the viewer connects the
        # TCP screen socket, and input flows back over this control channel.
        self._start_remote()
        if self._remote is None:
            return
        import secrets
        from services import screencap
        token = secrets.token_hex(8)
        self._remote.token = token
        try:
            monitors = screencap.enumerate_monitors()
        except Exception:
            monitors = [{"id": 1, "name": "Display 1"}]
        self.net.send({"type": "remote_offer", "media_port": self.net.media_port,
                       "token": token, "monitors": monitors})
        get_logger().info("remote: offered screen to viewer (media port %d)",
                          self.net.media_port)

    def _offer_audio(self):
        # A viewer wants to hear the meeting -> rotate the audio token and offer
        # the audio port; the viewer connects the audio socket and plays the mix.
        self._start_audio()
        if self._audio is None:
            return
        import secrets
        token = secrets.token_hex(8)
        self._audio.token = token
        self.net.send({"type": "audio_offer", "audio_port": self.net.audio_port,
                       "token": token})
        get_logger().info("audio: offered stream to viewer (audio port %d)",
                          self.net.audio_port)

    def _dispatch(self, msg):
        """Host-side handler for messages a viewer sends back (runs for the whole
        app run, not just during an interview)."""
        t = msg.get("type")
        if t == "cmd":
            action = msg.get("action")
            if action == "stealth":
                get_window_visibility_controller().set_capture_excluded(
                    bool(msg.get("value")))
            elif action == "refresh" and self.on_refresh is not None:
                self.on_refresh()
            elif action == "language":
                if self.overlay is not None:
                    self.overlay.set_language(msg.get("code"), msg.get("name", ""))
                if self.on_language is not None:
                    self.on_language(msg.get("code"), msg.get("name", ""))
            elif action == "remote_start":
                self._offer_remote()
            elif action == "remote_kf":
                if self._remote is not None:
                    self._remote.request_keyframe()
            elif action == "audio_start":
                self._offer_audio()
            return
        if t == "input":
            # Remote-control input over the reliable TCP control channel -> inject
            # on the shared monitor (dropped until a session set up the injector).
            inj = self.remote_injector
            if inj is not None:
                inj.apply(msg)
            return
        if t == "shared_text" and self.overlay is not None:
            self.overlay.set_shared_text(msg.get("text", ""))


def _run_interview(app, tray, system_prompt, meeting_id, start_geometry=None,
                   language_code="en", language_name="English", net=None,
                   host_session=None):
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

    # The host servers are already open (opened at app launch by HostSession).
    # This meeting just streams its data to whoever is connected: the console
    # updates the local overlay and mirrors each call to connected Viewers.
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

    # The interviewer's voice bleeds from the speakers into the mic, so the mic
    # stream would transcribe it too and show a duplicate "You" bubble. The guard
    # remembers the interviewer's recent words (fed via _EchoTap) so the mic
    # stream can drop utterances that echo them.
    echo_guard = EchoGuard()

    question_filter = QuestionFilter(answer_queue.put)
    transcriber = Transcriber(on_question=question_filter.feed,
                              console=_EchoTap(console, echo_guard),
                              language=language_code, source="loopback")

    # Second stream: the candidate's own microphone. Live words stream into the
    # "You" bubble via _MicConsole.partial; each finalized utterance locks the
    # bubble in and is saved. This does NOT trigger an AI answer. Finals go
    # straight through (no merge filter) so each utterance finalizes promptly.
    def on_user_speech(text):
        if echo_guard.is_echo(text):
            # Echo of the interviewer picked up by the mic: drop it and clear any
            # partial "You" bubble that started streaming before we were sure.
            get_logger().info("mic: dropped echoed interviewer speech: %r", text)
            console.discard_user_answer()
            return
        console.show_user_answer(text)
        try:
            database.add_message(meeting_id, "candidate", text)
        except Exception:
            get_logger().exception("failed to persist candidate line")

    mic_transcriber = Transcriber(on_question=on_user_speech,
                                  console=_MicConsole(console, echo_guard),
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

    # Register this meeting with the always-on host session, so viewer messages
    # (refresh / language / shared note) reach this overlay and pipeline. Remote
    # control, stealth and connection approval are handled by the session
    # regardless of whether an interview is running.
    if host_session is not None:
        host_session.begin_interview(overlay, console, restart_transcription,
                                     apply_language)

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
    if host_session is not None:
        host_session.end_interview()   # servers stay open; just unregister
    if net is not None:
        console.meeting_end()
        net.set_in_meeting(False)
    overlay.shutdown()
    overlay.close()
    app.processEvents()   # let the window actually disappear before setup reopens

    log.info("meeting ended (meeting_id=%s, action=%s)", meeting_id,
             "quit app" if state["quit"] else "back to setup")
    return not state["quit"]


def _handle_message(overlay, msg, on_refresh=None, on_language=None):
    """Viewer-side dispatch of a received message. Commands the host relays
    (stealth / language) are reflected locally; everything else is a display
    update replayed onto the overlay. (Host-side handling lives in
    HostSession._dispatch.)"""
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


class _RemoteBridge(QtCore.QObject):
    """Marshals a remote-control offer from the network receive thread to the GUI
    thread, where the remote-desktop window must be created."""
    offer = QtCore.Signal(object)


class _MeetingRecorder:
    """Viewer-side transcript recorder. Persists the meeting it's mirroring into
    the same local DB (meetings + messages) the host uses and with the same roles
    ('interviewer' / 'me' / 'candidate'), so a viewed interview shows up in the
    'Past meetings' history exactly like a hosted one.

    The meeting row is created lazily on the first content line — so joining a
    meeting already in progress still records — and closed on meeting_end /
    meeting_start, so each host interview becomes its own row. All calls arrive
    serially on the network receive thread; the DB uses short-lived connections."""

    def __init__(self):
        self._meeting_id = None
        self._answer = None       # str while an AI answer streams, else None
        self._host_name = ""

    def set_host_name(self, name):
        self._host_name = (name or "").strip()

    def _ensure_meeting(self):
        if self._meeting_id is None:
            try:
                self._meeting_id = database.create_meeting(
                    "Viewed interview", self._host_name, [])
            except Exception:
                get_logger().exception("viewer: could not create meeting record")
        return self._meeting_id

    def _save(self, role, content):
        content = (content or "").strip()
        if not content:
            return
        mid = self._ensure_meeting()
        if mid is None:
            return
        try:
            database.add_message(mid, role, content)
        except Exception:
            get_logger().exception("viewer: could not save %s line", role)

    def finalize(self):
        # Flush a half-streamed answer, then close the meeting so the next one
        # starts a fresh row.
        if self._answer:
            self._save("me", self._answer)
        self._answer = None
        self._meeting_id = None

    def record(self, msg):
        t = msg.get("type")
        if t == "question":
            self._save("interviewer", msg.get("text", ""))
        elif t == "answer_begin":
            self._answer = ""
        elif t == "answer_delta":
            if self._answer is not None:
                self._answer += msg.get("delta", "")
        elif t == "answer_end":
            if self._answer is not None:
                self._save("me", self._answer)   # skipped if empty (e.g. NO_ANSWER)
                self._answer = None
        elif t == "user_answer":
            self._save("candidate", msg.get("text", ""))
        elif t in ("meeting_start", "meeting_end"):
            self.finalize()


def _run_viewer(app, tray, net, target, start_geometry=None):
    """Run as a Viewer: the host's shared SCREEN is the primary window; the
    interview transcript is a secondary window opened from its toolbar.

    The remote-screen window opens immediately and shows the connection status
    (relay -> host -> connected) until the host's screen streams in. Closing it
    ends the viewer session; the host ending a meeting does NOT (the connection
    and screen share stay live). Returns True to go back to setup, False to quit.
    """
    log = get_logger()

    # target: {"mode":"relay","relay_host":h,"relay_port":p,"room":<host id>}.
    # The viewer reaches the host through the relay by ID — no direct connection.
    from services import relaylink
    from ui.remoteview import RemoteView
    rhost, rport, room = target["relay_host"], target["relay_port"], target["room"]
    password = target.get("password", "")   # host's unattended-access password, if any
    host, port = room, 0     # placeholders; the dialers below do the connecting
    screen_connect = lambda: relaylink.dial_via_relay(rhost, rport, room, "screen")
    where = f"id {room} via relay {rhost}:{rport}"
    log.info("viewer connecting to %s", where)

    # Report relay-vs-host connection phases on the screen window's banner.
    def on_conn_phase(code):
        text = {
            "relay_connecting": "Connecting to relay server…",
            "relay_failed": "Relay server connection failed",
            "host_connecting": "Connecting to host…",
            "host_failed": "Host connection failed",
        }.get(code)
        if text:
            remote.set_connection_status(text)

    control_dialer = lambda: relaylink.dial_via_relay(
        rhost, rport, room, "control", on_phase=on_conn_phase)

    # The transcript window: created hidden, shown via the screen's "Transcript"
    # button. It's a live mirror of the host's meeting; no capture/answering here.
    overlay = Overlay(start_geometry=start_geometry, role="viewer")

    def show_transcript():
        overlay.show()
        overlay.raise_()
        overlay.activateWindow()

    # Live meeting audio played on this PC's speakers (one client, re-offered on
    # each (re)connect so it always uses a fresh token).
    audio = {"client": None, "muted": False}

    # Mute the host's live audio locally (client-side only; the host keeps
    # transcribing). State is remembered so it survives audio reconnects.
    def on_mute_toggled(muted):
        audio["muted"] = muted
        if audio["client"] is not None:
            audio["client"].set_muted(muted)

    # The primary window: the host's screen. Opens now, before anything connects.
    remote = RemoteView(
        host,
        send_input=lambda ev: client.send({"type": "input", **ev}),
        connect_screen=screen_connect,
        on_open_transcript=show_transcript,
        on_toggle_mute=on_mute_toggled)
    remote.set_connection_status("Connecting to relay server…")
    remote.show()
    if tray is not None:
        tray.set_window(remote)

    def open_audio(msg):
        try:
            from services.audiostream import AudioClient
            if audio["client"] is not None:
                audio["client"].stop()
            ac = AudioClient(
                room, 0, msg.get("token"),
                connect=lambda: relaylink.dial_via_relay(rhost, rport, room, "audio"),
                on_status=lambda s: get_logger().info("viewer audio: %s", s))
            ac.set_muted(audio["muted"])   # carry the mute state across reconnects
            ac.start()
            audio["client"] = ac
        except Exception:
            get_logger().exception("audio: could not start playback (deps missing?)")

    remote_bridge = _RemoteBridge()

    def open_remote(msg):
        # GUI thread: start (or restart) the screen stream on the offer. Guard so
        # a missing dep (e.g. PyAV) shows a message on the banner, not a crash.
        monitors = msg.get("monitors") or [{"id": 1, "name": "Display 1"}]
        try:
            remote.start_screen(msg.get("media_port"), msg.get("token"),
                                monitors, monitors[0].get("id", 1))
        except Exception:
            get_logger().exception("remote: could not start screen (deps missing?)")
            remote.set_connection_status(
                "Screen unavailable — missing components (reinstall requirements)")

    remote_bridge.offer.connect(open_remote)

    # Save the mirrored meeting locally, so a viewed interview appears in the same
    # "Past meetings" history as a hosted one.
    recorder = _MeetingRecorder()

    # Received messages replay onto the transcript overlay; overlay methods are
    # thread-safe.
    def on_message(msg):
        recorder.record(msg)   # persist the transcript (also sees meeting_end below)
        # The host ending a meeting no longer ends the viewer: the connection and
        # screen share stay live (the host may start another meeting).
        if msg.get("type") == "meeting_end":
            overlay.info("[host ended the meeting — connection stays open]")
            return
        if msg.get("type") == "remote_offer":
            remote_bridge.offer.emit(msg)   # -> GUI thread starts the screen
            return
        if msg.get("type") == "audio_offer":
            open_audio(msg)                 # start/replace speaker playback
            return
        _handle_message(overlay, msg)

    def on_status(text):
        get_logger().info("viewer: %s", text)   # surface the outcome in the console too
        low = text.lower()
        if low.startswith("connected"):
            recorder.set_host_name(text.split("connected to", 1)[-1].strip())
            remote.set_connection_status("Connected")
            overlay.set_connected(True)     # host accepted -> open the shared notepad
            client.send({"type": "cmd", "action": "remote_start"})  # show the screen
            client.send({"type": "cmd", "action": "audio_start"})   # hear the meeting
        elif low.startswith("waiting"):
            remote.set_connection_status("Connecting to host…")
        elif low.startswith("denied"):
            reason = text.split("denied:", 1)[-1].strip() or "declined"
            remote.set_connection_status("Host connection failed: " + reason)
        elif low.startswith("reconnecting"):
            remote.set_connection_status("Reconnecting…")
        # "connection failed: …" is ignored: the relay dialer already reported the
        # specific relay/host failure via on_phase. A dropped link never ends the
        # session — NetClient reconnects on its own.

    client = NetClient(
        host, port, name=net.name if net is not None else "viewer",
        on_message=on_message,
        on_status=on_status,
        dialer=control_dialer,
        viewer_id=net.host_id if net is not None else "",
        code=password,
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

    # Closing the primary screen window ends the viewer session; the transcript's
    # own End button ends it too, but closing the transcript just hides it.
    remote.closed.connect(loop.quit)
    overlay.end_meeting.connect(loop.quit)
    overlay.quit_app.connect(on_quit)
    if tray is not None:
        tray.set_quit_handler(on_quit)
    loop.exec()

    try:
        remote.close()
    except Exception:
        pass
    if audio["client"] is not None:
        audio["client"].stop()
    client.stop()
    recorder.finalize()   # flush any half-streamed answer (client thread is stopped)
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
    # Claim an explicit AppUserModelID before any window exists, so Windows draws
    # the taskbar button from OUR window icon (the remote-view window, and any
    # non-tool window) instead of falling back to the host python.exe icon. This
    # is what setWindowIcon alone can't fix. Harmless no-op off Windows.
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                ctypes.c_wchar_p(config.APP_ID))
        except Exception:
            pass

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

    # Create the window-visibility controller now, on the GUI thread, so its Qt
    # thread affinity is correct. A viewer that connects and toggles stealth is
    # handled on a network thread; the controller marshals that back to the GUI
    # thread, but only if it was itself created here rather than there.
    get_window_visibility_controller()

    # Open the host servers NOW (at app start, not per-interview): the control +
    # media sockets, beacon and UPnP mapping come up immediately, so this PC is
    # discoverable, connectable and remote-controllable from launch.
    host_session = HostSession(net)
    host_session.start()

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
                              net=net, host_session=host_session):
            break   # user chose Quit during the interview

    host_session.stop()
    net.shutdown()
    if tray is not None:
        tray.hide()
    log.info("=== IronStack exiting ===")
    return 0


if __name__ == "__main__":
    main()
