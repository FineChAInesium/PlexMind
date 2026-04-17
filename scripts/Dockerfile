FROM python:3.12-slim

ENV TZ=UTC

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash curl file jq ffmpeg tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

COPY lib.sh transcribe.sh translate.sh maintenance.sh watermark.sh stop-job.sh \
     control_server.py fix_srt_ordering.py entrypoint.sh ./

RUN chmod +x *.sh

ENTRYPOINT ["/app/entrypoint.sh"]
