# Kinoshelf: one image with the web app, the background worker, ffmpeg and the IBM Plex fonts.
ARG PYTHON_IMAGE=python:3.13.11-slim-trixie
# Static ffmpeg and ffprobe (two files) instead of Debian's ffmpeg package keeps the image small.
ARG FFMPEG_IMAGE=mwader/static-ffmpeg:8.1.2

FROM ${FFMPEG_IMAGE} AS ffmpeg

FROM ${PYTHON_IMAGE} AS fonts
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY scripts/fetch-fonts.sh /fetch-fonts.sh
RUN sh /fetch-fonts.sh /fonts

FROM ${PYTHON_IMAGE}
# The commit this image was built from; the updater compares it with the registry's newest image.
ARG KS_REVISION=""
LABEL org.opencontainers.image.source="https://github.com/kamyar94/kinoshelf" \
      org.opencontainers.image.title="Kinoshelf" \
      org.opencontainers.image.licenses="LicenseRef-KF-SA-1.0"
ENV KS_REVISION=${KS_REVISION} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    KS_DATA_DIR=/data
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ffmpeg /ffmpeg /ffprobe /usr/local/bin/
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN pip install -r /app/requirements.lock
COPY pyproject.toml README.md LICENSE.md LICENSING.md /app/
COPY src /app/src
COPY --from=fonts /fonts/ /app/src/kinoshelf/web/static/fonts/
RUN pip install --no-deps /app && rm -rf /app/src/kinoshelf/__pycache__ \
    && python -c "import kinoshelf.web" \
    && mkdir -p /data && chown 10001:10001 /data
USER 10001:10001
EXPOSE 8484
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8484/api/health',timeout=5)" || exit 1
CMD ["kinoshelf", "serve"]
