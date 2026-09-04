FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/tmp/youtube-m3u \
    TZ=Asia/Taipei

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg nodejs tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

RUN useradd --system --uid 10001 --create-home --home-dir /home/app app
COPY --chown=app:app app.py index.html ./

USER app
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["sh", "-c", "exec uvicorn app:app --host=0.0.0.0 --port=${PORT:-8080} --proxy-headers --forwarded-allow-ips='*' --no-access-log"]
