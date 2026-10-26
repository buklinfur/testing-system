# Развёртывание в WSL2 (systemd)

Сервисы: `judge-api-internal` (API для воркеров, 127.0.0.1:8001), `judge-reaper`, `judge-worker`; публичный API (модуль 4) и Caddy добавятся рядом. PostgreSQL 16 работает на той же машине.

## 0. Предусловия (проверьте один раз)

1. systemd в WSL: `/etc/wsl.conf`
   ```ini
   [boot]
   systemd=true
   [automount]
   enabled=false
   [interop]
   enabled=false
   appendWindowsPath=false
   ```
   затем в PowerShell `wsl --shutdown`. (Automount/interop отключены по решению о безопасности; репозиторий и кэш держите внутри ext4.) В env-файлах systemd комментарии допустимы только на отдельных строках.
2. `systemctl is-system-running` отвечает `running` или `degraded`; cgroup v2: `stat -fc %T /sys/fs/cgroup` даёт `cgroup2fs`.
3. isolate установлен как в модуле 0 вместе с `isolate.service` (cg-keeper). Проверка: `sudo -u judge python -m runner.cli sandbox-check` после шага 2 ниже. **Юниты воркера работают от пользователя `judge`, а не root**: если sandbox-check от `judge` не проходит, а от root проходит, скажите, что именно у вас в модуле 0 (setuid/права), и я скорректирую `User=`.
4. Дистрибутив называется `Ubuntu` (Ubuntu 24.04 LTS). Автозапуск самого WSL при входе в Windows: планировщик заданий Windows, действие `wsl.exe -d Ubuntu -u root -- /bin/true` при входе (WSL стартует вместе с первым вызовом, systemd поднимет юниты).

## 1. Установка

```bash
sudo apt install postgresql      # один раз (PostgreSQL 16 в Ubuntu 24.04)
sudo ./deploy/install-wsl.sh     # пользователь judge, каталоги, venv в /opt/judge, юниты, шаблоны /etc/judge/*.env
sudo ./deploy/setup-postgres.sh  # роли и базы, пароли в env-файлы (см. ниже)
```

### PostgreSQL

`deploy/setup-postgres.sh` можно запускать повторно; пароли сохраняются, новые выдаёт `--rotate`. Что он делает:

* `conf.d/judge.conf`: `listen_addresses = 'localhost'` и `password_encryption = 'scram-sha-256'`. Основной `postgresql.conf` не трогается.
  Если в `pg_hba.conf` есть TCP-строка с `trust`, `md5` или `password`, скрипт останавливается и показывает её (правка руками).
* Роли `judge` (продовая) и `judge_test` (тесты и локальные проверки): LOGIN без superuser/createdb/createrole.
  У каждой своя база, у которой она владелец; `CONNECT` отобран у PUBLIC, так что `judge_test` не может подключиться к `judge`.
* Пароли: `openssl rand -hex 24`, в psql идут через stdin, в env-файлы через окружение, в `ps` и истории не видны.
  * `DATABASE_URL` → `/etc/judge/backend.env` (640 root:judge), остальные строки файла не меняются;
  * `JUDGE_TEST_DATABASE_URL` → `~/.config/judge/test.env` (600, владелец тот, кто вызвал sudo). Тесты и dev-проверки идут без sudo.
* В конце проверяет: слушает только `127.0.0.1`, вход по паролю работает, неверный пароль отвергается, пароли хранятся как SCRAM.

## 2. Первый запуск

```bash
j() { sudo -u judge bash -c 'set -a; . /etc/judge/backend.env; set +a; cd /opt/judge; PYTHONPATH=/opt/judge:/opt/judge/backend "$@"' _ "$@"; }
j .venv/bin/alembic upgrade head                              # таблицы (миграции лежат в backend/migrations)
j .venv/bin/python -m app.admin_cli create-worker-token main  # токен покажется один раз
sudo nano /etc/judge/worker.env  # JUDGE_WORKER_TOKEN=...
sudo systemctl enable --now judge-api-internal judge-reaper
curl -s http://127.0.0.1:8001/internal/health
w() { sudo -u judge bash -c 'set -a; . /etc/judge/worker.env; set +a; cd /opt/judge; PYTHONPATH=/opt/judge .venv/bin/python -m worker "$@"' _ "$@"; }
w plan     # должен показать 5 воркеров на разных ядрах
w check    # каждая строка ok: программа в песочнице видит ровно запланированные ядра
sudo systemctl enable --now judge-worker
```

## 3. Наблюдаемость

| Что | Как |
|---|---|
| Логи | `journalctl -u judge-worker -u judge-api-internal -u judge-reaper -f` |
| Метрики (Prometheus, читаются и глазами) | `curl http://127.0.0.1:8001/internal/metrics` (опционально защитить `JUDGE_METRICS_TOKEN`) |
| JSON: загрузка, метрики, таблица воркеров | `curl http://127.0.0.1:8001/internal/status` |
| Таблица в консоли | `python -m app.admin_cli status` (воркеры, состояние, возраст heartbeat, текущее задание, число проверок) |
| История инцидентов | таблица `queue_events` (потерян воркер, возврат в очередь, отказ после N попыток, перепроверки; хранится 14 дней) |

Метрики: `judge_queue_length`, `judge_queue_oldest_age_seconds`, `judge_in_flight`, `judge_workers{state}`,
`judge_typical_judge_seconds`, `judge_queue_wait_seconds`, `judge_finished_total{verdict}`, `judge_requeued_total`.
Тревожные признаки: `judge_workers{state="busy"}` равен числу воркеров и растёт `oldest_queued_age`; `requeued_total` растёт (воркеры падают); нет воркеров онлайн.

## 4. Обновление

`git pull`, снова `sudo ./deploy/install-wsl.sh`, затем `j .venv/bin/alembic upgrade head` (функция `j` из раздела 2) и
`sudo systemctl restart judge-api-internal judge-reaper judge-worker`.
Перезапуск воркеров корректный: текущие проверки дорабатываются (до 2 минут), потом процессы выходят.

## 5. Внешний доступ (Caddy)

Caddy (собственный домен `.ru`, модуль 6) проксирует только публичный API (порт 8000, модуль 4). **Порт 8001 (внутренний API) в Caddyfile не добавлять.**
Второй рубеж: внутренний API отвечает 403 на любой запрос с `X-Forwarded-For` / `X-Forwarded-Proto` / `X-Forwarded-Host`
(Caddy ставит их всегда), `Forwarded`, `X-Real-IP` и заголовками Cloudflare (`docs/WORKER_PROTOCOL.md`).

## 6. Проверки для разработчика (без sudo)

```bash
set -a; . ~/.config/judge/test.env; set +a                       # JUDGE_TEST_DATABASE_URL от setup-postgres.sh
python -m unittest discover -s tests/pg -t . -v                  # очередь, API, миграции (каждый класс в своей схеме)
export DATABASE_URL=$JUDGE_TEST_DATABASE_URL JUDGE_BLOB_DIR=/tmp/judge-blobs PYTHONPATH=.:backend
alembic upgrade head && alembic check                            # схема public базы judge_test
uvicorn app.internal_main:app --host 127.0.0.1 --port 8001 &
JUDGE_DEV=1 python -m app.dev_seed --count 3                     # посылки в очередь без публичного API
python -m app.admin_cli create-worker-token smoke | tail -1 > /tmp/judge-token && chmod 600 /tmp/judge-token
tools/smoke-internal-api.sh /tmp/judge-token                     # протокол воркера curl-ом, код выхода = число провалов
```
`dev_seed` и smoke-скрипт меняют данные: только для `judge_test`, не для продовой базы.
