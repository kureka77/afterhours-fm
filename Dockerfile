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
