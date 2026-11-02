"""ASR and TTS adapters for the phone bridge. Only the mocks exist in this build: no vendor SDK, no network.

A real adapter must implement the same two small interfaces (feed PCM, get finals; text in, PCM out) and is chosen in service construction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .audio import is_voiced, tone


class SpeechError(Exception):
    """An ASR or TTS adapter failure (vendor error, timeout, bad audio)."""


class AsrAdapter(Protocol):
    async def feed(self, pcm: bytes) -> list[str]:
        """Consume one audio chunk; return the final transcripts completed by it (usually none or one)."""

    async def aclose(self) -> None: ...


class TtsAdapter(Protocol):
    async def synthesize(self, text: str, lang: str) -> bytes:
        """Text -> 8 kHz PCM16 mono."""


@dataclass
class MockAsr:
    """Scripted ASR: a run of voiced chunks followed by `silence_chunks` quiet chunks yields the next scripted transcript."""

    script: list[str] = field(default_factory=list)
    silence_chunks: int = 2
    fail_on_chunk: int | None = None          # raise SpeechError when this many chunks have been fed (failure tests)
    _voiced_run: int = 0
    _quiet: int = 0
    _fed: int = 0
    closed: bool = False

    async def feed(self, pcm: bytes) -> list[str]:
        self._fed += 1
        if self.fail_on_chunk is not None and self._fed >= self.fail_on_chunk:
            raise SpeechError("mock ASR failure")
        if is_voiced(pcm):
            self._voiced_run += 1
            self._quiet = 0
            return []
        if self._voiced_run:
            self._quiet += 1
            if self._quiet >= self.silence_chunks and self.script:
                self._voiced_run = self._quiet = 0
                return [self.script.pop(0)]
        return []

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class MockTts:
    """Deterministic TTS: a tone whose length follows the text length (about 40 ms per character, capped at 3 s)."""

    fail: bool = False
    spoken: list[str] = field(default_factory=list)

    async def synthesize(self, text: str, lang: str) -> bytes:
        if self.fail:
            raise SpeechError("mock TTS failure")
        self.spoken.append(text)
        return tone(min(3000, max(200, 40 * len(text))))
