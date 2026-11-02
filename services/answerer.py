import base64
import threading
import time

from openai import OpenAI

from storage import database
from config import OPENAI_API_KEY, ANSWER_MODEL, SCREEN_ANSWER_MODEL
from logsetup import get_logger

# The model emits this (and nothing else) when the latest line needs no answer.
SKIP = "[NO_ANSWER]"

# Prefixes that tell the model who said each history line.
INTERVIEWER_TAG = "Interviewer: "
CANDIDATE_TAG = "Me (actually said): "
SCREEN_TAG = "Screen (captured): "
INSTRUCTION_TAG = "Instruction from me: "

# Hard ceiling on answer length. The prompt asks for <=5 short sentences; this is
# a backstop so a stray verbose answer can never become a wall of text. ~5
# sentences is well under this; reasoning effort is minimal so it eats little.
MAX_OUTPUT_TOKENS = 350

# Answers I ask for explicitly (a screenshot or a typed instruction) may be code
# or a worked solution, so they get more room and a little more thinking.
FORCED_MAX_OUTPUT_TOKENS = 3000
FORCED_REASONING = "minimal"
# With a screenshot attached the model has to look at the picture and reason, and
# its reasoning tokens count toward the output limit.
IMAGE_MAX_OUTPUT_TOKENS = 9000
IMAGE_REASONING = "medium"


class Answerer:
    """Turn an interview question into a short, natural answer using the OpenAI
    Responses API.

    The model decides whether the latest line even needs a response; if not, it
    replies with `[NO_ANSWER]` and nothing is shown. A rolling history of the
    interview is kept so each answer is grounded in what came before. The history
    is append-only and never trimmed, so the repeated prompt prefix stays
    identical between calls and OpenAI's automatic prompt caching applies."""

    def __init__(self, console, system_prompt, meeting_id=None, language="English",
                is_ai_enabled=None):
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set")

        self.client = OpenAI(api_key=OPENAI_API_KEY)
        self.console = console
        self.system_prompt = system_prompt
        self.meeting_id = meeting_id
        self.language = language or "English"
        # Whole conversation: interviewer lines, AI-suggested answers, and what I
        # actually said into the mic. Guarded by _lock because the mic thread and
        # the answer worker both write to it.
        self.history = []
        self._lock = threading.Lock()
        # JPEGs of the latest screen. Never stored in the history: they are attached
        # only to the answers I trigger myself (a screenshot or a typed instruction).
        self.screen_images = []
        self.is_ai_enabled = is_ai_enabled   # optional callable; False/None-return skips OpenAI
        self.log = get_logger()
        self.log.info("Answerer ready (model=%s, meeting_id=%s, language=%s)",
                      ANSWER_MODEL, meeting_id, self.language)

    def set_language(self, language):
        """Change the language answers are written in, mid-meeting."""
        self.language = language or "English"
        self.log.info("answer language -> %s", self.language)

    def _instructions(self, force=False):
        """System prompt plus a directive to answer in the chosen language. The
        marker [NO_ANSWER] must stay verbatim regardless of language. `force`
        marks an answer I asked for explicitly (screenshot / typed instruction)."""
        text = (
            f"{self.system_prompt}\n\n"
            f"LANGUAGE: Write every answer in {self.language}, no matter what "
            f"language the interviewer speaks. The only exception is the literal "
            f"token [NO_ANSWER], which you must always output exactly as-is.\n\n"
            f"CONVERSATION FORMAT: Lines starting with \"{INTERVIEWER_TAG.strip()}\" are "
            f"the interviewer. Lines starting with \"{CANDIDATE_TAG.strip()}\" are the "
            f"words I really said out loud. Assistant messages are only answers "
            f"you suggested earlier; I may not have used them. When my actual words "
            f"differ from a suggestion, trust my actual words, do not repeat what "
            f"I already said, and build on it for follow-up questions. Lines "
            f"starting with \"{SCREEN_TAG.strip()}\" are text read from my screen "
            f"(code, a test or a problem I am looking at); anything drawn is "
            f"described in words. When the actual screenshot is attached to a "
            f"message, trust the image over the description. Lines starting with "
            f"\"{INSTRUCTION_TAG.strip()}\" are instructions I typed to you during "
            f"the interview; follow them, and decide from their wording whether they "
            f"apply only to the current answer or to later answers too."
        )
        if force:
            text += (
                "\n\nEXPLICIT REQUEST: I am asking for an answer right now, using "
                "everything above (the interviewer's questions, my own words, the "
                "screen and my instructions). Never reply [NO_ANSWER]. Give the "
                "correct answer. For a coding problem, give complete working code "
                "followed by one short spoken line about the idea. For a test or "
                "multiple-choice problem, state the correct answer(s) and one short "
                "reason. The 5-sentence limit does not apply to code. If I gave an "
                "instruction, it overrides the style rules above."
            )
        return text

    def _save(self, role, content):
        if self.meeting_id is not None:
            try:
                database.add_message(self.meeting_id, role, content)
            except Exception:
                # persistence must never break the live interview, but log it.
                self.log.exception("failed to persist message (role=%s)", role)

    def _decide(self, buffer: str) -> str:
        """Classify the answer stream so far without waiting for the whole thing.
        Only a leading [NO_ANSWER] marker means 'show nothing'. Any OTHER leading
        bracket -- e.g. the model hedging with "[Note: ...]" -- is a real answer
        that was just mis-formatted; treat it as an answer (the stray note gets
        stripped before display) so questions like "what's your name?" are never
        silently dropped."""
        s = buffer.strip()
        if not s:
            return "wait"
        if s[0] != "[":
            return "answer"
        if "]" not in s:
            return "wait"        # wait for the bracketed token to close
        token = s[1:s.index("]")].strip().upper().replace(" ", "_")
        return "skip" if token == "NO_ANSWER" else "answer"

    def add_candidate_speech(self, text: str):
        """Record a finalized line of what I said into the mic. It joins the
        context for later answers but never triggers one. Consecutive lines
        merge into one entry so a long reply is not split into fragments."""
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            last = self.history[-1] if self.history else None
            if last and last["role"] == "user" and last["content"].startswith(CANDIDATE_TAG):
                last["content"] += " " + text
            else:
                self.history.append({"role": "user", "content": CANDIDATE_TAG + text})

    def answer(self, question: str):
        with self._lock:
            self.history.append({"role": "user", "content": INTERVIEWER_TAG + question})

        # Always show the interviewer line, even if it gets no answer.
        self.console.show_question(question)
        self._save("interviewer", question)

        self.log.info("question: %s", question)
        self._respond()

    def answer_screen(self, text: str, images=None):
        """Add what was read from a screenshot to the context and answer right
        away, with the screenshot images attached to this one request."""
        self.screen_images = list(images or [])
        with self._lock:
            self.history.append({"role": "user", "content": SCREEN_TAG + text})
            attach = len(self.history) - 1
        self._save("screen", text)
        self.log.info("screen text (%d chars, %d images)", len(text), len(self.screen_images))
        self._respond(force=True, attach_index=attach, images=self.screen_images)

    def answer_instruction(self, text: str):
        """Add an instruction I typed to the context and answer again right away."""
        self.console.show_instruction(text)
        with self._lock:
            self.history.append({"role": "user", "content": INSTRUCTION_TAG + text})
            attach = len(self.history) - 1
        self._save("instruction", text)
        self.log.info("instruction: %s", text)
        self._respond(force=True, attach_index=attach, images=self.screen_images)

    def _respond(self, force=False, attach_index=None, images=None):
        if self.is_ai_enabled is not None and not self.is_ai_enabled():
            self.log.info("AI answers disabled -> skipping OpenAI call")
            return

        with self._lock:
            snapshot = [dict(m) for m in self.history]
        if images and attach_index is not None:
            # The images ride on this one request only; the history stays text.
            parts = [{"type": "input_text", "text": snapshot[attach_index]["content"]}]
            for jpeg in images:
                parts.append({
                    "type": "input_image", "detail": "high",
                    "image_url": "data:image/jpeg;base64,"
                                 + base64.b64encode(jpeg).decode("ascii")})
            snapshot[attach_index] = {"role": "user", "content": parts}

        if images:
            model, effort, max_tokens = (SCREEN_ANSWER_MODEL, IMAGE_REASONING,
                                         IMAGE_MAX_OUTPUT_TOKENS)
        elif force:
            model, effort, max_tokens = ANSWER_MODEL, FORCED_REASONING, FORCED_MAX_OUTPUT_TOKENS
        else:
            model, effort, max_tokens = ANSWER_MODEL, "minimal", MAX_OUTPUT_TOKENS

        stream = self.client.responses.create(
            model=model,
            instructions=self._instructions(force),
            input=snapshot,
            stream=True,
            reasoning={"effort": effort},
            text={"verbosity": "medium" if force else "low"},
            max_output_tokens=max_tokens,
        )

        start = time.perf_counter()
        first_token = None
        buffer = ""
        started = False
        skipped = False

        try:
            for event in stream:
                if getattr(event, "type", None) != "response.output_text.delta":
                    continue
                delta = getattr(event, "delta", "") or ""
                if not delta:
                    continue

                if started:
                    buffer += delta
                    self.console.answer_delta(delta)
                    continue

                buffer += delta
                if skipped:
                    # Already a no-answer; keep draining (nothing shown) so we
                    # capture the short reason the model writes after the marker.
                    continue

                # Not committed yet: buffer until we know if this is an answer.
                decision = self._decide(buffer)
                if decision == "skip":
                    skipped = True
                    continue
                if decision == "answer":
                    started = True
                    first_token = time.perf_counter() - start
                    # Strip a stray leading "[...]" note the model shouldn't have
                    # emitted, so only the real answer is shown.
                    if buffer.lstrip().startswith("[") and "]" in buffer:
                        buffer = buffer.split("]", 1)[1].lstrip()
                    self.console.begin_answer()
                    self.console.answer_delta(buffer)
        finally:
            try:
                stream.close()
            except Exception:
                pass

        if not started:
            # No answer needed. The interviewer line is already shown and kept in
            # history for context; we add no answer bubble. Log the reason the
            # model gave after the marker (e.g. "[NO_ANSWER] just small talk").
            reason = buffer.strip()
            if "]" in reason:
                reason = reason.split("]", 1)[1].strip()
            self.log.info("no answer (reason: %s)", reason or "none given")
            return

        answer_text = buffer.strip()
        self.log.info("answer: %s", answer_text)
        with self._lock:
            self.history.append({"role": "assistant", "content": answer_text})
        self._save("me", answer_text)

        elapsed = time.perf_counter() - start
        ttf = f"{first_token:.1f}s" if first_token is not None else "n/a"
        self.console.end_answer(f"({ttf} to first word, {elapsed:.1f}s total)")
