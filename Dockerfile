# ── base: production Python dependencies ─────────────────────────────────────
FROM python:3.14-slim AS base

WORKDIR /app

# ffmpeg: vibra (Shazam-recognition fallback) shells out to it internally to
# decode non-WAV clips (MP3 straight off a live stream) before fingerprinting.
# cmake/build-essential/libcurl4-openssl-dev: only needed to compile vibra's
# C++ extension during `pip install` below — the resulting .so is what ships,
# not these tools, but this single-stage Dockerfile doesn't split build vs
# runtime, so they stay in the image. Traded simplicity for ~150MB of image
# bloat; revisit with a proper build stage if that ever matters.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    cmake \
    build-essential \
    libcurl4-openssl-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# Upgrade the toolchain that ships inside python:3.14-slim before installing
# anything. The base image pins a setuptools with CVE-2025-47273 (path
# traversal in PackageIndex) and a pip whose vendored msgpack carries
# GHSA-6v7p-g79w-8964 — both HIGH, both with fixes available, and both found by
# the Trivy job in ci.yml. Neither is imported by the app, but they are bytes we
# ship, and "we never call it" is not a defence a scanner accepts nor a habit
# worth forming.
RUN pip install --no-cache-dir --upgrade pip setuptools

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── dev: adds dev deps, runs with hot-reload ──────────────────────────────────
FROM base AS dev

COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY . .

CMD ["uvicorn", "main:app", "--reload", "--host", "0.0.0.0", "--port", "8000"]

# ── prod: lean image, no reload, DB stored in /data volume ───────────────────
FROM base AS prod

COPY . .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
