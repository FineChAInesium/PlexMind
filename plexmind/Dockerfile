FROM python:3.12-slim@sha256:dd29372629eeba2dd003fd9e9d35a5b8236c44727875a0364254b5127af88e65

WORKDIR /app

COPY plexmind/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY plexmind/app/ ./app/

RUN useradd -u 1000 -m plexmind && chown -R plexmind:plexmind /app
USER plexmind

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
