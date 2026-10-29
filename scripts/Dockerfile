FROM python:3.13.18-alpine@sha256:bbc0290146ebe0a8b9a8ed07d3f2c7bdeb4d917fc93300a4e47490c8b5a11b48

ENV TZ=UTC

WORKDIR /app

RUN apk add --no-cache \
    bash curl file jq ffmpeg tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone

COPY lib.sh transcribe.sh translate.sh maintenance.sh watermark.sh stop-job.sh \
     control_server.py fix_srt_ordering.py repair_srt_timestamps.py dedup_subs.py translation_cue.py entrypoint.sh \
     pgs_cleanup.py srt_encoding.py ./

RUN chmod +x *.sh

ENTRYPOINT ["/app/entrypoint.sh"]
