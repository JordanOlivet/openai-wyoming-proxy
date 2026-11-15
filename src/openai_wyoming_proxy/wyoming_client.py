"""Minimal async Wyoming client: one connection per request."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient
from wyoming.error import Error
from wyoming.info import Describe, Info
from wyoming.tts import Synthesize, SynthesizeVoice

# Bytes per AudioChunk sent to the STT server (1 s of 16 kHz mono s16le).
STT_CHUNK_BYTES = 32_000


class WyomingError(RuntimeError):
    """The Wyoming server answered with an error or an unexpected stream."""


@dataclass
class PcmAudio:
    data: bytes
    rate: int
    width: int
    channels: int

    @property
    def duration(self) -> float:
        frame = self.width * self.channels
        return len(self.data) / (frame * self.rate) if frame and self.rate else 0.0


async def _with_timeout(coro, timeout: float):
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise WyomingError(f"Wyoming server did not answer within {timeout:g} s") from exc
    except OSError as exc:
        raise WyomingError(f"Wyoming server unreachable: {exc}") from exc


async def describe(uri: str, timeout: float) -> Info:
    async def run() -> Info:
        async with AsyncClient.from_uri(uri) as client:
            await client.write_event(Describe().event())
            while True:
                event = await client.read_event()
                if event is None:
                    raise WyomingError("connection closed before info")
                if Info.is_type(event.type):
                    return Info.from_event(event)

    return await _with_timeout(run(), timeout)


async def transcribe(uri: str, audio: PcmAudio, language: str | None, timeout: float) -> str:
    async def run() -> str:
        async with AsyncClient.from_uri(uri) as client:
            await client.write_event(Transcribe(language=language).event())
            await client.write_event(
                AudioStart(rate=audio.rate, width=audio.width, channels=audio.channels).event()
            )
            for start in range(0, len(audio.data), STT_CHUNK_BYTES):
                chunk = audio.data[start : start + STT_CHUNK_BYTES]
                await client.write_event(
                    AudioChunk(
                        rate=audio.rate, width=audio.width, channels=audio.channels, audio=chunk
                    ).event()
                )
            await client.write_event(AudioStop().event())
            while True:
                event = await client.read_event()
                if event is None:
                    raise WyomingError("connection closed before transcript")
                if Transcript.is_type(event.type):
                    return (Transcript.from_event(event).text or "").strip()
                if Error.is_type(event.type):
                    raise WyomingError(Error.from_event(event).text)
                # TranscriptStart / TranscriptChunk / TranscriptStop: wait for the final Transcript.

    return await _with_timeout(run(), timeout)


async def synthesize(
    uri: str,
    text: str,
    voice: str | None,
    language: str | None,
    speaker: str | None,
    timeout: float,
) -> PcmAudio:
    async def run() -> PcmAudio:
        voice_spec = None
        if voice or language or speaker:
            voice_spec = SynthesizeVoice(name=voice, language=language, speaker=speaker)
        async with AsyncClient.from_uri(uri) as client:
            await client.write_event(Synthesize(text=text, voice=voice_spec).event())
            chunks: list[bytes] = []
            rate = width = channels = None
            while True:
                event = await client.read_event()
                if event is None:
                    break  # some servers just close the connection when done
                if AudioStart.is_type(event.type):
                    start = AudioStart.from_event(event)
                    rate, width, channels = start.rate, start.width, start.channels
                elif AudioChunk.is_type(event.type):
                    chunk = AudioChunk.from_event(event)
                    if rate is None:
                        rate, width, channels = chunk.rate, chunk.width, chunk.channels
                    chunks.append(chunk.audio)
                elif AudioStop.is_type(event.type):
                    break
                elif Error.is_type(event.type):
                    raise WyomingError(Error.from_event(event).text)
            if not chunks or rate is None:
                raise WyomingError("TTS server returned no audio")
            return PcmAudio(b"".join(chunks), rate, width, channels)

    return await _with_timeout(run(), timeout)
