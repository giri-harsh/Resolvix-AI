"""PCM helpers for the 8 kHz / 16-bit / mono stream the Voicebot protocol describes (Exotel docs; verify on a live call)."""
from __future__ import annotations

import array
import base64
import math

SAMPLE_RATE = 8000
CHUNK_MULTIPLE = 320            # documented: chunk size must be a multiple of 320 bytes
MIN_CHUNK = 3200                # documented minimum (the docs call it 100 ms; at 8 kHz/16-bit it is 200 ms: another item to verify)
MAX_CHUNK = 100_000
VOICED_RMS = 500                # simple energy threshold for the mock ASR and for barge-in


def b64decode_pcm(payload: str, max_bytes: int = MAX_CHUNK) -> bytes:
    """Decode a base64 media payload; raises ValueError if it is not base64, odd-length PCM16, or larger than max_bytes."""
    if len(payload) > (max_bytes * 4) // 3 + 8:
        raise ValueError("media payload too large")
    raw = base64.b64decode(payload, validate=True)
    if len(raw) > max_bytes or len(raw) % 2:
        raise ValueError("invalid PCM16 chunk")
    return raw


def b64encode_pcm(pcm: bytes) -> str:
    return base64.b64encode(pcm).decode("ascii")


def rms(pcm: bytes) -> float:
    if len(pcm) < 2:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def is_voiced(pcm: bytes) -> bool:
    return rms(pcm) >= VOICED_RMS


def silence(ms: int) -> bytes:
    return b"\x00" * (SAMPLE_RATE * ms // 1000 * 2)


def tone(ms: int, freq: int = 440, amplitude: int = 8000) -> bytes:
    n = SAMPLE_RATE * ms // 1000
    return array.array("h", (int(amplitude * math.sin(2 * math.pi * freq * i / SAMPLE_RATE)) for i in range(n))).tobytes()


def chunk_for_exotel(pcm: bytes, chunk_size: int = MIN_CHUNK) -> list[bytes]:
    """Split PCM into chunks that follow the documented size rules: a multiple of 320 bytes, >= MIN_CHUNK, <= MAX_CHUNK.
    The last chunk is padded with silence so it obeys the same rule."""
    if chunk_size % CHUNK_MULTIPLE or not MIN_CHUNK <= chunk_size <= MAX_CHUNK:
        raise ValueError("chunk_size must be a multiple of 320 between 3200 and 100000")
    out = [pcm[i:i + chunk_size] for i in range(0, len(pcm), chunk_size)]
    if out and len(out[-1]) < chunk_size:
        pad = (-len(out[-1])) % CHUNK_MULTIPLE
        out[-1] = out[-1] + b"\x00" * pad
        if len(out[-1]) < MIN_CHUNK:
            out[-1] = out[-1] + b"\x00" * (MIN_CHUNK - len(out[-1]))
    return out
