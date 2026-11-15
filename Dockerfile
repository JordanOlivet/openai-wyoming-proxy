# syntax=docker/dockerfile:1
FROM python:3.13-slim-trixie AS base
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

# `docker build --target test .` runs the test suite (CI does this before building the image)
FROM base AS test
RUN pip install ".[test]"
COPY tests ./tests
RUN pytest -q

FROM base AS runtime
RUN pip install . \
    && useradd --system --uid 10001 --no-create-home owp
USER owp
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/health' % os.getenv('PORT','8000'), timeout=4)"]
CMD ["openai-wyoming-proxy"]
