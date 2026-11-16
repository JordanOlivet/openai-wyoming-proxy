# Roadmap and ideas

Not urgent: everything works as is (v0.1.0). Notes kept here so they live next to the code.

## Shrink the image (~830 MB uncompressed, amd64)

Where the size comes from (`docker history`, v0.1.0):

| Layer | Size |
|---|---|
| `apt-get install ffmpeg` (Debian trixie, ffmpeg 7.1) | **457 MB** |
| `python:3.13-slim-trixie` base (Debian + Python) | ~133 MB |
| Python deps (`pip install .`: fastapi, uvicorn, wyoming...) | 20 MB |
| Our code | < 0.1 MB |

The Debian `ffmpeg` package pulls the full codec/filter set and their libraries (video codecs, X11/VA-API/Vulkan
bits, SDL, etc.). We only use a handful of audio features:

- **Decode** (STT uploads): demuxers `matroska/webm`, `ogg`, `mov/mp4/m4a`, `wav`, `mp3`, `flac`, `aac`; decoders
  `opus`, `vorbis`, `aac`, `mp3`, `flac`, `pcm_*`.
- **Encode** (TTS output): `libmp3lame` (mp3), `libopus` (opus/ogg), native `aac` (aac/m4a), `flac`, `pcm_s16le`;
  muxers `mp3`, `ogg`, `adts`, `ipod` (fragmented), `flac`, `wav`, `s16le`.
- **Filters**: `aresample` (implicit, `-ar`/`-ac`) and `atempo` (speed).

Options, from least to most effort:

1. **Multi-stage build of a minimal static ffmpeg** (`--disable-everything` then enable only the list above,
   `--enable-libmp3lame --enable-libopus`, static link) copied into a slim image. Expected: ffmpeg ~10-20 MB, image
   ~150-170 MB. Cost: a build stage to maintain (CI build time goes up, cache it); bump ffmpeg on our own schedule.
2. **Prebuilt static ffmpeg** (e.g. `mwader/static-ffmpeg`): no build to maintain, but it is a full build (~300 MB
   image, ffmpeg binary itself ~80-100 MB), so the gain is smaller.
3. **Alpine base** (`python:3.13-alpine` + `apk add ffmpeg`): Alpine's ffmpeg is also full-featured but its dependency
   tree is smaller than Debian's; worth a quick measurement before option 1. Watch musl wheels (all our deps are
   pure Python or have musllinux wheels).
4. **Drop ffmpeg entirely** (in-process decode/encode with Python bindings such as PyAV, which bundles its own
   ffmpeg libs, ~60-100 MB): saves the process spawn (see below) but not obviously the size. Not worth it alone.

Test plan for any of these: `docker build --target test .` (the suite already covers webm/opus decode and every
output format), then the end-to-end round trip against the real servers (TTS -> STT in mp3/opus/wav).

## Performance (measured 2026-11-16, decided: stay in Python)

Medians over 5 runs, short French sentence, Ryzen 3700x CPU (Parakeet TDT 0.6B v3 + Pocket TTS on CPU):

| Path | Direct Wyoming | Through the proxy | Overhead |
|---|---|---|---|
| STT, wav upload | 180 ms | 273 ms | ~+90 ms |
| STT, mp3 upload | 180 ms | 224 ms | ~+45 ms |
| TTS, wav output | 768 ms | 781 ms | ~+15 ms |
| TTS, mp3 output | 768 ms | 887 ms | ~+120 ms |

ffmpeg alone: decode mp3 ~64 ms, encode mp3 ~70 ms (mostly process spawn + codec init). The Python/HTTP part is in
the low milliseconds, so a Rust rewrite would gain almost nothing that matters next to the models; ffmpeg would still
be needed. Memory: ~38 MiB resident.

If latency ever matters, cheaper wins than a rewrite:

- **Skip ffmpeg for WAV in, WAV/PCM out** when the input is already 16 kHz mono s16le (parse it with `wave`), and
  resample other PCM WAV in Python only if needed.
- **Stream TTS**: forward Wyoming audio chunks as they arrive (chunked HTTP, wav/pcm directly, mp3/opus through an
  ffmpeg pipe) so playback starts before synthesis ends. Only helps clients that play streamed audio (hermes-webui
  and Open WebUI currently buffer the whole response).
- **Wyoming streaming synthesis** (`SynthesizeStart/Chunk/Stop`, `supports_synthesize_streaming`) for long texts.

## Other ideas

- `/v1/audio/translations` (Whisper-style translate-to-English) if a backend ever supports it.
- Per-request `language` for TTS when the server has multilingual voices.
- Prometheus metrics (request counts, backend latency) if the proxy ends up serving more clients.
