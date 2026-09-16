# prometheus-docker-exporter

A Prometheus exporter for Docker **container lifecycle and health**: what state every
container is in, whether its healthcheck passes, how often it restarts, and how it
last exited.

It deliberately does **not** export CPU, memory, network or block-IO per container —
that is [cAdvisor](https://github.com/google/cadvisor)'s job, and `docker stats`
blocks for 1–2 seconds per container, which makes it a poor fit for a scrape-time
exporter. Run both if you want resource usage; they do not overlap.

- Collects at **scrape time**, so values match the moment Prometheus asked, and
  series for removed containers disappear on their own.
- **Survives a Docker daemon outage** — keeps serving `/metrics`, reports `docker_up 0`
  instead of dying.
- Runs **non-root, read-only, with all capabilities dropped**, and never touches the
  Docker socket (it talks to a socket proxy over TCP).

## Quick start

```bash
docker compose up -d --build
curl -s localhost:8088/metrics | grep docker_container_state
```

That starts two containers: the exporter, and a socket proxy that is the only thing
with access to `/var/run/docker.sock`. Metrics land on `127.0.0.1:8088/metrics`.

To also get Prometheus and Grafana, wired to the configs in this repository:

```bash
docker compose --profile monitoring up -d
```

| Service | URL | Notes |
|---|---|---|
| Exporter | http://localhost:8088/metrics | also `/healthz`, `/` |
| Prometheus | http://localhost:9090 | targets, alert rules |
| Grafana | http://localhost:3000 | dashboard *Docker → Docker Containers*, anonymous admin |

All three bind to `127.0.0.1` only. See [Security](#security) before exposing them.

### Without Docker

```bash
python -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/python docker_exporter.py      # uses DOCKER_HOST or the default socket
```

## Metrics

Value metrics carry `container_id` (12 chars, matching `docker ps`), `container_name`
and `image`.

| Metric | Type | Description |
|---|---|---|
| `docker_container_state{state}` | gauge | 1 for the current state, 0 for the rest. States: `created`, `restarting`, `running`, `removing`, `paused`, `exited`, `dead` |
| `docker_container_health{status}` | gauge | 1/0 across `none`, `starting`, `healthy`, `unhealthy` |
| `docker_container_health_failing_streak` | gauge | Consecutive healthcheck failures |
| `docker_container_restarts_total` | counter | Times the daemon restarted the container |
| `docker_container_last_exit_code` | gauge | Exit code of the last run |
| `docker_container_start_time_seconds` | gauge | Unix ts of last start; 0 = never started |
| `docker_container_created_time_seconds` | gauge | Unix ts of creation |
| `docker_container_finished_time_seconds` | gauge | Unix ts container last stopped; 0 = still running or never stopped |
| `docker_container_oom_killed` | gauge | 1 if the kernel OOM killer terminated it |
| `docker_container_info` | gauge | Always 1; carries `image_id`, `compose_project`, `compose_service`, `restart_policy` |
| `docker_container_status` | gauge | **Deprecated**, use `docker_container_state`. 1 = running |
| `docker_up` | gauge | Docker daemon reachable on the last scrape |
| `docker_daemon_info{version,api_version}` | gauge | Always 1 |
| `docker_exporter_build_info{...}` | gauge | Always 1 |
| `docker_exporter_scrape_duration_seconds` | gauge | Time spent talking to Docker |
| `docker_exporter_scrape_errors_total` | counter | Failed scrapes |
| `docker_exporter_containers_scraped` | gauge | Containers in the last good scrape |

Every state gets a sample on every scrape, not just the current one. That is what
makes a crash-looping container honest: when it moves to `restarting`, it actively
reports `0` for `running` rather than leaving a stale `1` behind.

`docker_container_info` keeps the bulky metadata out of the other metrics' label
sets. Join it in when you need it:

```promql
docker_container_state{state="running"}
  * on (container_id) group_left (compose_project, compose_service)
  docker_container_info
```

### Useful queries

```promql
# Containers by state
sum by (state) (docker_container_state)

# Crash-looping right now
docker_container_state{state="restarting"} == 1

# Restart churn over the last hour
topk(5, increase(docker_container_restarts_total[1h]))

# Failing healthchecks
docker_container_health{status="unhealthy"} == 1

# Uptime of running containers, in seconds
(time() - docker_container_start_time_seconds)
  and on (container_id, container_name, image)
  (docker_container_state{state="running"} == 1)

# Stopped with an error (a clean `docker stop` exits 0)
(docker_container_state{state="exited"} == 1)
  and on (container_id, container_name, image)
  (docker_container_last_exit_code != 0)
```

> **Alert on `docker_up`, not on `up`.** The exporter stays healthy and keeps
> answering when the daemon dies, so Prometheus' own `up` stays 1 while the
> container data goes stale. `docker_up` is what reports the daemon.

## Configuration

Everything is environment driven.

| Variable | Default | Description |
|---|---|---|
| `EXPORTER_PORT` | `8088` | HTTP listen port |
| `EXPORTER_BIND_ADDRESS` | `0.0.0.0` | Listen address inside the container |
| `DOCKER_HOST` | default socket | Standard Docker SDK variable; set to `tcp://docker-socket-proxy:2375` in compose |
| `DOCKER_TIMEOUT_SECONDS` | `10` | Docker API timeout. Keep Prometheus' `scrape_timeout` above this |
| `EXPORTER_INSPECT_WORKERS` | `8` | Parallel container inspects per scrape |
| `EXPORTER_INCLUDE_STOPPED` | `true` | Include stopped containers |
| `EXPORTER_LABEL_FILTER` | *(empty)* | Comma-separated label selectors, e.g. `com.docker.compose.project=myapp` |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_FORMAT` | `text` | `text` or `json` |

Endpoints: `/metrics`, `/healthz` (liveness; deliberately does **not** call Docker, so
a daemon outage does not make the container look dead), and `/`.

## Security

**The Docker socket is root on the host.** Anything that can write to
`/var/run/docker.sock` can start a container with `/` mounted and take the machine.

**Mounting the socket `:ro` does not make the API read-only.** `:ro` applies to the
socket inode, not to the protocol spoken over it — `POST /containers/create` still
goes through. This is a common and expensive misconception.

So the socket is mounted into `tecnativa/docker-socket-proxy` and nothing else. The
exporter reaches it over TCP and never sees a socket:

```
docker-exporter ──tcp──> docker-socket-proxy ──unix socket──> dockerd
   (uid 10001)            (deny-by-default ACL)
```

That also happens to be what makes non-root work. The socket is `root:docker 0660`;
a non-root process could only read it by matching the host's `docker` GID via
`group_add` — and that match hands back full API access anyway.

The proxy ACL is deny-by-default; only `CONTAINERS`, `VERSION` and `PING` are on.
Verified against the running stack:

| Request | Result |
|---|---|
| `GET /version`, `/_ping`, `/containers/json` | 200 — what the exporter needs |
| `GET /images/json`, `/networks`, `/volumes`, `/info`, `/secrets`, `/services` | 403 |
| `POST /containers/create`, `/containers/prune`, `/images/create` | 403 |

Exporter hardening, as verified by `docker inspect`:
`user=10001:10001`, `ReadonlyRootfs=true`, `CapDrop=[ALL]`,
`no-new-privileges:true`, `tmpfs` on `/tmp`, memory and CPU limits. The image is
multi-stage, so neither pip nor a compiler ships in the runtime layer. The
`docker-api` network is `internal: true` and has no route off the host.

**Checklist.** Exposed: `8088/tcp` on loopback only — `/metrics` is unauthenticated,
so put it behind a reverse proxy with TLS and auth, or a private network, before
scraping it remotely. Write access to the Docker API: none, `POST` is blocked at the
proxy. Logged: startup/shutdown, daemon errors and availability flips — never
container contents or environment variables.

For a stricter proxy, `wollomatic/socket-proxy:1.13.1` is built `FROM scratch`, runs
non-root and allowlists by path regex. `tecnativa` is the default here because it is
the widely understood option, but internally it is haproxy running as root.

## Integrations

**Prometheus** — `prometheus/prometheus.yml` plus ten alert rules in
`prometheus/alerts/docker-exporter.rules.yml`: exporter down, daemon unreachable,
scrape errors, crash-looping, restart flapping, unexpected exit (recent and
stale), OOM kill (recent and stale), unhealthy.

Both `docker_container_oom_killed` and `docker_container_last_exit_code` describe
a container's *last* exit, not a moment in time — they stay set until the
container is removed or restarted, so an un-split rule would page forever on a
container nobody's touched in weeks. Both alerts are split by
`docker_container_finished_time_seconds` age (under 15 minutes vs. older):

| Event | Recent (< 15m) | Stale (≥ 15m) |
|---|---|---|
| Unexpected exit | `DockerContainerExitedUnexpectedly` — `warning` | `DockerContainerExitedUnexpectedlyStale` — `info` |
| OOM kill | `DockerContainerOOMKilled` — `critical` | `DockerContainerOOMKilledStale` — `warning` |

```bash
docker run --rm --entrypoint promtool -v "$PWD/prometheus:/p:ro" \
  prom/prometheus:v3.13.3 check rules /p/alerts/docker-exporter.rules.yml
```

**Grafana** — `grafana/dashboards/docker-containers.json`, auto-provisioned by the
`monitoring` profile into the *Docker* folder. Import it manually elsewhere; it uses
a datasource variable, so it is not tied to this stack.

**OpenTelemetry** — `otel/otel-collector-config.yaml` scrapes the exporter and
forwards OTLP, for Datadog, Grafana Cloud, Honeycomb, New Relic and friends. Needs
the contrib distribution and `OTLP_ENDPOINT` / `OTLP_API_KEY` in the environment.

**VictoriaMetrics, Thanos, Grafana Mimir, Grafana Alloy** — all speak the Prometheus
scrape protocol, so point them at `http://docker-exporter:8088/metrics` directly. No
adapter needed.

## Testing locally

```bash
docker compose --profile monitoring up -d --build
```

**Is it up?**

```bash
curl -s localhost:8088/healthz                  # -> ok
curl -s localhost:8088/metrics | grep '^docker_up'
curl -s 'localhost:9090/api/v1/targets?state=active' | jq '.data.activeTargets[].health'
```

**Does it catch a crash loop?** The behaviour the old implementation got wrong:

```bash
docker run -d --restart=always --name crashloop alpine sh -c 'sleep 1; exit 1'
sleep 30
curl -s localhost:8088/metrics | grep crashloop | grep -E 'state|restarts'
docker rm -f crashloop
```

`docker_container_restarts_total` climbs, and exactly one `state` series is 1 at any
moment — the rest are explicitly 0.

**Does it survive the daemon going away?** Stop the proxy, which is what the exporter
talks to:

```bash
docker compose stop docker-socket-proxy
curl -s -o /dev/null -w '%{http_code}\n' localhost:8088/metrics   # still 200
curl -s localhost:8088/metrics | grep -E '^(docker_up|docker_exporter_scrape_errors_total)'
docker compose start docker-socket-proxy
```

`docker_up` drops to 0, the error counter climbs, the process stays alive, and
metrics come back on their own.

**Is the hardening real?**

```bash
docker compose exec docker-exporter id                         # uid=10001
docker compose exec docker-exporter ls /var/run/docker.sock    # No such file
docker inspect docker-exporter \
  --format 'ro={{.HostConfig.ReadonlyRootfs}} caps={{.HostConfig.CapDrop}}'
```

**Tear down:** `docker compose --profile monitoring down -v`

## Layout

```
docker_exporter.py                        exporter (single file)
Dockerfile                                multi-stage, non-root runtime
docker-compose.yml                        exporter + socket proxy; monitoring profile
prometheus/prometheus.yml                 scrape config
prometheus/alerts/                        alert rules
grafana/dashboards/, grafana/provisioning/  dashboard + auto-provisioning
otel/otel-collector-config.yaml           Prometheus receiver -> OTLP
docs/                                     architecture, metrics, security
```

## Versions

Pinned and verified on 2026-09-16: `docker` 7.2.0, `prometheus-client` 0.26.0,
base image `python:3.14-slim-trixie`, `tecnativa/docker-socket-proxy` v0.5.0,
`prom/prometheus` v3.13.3, `grafana/grafana` 13.2.2,
`otel/opentelemetry-collector-contrib` 0.161.0.
