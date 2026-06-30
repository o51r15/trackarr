FROM python:3.12-slim

LABEL org.opencontainers.image.title="Trackarr" \
      org.opencontainers.image.description="Automated BitTorrent tracker management for qBittorrent" \
      org.opencontainers.image.source="https://github.com/o51r15/trackarr"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/
COPY config.example.json .
COPY tracker_urls.txt .

RUN mkdir -p /app/data

EXPOSE 7374
VOLUME ["/app/data"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7374", "--log-level", "info"]
