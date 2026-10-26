# Развёртывание в WSL2 (systemd)

Сервисы: `judge-api-internal` (API для воркеров, 127.0.0.1:8001), `judge-reaper`, `judge-worker`; публичный API (модуль 4) и Caddy добавятся рядом. PostgreSQL 16 работает на той же машине.

## 0. Предусловия (проверьте один раз)

1. systemd в WSL: `/etc/wsl.conf`
   ```ini
   [boot]
   systemd=true
   [user]
   default=<ваш пользователь>
   [automount]
   enabled=true
   options="ro,uid=1001,gid=1001,umask=077"
   [interop]
   enabled=false
   appendWindowsPath=false
   ```
   затем в PowerShell `wsl --shutdown`. (Interop отключён, диски Windows смонтированы только для чтения и только для вашего
   пользователя; решение о безопасности. Репозиторий и кэш держите внутри ext4, в `/mnt/c` ничего не пишется.) Скрипты
   установки `wsl.conf`, `/mnt/c` и настройки прокси не трогают. В env-файлах systemd комментарии допустимы только на отдельных строках.
2. `systemctl is-system-running` отвечает `running` или `degraded`; cgroup v2: `stat -fc %T /sys/fs/cgroup` даёт `cgroup2fs`.
3. isolate установлен как в модуле 0 вместе с `isolate.service` (cg-keeper; без него `judge-worker` не запустится, `Requires=`). Проверка: `sandbox-check` от `judge` после шага 1 ниже. **Юниты воркера работают от пользователя `judge`, а не root**: если sandbox-check от `judge` не проходит, а от root проходит, скажите, что именно у вас в модуле 0 (setuid/права), и я скорректирую `User=`.
4. Дистрибутив называется `Ubuntu` (Ubuntu 24.04 LTS). Автозапуск самого WSL при входе в Windows: планировщик заданий Windows, действие `wsl.exe -d Ubuntu -u root -- /bin/true` при входе (WSL стартует вместе с первым вызовом, systemd поднимет юниты).

## 1. Установка

Сеть из WSL идёт через прокси на Windows (`127.0.0.1:8899`), а `sudo` по умолчанию сбрасывает переменные прокси,
поэтому `install-wsl.sh` запускается с `--preserve-env` (без этого pip не скачает пакеты; скрипт проверяет доступ к PyPI заранее).

```bash
sudo apt install postgresql rsync   # один раз (PostgreSQL 16 в Ubuntu 24.04)
P="--preserve-env=HTTP_PROXY,HTTPS_PROXY,NO_PROXY,http_proxy,https_proxy,no_proxy"
sudo $P ./deploy/install-wsl.sh     # пользователь judge, каталоги, код в /opt/judge (root, только чтение), venv, юниты, шаблоны env
sudo ./deploy/setup-postgres.sh     # роли и базы, пароли в env-файлы (см. ниже)
sudo $P ./deploy/install-wsl.sh     # ещё раз: теперь есть DATABASE_URL, скрипт применит миграции и создаст токен воркера
```

`install-wsl.sh` можно запускать сколько угодно раз (обновление тоже им). Код копируется `rsync --delete` с владельцем root
и без права записи для группы и остальных: пользователь `judge` и разработчик не могут его менять. Байткод собирается
заранее (`judge` не может писать `__pycache__`). Если в `/etc/judge/worker.env` ещё `JUDGE_WORKER_TOKEN=CHANGE_ME`,
скрипт создаёт токен и записывает его туда сам, не показывая.

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
# миграции и токен воркера уже сделал install-wsl.sh; j нужна для ручных команд (admin_cli status и т.п.)
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

`git pull`, снова `sudo $P ./deploy/install-wsl.sh` (он же применит новые миграции), затем
`sudo systemctl restart judge-api-internal judge-reaper judge-worker`.
Перезапуск воркеров корректный: текущие проверки дорабатываются (до 2 минут), потом процессы выходят.

## 5. Внешний доступ (Caddy)

Caddy (собственный домен `.ru`, модуль 6) проксирует только публичный API (порт 8000, модуль 4). **Порт 8001 (внутренний API) в Caddyfile не добавлять.**
Второй рубеж: внутренний API отвечает 403 на любой запрос с `X-Forwarded-For` / `X-Forwarded-Proto` / `X-Forwarded-Host`
(Caddy ставит их всегда), `Forwarded`, `X-Real-IP` и заголовками Cloudflare (`docs/WORKER_PROTOCOL.md`).

## 6. Временный доступ без пароля на время стабилизации (удалить до запуска с учениками)

На время отладки модуля 3 разработчик получает `sudo` без пароля ровно на одну программу,
[`deploy/dev/judge-dev`](../deploy/dev/judge-dev), через [`deploy/dev/sudoers-judge-dev`](../deploy/dev/sudoers-judge-dev).
От root она выполняет только `svc` (systemctl для трёх юнитов `judge-*`) и `signal` (сигнал процессу воркера или
reaper). Всё остальное делается через `runuser -u judge`, входные файлы читаются правами вызывающего.
Python-код запускается, только если всё в `/opt/judge` принадлежит root и недоступно на запись другим.

```bash
sudo install -o root -g root -m 0755 deploy/dev/judge-dev /usr/local/sbin/judge-dev
sudo visudo -cf deploy/dev/sudoers-judge-dev
sudo install -o root -g root -m 0440 deploy/dev/sudoers-judge-dev /etc/sudoers.d/judge-dev
```

Чистка тестовых данных в этот доступ **не входит**: `sudo ./deploy/dev/reset-production.sh --yes-wipe-production`
(с паролем) пересоздаёт базу `judge` миграциями, удаляет блобы и кэш воркеров и записывает новый токен воркера.

> **Обязательно перед запуском с учениками (первый шаг модуля 6):**
> ```bash
> sudo rm /etc/sudoers.d/judge-dev /usr/local/sbin/judge-dev
> sudo -l -U buklinfur | grep -c judge-dev   # должно быть 0
> ```

## 7. Проверки для разработчика (без sudo)

```bash
set -a; . ~/.config/judge/test.env; set +a                       # JUDGE_TEST_DATABASE_URL от setup-postgres.sh
python -m unittest discover -s tests/pg -t . -v                  # очередь, API, миграции (каждый класс в своей схеме)
export DATABASE_URL=$JUDGE_TEST_DATABASE_URL JUDGE_BLOB_DIR=/tmp/judge-blobs PYTHONPATH=.:backend
alembic upgrade head && alembic check                            # схема public базы judge_test
uvicorn app.internal_main:app --host 127.0.0.1 --port 8001 &
JUDGE_DEV=1 python -m app.dev_seed --count 3                     # посылки в очередь без публичного API
JUDGE_DEV=1 python -m app.dev_seed --package problems-examples/sum-of-two --source sol.cpp --language cpp --user 2
python -m app.admin_cli judgements --last 20                     # последние проверки: вердикт, первый провал, вид TL
python -m app.admin_cli create-worker-token smoke | tail -1 > /tmp/judge-token && chmod 600 /tmp/judge-token
tools/smoke-internal-api.sh /tmp/judge-token                     # протокол воркера curl-ом, код выхода = число провалов
```
`dev_seed` и smoke-скрипт меняют данные: только для `judge_test`, не для продовой базы.
