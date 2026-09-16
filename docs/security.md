# Модель безопасности и hardening

## Риск Docker сокета

### Docker socket = root на хосте

Кто может писать в `/var/run/docker.sock`, тот может:
- Запустить контейнер с `--volume /:/mnt` (примонтировать весь хост).
- Завести shell в этом контейнере.
- Получить полный root доступ к файловой системе хоста.

**Вывод:** доступ к сокету — это критический риск безопасности, эквивалентный sudo для любого процесса.

### Заблуждение о :ro флаге

Часто пишут:
```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock:ro
```

**Это НЕ делает API read-only.**

`:ro` делает read-only сам bind-mount, то есть запрещает изменять файл сокета как объект файловой системы. Но обмен с демоном идёт не через запись в файл, а через `connect()` к сокету и HTTP поверх установленного соединения — на это флаг не распространяется вообще. POST `/containers/create` проходит через `:ro`-маунт без проблем.

**Проверено в этом проекте:** в compose файле сокет монтируется как `:ro`, но это defence in depth, а не главный механизм контроля.

## Архитектура доступа: docker-socket-proxy

### Схема подключения

```
docker-exporter (TCP)
    ↓ DOCKER_HOST=tcp://docker-socket-proxy:2375
docker-socket-proxy (haproxy, tecnativa)
    ↓ (единственный контейнер с доступом к сокету)
/var/run/docker.sock (Unix socket на хосте)
```

Экспортер **никогда не видит сокет**, работает только по TCP.

### Образ docker-socket-proxy

**Используется:** `tecnativa/docker-socket-proxy:v0.5.0`

- Это контейнеризованный haproxy, который переопубликует Unix socket Docker на TCP порту 2375.
- Основан на Alpine, внутри контейнера работает от root (проверено: `id` в контейнере отдаёт `uid=0(root)`).
- Поддерживает ACL через переменные окружения.

**Почему именно tecnativa?**
- Де-факто стандарт в сообществе.
- Простая настройка (переменные окружения).
- Хорошо задокументирован.

**Альтернатива:** wollomatic/socket-proxy:1.13.1
- Образ из scratch (меньше поверхность атаки).
- Работает non-root.
- ACL через регулярные выражения на пути (более гибко).
- Но менее известна, требует более тщательной настройки.

### ACL прокси (deny by default)

Переменные окружения в docker-compose.yml:

```yaml
CONTAINERS: 1   # GET /containers/json, /containers/{id}/json
VERSION: 1      # GET /version — нужен для docker_daemon_info и согласования версии API
PING: 1         # GET /_ping
POST: 0         # блокирует мутирующие POST-запросы независимо от секций выше
# Явно запрещены (это уже по умолчанию, но выписаны для visibility):
AUTH: 0
BUILD: 0
COMMIT: 0
CONFIGS: 0
DISTRIBUTION: 0
EVENTS: 0
EXEC: 0
IMAGES: 0
INFO: 0
NETWORKS: 0
NODES: 0
PLUGINS: 0
SECRETS: 0
SERVICES: 0
SESSION: 0
SWARM: 0
SYSTEM: 0
TASKS: 0
VOLUMES: 0
```

**Проверено практически (GET вернул 200, остальное 403):**
- `GET /version` — 200 (нужен для docker_daemon_info и согласования API версии).
- `GET /_ping` — 200 (оператор может дебагить доступность).
- `GET /containers/json` — 200 (список контейнеров).
- `GET /containers/{id}/json` — 200 (inspect контейнера).
- `GET /images/json` — 403 (запрещено).
- `GET /networks` — 403.
- `GET /volumes` — 403.
- `GET /info` — 403.
- `GET /secrets` — 403.
- `GET /services` — 403.
- `POST /containers/create` — 403 (мутации заблокированы).
- `POST /containers/prune` — 403.
- `POST /images/create` — 403.

**Следствие:** экспортер имеет доступ только к читаемой информации о контейнерах, не может ничего изменять.

## Hardening экспортера

### docker-compose.yml — настройки контейнера

```yaml
user: "10001:10001"           # Non-root UID/GID
read_only: true               # ReadonlyRootfs (см. ниже)
cap_drop:
  - ALL                       # Удалены все Linux capabilities
security_opt:
  - no-new-privileges:true    # Процесс и его потомки не получат нов привилегий
tmpfs:
  - /tmp                      # /tmp как tmpfs в памяти, очищается при перезагрузке
deploy:
  resources:
    limits:
      memory: 128M
      cpus: "0.50"
```

### Dockerfile — build time

```dockerfile
# Multi-stage build:
# 1. builder: установить зависимости в venv
# 2. runtime: скопировать только venv, без pip и компиляторов

FROM python:3.14-slim-trixie AS builder
  # venv с зависимостями

FROM python:3.14-slim-trixie
  # Сам slim образ, не полный python
  # Создать non-root UID 10001
  RUN groupadd --gid 10001 exporter
  RUN useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin exporter
  # Скопировать только venv из builder
  COPY --from=builder /opt/venv /opt/venv
  USER 10001:10001
```

**Результат:**
- В финальном образе нет pip (нельзя установить уязвимые пакеты).
- Нет компиляторов (нельзя компилировать эксплойты).
- Non-root UID 10001 (процесс не может читать файлы, принадлежащие root).

### ReadonlyRootfs = true

```yaml
read_only: true
```

Это означает, что `/` монтируется как read-only при запуске контейнера (в docker inspect видно `ReadonlyRootfs=true`).

**Следствие:** процесс не может:
- Модифицировать исполняемые файлы.
- Писать в системные директории.
- Подменять библиотеки.

**Исключение:** `tmpfs: [/tmp]` создаёт `/tmp` как читаемую-записываемую в памяти (нужна для Python temp файлов, если они есть).

### Сеть docker-api

```yaml
networks:
  docker-api:
    internal: true  # Нет маршрутов наружу
```

- Между экспортером и proxy только приватный трафик.
- Даже если proxy скомпрометирован, он не может напрямую выйти наружу по этой сети.

## Доступ к /metrics

### Отсутствие аутентификации

Эндпоинт `/metrics` **не требует аутентификации**. Это стандарт Prometheus — экспортер доступен любому, кто может собирать метрики.

### Bind address по умолчанию

```yaml
ports:
  - "127.0.0.1:8088:8088"
```

- Слушает только на loopback (127.0.0.1).
- Не доступен с других хостов напрямую.

**Если нужен удаленный сбор:**
- Измените на `"8088:8088"` (слушайте на 0.0.0.0) **только** если за экспортером стоит reverse proxy с TLS и авторизацией.
- Или поставьте экспортер в приватной сети Kubernetes/Docker Swarm, доступной только из сборщика метрик.

## Логирование

### Что логируется

```python
log.info("docker_exporter 1.0.0 starting: ...")        # Старт с конфигом
log.info("listening on 127.0.0.1:8088")                # Готовность слушать
log.error("Docker daemon unreachable: ...")             # Потеря доступа
log.info("Docker daemon reachable again")               # Восстановление
log.exception("unexpected error during scrape")         # Неожиданная ошибка, трейсбек один раз
log.debug("container ... vanished during scrape")       # Гонка при inspect (можно выключить)
```

### Что не логируется

- Содержимое контейнеров.
- Переменные окружения или labels контейнеров (кроме compose project/service для метрик).
- Содержимое stderr/stdout контейнеров и демона.
- Имена образов и контейнеров в логах не фигурируют (они есть только в лейблах метрик; имя контейнера появляется лишь в DEBUG-сообщении о гонке при inspect).

**Вывод:** логи безопасны для отправки в центральный логилектор (ELK, Datadog, и т.д.).

## Security Checklist

### Что открыто наружу?

- **8088/tcp** (metrics) слушает на **127.0.0.1**, не доступен с других хостов.
- **2375/tcp** (proxy) слушает на **docker-api** сети (internal=true).
- Docker socket доступен **только proxy**, не самому экспортеру.

**Оценка:** только loopback для метрик = безопасно по умолчанию.

### У кого есть write доступ?

- **Никто** — POST глобально запрещён на proxy (POST: 0).
- Экспортер может только читать состояние контейнеров.

**Оценка:** read-only, нет мутаций.

### Что нужно знать при поддержке?

1. **Если демон перезагружается:** экспортер автоматически пересоздает клиент при ошибке, логирует восстановление.
2. **Если метрики растут медленно:** проверить `docker_exporter_scrape_duration_seconds` (inspect параллелен, но может быть bottleneck на очень большом количестве контейнеров).
3. **Если proxy недоступен:** логируется `ERROR: Docker daemon unreachable`. Проверить `docker-socket-proxy` контейнер, его сетевой интерфейс, сокет на хосте.
4. **Если нужен удалённый доступ к /metrics:** никогда не публикуйте напрямую на 0.0.0.0. Используйте reverse proxy с TLS и авторизацией (nginx, traefik, и т.д.).

### Кто имеет root доступ?

- **docker-socket-proxy** контейнер — работает от root, имеет доступ к сокету. Это критично.
- **docker-exporter** контейнер — UID 10001:10001, non-root, не имеет доступа к сокету.

**Вывод:** если скомпрометирован docker-exporter, хост не скомпрометирован. Если скомпрометирован proxy — да, хост в опасности (но proxy делает только то, что разрешено ACL).
