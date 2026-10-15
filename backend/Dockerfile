# HuRI website backend — OIDC relying party + browser<->HuRI websocket bridge.
# Build context is this directory (backend/).
#
#   docker build -t <region>-docker.pkg.dev/<project>/<repo>/huri-website:latest .
#   docker push  <region>-docker.pkg.dev/<project>/<repo>/huri-website:latest
#
# The HuRI session goes through huri-core's client SDK and huri-events, both
# installed from git by requirements.txt (hence git below); the HuRI server
# itself is not in this image, point HURI_URL at one.
#
# TODO(deploy): huri_presets.py reads the repo-root `presets/` folder (a
# sibling of this backend/ directory), which is outside this image's
# build context. Either widen the build context to the repo root and
# `COPY ../presets ./presets`, or mount presets/ as a volume at runtime and
# point HURI_PRESETS_DIR at it — otherwise the image falls back to no presets
# (the Event Configuration modal still works with a custom combination).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# libsndfile backs soundfile/librosa at runtime; git installs huri-core and
# huri-events from their repositories (requirements.txt).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libsndfile1 git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run unprivileged.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
