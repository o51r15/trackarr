FROM mcr.microsoft.com/powershell:7-ubuntu-22.04

LABEL org.opencontainers.image.title="Trackarr" \
      org.opencontainers.image.description="Automated BitTorrent tracker management for qBittorrent" \
      org.opencontainers.image.source="https://github.com/o51r15/trackarr"

WORKDIR /app

# Install Python + ping dependencies
RUN apt-get update && apt-get install -y python3 python3-pip --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
COPY ping/requirements.txt /app/ping/requirements.txt
RUN pip3 install --no-cache-dir -r /app/ping/requirements.txt

# Install trackerping binary
COPY ping/trackerping.py /usr/local/bin/trackerping
RUN chmod +x /usr/local/bin/trackerping

# Copy bridge and scripts
COPY trackerping.ps1        .
COPY tracker-discovery.ps1  .
COPY trackarr-bridge.ps1    .
COPY trackarr-gui.html      .
COPY tracker_urls.txt       .

RUN mkdir -p /app/tracker-data /data
RUN echo '{"port":7374}' > /app/bridge-config.json

EXPOSE 7374

VOLUME ["/app/tracker-data", "/data"]

# Default entrypoint runs the bridge.
# The bridge calls: docker run --rm ... trackarr trackerping -l -o ...
# which re-uses this same image as the ephemeral ping runner.
ENTRYPOINT ["pwsh", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", "/app/trackarr-bridge.ps1"]
