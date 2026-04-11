FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl jq cron ffmpeg docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY lib.sh transcribe.sh translate.sh maintenance.sh watermark.sh \
     fix_srt_ordering.py entrypoint.sh ./

RUN chmod +x *.sh

ENTRYPOINT ["/app/entrypoint.sh"]
