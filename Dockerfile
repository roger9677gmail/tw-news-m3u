FROM denoland/deno:bin-2.9.6 AS deno

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

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && deno --version

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
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).read()" || exit 1

CMD ["uvicorn", "app.main:app", "--host=0.0.0.0", "--port=8000", "--proxy-headers", "--forwarded-allow-ips=*", "--no-access-log"]
