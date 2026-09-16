# Справочник метрик

## Метрики состояния контейнеров

Все метрики контейнеров имеют лейблы: `container_id` (12 символов, как в `docker ps`), `container_name`, `image`.

### docker_container_state (Gauge)

**Labels:** container_id, container_name, image, state

```
docker_container_state{container_id="abc123...", container_name="postgres", image="postgres:16", state="running"} 1
docker_container_state{container_id="abc123...", container_name="postgres", image="postgres:16", state="paused"} 0
docker_container_state{container_id="abc123...", container_name="postgres", image="postgres:16", state="exited"} 0
```

**Значение:** 1, если контейнер находится в указанном состоянии, иначе 0.

**Известные состояния:** created, restarting, running, removing, paused, exited, dead.

**Почему отдельная серия на каждое состояние?**

Это идиома kube-state-metrics и правильно отражает переходы:
- Контейнер в состоянии `running`: state{state="running"}=1, state{state="paused"}=0, state{state="exited"}=0 и т.д.
- Контейнер перешёл в `restarting`: state{state="running"}=0, state{state="restarting"}=1.
- Без этого при переходе из running в exited, серия state{state="running"} осталась бы в памяти Prometheus, давая неверную информацию.

### docker_container_health (Gauge)

**Labels:** container_id, container_name, image, status

```
docker_container_health{..., status="healthy"} 1
docker_container_health{..., status="starting"} 0
docker_container_health{..., status="unhealthy"} 0
docker_container_health{..., status="none"} 0
```

**Значение:** 1 для текущего статуса, 0 для остальных.

**Известные статусы:** none (контейнер без HEALTHCHECK), starting, healthy, unhealthy.

**Применение:** таким же образом как docker_container_state — переходы видны явно.

### docker_container_health_failing_streak (Gauge)

**Labels:** container_id, container_name, image

**Значение:** количество последовательных неудачных проверок здоровья (из поля `Health.FailingStreak`).

### docker_container_restarts_total (Counter)

**Labels:** container_id, container_name, image

**Значение:** количество раз, которое демон перезапустил этот контейнер (из `RestartCount` при inspect).

**Примечание об имени.** В коде семейство создаётся как `CounterMetricFamily("docker_container_restarts", ...)` — суффикс `_total` добавляет сама клиентская библиотека при экспозиции, поэтому наружу метрика выходит под именем `docker_container_restarts_total`. Именно это имя нужно использовать в PromQL.

Раньше это был `Gauge` с именем `docker_container_restarts_total`. Суффикс `_total` в Prometheus зарезервирован за счётчиками, и `rate()`/`increase()` по Gauge работают некорректно: клиент не объявлял метрику как counter, поэтому обработка сброса значения (контейнер пересоздали — счётчик упал в 0) была неверной. Теперь тип объявлен честно.

### docker_container_last_exit_code (Gauge)

**Labels:** container_id, container_name, image

**Значение:** exit code последнего запуска контейнера, как его отдаёт `State.ExitCode`: 0 — штатное завершение, ненулевой — ошибка. Процесс, убитый сигналом N, Docker репортит как 128+N (например, 137 = 128+9, SIGKILL, обычно OOM; 143 = 128+15, SIGTERM). У работающего контейнера значение 0.

**Та же особенность, что у `docker_container_oom_killed`:** это состояние *последнего* завершения, а не разовое событие — значение остаётся ненулевым сколько угодно, пока контейнер не удалят или не перезапустят. Поэтому алерт `DockerContainerExitedUnexpectedly` в `prometheus/alerts/docker-exporter.rules.yml` тоже разделён по давности через `docker_container_finished_time_seconds`: `DockerContainerExitedUnexpectedly` (warning, < 15 мин) и `DockerContainerExitedUnexpectedlyStale` (info, ≥ 15 мин) — иначе контейнер, упавший месяц назад и забытый, спамил бы warning вечно.

### docker_container_start_time_seconds (Gauge)

**Labels:** container_id, container_name, image

**Значение:** Unix timestamp, когда контейнер был в последний раз запущен (0 = никогда не был запущен).

### docker_container_created_time_seconds (Gauge)

**Labels:** container_id, container_name, image

**Значение:** Unix timestamp создания контейнера.

### docker_container_finished_time_seconds (Gauge)

**Labels:** container_id, container_name, image

**Значение:** Unix timestamp последней остановки контейнера (из `State.FinishedAt`). 0 = контейнер сейчас работает или ещё ни разу не останавливался.

**Назначение:** без этой метрики нельзя отличить «контейнер только что упал» от «контейнер лежит мёртвым уже три недели» — обе ситуации выглядят как `docker_container_state{state="exited"} == 1`. Используется в алерте `DockerContainerOOMKilled`/`DockerContainerOOMKilledStale`, чтобы не спамить critical-алертом на давно забытый контейнер (см. ниже).

### docker_container_oom_killed (Gauge)

**Labels:** container_id, container_name, image

**Значение:** 1 если контейнер был убит OOM killer, 0 иначе.

**Важная особенность:** это флаг из `State.OOMKilled`, который описывает *последний* завершённый запуск контейнера, а не факт «OOM только что произошёл». Он остаётся равным 1 сколь угодно долго — пока контейнер не удалят или не перезапустят. На практике это означает, что наивный алерт `docker_container_oom_killed == 1` будет непрерывно firing на контейнер, который умер от OOM месяц назад и про который все забыли. Поэтому в правилах алертинга (`prometheus/alerts/docker-exporter.rules.yml`) эта метрика используется только вместе с `docker_container_finished_time_seconds`, разделяя случаи по давности события на `DockerContainerOOMKilled` (critical, < 15 мин) и `DockerContainerOOMKilledStale` (warning, ≥ 15 мин).

### docker_container_info (Info-метрика, Gauge)

**Labels:** container_id, container_name, image, image_id, compose_project, compose_service, restart_policy

**Значение:** всегда 1.

**Назначение:** несёт тяжёлые метаданные, которые не стоит размножать на каждой метрике (это увеличит кардинальность и размер БД). Вместо этого используется join в PromQL.

**Пример:** получить restart policy каждого контейнера:
```promql
docker_container_state{state="running"}
  * on(container_id) group_left(restart_policy) docker_container_info
```

Результат: у каждого запущенного контейнера будет лейбл restart_policy (e.g. "always", "unless-stopped").

### docker_container_status (Gauge) — DEPRECATED

**Labels:** container_id, container_name, image

**Значение:** 1 если running, 0 иначе.

**Почему остаётся?** Обратная совместимость со старыми мониторингом стеками.

**Что использовать вместо?** `docker_container_state{state="running"}`.

## Self-метрики экспортера

### docker_up (Gauge)

**Labels:** нет

**Значение:** 1 если Docker демон был доступен на последнем скрейпе, 0 иначе.

**Критично:** экспортер продолжает отвечать HTTP 200 на `/metrics` даже когда демон недоступен. Prometheus метрика `up{job="docker-exporter"}` останется 1, а `docker_up` станет 0. **Алертите на `docker_up == 0`, а не на `up`.**

### docker_daemon_info (Info-метрика, Gauge)

**Labels:** version, api_version

**Значение:** всегда 1.

**Назначение:** версия демона, эмитится только если snapshot успешен (демон доступен).

### docker_exporter_build_info (Info-метрика, Gauge)

**Labels:** version, docker_sdk_version, python_version

**Значение:** всегда 1.

**Назначение:** версия самого экспортера, версия Docker SDK и Python, с которыми был собран образ.

### docker_exporter_containers_scraped (Gauge)

**Labels:** нет

**Значение:** количество контейнеров, включённых в последний успешный скрейп (т.е. либо все, если include_stopped=true, либо только running).

### docker_exporter_scrape_duration_seconds (Gauge)

**Labels:** нет

**Значение:** длительность последнего скрейпа в секундах (от начала collect() до end), включая timeout ошибок.

### docker_exporter_scrape_errors_total (Counter)

**Labels:** нет

**Значение:** кумулятивное количество скрейпов, которые не смогли достичь демона (`DockerException`, `requests.RequestException` или любое неожиданное исключение).

## Примеры PromQL запросов

### 1. Контейнеры, которые часто перезапускаются

```promql
increase(docker_container_restarts_total[1h]) > 1
```

Возвращает контейнеры, перезапустившиеся больше одного раза за час. `increase()` здесь нагляднее `rate()`: считает штуки за окно, а не перезапуски в секунду.

### 2. Контейнеры в состоянии restarting

```promql
docker_container_state{state="restarting"} == 1
```

Сравнение с 1 обязательно: серия существует для каждого контейнера, но со значением 0 у тех, кто сейчас не перезапускается.

### 3. Нездоровые контейнеры

```promql
docker_container_health{status="unhealthy"} == 1
```

### 4. Join: контейнеры, которые часто перезагружаются, с их docker-compose проектом

```promql
(increase(docker_container_restarts_total[1h]) > 1)
  * on(container_id) group_left(compose_project) docker_container_info
```

Скобки здесь обязательны: без них `* on(...) group_left(...)` привяжется к правой части сравнения, и запрос будет означать совсем не то, что читается.

### 5. Недавно созданные контейнеры (за последний час)

```promql
docker_container_created_time_seconds > (time() - 3600)
```

### 6. Контейнеры, убитые OOM killer

```promql
docker_container_oom_killed == 1
```

## Примечание о label кардинальности

Лейбл `image` содержит полный путь образа (e.g. `docker.io/library/postgres:16` или хеш при dangling образах). Это может привести к высокой кардинальности если на хосте крутятся разные версии одного сервиса. Info-метрика `docker_container_info` несёт `image_id` (хеш слоя), чтобы можно было группировать по образу точнее:

```promql
count by (image_id) (docker_container_info)
```

Вернёт количество контейнеров, запущенных из каждого образа (по хешу слоя).
