# openai-wyoming-proxy

An OpenAI-compatible audio API in front of [Wyoming](https://github.com/OHF-Voice/wyoming)
speech servers, so tools that speak the OpenAI audio API (Open WebUI, Hermes Agent and its web UIs,
scripts with `curl`...) can reuse the same local STT/TTS as Home Assistant Assist.

```
client ──HTTP /v1/audio/transcriptions──► proxy ──ffmpeg → 16 kHz PCM──► Wyoming STT (e.g. Parakeet)
client ──HTTP /v1/audio/speech─────────► proxy ◄──────── PCM ─────────── Wyoming TTS (e.g. Pocket TTS)
                                           └── ffmpeg → mp3 / opus / aac / flac / wav / pcm
```

This is the opposite direction of [wyoming-openai](https://github.com/roryeckel/wyoming_openai),
which exposes OpenAI-compatible services *as* Wyoming servers.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| `POST` | `/v1/audio/transcriptions` | Multipart `file` in any format ffmpeg reads (webm/opus from browsers, ogg voice notes, m4a, mp3, wav...). `language` (optional, `fr-FR` → `fr`), `response_format` = `json` (default), `text`, `verbose_json`, `srt`, `vtt`. `model` and `prompt` are accepted and ignored. |
| `POST` | `/v1/audio/speech` | JSON `{input, voice, response_format, speed}`. Formats: `mp3` (default), `opus`, `aac`, `flac`, `wav`, `pcm` (24 kHz s16le), plus `ogg` and `m4a`. `speed` 0.25-4.0. `model` and `instructions` are ignored. |
| `POST` | `/v1/audio/speech` (`Content-Type: text/plain`) | Shell-friendly variant: the body is the text, options go in the query string. |
| `GET` | `/v1/audio/voices`, `/v1/audio/models`, `/v1/models` | Read from the servers' Wyoming `describe` (Open WebUI uses the first two). |
| `GET` | `/health` | Liveness, no auth. |

Voice selection: a voice the TTS server knows is used as is; a name listed in `TTS_VOICE_ALIASES` is
mapped; anything else (OpenAI defaults such as `alloy`) falls back to `TTS_VOICE`.

## Configuration

| Variable | Default | |
|---|---|---|
| `STT_URI` | unset | Wyoming STT server, e.g. `tcp://wyoming-stt:10300`. Unset = transcription returns 503. |
| `TTS_URI` | unset | Wyoming TTS server, e.g. `tcp://tts-pocket:10200`. Unset = speech returns 503. |
| `STT_LANGUAGE` | server default | Language when the request sets none. |
| `TTS_VOICE` | server default | Default / fallback voice. |
| `TTS_LANGUAGE`, `TTS_SPEAKER` | unset | Sent with the voice. |
| `TTS_VOICE_ALIASES` | none | `alloy=estelle,nova=other-voice`. |
| `API_KEY` | unset | When set, `Authorization: Bearer <key>` is required (except `/health`). Clients that insist on a key can send anything when it is unset. |
| `MAX_UPLOAD_MB` | `25` | Transcription upload limit. |
| `MAX_TTS_CHARS` | `10000` | Speech input limit. |
| `WYOMING_TIMEOUT` | `60` | Seconds for one Wyoming round trip. |
| `HOST`, `PORT` | `0.0.0.0`, `8000` | Listen address. |
| `LOG_LEVEL`, `ACCESS_LOG` | `INFO`, `false` | |

See [`compose.example.yml`](compose.example.yml). The image is published to
`ghcr.io/jordanolivet/openai-wyoming-proxy` (`latest` = main, `X.Y.Z` / `X.Y` = tags `vX.Y.Z`).

## Client recipes

**curl**

```bash
curl -F file=@note.ogg -F response_format=text http://proxy:8000/v1/audio/transcriptions
curl -H 'Content-Type: application/json' -d '{"input":"Bonjour","response_format":"mp3"}' \
  http://proxy:8000/v1/audio/speech -o bonjour.mp3
```

**Hermes Agent** (`config.yaml`, command providers: works for every Hermes surface, Discord voice
notes and browser recordings included, without Python or ffmpeg in the Hermes container)

```yaml
stt:
  provider: wyoming
  providers:
    wyoming:
      type: command
      command: curl -sSf --max-time 120 -F file=@{input_path} -F response_format=text http://openai-wyoming-proxy:8000/v1/audio/transcriptions
tts:
  provider: wyoming
  providers:
    wyoming:
      type: command
      # quoted: the ': ' in the header would otherwise break the YAML
      command: "curl -sSf --max-time 120 -H 'Content-Type: text/plain; charset=utf-8' --data-binary @{text_path} -o {output_path} 'http://openai-wyoming-proxy:8000/v1/audio/speech?response_format={format}'"
```

**Open WebUI**: Admin Settings → Audio → STT engine *OpenAI*, TTS engine *OpenAI*, base URL
`http://openai-wyoming-proxy:8000/v1` for both, any API key (unless `API_KEY` is set).

## Development

Ideas, measurements and the image-size plan: [`docs/roadmap.md`](docs/roadmap.md).


```bash
docker build --target test .          # test suite with fake Wyoming servers (needs ffmpeg, hence Docker)
docker build --target runtime -t openai-wyoming-proxy .
```

Or locally: `pip install -e '.[test]' && pytest` (ffmpeg on the `PATH`).
