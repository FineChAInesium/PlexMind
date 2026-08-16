FROM python:3.13.15-alpine@sha256:540c7d91f98ff6880174c40e99067bf5941eb54d818a7a5e094d188b196a934d

WORKDIR /app

COPY plexmind/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY plexmind/app/ ./app/

RUN adduser -D -u 1000 plexmind && chown -R plexmind:plexmind /app
USER plexmind

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
