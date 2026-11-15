"""Audio conversions through ffmpeg (decode any upload, encode TTS output)."""

from __future__ import annotations

import asyncio
import io
import os
import tempfile
import wave

from .wyoming_client import PcmAudio

STT_RATE = 16_000

# OpenAI response_format (plus a few aliases Hermes uses) -> (ffmpeg args, content type)
_ENCODINGS: dict[str, tuple[list[str], str]] = {
    "mp3": (["-c:a", "libmp3lame", "-q:a", "4", "-f", "mp3"], "audio/mpeg"),
    "opus": (["-c:a", "libopus", "-b:a", "48k", "-f", "ogg"], "audio/ogg"),
    "ogg": (["-c:a", "libopus", "-b:a", "48k", "-f", "ogg"], "audio/ogg"),
    "aac": (["-c:a", "aac", "-b:a", "96k", "-f", "adts"], "audio/aac"),
    "m4a": (["-c:a", "aac", "-b:a", "96k", "-f", "ipod", "-movflags", "frag_keyframe+empty_moov"],
            "audio/mp4"),  # fragmented: the output is a pipe, MP4 cannot seek back
    "flac": (["-c:a", "flac", "-f", "flac"], "audio/flac"),
    "wav": (["-c:a", "pcm_s16le", "-f", "wav"], "audio/wav"),
    # OpenAI "pcm" = raw 24 kHz 16-bit mono little-endian
    "pcm": (["-c:a", "pcm_s16le", "-ar", "24000", "-ac", "1", "-f", "s16le"], "audio/pcm"),
}

SUPPORTED_FORMATS = frozenset(_ENCODINGS)


class AudioError(RuntimeError):
    """ffmpeg could not decode or encode the audio."""


async def _run_ffmpeg(ffmpeg: str, args: list[str], stdin: bytes | None = None) -> bytes:
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise AudioError(f"ffmpeg not found ({ffmpeg})") from exc
    out, err = await proc.communicate(stdin)
    if proc.returncode != 0:
        detail = err.decode("utf-8", "replace").strip().splitlines()
        raise AudioError(detail[-1] if detail else f"ffmpeg exited with {proc.returncode}")
    return out


async def decode_for_stt(data: bytes, ffmpeg: str = "ffmpeg") -> PcmAudio:
    """Decode any container/codec ffmpeg knows into 16 kHz mono s16le."""
    # A temp file instead of a pipe: MP4/M4A uploads often keep their index at the end
    # and cannot be demuxed from a non-seekable stream.
    fd, path = tempfile.mkstemp(prefix="owp-stt-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        pcm = await _run_ffmpeg(
            ffmpeg, ["-i", path, "-vn", "-ac", "1", "-ar", str(STT_RATE), "-f", "s16le", "pipe:1"]
        )
    finally:
        os.unlink(path)
    return PcmAudio(pcm, STT_RATE, 2, 1)


def _atempo_chain(speed: float) -> str:
    """ffmpeg atempo only accepts 0.5..2.0 per stage: chain stages for wider ranges."""
    stages = []
    while speed > 2.0:
        stages.append(2.0)
        speed /= 2.0
    while speed < 0.5:
        stages.append(0.5)
        speed /= 0.5
    stages.append(speed)
    return ",".join(f"atempo={stage:.4f}" for stage in stages)


def pcm_to_wav(audio: PcmAudio) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(audio.channels)
        wav.setsampwidth(audio.width)
        wav.setframerate(audio.rate)
        wav.writeframes(audio.data)
    return buffer.getvalue()


async def encode_tts(
    audio: PcmAudio, fmt: str, speed: float = 1.0, ffmpeg: str = "ffmpeg"
) -> tuple[bytes, str]:
    """Encode raw PCM from the TTS server into the requested response format."""
    args, content_type = _ENCODINGS[fmt]
    if fmt == "wav" and abs(speed - 1.0) < 1e-3:
        return pcm_to_wav(audio), content_type
    if audio.width != 2:
        raise AudioError(f"unsupported TTS sample width {audio.width}")
    filters = ["-af", _atempo_chain(speed)] if abs(speed - 1.0) >= 1e-3 else []
    out = await _run_ffmpeg(
        ffmpeg,
        ["-f", "s16le", "-ar", str(audio.rate), "-ac", str(audio.channels), "-i", "pipe:0",
         *filters, *args, "pipe:1"],
        stdin=audio.data,
    )
    return out, content_type
