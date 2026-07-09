import sys
import json
import ctypes
import queue
import asyncio
import threading
import urllib.parse

import numpy as np
import websockets

from config import (
    DEEPGRAM_API_KEY,
    DEEPGRAM_URL,
    DEEPGRAM_MODEL,
    DEEPGRAM_LANGUAGE,
    TARGET_SR,
    CAPTURE_SR,
    BLOCK_FRAMES,
    ENDPOINTING_MS,
    UTTERANCE_END_MS,
)
from services.audio_utils import find_loopback, find_microphone, resample
from logsetup import get_logger


class Transcriber:
    """Capture an audio source and stream it to Deepgram's real-time
    speech-to-text API.

    `source` selects what is captured:
      - "loopback"   : system audio (the interviewer's voice) -- the default.
      - "microphone" : the real mic (the candidate's own voice).

    Deepgram emits interim results word-by-word as soon as speech is heard;
    those are sent to `console.partial` (live line). When a pause finishes an
    utterance, the assembled final transcript is passed to `on_question`.
    """

    def __init__(self, on_question, console, language=DEEPGRAM_LANGUAGE,
                 source="loopback"):
        if not DEEPGRAM_API_KEY:
            raise RuntimeError("DEEPGRAM_API_KEY is not set")

        self.on_question = on_question
        self.console = console
        self.source = source
        self.audio_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.finalized = []   # confirmed segments of the current utterance
        self._language = language or DEEPGRAM_LANGUAGE

        # Set once run() is on its event loop, so stop()/set_language (called
        # from the GUI thread) can wake the loop and act on it cleanly.
        self._loop = None
        self._async_stop = None
        self._async_restart = None

    def stop(self):
        self.stop_event.set()
        self._wake(self._async_stop)

    def restart(self):
        """Force a reconnect of the Deepgram websocket (same language). Use when
        transcription silently stops picking up speech; audio capture keeps
        running across the brief gap."""
        get_logger().info("transcription: manual restart requested (source=%s)", self.source)
        self._wake(self._async_restart)

    def set_language(self, code):
        """Switch transcription language mid-meeting. Deepgram fixes the
        language at connection time, so this reconnects the websocket with the
        new language; audio capture keeps running across the brief gap."""
        code = code or DEEPGRAM_LANGUAGE
        if code == self._language:
            return
        self._language = code
        get_logger().info("transcription: language -> %s (reconnecting)", code)
        self._wake(self._async_restart)

    def _wake(self, event):
        """Set an asyncio.Event that lives on the run() loop, from any thread."""
        loop = self._loop
        if loop is not None and event is not None:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass   # loop already stopped/closed

    def _drain_queue(self):
        """Drop audio captured during a reconnect gap so we don't replay stale
        speech into the new (possibly different-language) connection."""
        try:
            while True:
                self.audio_queue.get_nowait()
        except queue.Empty:
            pass

    # --- audio capture (runs in its own thread) ---

    def _capture_worker(self):
        # All WASAPI/soundcard work happens on this thread. The main thread is an
        # STA apartment (claimed for Qt in main.py), so this thread needs its own
        # COM apartment; soundcard expects multithreaded (MTA). Without this, the
        # first soundcard call below fails because there is no MTA to join.
        if sys.platform == "win32":
            ctypes.windll.ole32.CoInitializeEx(None, 0)  # 0 = COINIT_MULTITHREADED

        try:
            # A real mic is usually mono; loopback follows the (stereo) speakers.
            if self.source == "microphone":
                device = find_microphone()
                channels = 1
                get_logger().info("audio: capturing from microphone %r",
                                  getattr(device, "name", device))
                self.console.info("[audio] capturing microphone...")
            else:
                device = find_loopback()
                channels = 2
                get_logger().info("audio: capturing from loopback %r",
                                  getattr(device, "name", device))
                self.console.info("[audio] capturing system audio...")

            with device.recorder(
                samplerate=CAPTURE_SR,
                channels=channels,
                blocksize=BLOCK_FRAMES,
            ) as recorder:
                while not self.stop_event.is_set():
                    data = recorder.record(numframes=BLOCK_FRAMES)

                    # Average to mono. record() returns shape (frames, channels),
                    # so this collapses cleanly whether channels is 1 or 2.
                    mono = np.mean(data, axis=1) if data.ndim > 1 else data
                    mono = resample(mono, CAPTURE_SR, TARGET_SR)
                    mono = np.clip(mono, -1, 1)

                    pcm16 = (mono * 32767).astype(np.int16).tobytes()
                    self.audio_queue.put(pcm16)
            get_logger().info("audio: capture stopped")
        except Exception:
            get_logger().exception("audio: capture worker failed")
            try:
                self.console.info("[error] audio capture failed (see log)")
            except Exception:
                pass

    # --- websocket send / receive ---

    async def _send_audio(self, ws):
        while not self.stop_event.is_set():
            try:
                chunk = await asyncio.to_thread(self.audio_queue.get, True, 0.25)
            except queue.Empty:
                continue

            # Deepgram takes raw PCM16 as binary websocket frames.
            await ws.send(chunk)

        # Tell Deepgram no more audio is coming so it can flush.
        try:
            await ws.send(json.dumps({"type": "CloseStream"}))
        except Exception:
            pass

    def _flush(self):
        text = " ".join(self.finalized).strip()
        self.finalized = []
        if text:
            self.on_question(text)

    async def _receive(self, ws):
        async for msg in ws:
            event = json.loads(msg)
            t = event.get("type")

            if t == "Results":
                alt = event["channel"]["alternatives"][0]
                transcript = alt.get("transcript", "").strip()

                if event.get("is_final"):
                    # A chunk is locked in; append it and show what's confirmed.
                    if transcript:
                        self.finalized.append(transcript)
                    self.console.partial(" ".join(self.finalized))
                elif transcript:
                    # Interim words: confirmed text so far + the live tail.
                    self.console.partial(" ".join(self.finalized + [transcript]))

                # A pause ended the utterance -> hand it off as a question.
                if event.get("speech_final"):
                    self._flush()

            elif t == "UtteranceEnd":
                # Backstop in case speech_final never arrived for this turn.
                self._flush()

    # --- run loop ---

    async def run(self):
        self._loop = asyncio.get_running_loop()
        self._async_stop = asyncio.Event()
        self._async_restart = asyncio.Event()
        if self.stop_event.is_set():
            return   # stop() was called before we even got going

        headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}

        # Audio capture is independent of the Deepgram connection and the chosen
        # language, so it runs once and keeps going across language reconnects.
        threading.Thread(target=self._capture_worker, daemon=True).start()

        # (Re)connect loop: a language change closes the current websocket and
        # comes back around to open a new one with the new language.
        while not self.stop_event.is_set():
            language = self._language
            params = {
                "model": DEEPGRAM_MODEL,
                "language": language,
                "encoding": "linear16",
                "sample_rate": TARGET_SR,
                "channels": 1,
                "interim_results": "true",
                "smart_format": "true",
                "endpointing": ENDPOINTING_MS,
                "utterance_end_ms": UTTERANCE_END_MS,
            }
            url = f"{DEEPGRAM_URL}?{urllib.parse.urlencode(params)}"

            get_logger().info("transcription: connecting to Deepgram (model=%s, lang=%s)",
                              DEEPGRAM_MODEL, language)
            try:
                async with websockets.connect(url, additional_headers=headers) as ws:
                    get_logger().info("transcription: connected")
                    self.finalized = []   # don't carry a half utterance across reconnects

                    sender = asyncio.create_task(self._send_audio(ws))
                    receiver = asyncio.create_task(self._receive(ws))
                    stopper = asyncio.create_task(self._async_stop.wait())
                    restarter = asyncio.create_task(self._async_restart.wait())
                    tasks = (sender, receiver, stopper, restarter)

                    try:
                        # Return as soon as a socket task finishes, or stop() /
                        # set_language() is requested.
                        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        # Cancel whatever is still running and let it unwind
                        # *inside* the loop, so the websocket's pending
                        # overlapped I/O is torn down before the event loop
                        # closes. Without this, ending a meeting on Windows
                        # crashes with "Overlapped object still has pending
                        # operation at deallocation".
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
            except Exception:
                if self.stop_event.is_set():
                    break
                get_logger().exception("transcription: connection error")

            if self._async_restart.is_set():
                # Language change: reconnect with the new language, dropping any
                # audio buffered during the gap.
                self._async_restart.clear()
                self._drain_queue()
                self.console.partial("")
                continue
            # Otherwise the socket ended or stop() was called.
            if self.stop_event.is_set():
                break
            # Unexpected socket close (not stop, not language change): brief
            # backoff (interruptible by stop) then reconnect.
            get_logger().warning("transcription: socket closed unexpectedly, reconnecting")
            try:
                await asyncio.wait_for(self._async_stop.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

        get_logger().info("transcription: stopped")
