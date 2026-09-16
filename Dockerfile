# syntax=docker/dockerfile:1

# ---------------------------------------------------------------- build stage
# Dependencies are installed into a venv here so the final image carries neither
# pip nor any build tooling.
FROM python:3.14-slim-trixie AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install -r /tmp/requirements.txt

# --------------------------------------------------------------- runtime stage
FROM python:3.14-slim-trixie

LABEL org.opencontainers.image.title="docker-exporter" \
      org.opencontainers.image.description="Prometheus exporter for Docker container lifecycle and health metrics" \
      org.opencontainers.image.source="https://github.com/Artem-Glebov/docker-exporter" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    EXPORTER_PORT=8088

# Fixed high UID/GID: the exporter never touches the Docker socket (it talks to
# the socket proxy over TCP), so it has no reason to match a host group.
RUN groupadd --gid 10001 exporter \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin exporter

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY docker_exporter.py /app/docker_exporter.py

USER 10001:10001

EXPOSE 8088

# /healthz deliberately does not call Docker, so a daemon outage does not make
# the container itself look unhealthy. slim has no curl, hence urllib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request; p=os.environ.get('EXPORTER_PORT','8088'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz', timeout=3).status==200 else 1)"]

ENTRYPOINT ["python", "/app/docker_exporter.py"]
