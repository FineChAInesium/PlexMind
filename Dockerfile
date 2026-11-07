FROM python:3.13.18-alpine@sha256:bbc0290146ebe0a8b9a8ed07d3f2c7bdeb4d917fc93300a4e47490c8b5a11b48

ENV TZ=UTC

WORKDIR /app

# apk upgrade pulls in security fixes released since the pinned base image
# (zlib 1.3.2 -> 1.4 for CVE-2026-4738 failed the v0.9.4 release scan).
RUN apk upgrade --no-cache \
    && apk add --no-cache \
    bash curl file jq ffmpeg tini tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone

COPY plexmind/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY scripts/lib.sh scripts/transcribe.sh scripts/translate.sh scripts/maintenance.sh scripts/watermark.sh \
     scripts/stop-job.sh scripts/control_server.py scripts/docker_broker.py scripts/supervisor.py \
     scripts/fix_srt_ordering.py scripts/repair_srt_timestamps.py scripts/dedup_subs.py \
     scripts/translation_cue.py scripts/pgs_cleanup.py scripts/srt_encoding.py scripts/subtitle_inventory.py ./
COPY plexmind/app/ ./app/

# The API and recommendation worker run as this user; supervisor.py starts them.
RUN chmod +x *.sh && adduser -D -u 1000 plexmind && chown -R plexmind:plexmind /app/app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)"]

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["python3", "/app/supervisor.py"]
