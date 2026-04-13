FROM python:3.12-slim

RUN pip install --no-cache-dir docker prometheus_client

COPY docker_exporter.py /app/docker_exporter.py

WORKDIR /app
EXPOSE 8088
CMD ["python", "docker_exporter.py"]