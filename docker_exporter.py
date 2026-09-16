#!/usr/bin/env python3
"""Prometheus exporter for Docker container lifecycle and health metrics.

Metrics are collected at scrape time by a custom ``Collector`` rather than by a
background polling loop. That keeps the exposed values consistent with the moment
Prometheus asked for them, and makes series for removed containers disappear on
their own instead of having to be pruned by hand.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Iterable, Iterator
from wsgiref.simple_server import WSGIRequestHandler, make_server

import docker
import requests
from docker.errors import DockerException, NotFound
from prometheus_client import CollectorRegistry, make_wsgi_app
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.exposition import ThreadingWSGIServer
from prometheus_client.registry import Collector

__version__ = "1.0.0"

log = logging.getLogger("docker_exporter")

# Every lifecycle state the Docker daemon can report for a container. One series is
# emitted per state so that PromQL can group on it, rather than encoding the state
# as a magic number in the metric value.
CONTAINER_STATES = (
    "created",
    "restarting",
    "running",
    "removing",
    "paused",
    "exited",
    "dead",
)

# "none" is what the daemon reports for containers without a HEALTHCHECK.
HEALTH_STATUSES = ("none", "starting", "healthy", "unhealthy")

VALUE_LABELS = ["container_id", "container_name", "image"]

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"


# --------------------------------------------------------------------------- config


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer, falling back to %d", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    log.warning("%s=%r is not a boolean, falling back to %s", name, raw, default)
    return default


class Config:
    """Runtime configuration, entirely environment driven."""

    def __init__(self) -> None:
        self.port = _env_int("EXPORTER_PORT", 8088)
        self.bind_address = _env_str("EXPORTER_BIND_ADDRESS", "0.0.0.0")
        self.docker_timeout = _env_int("DOCKER_TIMEOUT_SECONDS", 10)
        self.inspect_workers = max(1, _env_int("EXPORTER_INSPECT_WORKERS", 8))
        self.include_stopped = _env_bool("EXPORTER_INCLUDE_STOPPED", True)
        self.label_filter = [
            item.strip()
            for item in _env_str("EXPORTER_LABEL_FILTER").split(",")
            if item.strip()
        ]
        self.log_level = _env_str("LOG_LEVEL", "INFO").upper()
        self.log_format = _env_str("LOG_FORMAT", "text").lower()

    def describe(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "bind_address": self.bind_address,
            "docker_host": os.environ.get("DOCKER_HOST", "<default socket>"),
            "docker_timeout": self.docker_timeout,
            "inspect_workers": self.inspect_workers,
            "include_stopped": self.include_stopped,
            "label_filter": self.label_filter,
        }


# -------------------------------------------------------------------------- logging


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def setup_logging(config: Config) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if config.log_format == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s")
        )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, config.log_level, logging.INFO))
    # The SDK logs every HTTP call at DEBUG, which is far too noisy to inherit.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# ------------------------------------------------------------------------- helpers


def _parse_timestamp(value: str | None) -> float:
    """Convert a Docker RFC3339 timestamp to a unix timestamp.

    Docker uses the zero time ("0001-01-01T00:00:00Z") to mean "never", which is
    reported as 0 so that dashboards can filter it out.
    """
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        log.debug("could not parse timestamp %r", value)
        return 0.0
    if parsed.year <= 1:
        return 0.0
    return parsed.timestamp()


def _short_id(container_id: str) -> str:
    """Truncate to 12 chars so IDs line up with what `docker ps` prints."""
    return container_id[:12]


# ------------------------------------------------------------------------ collector


class DockerCollector(Collector):
    """Collects Docker container metrics on demand, once per scrape."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._client: docker.DockerClient | None = None
        self._client_lock = threading.Lock()
        self._scrape_errors = 0.0
        # Tracked only so that daemon availability flips are logged once rather
        # than on every scrape.
        self._last_reachable: bool | None = None

    # -- docker client ------------------------------------------------------

    def _get_client(self) -> docker.DockerClient:
        with self._client_lock:
            if self._client is None:
                self._client = docker.from_env(timeout=self._config.docker_timeout)
            return self._client

    def _drop_client(self) -> None:
        """Discard the cached client so the next scrape reconnects.

        Needed because a daemon restart leaves the pooled connections broken.
        """
        with self._client_lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - closing must never mask the real error
                pass

    # -- data gathering -----------------------------------------------------

    def _list_containers(self, client: docker.DockerClient) -> list[dict[str, Any]]:
        filters = {"label": self._config.label_filter} if self._config.label_filter else None
        return client.api.containers(all=self._config.include_stopped, filters=filters)

    def _inspect(self, client: docker.DockerClient, container_id: str) -> dict[str, Any]:
        """Fetch the details that `/containers/json` does not return.

        The list endpoint already carries State, Health, Labels and ImageID, but
        RestartCount, ExitCode, StartedAt and OOMKilled only come from inspect.
        """
        try:
            return client.api.inspect_container(container_id)
        except NotFound:
            # The container was removed between listing and inspecting. That is a
            # normal race on a busy host, not an error worth failing the scrape.
            log.debug("container %s vanished during scrape", _short_id(container_id))
            return {}
        except DockerException as exc:
            log.warning("inspect of %s failed: %s", _short_id(container_id), exc)
            return {}

    def _snapshot(self) -> dict[str, Any]:
        client = self._get_client()
        listed = self._list_containers(client)

        details: dict[str, dict[str, Any]] = {}
        if listed:
            workers = min(self._config.inspect_workers, len(listed))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = pool.map(
                    lambda raw: (raw["Id"], self._inspect(client, raw["Id"])), listed
                )
                details = dict(results)

        version = client.version()
        return {
            "daemon_version": str(version.get("Version", "unknown")),
            "api_version": str(version.get("ApiVersion", "unknown")),
            "containers": [
                self._normalise(raw, details.get(raw["Id"], {})) for raw in listed
            ],
        }

    @staticmethod
    def _normalise(raw: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
        names = raw.get("Names") or []
        labels = raw.get("Labels") or {}
        state = detail.get("State") or {}
        health = raw.get("Health") or state.get("Health") or {}
        host_config = detail.get("HostConfig") or {}

        return {
            "id": _short_id(raw.get("Id", "")),
            "name": names[0].lstrip("/") if names else "unknown",
            # The list endpoint reports the image reference the container was
            # created from, which sidesteps the dangling-image lookups the old
            # implementation needed.
            "image": raw.get("Image") or raw.get("ImageID") or "unknown",
            "image_id": raw.get("ImageID", ""),
            "state": (raw.get("State") or state.get("Status") or "unknown").lower(),
            "health": (health.get("Status") or "none").lower(),
            "failing_streak": int(health.get("FailingStreak") or 0),
            "created": float(raw.get("Created") or 0),
            "restart_count": int(detail.get("RestartCount") or 0),
            "exit_code": int(state.get("ExitCode") or 0),
            "started_at": _parse_timestamp(state.get("StartedAt")),
            "finished_at": _parse_timestamp(state.get("FinishedAt")),
            "oom_killed": bool(state.get("OOMKilled")),
            "compose_project": labels.get(COMPOSE_PROJECT_LABEL, ""),
            "compose_service": labels.get(COMPOSE_SERVICE_LABEL, ""),
            "restart_policy": (host_config.get("RestartPolicy") or {}).get("Name", ""),
        }

    # -- metric emission ----------------------------------------------------

    def collect(self) -> Iterator[Any]:
        started = time.perf_counter()
        snapshot: dict[str, Any] | None = None
        try:
            snapshot = self._snapshot()
        except (DockerException, requests.RequestException) as exc:
            # The SDK only wraps connection failures in DockerException on some
            # paths; a daemon that disappears mid-request surfaces as a raw
            # requests.ConnectionError, which is the common production case.
            self._scrape_errors += 1
            self._drop_client()
            if self._last_reachable is not False:
                log.error("Docker daemon unreachable: %s", exc)
            self._last_reachable = False
        except Exception as exc:  # noqa: BLE001
            # A collector that raises turns /metrics into a 500 and takes the
            # exporter's own diagnostics down with it, so nothing may escape here.
            self._scrape_errors += 1
            self._drop_client()
            # Full traceback once, then stay quiet: this runs on every scrape.
            if self._last_reachable is not False:
                log.exception("unexpected error during scrape")
            else:
                log.warning("unexpected error during scrape: %s", exc)
            self._last_reachable = False
        else:
            if self._last_reachable is False:
                log.info("Docker daemon reachable again")
            self._last_reachable = True

        duration = time.perf_counter() - started
        yield from self._exporter_metrics(snapshot, duration)
        if snapshot is not None:
            yield from self._container_metrics(snapshot["containers"])

    def _exporter_metrics(
        self, snapshot: dict[str, Any] | None, duration: float
    ) -> Iterable[Any]:
        yield GaugeMetricFamily(
            "docker_up",
            "Whether the Docker daemon was reachable on the last scrape (1 = yes).",
            value=1 if snapshot is not None else 0,
        )

        build = GaugeMetricFamily(
            "docker_exporter_build_info",
            "Build information about the running exporter.",
            labels=["version", "docker_sdk_version", "python_version"],
        )
        build.add_metric(
            [__version__, docker.__version__, ".".join(map(str, sys.version_info[:3]))],
            1,
        )
        yield build

        if snapshot is not None:
            daemon = GaugeMetricFamily(
                "docker_daemon_info",
                "Version information reported by the Docker daemon.",
                labels=["version", "api_version"],
            )
            daemon.add_metric([snapshot["daemon_version"], snapshot["api_version"]], 1)
            yield daemon

            yield GaugeMetricFamily(
                "docker_exporter_containers_scraped",
                "Number of containers included in the last successful scrape.",
                value=len(snapshot["containers"]),
            )

        yield GaugeMetricFamily(
            "docker_exporter_scrape_duration_seconds",
            "Duration of the last scrape of the Docker daemon.",
            value=duration,
        )
        yield CounterMetricFamily(
            "docker_exporter_scrape_errors_total",
            "Total number of scrapes that failed to reach the Docker daemon.",
            value=self._scrape_errors,
        )

    def _container_metrics(self, containers: list[dict[str, Any]]) -> Iterable[Any]:
        state = GaugeMetricFamily(
            "docker_container_state",
            "Container lifecycle state (1 = container is in this state).",
            labels=VALUE_LABELS + ["state"],
        )
        health = GaugeMetricFamily(
            "docker_container_health",
            "Container healthcheck status (1 = container has this status).",
            labels=VALUE_LABELS + ["status"],
        )
        failing_streak = GaugeMetricFamily(
            "docker_container_health_failing_streak",
            "Number of consecutive healthcheck failures.",
            labels=VALUE_LABELS,
        )
        restarts = CounterMetricFamily(
            "docker_container_restarts",
            "Number of times the daemon has restarted this container.",
            labels=VALUE_LABELS,
        )
        exit_code = GaugeMetricFamily(
            "docker_container_last_exit_code",
            "Exit code of the last run of the container.",
            labels=VALUE_LABELS,
        )
        start_time = GaugeMetricFamily(
            "docker_container_start_time_seconds",
            "Unix timestamp the container was last started (0 = never started).",
            labels=VALUE_LABELS,
        )
        created_time = GaugeMetricFamily(
            "docker_container_created_time_seconds",
            "Unix timestamp the container was created.",
            labels=VALUE_LABELS,
        )
        finished_time = GaugeMetricFamily(
            "docker_container_finished_time_seconds",
            "Unix timestamp the container last stopped (0 = still running or never stopped). "
            "Lets alerts distinguish a container that just failed from one that has been "
            "sitting dead for a long time.",
            labels=VALUE_LABELS,
        )
        oom_killed = GaugeMetricFamily(
            "docker_container_oom_killed",
            "Whether the container was killed by the OOM killer (1 = yes).",
            labels=VALUE_LABELS,
        )
        info = GaugeMetricFamily(
            "docker_container_info",
            "Container metadata, always 1. Join onto other metrics by container_id.",
            labels=VALUE_LABELS
            + ["image_id", "compose_project", "compose_service", "restart_policy"],
        )
        legacy_status = GaugeMetricFamily(
            "docker_container_status",
            "Deprecated, use docker_container_state. 1 = running, 0 = not running.",
            labels=VALUE_LABELS,
        )

        for container in containers:
            base = [container["id"], container["name"], container["image"]]

            # Emitting a sample for every known state (not just the current one)
            # means a container that moves to `restarting` actively reports 0 for
            # `running`, instead of leaving a stale 1 behind.
            observed = container["state"]
            states = CONTAINER_STATES
            if observed not in states:
                log.debug("unknown container state %r for %s", observed, container["name"])
                states = states + (observed,)
            for candidate in states:
                state.add_metric(base + [candidate], 1 if candidate == observed else 0)

            observed_health = container["health"]
            statuses = HEALTH_STATUSES
            if observed_health not in statuses:
                statuses = statuses + (observed_health,)
            for candidate in statuses:
                health.add_metric(
                    base + [candidate], 1 if candidate == observed_health else 0
                )

            failing_streak.add_metric(base, container["failing_streak"])
            restarts.add_metric(base, container["restart_count"])
            exit_code.add_metric(base, container["exit_code"])
            start_time.add_metric(base, container["started_at"])
            created_time.add_metric(base, container["created"])
            finished_time.add_metric(base, container["finished_at"])
            oom_killed.add_metric(base, 1 if container["oom_killed"] else 0)
            info.add_metric(
                base
                + [
                    container["image_id"],
                    container["compose_project"],
                    container["compose_service"],
                    container["restart_policy"],
                ],
                1,
            )
            legacy_status.add_metric(base, 1 if observed == "running" else 0)

        yield state
        yield health
        yield failing_streak
        yield restarts
        yield exit_code
        yield start_time
        yield created_time
        yield finished_time
        yield oom_killed
        yield info
        yield legacy_status


# ------------------------------------------------------------------------- serving


class _QuietHandler(WSGIRequestHandler):
    """Suppresses per-request stderr logging from wsgiref."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


def build_app(registry: CollectorRegistry):
    """WSGI app exposing /metrics plus a liveness endpoint.

    prometheus_client's own server only answers /metrics, so the routing is done
    here to add /healthz -- which deliberately does not touch Docker, so that a
    daemon outage does not make the container look dead.
    """
    metrics_app = make_wsgi_app(registry)

    def app(environ, start_response):
        path = environ.get("PATH_INFO", "/")
        if path == "/metrics":
            return metrics_app(environ, start_response)
        if path in ("/healthz", "/-/healthy"):
            body = b"ok\n"
            start_response(
                "200 OK",
                [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(body)))],
            )
            return [body]
        if path == "/":
            body = (
                b"<html><head><title>Docker Exporter</title></head>"
                b"<body><h1>Docker Exporter</h1>"
                b'<p><a href="/metrics">Metrics</a></p></body></html>'
            )
            start_response(
                "200 OK",
                [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body)))],
            )
            return [body]
        body = b"not found\n"
        start_response(
            "404 Not Found",
            [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(body)))],
        )
        return [body]

    return app


def main() -> int:
    config = Config()
    setup_logging(config)

    registry = CollectorRegistry()
    registry.register(DockerCollector(config))

    httpd = make_server(
        config.bind_address,
        config.port,
        build_app(registry),
        ThreadingWSGIServer,
        handler_class=_QuietHandler,
    )

    log.info("docker_exporter %s starting: %s", __version__, config.describe())

    # serve_forever runs off the main thread so that the signal handler can call
    # shutdown() without deadlocking against it.
    server_thread = threading.Thread(target=httpd.serve_forever, name="http", daemon=True)
    server_thread.start()
    log.info("listening on %s:%d", config.bind_address, config.port)

    stop = threading.Event()

    def handle_signal(signum: int, _frame: Any) -> None:
        log.info("received %s, shutting down", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    stop.wait()
    httpd.shutdown()
    httpd.server_close()
    server_thread.join(timeout=5)
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
