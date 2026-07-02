FROM python:3.12-slim

LABEL org.opencontainers.image.title="Trackarr" \
      org.opencontainers.image.description="Automated BitTorrent tracker management for qBittorrent" \
      org.opencontainers.image.source="https://github.com/o51r15/trackarr"

WORKDIR /app

# Install Docker CLI so Trackarr can spawn ephemeral ping containers via the
# mounted Docker socket (/var/run/docker.sock) when VPN_CONTAINER is set.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    chmod a+r /etc/apt/keyrings/docker.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends docker-ce-cli && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

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
