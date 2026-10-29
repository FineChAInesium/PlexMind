FROM python:3.13.18-alpine@sha256:bbc0290146ebe0a8b9a8ed07d3f2c7bdeb4d917fc93300a4e47490c8b5a11b48

WORKDIR /app

COPY plexmind/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY plexmind/app/ ./app/

RUN adduser -D -u 1000 plexmind && chown -R plexmind:plexmind /app
USER plexmind

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
