FROM denoland/deno:bin-2.9.6 AS deno

FROM brainicism/bgutil-ytdlp-pot-provider:1.3.2 AS pot-provider

FROM python:3.12-slim-bookworm

ARG APP_VERSION=1.0.0
LABEL org.opencontainers.image.title="Taiwan News M3U Relay" \
      org.opencontainers.image.description="On-demand HLS relay for official public Taiwan news livestreams" \
      org.opencontainers.image.version="${APP_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    XDG_CACHE_HOME=/tmp/.cache \
    TZ=Asia/Taipei

COPY --from=deno /deno /usr/local/bin/deno
COPY --from=pot-provider /usr/local/bin/node /usr/local/bin/node
COPY --from=pot-provider /app /opt/bgutil-provider

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && deno --version \
    && node --version

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

RUN useradd --system --uid 10001 --create-home --home-dir /home/app app
COPY --chown=app:app app ./app
COPY --chown=app:app channels.json ./channels.json

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os, urllib.request; port=os.getenv('PORT', '8000'); urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=3).read()" || exit 1

ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["sh", "-c", "node /opt/bgutil-provider/build/main.js --port 4416 & exec uvicorn app.main:app --host=0.0.0.0 --port=${PORT:-8000} --proxy-headers --forwarded-allow-ips='*' --no-access-log"]
