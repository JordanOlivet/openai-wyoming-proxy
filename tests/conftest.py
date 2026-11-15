"""Fake Wyoming STT/TTS servers running in a background event loop."""

from __future__ import annotations

import asyncio
import math
import socket
import struct
import threading

import pytest
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import (
    AsrModel, AsrProgram, Attribution, Describe, Info, TtsProgram, TtsVoice,
)
from wyoming.server import AsyncEventHandler, AsyncTcpServer
from wyoming.tts import Synthesize

ATTR = Attribution(name="test", url="https://example.invalid")
TTS_RATE = 22_050


def tone(seconds: float, rate: int = TTS_RATE) -> bytes:
    frames = int(seconds * rate)
    return b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * n / rate))) for n in range(frames)
    )


class FakeStt(AsyncEventHandler):
    calls: list[dict] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.language = None
        self.audio = bytearray()
        self.format = None

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            info = Info(asr=[AsrProgram(
                name="fake-asr", attribution=ATTR, installed=True, description=None, version="1",
                models=[AsrModel(name="fake-model", attribution=ATTR, installed=True,
                                 description=None, version="1", languages=["fr", "en"])],
            )])
            await self.write_event(info.event())
            return False
        if Transcribe.is_type(event.type):
            self.language = Transcribe.from_event(event).language
        elif AudioStart.is_type(event.type):
            start = AudioStart.from_event(event)
            self.format = (start.rate, start.width, start.channels)
        elif AudioChunk.is_type(event.type):
            self.audio.extend(AudioChunk.from_event(event).audio)
        elif AudioStop.is_type(event.type):
            FakeStt.calls.append({"language": self.language, "bytes": len(self.audio),
                                  "format": self.format})
            await self.write_event(Transcript(text=f" bonjour {len(self.audio)} ").event())
            return False
        return True


class FakeTts(AsyncEventHandler):
    calls: list[dict] = []

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            voices = [TtsVoice(name=name, attribution=ATTR, installed=True, description=None,
                               version="1", languages=["fr"]) for name in ("voice-a", "voice-b")]
            info = Info(tts=[TtsProgram(name="fake-tts", attribution=ATTR, installed=True,
                                        description=None, version="1", voices=voices)])
            await self.write_event(info.event())
            return False
        if Synthesize.is_type(event.type):
            synth = Synthesize.from_event(event)
            FakeTts.calls.append({"text": synth.text,
                                  "voice": synth.voice.name if synth.voice else None,
                                  "language": synth.voice.language if synth.voice else None})
            await self.write_event(AudioStart(rate=TTS_RATE, width=2, channels=1).event())
            audio = tone(0.5)
            for i in range(0, len(audio), 4096):
                await self.write_event(
                    AudioChunk(rate=TTS_RATE, width=2, channels=1, audio=audio[i:i + 4096]).event())
            await self.write_event(AudioStop().event())
            return False
        return True


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def wyoming_servers():
    loop = asyncio.new_event_loop()
    ports = {"stt": _free_port(), "tts": _free_port()}
    ready = threading.Event()

    async def main():
        await AsyncTcpServer("127.0.0.1", ports["stt"]).start(FakeStt)
        await AsyncTcpServer("127.0.0.1", ports["tts"]).start(FakeTts)
        ready.set()

    thread = threading.Thread(target=lambda: (loop.run_until_complete(main()), loop.run_forever()),
                              daemon=True)
    thread.start()
    assert ready.wait(10)
    yield {"stt": f"tcp://127.0.0.1:{ports['stt']}", "tts": f"tcp://127.0.0.1:{ports['tts']}"}
    loop.call_soon_threadsafe(loop.stop)


@pytest.fixture(autouse=True)
def _reset_calls():
    FakeStt.calls.clear()
    FakeTts.calls.clear()
