"""Runtime settings, read once from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw is not None else default


def _parse_aliases(raw: str | None) -> dict[str, str]:
    """Parse ``alloy=cml12080,nova=estelle`` into a lowercase-keyed mapping."""
    aliases: dict[str, str] = {}
    for item in (raw or "").split(","):
        if "=" not in item:
            continue
        alias, target = (part.strip() for part in item.split("=", 1))
        if alias and target:
            aliases[alias.lower()] = target
    return aliases


@dataclass(frozen=True)
class Settings:
    # Wyoming backends, e.g. tcp://wyoming-stt:10300. An unset URI disables the endpoint.
    stt_uri: str | None = None
    tts_uri: str | None = None
    # Language sent to the STT server when the request does not set one (None = server default).
    stt_language: str | None = None
    # Voice used when the request names no voice, or an unknown one (None = server default).
    tts_voice: str | None = None
    # Language sent with the TTS voice (None = let the server decide).
    tts_language: str | None = None
    tts_speaker: str | None = None
    # OpenAI voice names (alloy, nova...) mapped to Wyoming voices.
    tts_voice_aliases: dict[str, str] = field(default_factory=dict)
    # Optional bearer token. When unset, the API is open (keep it on a private network).
    api_key: str | None = None
    max_upload_mb: float = 25.0
    max_tts_chars: int = 10_000
    # Seconds allowed for one Wyoming round trip (connect + full answer).
    wyoming_timeout: float = 60.0
    ffmpeg_bin: str = "ffmpeg"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            stt_uri=_env("STT_URI"),
            tts_uri=_env("TTS_URI"),
            stt_language=_env("STT_LANGUAGE"),
            tts_voice=_env("TTS_VOICE"),
            tts_language=_env("TTS_LANGUAGE"),
            tts_speaker=_env("TTS_SPEAKER"),
            tts_voice_aliases=_parse_aliases(_env("TTS_VOICE_ALIASES")),
            api_key=_env("API_KEY"),
            max_upload_mb=_env_float("MAX_UPLOAD_MB", 25.0),
            max_tts_chars=int(_env_float("MAX_TTS_CHARS", 10_000)),
            wyoming_timeout=_env_float("WYOMING_TIMEOUT", 60.0),
            ffmpeg_bin=_env("FFMPEG_BIN", "ffmpeg") or "ffmpeg",
        )
