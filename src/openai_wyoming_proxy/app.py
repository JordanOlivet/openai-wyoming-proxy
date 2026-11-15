"""OpenAI-compatible audio API (/v1/audio/*) in front of Wyoming STT and TTS servers."""

from __future__ import annotations

import hmac
import json
import logging
import time
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.datastructures import UploadFile as StarletteUploadFile

from . import __version__
from .audio import SUPPORTED_FORMATS, AudioError, decode_for_stt, encode_tts
from .config import Settings
from .wyoming_client import WyomingError, describe, synthesize, transcribe

log = logging.getLogger("openai_wyoming_proxy")

STT_FORMATS = ("json", "text", "verbose_json", "srt", "vtt")
VOICE_CACHE_SECONDS = 300.0


class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str = "invalid_request_error"):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


def _error(status: int, message: str, code: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": code, "param": None, "code": code}},
        status_code=status,
    )


def _language(value: str | None) -> str | None:
    """'fr-FR' / 'FR' -> 'fr' (Wyoming servers expect a bare language code)."""
    value = (value or "").strip()
    return value.split("-")[0].split("_")[0].lower() if value else None


def _timestamp(seconds: float, sep: str) -> str:
    ms = int(round(seconds * 1000))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{ms:03d}"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="openai-wyoming-proxy", version=__version__, docs_url=None, redoc_url=None)
    voice_cache: dict[str, Any] = {"at": 0.0, "voices": None}

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _error(exc.status, exc.message, exc.code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(400, str(exc.errors()[:1]), "invalid_request_error")

    async def require_key(request: Request) -> None:
        if not settings.api_key:
            return
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not token or not hmac.compare_digest(token, settings.api_key):
            raise ApiError(401, "Invalid API key", "invalid_api_key")

    async def tts_voices() -> list[dict[str, Any]] | None:
        """Voices advertised by the TTS server, cached; None when it cannot be described."""
        if not settings.tts_uri:
            return None
        now = time.monotonic()
        if voice_cache["voices"] is not None and now - voice_cache["at"] < VOICE_CACHE_SECONDS:
            return voice_cache["voices"]
        try:
            info = await describe(settings.tts_uri, min(settings.wyoming_timeout, 10.0))
        except WyomingError as exc:
            log.warning("TTS describe failed: %s", exc)
            return voice_cache["voices"]
        voices = [
            {"id": voice.name, "name": voice.description or voice.name,
             "languages": list(voice.languages or [])}
            for program in info.tts for voice in program.voices
        ]
        voice_cache.update(at=now, voices=voices)
        return voices

    async def resolve_voice(requested: str | None) -> str | None:
        requested = (requested or "").strip()
        if not requested:
            return settings.tts_voice
        voices = await tts_voices()
        known = {voice["id"] for voice in voices or []}
        if requested in known:
            return requested
        alias = settings.tts_voice_aliases.get(requested.lower())
        if alias:
            return alias
        if voices is None:
            # Server cannot be described: pass the name through rather than guess.
            return requested
        # OpenAI clients send their own defaults (alloy...): fall back to ours.
        return settings.tts_voice

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__,
                "stt": bool(settings.stt_uri), "tts": bool(settings.tts_uri)}

    @app.get("/v1/models", dependencies=[Depends(require_key)])
    async def models() -> dict[str, Any]:
        data = []
        for kind, uri in (("stt", settings.stt_uri), ("tts", settings.tts_uri)):
            if not uri:
                continue
            names: list[str] = []
            try:
                info = await describe(uri, min(settings.wyoming_timeout, 10.0))
                if kind == "stt":
                    names = [model.name for program in info.asr for model in program.models]
                else:
                    names = [program.name for program in info.tts]
            except WyomingError as exc:
                log.warning("%s describe failed: %s", kind, exc)
            for name in names or [kind]:
                data.append({"id": name, "object": "model", "created": 0, "owned_by": f"wyoming-{kind}"})
        return {"object": "list", "data": data}

    # Open WebUI asks custom OpenAI TTS servers for these two lists.
    @app.get("/v1/audio/models", dependencies=[Depends(require_key)])
    async def audio_models() -> dict[str, Any]:
        listing = await models()
        return {"models": [{"id": item["id"]} for item in listing["data"]]}

    @app.get("/v1/audio/voices", dependencies=[Depends(require_key)])
    async def audio_voices() -> dict[str, Any]:
        return {"voices": [{"id": v["id"], "name": v["name"]} for v in await tts_voices() or []]}

    @app.post("/v1/audio/transcriptions", dependencies=[Depends(require_key)])
    async def transcriptions(request: Request) -> Response:
        if not settings.stt_uri:
            raise ApiError(503, "Speech-to-text is not configured (STT_URI)", "service_unavailable")
        max_bytes = int(settings.max_upload_mb * 1024 * 1024)
        if int(request.headers.get("content-length") or 0) > max_bytes + 65_536:
            raise ApiError(413, f"Upload larger than {settings.max_upload_mb:g} MB")
        form = await request.form()
        upload = form.get("file")
        if not isinstance(upload, StarletteUploadFile):
            raise ApiError(400, "Missing 'file' upload")
        data = await upload.read()
        if not data:
            raise ApiError(400, "Uploaded file is empty")
        if len(data) > max_bytes:
            raise ApiError(413, f"Upload larger than {settings.max_upload_mb:g} MB")
        response_format = str(form.get("response_format") or "json").lower()
        if response_format not in STT_FORMATS:
            raise ApiError(400, f"Unsupported response_format '{response_format}'")
        language = _language(str(form.get("language") or "")) or settings.stt_language

        try:
            audio = await decode_for_stt(data, settings.ffmpeg_bin)
        except AudioError as exc:
            raise ApiError(400, f"Could not decode audio: {exc}") from exc
        if not audio.data:
            text = ""
        else:
            try:
                text = await transcribe(settings.stt_uri, audio, language, settings.wyoming_timeout)
            except WyomingError as exc:
                raise ApiError(502, f"STT backend error: {exc}", "upstream_error") from exc
        log.info("stt: %.1fs audio (%s) -> %d chars", audio.duration, upload.filename, len(text))

        if response_format == "text":
            return PlainTextResponse(text)
        if response_format == "json":
            return JSONResponse({"text": text})
        duration = audio.duration
        if response_format == "verbose_json":
            segment = {"id": 0, "seek": 0, "start": 0.0, "end": round(duration, 3), "text": text,
                       "tokens": [], "temperature": 0.0, "avg_logprob": 0.0,
                       "compression_ratio": 0.0, "no_speech_prob": 0.0}
            return JSONResponse({"task": "transcribe", "language": language or "",
                                 "duration": round(duration, 3), "text": text,
                                 "segments": [segment] if text else []})
        if response_format == "srt":
            body = f"1\n{_timestamp(0, ',')} --> {_timestamp(duration, ',')}\n{text}\n" if text else ""
            return PlainTextResponse(body)
        body = "WEBVTT\n\n" + (f"{_timestamp(0, '.')} --> {_timestamp(duration, '.')}\n{text}\n" if text else "")
        return PlainTextResponse(body, media_type="text/vtt")

    @app.post("/v1/audio/speech", dependencies=[Depends(require_key)])
    async def speech(request: Request) -> Response:
        if not settings.tts_uri:
            raise ApiError(503, "Text-to-speech is not configured (TTS_URI)", "service_unavailable")
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        params: dict[str, Any] = dict(request.query_params)
        raw = await request.body()
        if content_type == "text/plain":
            # Shell-friendly variant: text as the body, options in the query string
            # (curl --data-binary @file 'http://.../v1/audio/speech?response_format=mp3').
            params["input"] = raw.decode("utf-8", "replace")
        else:
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError as exc:
                raise ApiError(400, f"Invalid JSON body: {exc}") from exc
            if not isinstance(body, dict):
                raise ApiError(400, "JSON body must be an object")
            params.update(body)

        text = str(params.get("input") or "").strip()
        if not text:
            raise ApiError(400, "'input' is required")
        if len(text) > settings.max_tts_chars:
            raise ApiError(400, f"'input' is longer than {settings.max_tts_chars} characters")
        fmt = str(params.get("response_format") or "mp3").lower()
        if fmt not in SUPPORTED_FORMATS:
            raise ApiError(400, f"Unsupported response_format '{fmt}'")
        try:
            speed = float(params.get("speed") or 1.0)
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "'speed' must be a number") from exc
        if not 0.25 <= speed <= 4.0:
            raise ApiError(400, "'speed' must be between 0.25 and 4.0")
        voice = await resolve_voice(params.get("voice"))

        started = time.monotonic()
        try:
            audio = await synthesize(settings.tts_uri, text, voice, settings.tts_language,
                                     settings.tts_speaker, settings.wyoming_timeout)
        except WyomingError as exc:
            raise ApiError(502, f"TTS backend error: {exc}", "upstream_error") from exc
        try:
            payload, media_type = await encode_tts(audio, fmt, speed, settings.ffmpeg_bin)
        except AudioError as exc:
            raise ApiError(500, f"Could not encode audio: {exc}", "server_error") from exc
        log.info("tts: %d chars, voice=%s -> %.1fs %s in %.2fs", len(text), voice or "default",
                 audio.duration, fmt, time.monotonic() - started)
        return Response(payload, media_type=media_type)

    return app
