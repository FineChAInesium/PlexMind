FROM python:3.13.15-alpine@sha256:540c7d91f98ff6880174c40e99067bf5941eb54d818a7a5e094d188b196a934d

ENV TZ=UTC

WORKDIR /app

RUN apk add --no-cache \
    bash curl file jq ffmpeg tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone

COPY lib.sh transcribe.sh translate.sh maintenance.sh watermark.sh stop-job.sh \
     control_server.py fix_srt_ordering.py repair_srt_timestamps.py entrypoint.sh ./

RUN chmod +x *.sh

ENTRYPOINT ["/app/entrypoint.sh"]
