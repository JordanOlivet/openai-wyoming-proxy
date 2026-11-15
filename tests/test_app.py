from __future__ import annotations

import io
import subprocess
import wave

import pytest
from fastapi.testclient import TestClient

from openai_wyoming_proxy.app import create_app
from openai_wyoming_proxy.config import Settings, _parse_aliases

from .conftest import FakeStt, FakeTts, tone


def make_client(servers, **overrides) -> TestClient:
    settings = Settings(stt_uri=servers["stt"], tts_uri=servers["tts"], stt_language="fr",
                        tts_voice="voice-a", tts_language="fr", wyoming_timeout=10, **overrides)
    return TestClient(create_app(settings))


def wav_bytes(seconds: float = 1.0, rate: int = 44_100) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        mono = tone(seconds, rate)
        wav.writeframes(b"".join(mono[i:i + 2] * 2 for i in range(0, len(mono), 2)))
    return buffer.getvalue()


def webm_bytes() -> bytes:
    """What a browser MediaRecorder uploads: Opus in WebM."""
    return subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "wav", "-i", "pipe:0",
         "-c:a", "libopus", "-f", "webm", "pipe:1"],
        input=wav_bytes(), capture_output=True, check=True,
    ).stdout


def test_health(wyoming_servers):
    assert make_client(wyoming_servers).get("/health").json()["status"] == "ok"


@pytest.mark.parametrize("payload,name", [(wav_bytes, "a.wav"), (webm_bytes, "a.webm")])
def test_transcription_any_format_is_resampled(wyoming_servers, payload, name):
    response = make_client(wyoming_servers).post(
        "/v1/audio/transcriptions", files={"file": (name, payload())}, data={"model": "whisper-1"})
    assert response.status_code == 200, response.text
    call = FakeStt.calls[-1]
    assert call["format"] == (16_000, 2, 1)
    assert call["language"] == "fr"
    assert 30_000 <= call["bytes"] <= 34_000  # ~1 s of 16 kHz mono s16le
    assert response.json() == {"text": f"bonjour {call['bytes']}"}


def test_transcription_text_and_language_override(wyoming_servers):
    response = make_client(wyoming_servers).post(
        "/v1/audio/transcriptions", files={"file": ("a.wav", wav_bytes())},
        data={"response_format": "text", "language": "en-US"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text.startswith("bonjour ")
    assert FakeStt.calls[-1]["language"] == "en"


@pytest.mark.parametrize("fmt", ["verbose_json", "srt", "vtt"])
def test_transcription_other_formats(wyoming_servers, fmt):
    response = make_client(wyoming_servers).post(
        "/v1/audio/transcriptions", files={"file": ("a.wav", wav_bytes())},
        data={"response_format": fmt})
    assert response.status_code == 200
    assert "bonjour" in response.text


def test_transcription_rejects_garbage(wyoming_servers):
    response = make_client(wyoming_servers).post(
        "/v1/audio/transcriptions", files={"file": ("a.webm", b"not audio at all")})
    assert response.status_code == 400
    assert response.json()["error"]["message"].startswith("Could not decode audio")


def test_transcription_upload_limit(wyoming_servers):
    response = make_client(wyoming_servers, max_upload_mb=0.01).post(
        "/v1/audio/transcriptions", files={"file": ("a.wav", wav_bytes())})
    assert response.status_code == 413


@pytest.mark.parametrize("fmt,magic", [
    ("mp3", (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")),
    ("wav", (b"RIFF",)), ("opus", (b"OggS",)), ("ogg", (b"OggS",)), ("flac", (b"fLaC",)),
    ("aac", (b"\xff\xf1", b"\xff\xf9")), ("m4a", (b"\x00\x00\x00",)), ("pcm", (b"",)),
])
def test_speech_formats(wyoming_servers, fmt, magic):
    response = make_client(wyoming_servers).post(
        "/v1/audio/speech", json={"model": "tts-1", "input": "Salut", "response_format": fmt})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("audio/")
    assert response.content.startswith(magic)
    assert len(response.content) > 100


def test_speech_pcm_is_24khz(wyoming_servers):
    response = make_client(wyoming_servers).post(
        "/v1/audio/speech", json={"input": "Salut", "response_format": "pcm"})
    assert abs(len(response.content) - 24_000) < 500  # 0.5 s * 24 kHz * 2 bytes


def test_speech_voice_resolution(wyoming_servers):
    client = make_client(wyoming_servers, tts_voice_aliases={"nova": "voice-b"})
    for requested, expected in [("voice-b", "voice-b"), ("alloy", "voice-a"),
                                ("nova", "voice-b"), (None, "voice-a")]:
        body = {"input": "Salut", "response_format": "wav"}
        if requested:
            body["voice"] = requested
        assert client.post("/v1/audio/speech", json=body).status_code == 200
        assert FakeTts.calls[-1]["voice"] == expected
        assert FakeTts.calls[-1]["language"] == "fr"


def test_speech_plain_text_body(wyoming_servers):
    response = make_client(wyoming_servers).post(
        "/v1/audio/speech?response_format=wav&voice=voice-b",
        content="Il fait quinze degrés.\n".encode(),
        headers={"content-type": "text/plain; charset=utf-8"})
    assert response.status_code == 200
    assert response.content.startswith(b"RIFF")
    assert FakeTts.calls[-1] == {"text": "Il fait quinze degrés.", "voice": "voice-b",
                                 "language": "fr"}


def test_speech_speed_changes_duration(wyoming_servers):
    response = make_client(wyoming_servers).post(
        "/v1/audio/speech", json={"input": "Salut", "response_format": "pcm", "speed": 2.0})
    assert abs(len(response.content) - 12_000) < 1_000


@pytest.mark.parametrize("body", [{}, {"input": "x", "response_format": "midi"},
                                  {"input": "x", "speed": 9}])
def test_speech_validation(wyoming_servers, body):
    assert make_client(wyoming_servers).post("/v1/audio/speech", json=body).status_code == 400


def test_api_key(wyoming_servers):
    client = make_client(wyoming_servers, api_key="secret")
    assert client.get("/health").status_code == 200
    assert client.get("/v1/audio/voices").status_code == 401
    ok = client.get("/v1/audio/voices", headers={"Authorization": "Bearer secret"})
    assert ok.json() == {"voices": [{"id": "voice-a", "name": "voice-a"},
                                    {"id": "voice-b", "name": "voice-b"}]}


def test_models(wyoming_servers):
    client = make_client(wyoming_servers)
    ids = [item["id"] for item in client.get("/v1/models").json()["data"]]
    assert ids == ["fake-model", "fake-tts"]
    assert client.get("/v1/audio/models").json() == {"models": [{"id": "fake-model"},
                                                                {"id": "fake-tts"}]}


def test_backend_down(wyoming_servers):
    settings = Settings(stt_uri="tcp://127.0.0.1:1", tts_uri="tcp://127.0.0.1:1", wyoming_timeout=2)
    client = TestClient(create_app(settings))
    assert client.post("/v1/audio/speech", json={"input": "x"}).status_code == 502
    assert client.post("/v1/audio/transcriptions",
                       files={"file": ("a.wav", wav_bytes())}).status_code == 502


def test_disabled_endpoints():
    client = TestClient(create_app(Settings()))
    assert client.post("/v1/audio/speech", json={"input": "x"}).status_code == 503


def test_parse_aliases():
    assert _parse_aliases(" Alloy = a , nova=b,bad, =x") == {"alloy": "a", "nova": "b"}
