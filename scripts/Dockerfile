FROM python:3.12-slim@sha256:dd29372629eeba2dd003fd9e9d35a5b8236c44727875a0364254b5127af88e65

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
