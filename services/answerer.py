import time

from openai import OpenAI

from storage import database
from config import OPENAI_API_KEY, ANSWER_MODEL
from logsetup import get_logger

# The model emits this (and nothing else) when the latest line needs no answer.
SKIP = "[NO_ANSWER]"

# How many conversation turns to keep as context (resume + JD always live in the
# system prompt, so trimming history never drops them).
MAX_HISTORY = 40

# Hard ceiling on answer length. The prompt asks for <=5 short sentences; this is
# a backstop so a stray verbose answer can never become a wall of text. ~5
# sentences is well under this; reasoning effort is minimal so it eats little.
MAX_OUTPUT_TOKENS = 350


class Answerer:
    """Turn an interview question into a short, natural answer using the OpenAI
    Responses API.

    The model decides whether the latest line even needs a response; if not, it
    replies with `[NO_ANSWER]` and nothing is shown. A rolling history of the
    interview is kept so each answer is grounded in what came before."""

    def __init__(self, console, system_prompt, meeting_id=None, language="English"):
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set")

        self.client = OpenAI(api_key=OPENAI_API_KEY)
        self.console = console
        self.system_prompt = system_prompt
        self.meeting_id = meeting_id
        self.language = language or "English"
        self.history = []   # rolling conversation: interviewer lines + my answers
        self.log = get_logger()
        self.log.info("Answerer ready (model=%s, meeting_id=%s, language=%s)",
                      ANSWER_MODEL, meeting_id, self.language)

    def set_language(self, language):
        """Change the language answers are written in, mid-meeting."""
        self.language = language or "English"
        self.log.info("answer language -> %s", self.language)

    def _instructions(self):
        """System prompt plus a directive to answer in the chosen language. The
        marker [NO_ANSWER] must stay verbatim regardless of language."""
        return (
            f"{self.system_prompt}\n\n"
            f"LANGUAGE: Write every answer in {self.language}, no matter what "
            f"language the interviewer speaks. The only exception is the literal "
            f"token [NO_ANSWER], which you must always output exactly as-is."
        )

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

    def answer(self, question: str):
        self.history.append({"role": "user", "content": question})

        # Always show the interviewer line, even if it gets no answer.
        self.console.show_question(question)
        self._save("interviewer", question)

        self.log.info("question: %s", question)

        stream = self.client.responses.create(
            model=ANSWER_MODEL,
            instructions=self._instructions(),
            input=self.history,
            stream=True,
            reasoning={"effort": "minimal"},
            text={"verbosity": "low"},
            max_output_tokens=MAX_OUTPUT_TOKENS,
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
            self._trim()
            return

        answer_text = buffer.strip()
        self.log.info("answer: %s", answer_text)
        self.history.append({"role": "assistant", "content": answer_text})
        self._save("me", answer_text)
        self._trim()

        elapsed = time.perf_counter() - start
        ttf = f"{first_token:.1f}s" if first_token is not None else "n/a"
        self.console.end_answer(f"({ttf} to first word, {elapsed:.1f}s total)")

    def _trim(self):
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-MAX_HISTORY:]
