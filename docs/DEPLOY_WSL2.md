# Развёртывание в WSL2 (systemd)

Сервисы: `judge-api-internal` (API для воркеров, 127.0.0.1:8001), `judge-reaper`, `judge-worker`; публичный API (модуль 4)
и Caddy добавятся рядом. PostgreSQL 16 работает на той же машине. Всё работает от системного пользователя `judge`,
код лежит в `/opt/judge` (принадлежит root, только чтение), настройки в `/etc/judge/*.env`.

Последовательность ниже проверена целиком на Windows 11 + WSL2 + Ubuntu 24.04 (октябрь 2026, модуль 3).

## 0. Предусловия (один раз)

1. `/etc/wsl.conf`:
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
   затем в PowerShell `wsl --shutdown`. Interop отключён, диски Windows смонтированы только для чтения и только для вашего
   пользователя (решение о безопасности). Репозиторий, база и кэш живут внутри ext4; в `/mnt/c` ничего не пишется.
   Скрипты установки `wsl.conf`, `/mnt/c` и настройки прокси не трогают.
2. `systemctl is-system-running` отвечает `running` или `degraded`; `stat -fc %T /sys/fs/cgroup` даёт `cgroup2fs`.
3. isolate 2.x установлен как в модуле 0 (`/usr/local/bin/isolate`, setuid) вместе с `isolate.service` (cg-keeper).
   Число box-ов isolate выводит из диапазона subuid пользователя `isolate` (`/etc/subuid`, 65536 номеров).
   Воркеры занимают box 0–9 (`JUDGE_BOX_BASE + 2i`), ручные запуски `runner.cli` — 100–101.
4. Дистрибутив называется `Ubuntu` (Ubuntu 24.04 LTS). Автозапуск WSL при входе в Windows: планировщик заданий,
   действие `wsl.exe -d Ubuntu -u root -- /bin/true` (WSL стартует, systemd поднимает включённые юниты).

## 1. Сеть: прокси на Windows

DNS внутри WSL не работает; наружу ходят через HTTP-прокси на Windows `127.0.0.1:8899` (`HTTP(S)_PROXY`, а
`NO_PROXY=localhost,127.0.0.1,::1`, поэтому PostgreSQL и внутренний API идут мимо прокси). Что из этого следует:

* **`sudo` сбрасывает переменные прокси.** Всё, что качает из сети под sudo, запускайте с
  `--preserve-env=HTTP_PROXY,HTTPS_PROXY,NO_PROXY,http_proxy,https_proxy,no_proxy`.
* `install-wsl.sh` сначала пробует `pip install --no-index`: если все закреплённые версии уже стоят, сеть не нужна
  (обычное обновление кода). Только если чего-то не хватает, он проверяет доступ к PyPI и называет прокси в ошибке.
* Ошибка `PyPI is not reachable through proxy none` — переменные не дошли через sudo; `... through proxy http://127.0.0.1:8899`
  — не запущен прокси на Windows. Повторять установку без исправления причины бесполезно.
* Сервисам сеть наружу не нужна: в юнитах переменных прокси нет, воркеры ходят только на `127.0.0.1:8001`.

## 2. Установка

```bash
sudo apt install postgresql rsync                     # один раз (PostgreSQL 16 из Ubuntu 24.04)
P="--preserve-env=HTTP_PROXY,HTTPS_PROXY,NO_PROXY,http_proxy,https_proxy,no_proxy"
sudo $P ./deploy/install-wsl.sh                       # 1-й раз: пользователь judge, каталоги, код, venv, юниты, шаблоны env
sudo ./deploy/setup-postgres.sh                       # роли и базы, пароли в env-файлы (ниже)
sudo $P ./deploy/install-wsl.sh                       # 2-й раз: теперь есть DATABASE_URL: миграции и токен воркера
```

Ожидаемый конец второго запуска: строки alembic `Running upgrade ...`, затем
`worker token '<host>-<время>' created and written to /etc/judge/worker.env (not shown)` и `installed.`

Что делает `install-wsl.sh` (его можно запускать сколько угодно раз, обновление тоже им):

| Шаг | Подробности |
|---|---|
| Пользователь и каталоги | `judge` (system, nologin, домашний `/var/lib/judge`), `/var/lib/judge/blobs` и `/var/cache/judge/worker` (judge), `/etc/judge` (750 root:judge) |
| Код | `rsync --delete` в `/opt/judge` с владельцем root и без записи для группы и остальных; `__pycache__` не копируется, байткод собирается заранее (`judge` не может его писать) |
| venv | `/opt/judge/.venv`, точные версии из `requirements.txt`; сеть только если чего-то не хватает |
| Env и юниты | шаблоны `/etc/judge/*.env` только если файлов нет; юниты в `/etc/systemd/system`, `daemon-reload`; ничего не включает и не запускает |
| База | от `judge`: `alembic upgrade head`; если в `worker.env` `JUDGE_WORKER_TOKEN=CHANGE_ME` — создаёт токен и записывает его, не показывая |

Команды от `judge` скрипты запускают через `/usr/sbin/runuser` по абсолютному пути и с тем же `PATH`, что systemd даёт
сервисам (`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin`): с урезанным `PATH` раннер не найдёт isolate в `/usr/local/bin`.

### PostgreSQL (`deploy/setup-postgres.sh`)

Можно запускать повторно; пароли сохраняются, новые выдаёт `--rotate`.

* `conf.d/judge.conf`: `listen_addresses = 'localhost'`, `password_encryption = 'scram-sha-256'`; основной `postgresql.conf` не меняется.
  TCP-строка в `pg_hba.conf` с `trust`/`md5`/`password` останавливает скрипт (правка руками).
* Роли `judge` (продовая) и `judge_test` (тесты): LOGIN без superuser/createdb/createrole; у каждой своя база-владелец,
  `CONNECT` отобран у PUBLIC (`judge_test` не может подключиться к `judge`).
* Пароли `openssl rand -hex 24`; в psql через stdin, в файлы через окружение (не видны в `ps` и истории):
  `DATABASE_URL` → `/etc/judge/backend.env` (640 root:judge), `JUDGE_TEST_DATABASE_URL` → `~/.config/judge/test.env` (600).
* В конце проверяет: слушает только `127.0.0.1`, вход по паролю работает, неверный отвергается, пароли хранятся как SCRAM.

## 3. Проверка до запуска сервисов

```bash
j() { sudo -u judge bash -c 'set -a; . /etc/judge/backend.env; set +a; cd /opt/judge; PYTHONPATH=/opt/judge:/opt/judge/backend "$@"' _ "$@"; }
w() { sudo -u judge bash -c 'set -a; . /etc/judge/worker.env;  set +a; cd /opt/judge; PYTHONPATH=/opt/judge "$@"' _ "$@"; }
w .venv/bin/python -m runner.cli sandbox-check   # 5 строк ok (box 100-101)
w .venv/bin/python -m runner.cli redteam         # 14 строк ok: лимиты, нет сети, секреты judge невидимы
w .venv/bin/python -m worker plan                # 5 воркеров на ядрах 1-5, box 0-9; система: ядра 0, 6, 7
j .venv/bin/python -m app.admin_cli status       # база доступна, воркеров пока нет
```

`redteam` от `judge` важнее, чем от вашего пользователя: `judge` может читать пароль базы и токен (`/etc/judge`),
все тесты (`/var/lib/judge/blobs`) и кэш с ответами (`/var/cache/judge`); программа в песочнице не должна видеть ничего из этого.

## 4. Запуск

```bash
sudo systemctl enable --now judge-api-internal judge-reaper judge-worker
j .venv/bin/python -m app.admin_cli status       # через пару секунд: 5 воркеров idle, hb 0-1s
journalctl -u judge-worker -u judge-api-internal -u judge-reaper --since -5min -p warning   # пусто
```

`judge-worker` требует `isolate.service` (`Requires=` + `After=`): без cg-keeper все проверки закончились бы IE.
Внутренний API запускается с `--timeout-keep-alive 75`: воркеры шлют heartbeat раз в 5 с по постоянному соединению,
а у uvicorn по умолчанию 5 с, и heartbeat попадал в закрывающееся соединение (`heartbeat failed: Remote end closed connection`).

### Поведение при остановке и сбоях (проверено)

| Что | Что происходит |
|---|---|
| `systemctl stop/restart judge-worker` (один SIGTERM supervisor-у) | свободные воркеры выходят сразу, занятые дорабатывают текущую проверку (до `JUDGE_DRAIN_TIMEOUT_S`, 120 с), потом выходят |
| Второй SIGTERM | проверка обрывается на границе теста, задание возвращается в очередь через `release` (не считается провалом) и перепроверяется (`attempt+1`) |
| Процесс воркера убит (`kill -9`, сбой) | supervisor поднимает его через 1 с (затем 2, 4 … 60 с); новый экземпляр при регистрации сразу возвращает задание прежнего (`requeued`, `worker restarted`, засчитывается как провал) |
| Воркер завис (нет heartbeat 30 с) | reaper возвращает задание в очередь (`worker lost (no heartbeat)`); очнувшийся воркер получает `cancel` или 409 и результат не доставляет |
| Supervisor вышел | `Restart=always`: systemd перезапускает сервис через 3 с |

Не отправляйте `systemctl kill` без `--kill-whom=main`: по умолчанию сигнал получат все процессы юнита, включая сами
процессы `isolate`; `systemctl stop` при `KillMode=mixed` шлёт SIGTERM только supervisor-у.

## 5. Наблюдаемость

| Что | Как |
|---|---|
| Логи | `journalctl -u judge-worker -u judge-api-internal -u judge-reaper -f` (группа `adm` читает без sudo) |
| Метрики Prometheus | `curl http://127.0.0.1:8001/internal/metrics` (опционально защитить `JUDGE_METRICS_TOKEN`) |
| JSON: загрузка, метрики, воркеры | `curl http://127.0.0.1:8001/internal/status` (одна транзакция: все числа из одного момента) |
| Воркеры в консоли | `j .venv/bin/python -m app.admin_cli status` |
| Последние проверки | `j .venv/bin/python -m app.admin_cli judgements --last 20` (вердикт, первый провал, вид TL, попытки, воркер или IE) |
| История очереди | `j .venv/bin/python -m app.admin_cli events --last 30` (регистрации, `released`, `requeued`, `gave_up`, с воркером; 14 дней) |

Метрики: `judge_queue_length`, `judge_queue_oldest_age_seconds`, `judge_in_flight`, `judge_workers{state}`,
`judge_typical_judge_seconds`, `judge_queue_wait_seconds`, `judge_finished_total{verdict}`, `judge_requeued_total`.
Тревожные признаки: все воркеры заняты и растёт `oldest_queued_age`; растёт `requeued_total` (воркеры падают); нет воркеров онлайн.

В журнале `isolate.service` записи `A process of this unit has been killed by the OOM killer` появляются при каждом
`redteam` (тесты `memory-bomb` специально выедают память, ядро убивает их внутри cgroup песочницы) и при решениях учеников
с ML. Сам cg-keeper при этом не страдает.

## 6. Обновление

```bash
git pull
sudo $P ./deploy/install-wsl.sh        # код, зависимости (если менялись), юниты, новые миграции
sudo systemctl restart judge-api-internal judge-reaper judge-worker
```
Перезапуск воркеров корректный: текущие проверки дорабатываются, потом процессы выходят и systemd поднимает новые.

## 7. Внешний доступ (Caddy)

Caddy (собственный домен `.ru`, модуль 6) проксирует только публичный API (порт 8000, модуль 4). **Порт 8001 в Caddyfile не добавлять.**
Второй рубеж: внутренний API отвечает 403 на любой запрос с `X-Forwarded-For` / `X-Forwarded-Proto` / `X-Forwarded-Host`
(Caddy ставит их всегда), `Forwarded`, `X-Real-IP` и заголовками Cloudflare (`docs/WORKER_PROTOCOL.md`).

## 8. Временный доступ без пароля на время стабилизации (удалить до запуска с учениками)

На время отладки модуля 3 разработчик получил `sudo` без пароля ровно на одну программу,
[`deploy/dev/judge-dev`](../deploy/dev/judge-dev), через [`deploy/dev/sudoers-judge-dev`](../deploy/dev/sudoers-judge-dev).
От root она выполняет только `svc` (systemctl для трёх юнитов `judge-*`, без пейджера, `kill` только главному процессу)
и `signal` (сигнал процессу воркера или reaper). Всё остальное — через `runuser -u judge`; входные файлы читаются правами
вызывающего и копируются в закрытый каталог `judge`; Python запускается, только если `/opt/judge` принадлежит root.

```bash
sudo install -o root -g root -m 0755 deploy/dev/judge-dev /usr/local/sbin/judge-dev
sudo visudo -cf deploy/dev/sudoers-judge-dev
sudo install -o root -g root -m 0440 deploy/dev/sudoers-judge-dev /etc/sudoers.d/judge-dev
```

Остановка долгих процессов `judge-dev api` / `worker run-one`: через `judge-dev signal TERM <pid>` или `svc stop`, а не
убийством самого `sudo` (SIGKILL родителю не доходит до процессов `judge`, они остаются сиротами).

Чистка тестовых данных в этот доступ **не входит**: `sudo ./deploy/dev/reset-production.sh --yes-wipe-production`
(с паролем) останавливает сервисы, пересоздаёт базу `judge` миграциями, удаляет блобы и кэш воркеров и записывает новый токен.

> **Обязательно перед запуском с учениками (первый шаг модуля 6):**
> ```bash
> sudo rm /etc/sudoers.d/judge-dev /usr/local/sbin/judge-dev
> sudo -l -U buklinfur | grep -c judge-dev   # должно быть 0
> ```

## 9. Проверки для разработчика (без sudo)

```bash
set -a; . ~/.config/judge/test.env; set +a                       # JUDGE_TEST_DATABASE_URL от setup-postgres.sh
python -m unittest discover -s tests -t .                        # всё, что не требует базы
python -m unittest discover -s tests/pg -t . -v                  # очередь, API, миграции, dev_seed (каждый класс в своей схеме)
export DATABASE_URL=$JUDGE_TEST_DATABASE_URL JUDGE_BLOB_DIR=/tmp/judge-blobs PYTHONPATH=.:backend
alembic upgrade head && alembic check                            # схема public базы judge_test
uvicorn app.internal_main:app --host 127.0.0.1 --port 8002 --timeout-keep-alive 75 &   # 8001 занят сервисом
JUDGE_DEV=1 python -m app.dev_seed --count 3                     # посылки в очередь без публичного API
JUDGE_DEV=1 python -m app.dev_seed --package problems-examples/sum-of-two --source sol.cpp --language cpp --user 2
python -m app.admin_cli judgements --last 20
python -m app.admin_cli create-worker-token smoke | tail -1 > /tmp/judge-token && chmod 600 /tmp/judge-token
tools/smoke-internal-api.sh /tmp/judge-token http://127.0.0.1:8002   # протокол воркера curl-ом, код выхода = число провалов
```
`dev_seed` и smoke-скрипт меняют данные: только для `judge_test`. Ручные `runner.cli` (box 100–101) можно запускать
при работающих сервисах; два ручных запуска одновременно конфликтуют между собой.
