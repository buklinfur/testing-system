# Развёртывание в WSL2 (systemd)

Сервисы: `judge-api-internal` (API для воркеров, 127.0.0.1:8001), `judge-reaper`, `judge-worker`; публичное приложение
(вход, 127.0.0.1:8000) пока запускается вручную (§7), его юнит и Caddy — модуль 6. PostgreSQL 16 работает на той же машине. Всё работает от системного пользователя `judge`,
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

### GitHub по SSH через прокси

Репозиторий — `git@github.com:buklinfur/testing-system.git` (remote `testing-system`, основная ветка `master`,
`ROADMAP.md` → «Репозиторий»). Обычный порт 22 и DNS из WSL недоступны, поэтому SSH идёт к `ssh.github.com:443`
через тот же HTTP-прокси (метод `CONNECT`). В `~/.ssh/config` разработчика:

```
Host github.com
    HostName ssh.github.com
    Port 443
    User git
    ProxyCommand nc -X connect -x 127.0.0.1:8899 %h %p
```

* `nc` — из пакета `netcat-openbsd`, он есть в Ubuntu по умолчанию; у других вариантов netcat ключа `-X` нет.
* Имя `ssh.github.com` разрешает прокси, а не WSL: локальный DNS для этого не нужен.
* Ключ — обычная пара ключей SSH пользователя, добавленная в GitHub. Ключи в репозиторий и в документацию не
  попадают.
* Проверка: `ssh -T git@github.com` отвечает приветствием GitHub. `nc: proxy error` или `Connection refused` на
  `127.0.0.1:8899` — не запущен прокси на Windows; повторять push без него бесполезно.
* Первое подключение спрашивает отпечаток хоста `[ssh.github.com]:443` — сверьте его с опубликованными отпечатками
  GitHub.

## 2. Установка

```bash
sudo apt install postgresql rsync                     # один раз (PostgreSQL 16 из Ubuntu 24.04)
P="--preserve-env=HTTP_PROXY,HTTPS_PROXY,NO_PROXY,http_proxy,https_proxy,no_proxy"
sudo $P ./deploy/install-wsl.sh                       # 1-й раз: пользователь judge, каталоги, код, venv, юниты, шаблоны env
sudo ./deploy/setup-postgres.sh                       # роли и базы, пароли в env-файлы (ниже)
sudo $P ./deploy/install-wsl.sh                       # 2-й раз: теперь есть DATABASE_URL: миграции и токен воркера
```

Ожидаемый конец второго запуска: строки alembic `Running upgrade ...`, затем
`worker token '<host>-<время>' created and written to /tmp/tmp.…/token (0600, not shown)`,
`worker token '<host>-<время>' written to /etc/judge/worker.env (not shown)` и `installed.` (временный файл токена
удаляется сразу, D-089).

Модуль 4а добавил пакеты (`argon2-cffi`, `cryptography`, `pyotp`, `segno`): первый `install-wsl.sh` после обновления
качает их, поэтому нужен прокси и `$P`.

Что делает `install-wsl.sh` (его можно запускать сколько угодно раз, обновление тоже им):

| Шаг | Подробности |
|---|---|
| Пользователь и каталоги | `judge` (system, nologin, домашний `/var/lib/judge`), `/var/lib/judge/blobs` и `/var/cache/judge/worker` (judge), `/etc/judge` (750 root:judge) |
| Код | `rsync --delete` в `/opt/judge` с владельцем root и без записи для группы и остальных; `__pycache__` не копируется, байткод собирается заранее (`judge` не может его писать) |
| venv | `/opt/judge/.venv`, точные версии из `requirements.txt`; сеть только если чего-то не хватает |
| Env и юниты | шаблоны `/etc/judge/*.env` только если файлов нет; юниты в `/etc/systemd/system`, `daemon-reload`; ничего не включает и не запускает |
| База | от `judge`: `alembic upgrade head`; если в `worker.env` `JUDGE_WORKER_TOKEN=CHANGE_ME` — создаёт токен и записывает его, не показывая (`deploy/lib/worker-token.sh`, D-089: `judgectl worker token create --out` в личный каталог `mktemp -d` 0700, оттуда его читает `awk`; ни в командной строке, ни в переменной, ни в выводе токена нет; новый `worker.env` — под `umask 077`, итог `root:judge 0640`; каталог удаляет `trap` на `EXIT`, и при сбое тоже) |

Если в venv не хватает закреплённых пакетов, скрипт проверяет доступ к PyPI **до** копирования кода: при недоступной
сети `/opt/judge` остаётся прежним, а не с новым кодом при старых пакетах. В конце он перечисляет переменные, которые
заданы в `deploy/env/*.env.example`, но отсутствуют в существующих `/etc/judge/*.env` (шаблоны копируются только при
первой установке) — только имена, без значений.

Команды от `judge` скрипты запускают через `/usr/sbin/runuser` по абсолютному пути и с тем же `PATH`, что systemd даёт
сервисам (`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin`): с урезанным `PATH` раннер не найдёт isolate в `/usr/local/bin`.

### Ключ 2FA и origin публичного приложения (модуль 4а)

`install-wsl.sh` создаёт `/etc/judge/backend.env` из шаблона только один раз, поэтому на установке из модуля 3 новых
переменных там нет. Обе команды ниже правят файл на месте (владелец `root:judge` и права 640 не меняются),
ключ на экран не выводится, а уже заданный настоящий ключ не перезаписывается: новый ключ сделал бы нечитаемыми
секреты 2FA всех админов ([`AUTH.md`](AUTH.md) §5).

```bash
sudo /opt/judge/.venv/bin/python - /etc/judge/backend.env <<'EOF'
import sys
from cryptography.fernet import Fernet
path, name = sys.argv[1], "JUDGE_SECRET_KEY"
with open(path, "r+", encoding="utf-8") as f:  # rewritten in place: owner and mode stay as they are
    lines = f.read().splitlines()
    old = [l.split("=", 1)[1].strip() for l in lines if l.startswith(name + "=")]
    if any(v and v != "CHANGE_ME" for v in old):
        sys.exit(f"{name} is already set: left as it is (a new key would make enrolled 2FA secrets unreadable)")
    lines = [l for l in lines if not l.startswith(name + "=")] + [f"{name}={Fernet.generate_key().decode()}"]
    f.seek(0); f.write("\n".join(lines) + "\n"); f.truncate()
print(f"{name} written to {path} (not shown)")
EOF
```

`JUDGE_PUBLIC_ORIGIN` — ровно то, что браузер пришлёт в `Origin` (`https://<домен>`; `http` допустим только для
`localhost`, для проверок до модуля 6). Повторный запуск заменяет значение:

```bash
sudo /opt/judge/.venv/bin/python - /etc/judge/backend.env https://judge.example.ru <<'EOF'
import sys
path, value, name = sys.argv[1], sys.argv[2], "JUDGE_PUBLIC_ORIGIN"
with open(path, "r+", encoding="utf-8") as f:
    lines = [l for l in f.read().splitlines() if not l.startswith(name + "=")] + [f"{name}={value}"]
    f.seek(0); f.write("\n".join(lines) + "\n"); f.truncate()
print(f"{name}={value} written to {path}")
EOF
```

**Сохраните копию ключа вне этой машины** (менеджер паролей, бумага в сейфе): если WSL или диск пропадут, без ключа
все секреты 2FA придётся выдавать заново (`admin reset-2fa` каждому админу). Посмотреть ключ, чтобы переписать:
`sudo grep '^JUDGE_SECRET_KEY=' /etc/judge/backend.env` — и очистите экран терминала после.

Необязательно: `JUDGE_TIMEZONE` (например `Asia/Novosibirsk`, по умолчанию `UTC`) — часовой пояс таблиц judgectl и
времени без смещения в `contest.yaml`; API всегда отвечает в UTC. Задаётся той же командой, что и origin, с другим
именем переменной.

После правки env перезапускать нечего, пока публичное приложение не работает как сервис (модуль 6); judgectl читает
файл при каждом запуске.

### Первый администратор

```bash
j .venv/bin/python -m app.judgectl admin add <логин> --name "<имя>"   # пароль дважды, затем QR и код 2FA
j .venv/bin/python -m app.judgectl admin list                              # 2FA = confirmed
```

`j` — функция из §3. Нужен настоящий терминал (`sudo -u judge` его сохраняет). Если код не подтвердился: `judgectl admin reset-2fa <логин>`.
`<…>` — плейсхолдеры: подставьте значение вместо них вместе со скобками (скопированная как есть команда упадёт на
перенаправлении, а не создаст админа со словом из примера). Админа, созданного по ошибке, можно удалить, пока он ни
разу не входил в веб: `judgectl admin delete <логин>`.
Остальные команды — [`JUDGECTL.md`](JUDGECTL.md).

### PostgreSQL (`deploy/setup-postgres.sh`)

Можно запускать повторно; пароли сохраняются, новые выдаёт `--rotate`.

* `conf.d/judge.conf`: `listen_addresses = 'localhost'`, `password_encryption = 'scram-sha-256'`; основной `postgresql.conf` не меняется.
  TCP-строка в `pg_hba.conf` с `trust`/`md5`/`password` останавливает скрипт (правка руками).
* Роли `judge` (продовая) и `judge_test` (тесты): LOGIN без superuser/createdb/createrole; у каждой своя база-владелец,
  `CONNECT` отобран у PUBLIC (`judge_test` не может подключиться к `judge`).
* Пароли `openssl rand -hex 24`; в psql через stdin, в файлы через окружение (не видны в `ps` и истории):
  `DATABASE_URL` → `/etc/judge/backend.env` (640 root:judge), `JUDGE_TEST_DATABASE_URL` → `~/.config/judge/test.env` (600).
* В конце проверяет: слушает только `127.0.0.1`, вход по паролю работает, неверный отвергается, пароли хранятся как SCRAM.
* Базы создаются в `UTF8` с локалью `C.UTF-8` (`LC_COLLATE` и `LC_CTYPE`, из `template0`; `JUDGE_DB_LOCALE` в
  `deploy/lib/pg-database.sh`), а не с той, что у кластера. От `LC_CTYPE` зависит `lower()`: уникальность имён файлов
  4в.3 (`lower(path)`, `lower(name)`) должна сворачивать и кириллицу. Уже созданная база при повторном запуске свою
  локаль сохраняет. Проверить локаль рабочей базы (ничего не меняет):

  ```bash
  sudo -u postgres psql -X -d judge -c "SELECT pg_encoding_to_char(encoding) AS encoding, datcollate, datctype, lower('Ё') AS lower_yo FROM pg_database WHERE datname = current_database()"
  ```

  Ожидается `UTF8 | C.UTF-8 | C.UTF-8 | ё`. Если `lower_yo` — `Ё` (локаль `C` или `POSIX`), имена `Отчёт.pdf` и
  `отчёт.pdf` база различает: тогда повтор ловят только правила сервиса (`casefold`). Сменить локаль готовой базы
  нельзя — только пересоздать (`reset-production.sh` до учеников делает именно это).

## 3. Проверка до запуска сервисов

```bash
j() { sudo -u judge bash -c 'set -a; . /etc/judge/backend.env; set +a; cd /opt/judge; PYTHONPATH=/opt/judge:/opt/judge/backend "$@"' _ "$@"; }
w() { sudo -u judge bash -c 'set -a; . /etc/judge/worker.env;  set +a; cd /opt/judge; PYTHONPATH=/opt/judge "$@"' _ "$@"; }
w .venv/bin/python -m runner.cli sandbox-check   # 5 строк ok (box 100-101)
w .venv/bin/python -m runner.cli redteam         # 14 строк ok: лимиты, нет сети, секреты judge невидимы
w .venv/bin/python -m worker plan                # 5 воркеров на ядрах 1-5, box 0-9; система: ядра 0, 6, 7
j .venv/bin/python -m app.judgectl queue status  # база доступна
j .venv/bin/python -m app.judgectl worker list   # воркеров пока нет
```

`j` и `w` — функции оболочки: они есть только в том терминале, где их объявили. Чтобы они были в каждом новом
терминале, добавьте эти две строки (и `P=...` из §2) в `~/.bashrc` и выполните `source ~/.bashrc`.

`redteam` от `judge` важнее, чем от вашего пользователя: `judge` может читать пароль базы и токен (`/etc/judge`),
все тесты (`/var/lib/judge/blobs`) и кэш с ответами (`/var/cache/judge`); программа в песочнице не должна видеть ничего из этого.

### Задачи и контесты (модуль 4а.2)

`judge` не читает ваш домашний каталог, поэтому пакеты и `contest.yaml` передаются через stdin (`-`):

```bash
tools/pack-problem.sh problems-examples/sum-of-two | j .venv/bin/python -m app.judgectl problem import -
j .venv/bin/python -m app.judgectl contest apply - < contests-examples/practice.yaml
j .venv/bin/python -m app.judgectl contest publish practice
```

Подробно: [`PROBLEM_IMPORT.md`](PROBLEM_IMPORT.md), [`CONTESTS.md`](CONTESTS.md), [`JUDGECTL.md`](JUDGECTL.md).

## 4. Запуск

```bash
sudo systemctl enable --now judge-api-internal judge-reaper judge-worker
j .venv/bin/python -m app.judgectl worker list   # через пару секунд: 5 воркеров idle, heartbeat 0-1s
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
| Всё сразу | `j .venv/bin/python -m app.judgectl status --details` (база и миграция, воркеры, очередь, конфигурация, блобы без файлов и кто на них ссылается) |
| Воркеры в консоли | `j .venv/bin/python -m app.judgectl worker list` (и `queue status`) |
| Последние проверки | `j .venv/bin/python -m app.judgectl queue judgements --last 20` (вердикт, первый провал, вид TL, попытки, воркер или IE) |
| История очереди | `j .venv/bin/python -m app.judgectl queue events --last 30` (регистрации, `released`, `requeued`, `gave_up`, с воркером; 14 дней) |

Метрики: `judge_queue_length`, `judge_queue_oldest_age_seconds`, `judge_in_flight`, `judge_workers{state}`,
`judge_typical_judge_seconds`, `judge_queue_wait_seconds`, `judge_finished_total{verdict}`, `judge_requeued_total`,
`judge_orphan_reclaims` (задания, отобранные у воркера, который держал их, не называя в heartbeat; D-069).
Тревожные признаки: все воркеры заняты и растёт `oldest_queued_age`; растёт `requeued_total` (воркеры падают); нет воркеров онлайн;
растёт `judge_orphan_reclaims` (воркеры теряют задания: смотреть `journalctl -u judge-worker` на «could not be delivered»).

**Воркеры остановились с кодом 78** — окончательная ошибка конфигурации ([`WORKER.md`](WORKER.md) «Остановка и
сбои»): supervisor не перезапускает воркер, вышедший с 78, останавливает остальные и выходит с 78 сам, а systemd
(`RestartPreventExitStatus=78`) службу не перезапускает. Выглядит так:

```text
$ systemctl status judge-worker
     Active: failed (Result: exit-code) since …
    Process: 1234 ExecStart=/opt/judge/.venv/bin/python -m worker run (code=exited, status=78/CONFIG)

$ journalctl -u judge-worker -n 20
ERROR [w0] judge.worker: cannot register: authentication failed …
ERROR [sup] judge.supervisor: worker 0 exited with code 78 (a configuration error, see its log above): not restarting; …
```

Причину называет строка `ERROR` перед ней: токен (`JUDGE_WORKER_TOKEN` в `/etc/judge/worker.env` отозван или неверен —
новый: вписать `JUDGE_WORKER_TOKEN=CHANGE_ME` и запустить `sudo ./deploy/install-wsl.sh`, он создаст и запишет токен,
не показывая), настройка (`bad configuration: …`) или dev-бэкенд (`backend=local is UNSAFE …`,
D-065). Исправить и `sudo systemctl start judge-worker`. Код **75** (вытеснение процессом с тем же именем) так не
выглядит: supervisor перезапускает воркер через минуту, служба остаётся `active`.

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

**Обновление до 4а.4 (проверка задач эталонами).** Меняются протокол воркера и схема (миграция `b7e3d91a4c62`),
поэтому сервисы останавливаются до установки, а не перезапускаются после. `/etc/judge/worker.env` создаётся из
шаблона только один раз, его надо поправить руками: воркер получает проверки версий задач, только если объявляет
capability `problem_check` (миграция уже добавила её неотозванным токенам с `cpu`).

```bash
(umask 077; sudo -u postgres pg_dump -Fc judge > ~/judge-before-4a4.dump)     # резервная копия базы
sudo systemctl stop judge-worker judge-reaper judge-api-internal
sudo $P ./deploy/install-wsl.sh                                               # код + миграция
sudo sed -i 's/^JUDGE_CAPABILITIES=cpu$/JUDGE_CAPABILITIES=cpu,problem_check/' /etc/judge/worker.env
sudo systemctl start judge-api-internal judge-reaper judge-worker
j .venv/bin/python -m app.judgectl worker list      # CAPS: cpu,problem_check у каждого воркера
j .venv/bin/python -m app.judgectl status           # migration b7e3d91a4c62, раздел checks
```
Существующие версии задач остаются `ready` (их никто не проверял). Новые импорты с чекером, интерактором или
эталонами ждут проверки (`pending_check`); если ни один воркер не объявляет `problem_check`, они не станут `ready`
никогда — `judgectl status` покажет растущее число `pending_check`. Перепроверить старую версию:
`judgectl problem recheck <slug>@<v>`. Настройки бэкенда: `JUDGE_MAX_CONCURRENT_CHECKS` (1),
`JUDGE_CHECK_TIMEOUT_SEC` (600) в `backend.env`.

**Обновление от 4а (`master`) до `module-4v`: 4в.1–4в.4 и 4б.0 одним путём.** Заменяет прежние разделы
«Обновление до 4в.1», «до 4б.0», «до 4в.2» и «до 4в.3» (на них ссылаются D-064…D-082): все их команды и проверки — здесь.
Исходное состояние — установка 4а с `master` (последняя миграция `b7e3d91a4c62`, раздел 4а.4 выше выполнен).

Что меняется:
- **схема** — четыре миграции подряд, `install-wsl.sh` применяет их одним `alembic upgrade head`:
  `b32303b5df22` (лента, ручные задания, работы, оценки; у продлений вторая цель) → `e73284028c20` (комментарии) →
  `32cd8723a2bc` (вложения и ссылки, у файлов сдачи вид `text` / `file`) → `b8e0cbcc4cda` (библиотека материалов,
  Справочник, ссылки `[[памятка:slug]]`; заполняет `material_refs` из уже сохранённых условий и текстов);
- **воркер** — юнит `judge-worker` получает `RestartPreventExitStatus=78` (D-066); протокол — 409 `deregistered` и
  поле `reclaimed` (совместимо в обе стороны, D-068, D-069); dev-бэкенд `local` под systemd не стартует никогда (D-065);
- **бэкенд** — отдача файлов (`FILES.md`), приём загрузок во временный каталог `<JUDGE_BLOB_DIR>/tmp` (создаётся сам,
  владелец `judge`); девять новых необязательных переменных (таблица ниже); новых пакетов Python нет.

Переменные `/etc/judge/backend.env` (все из `backend/app/settings.py`; **новые в 4в — жирным**). Обязательных
новых нет: без строки действует значение по умолчанию; неверное значение (не число, 0 там, где нужен минимум 1,
отрицательная частота) останавливает запуск с сообщением.

| Переменная | По умолчанию | Обязательна |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://judge:judge@127.0.0.1:5432/judge` | да (пишет `setup-postgres.sh`) |
| `JUDGE_SECRET_KEY` | — | да для публичного приложения и 2FA (ключ Fernet) |
| `JUDGE_PUBLIC_ORIGIN` | — | да для публичного приложения (`https://<домен>`) |
| `JUDGE_PUBLIC_PORT` / `JUDGE_INTERNAL_PORT` | `8000` / `8001` | нет |
| `JUDGE_BLOB_DIR` | `/var/lib/judge/blobs` | нет |
| `JUDGE_TIMEZONE` | `UTC` | нет |
| `JUDGE_METRICS_TOKEN` | — (метрики без токена) | нет |
| `JUDGE_TOTP_ISSUER` | `judge` | нет |
| `JUDGE_LOGIN_FAIL_LIMIT`, `JUDGE_ADMIN_LOGIN_FAIL_LIMIT` | `10`, `5` | нет |
| `JUDGE_LOGIN_WINDOW_S`, `JUDGE_LOGIN_LOCKOUT_S` | `900`, `900` | нет |
| `JUDGE_IP_FAIL_LIMIT`, `JUDGE_IP_WINDOW_S`, `JUDGE_IP_LOCKOUT_S` | `300`, `900`, `900` | нет |
| `JUDGE_AUTH_RETENTION_DAYS` | `30` | нет |
| `JUDGE_HEARTBEAT_TIMEOUT_S`, `JUDGE_MAX_ATTEMPTS`, `JUDGE_MAX_JUDGING_S` | `30`, `3`, `1800` | нет |
| `JUDGE_FAIR_WINDOW_S`, `JUDGE_EVENT_RETENTION_DAYS`, `JUDGE_REAPER_INTERVAL_S` | `300`, `14`, `5` | нет |
| `JUDGE_MAX_CONCURRENT_CHECKS`, `JUDGE_CHECK_TIMEOUT_SEC` | `1`, `600` | нет |
| `JUDGE_IMPORT_MAX_ARCHIVE_MB`, `JUDGE_IMPORT_MAX_UNPACKED_MB` | `100`, `512` | нет |
| `JUDGE_IMPORT_MAX_FILES`, `JUDGE_IMPORT_MAX_RATIO` | `5000`, `0` (выкл.) | нет |
| **`JUDGE_ASSIGNMENT_TEXT_MAX_KB`** | `64` — текст ответа ручного задания (4в.1) | нет |
| **`JUDGE_COMMENT_RATE_PER_MIN`** | `10` — комментариев ученика в минуту, `0` — без лимита (4в.2) | нет |
| **`JUDGE_SUBMISSION_FILES_MAX`** | `10` — файлов и ссылок в одной сдаче (4в.3) | нет |
| **`JUDGE_SUBMISSION_FILE_MAX_MB`**, **`JUDGE_SUBMISSION_TOTAL_MAX_MB`** | `20`, `50` — файл сдачи, все файлы сдачи | нет |
| **`JUDGE_STUDENT_QUOTA_MB`** | `200` — различные файлы ученика, `0` — без квоты | нет |
| **`JUDGE_ITEM_ATTACHMENTS_MAX`** | `20` — вложений у элемента ленты и у версии материала (4в.3, 4в.4) | нет |
| **`JUDGE_ATTACHMENT_MAX_MB`** | `50` — файл преподавателя (элемент, материал) | нет |

В `/etc/judge/worker.env` новых переменных нет; в нём не должно быть dev-переменных (`JUDGE_BACKEND=local`,
`JUDGE_ALLOW_UNSAFE_LOCAL`, `JUDGE_DEV`): воркер под systemd с ними откажется стартовать с кодом 78 (D-065).

Команды — из рабочей копии на ветке `module-4v` **после push** (код на установку копирует `install-wsl.sh` из
этого каталога):

```bash
# 0. резервная копия базы, вне репозитория (D-064)
(umask 077; sudo -u postgres pg_dump -Fc judge > ~/judge-before-4v.dump)

# 1. код: ветка module-4v, ничего не изменено, совпадает с отправленной
git switch module-4v && git status --short          # пусто
git fetch testing-system && git status -sb | head -1  # ## module-4v...testing-system/module-4v (без ahead/behind)

# 2. локаль рабочей базы (ничего не меняет): ожидается UTF8 | C.UTF-8 | C.UTF-8 | ё
sudo -u postgres psql -X -d judge -c "SELECT pg_encoding_to_char(encoding) AS encoding, datcollate, datctype, lower('Ё') AS lower_yo FROM pg_database WHERE datname = current_database()"

# 3. остановка; публичное приложение (python -m app.public_main) — Ctrl-C в его терминале
sudo systemctl stop judge-worker judge-reaper judge-api-internal

# 4. код, юниты (+ daemon-reload), alembic upgrade head: b32303b5df22 → e73284028c20 → 32cd8723a2bc → b8e0cbcc4cda
sudo $P ./deploy/install-wsl.sh

# 5. переменные: строки со значениями по умолчанию (можно не добавлять; добавленные легко поменять потом)
for kv in JUDGE_ASSIGNMENT_TEXT_MAX_KB=64 JUDGE_COMMENT_RATE_PER_MIN=10 JUDGE_SUBMISSION_FILES_MAX=10 \
          JUDGE_SUBMISSION_FILE_MAX_MB=20 JUDGE_SUBMISSION_TOTAL_MAX_MB=50 JUDGE_STUDENT_QUOTA_MB=200 \
          JUDGE_ITEM_ATTACHMENTS_MAX=20 JUDGE_ATTACHMENT_MAX_MB=50; do
  sudo grep -q "^${kv%%=*}=" /etc/judge/backend.env || echo "$kv" | sudo tee -a /etc/judge/backend.env
done
sudo grep -E '^(JUDGE_BACKEND|JUDGE_ALLOW_UNSAFE_LOCAL|JUDGE_DEV)=' /etc/judge/worker.env   # только JUDGE_BACKEND=isolate

# 6. проверки до запуска
systemctl show judge-worker -p RestartPreventExitStatus   # RestartPreventExitStatus=78: юнит перечитан
w .venv/bin/python -m runner.cli sandbox-check            # 5 строк ok (менялся runner/sandbox/local.py, D-065)
w .venv/bin/python -m runner.cli redteam                  # все ok
j .venv/bin/python -m app.judgectl status                 # migration b8e0cbcc4cda = head, config без замечаний

# 7. запуск
sudo systemctl start judge-api-internal judge-reaper judge-worker
systemctl status judge-worker --no-pager | head -3        # active (running), не failed 78/CONFIG
j .venv/bin/python -m app.judgectl worker list            # online, CAPS cpu,problem_check
j .venv/bin/python -m app.public_main                     # публичное приложение, в отдельном терминале

# 8. smoke (в третьем терминале): все блоки, в том числе лента, картинки, комментарии, файлы, материалы
SEED_CMD=tools/seed-as-judge.sh tools/smoke-admin-api.sh http://127.0.0.1:8000 https://<домен>   # failures: 0
# SKIP бывает только у сдач, если dev-student-1/-2 ещё не созданы: тогда запустить smoke ещё раз
```

Если в шаге 2 `datctype` не `C.UTF-8` (`lower_yo` = `Ё`), обновлению это не мешает, но поиск Справочника по
кириллице станет чувствителен к регистру; `reset-production.sh` перед учениками пересоздаёт базу с `C.UTF-8`
(D-073, D-080).

**Проверки вживую** (после smoke):
* **Отказ после deregister (D-068).** Дать воркерам работу и остановить их посреди неё:
  ```bash
  tools/seed-as-judge.sh --count 10                 # 10 посылок в очередь (dev_seed от judge, D-047)
  sudo systemctl stop judge-worker                  # сразу, пока они проверяются
  journalctl -u judge-api-internal --since "-2 min" | grep -E 'heartbeat|deregister' | tail -5
  j .venv/bin/python -m app.judgectl worker list    # STATE stopped у всех
  j .venv/bin/python -m app.judgectl queue events --last 15       # released / worker_stopped, без requeued
  j .venv/bin/python -m app.judgectl queue judgements --last 10   # недоделанные — queued, FAIL 0
  sudo systemctl start judge-worker
  ```
  После строки `POST /internal/worker/v1/deregister … 200` у того же воркера нет `heartbeat … 200`: поздний heartbeat
  получает `409`. Воркеры — `stopped`, их задания вернулись в очередь без провала.
* **Код 78 (D-066):** при нормальной работе `systemctl status judge-worker` — `active (running)`; после ошибки
  конфигурации — `failed`, `status=78/CONFIG` (§5).
* **Страховка «idle, но держит» (D-069)** вживую не воспроизводится; метрика должна быть 0:
  `curl -s --noproxy '*' http://127.0.0.1:8001/internal/metrics | grep judge_orphan_reclaims`
  (с `-H "Authorization: Bearer $JUDGE_METRICS_TOKEN"`, если он задан). Рост — смотреть `journalctl -u judge-worker`
  на «could not be delivered».
* **Картинки условия, вложения, файлы работ** (D-070, D-071, D-082) — блоки smoke «statement pictures» и «files and
  links»: заголовки `nosniff`, CSP `sandbox`, `ETag`, 304, предупреждение импорта о подделке.

**Ручная проверка через консоль** (без `SEED_CMD` smoke пропускает действия учеников — `SKIP` с командой, — тогда
обязательна; с ним — по желанию). `<id>` печатают команды `create`:

```bash
J='j .venv/bin/python -m app.judgectl'
$J group create 'проверка 4в'
# лента и работы (4в.1)
$J item create --kind assignment --title 'Проверка' --group 'проверка 4в' --due '2026-12-31 23:59' \
    --grading points --max-points 5 --answer text_or_files --comments on
$J item publish <id>
$J work list --item <id>                                  # пусто, пока в группе нет учеников
$J group add 'проверка 4в' <логин>
$J work show <id> <логин>                                 # assigned, сдач нет
# комментарии (4в.2)
$J comment post <id> --text 'Пишите вопросы сюда'
$J comment show <id>                                      # комментарий консоли
$J comment list --item <id>                               # одна ветка, AWAITING no
$J comment delete <comment-id> --reason 'проверка'
$J comment delete <comment-id>                            # already_deleted, выход 1
# вложения (4в.3): файл через stdin — judge не читает ваш домашний каталог
$J item attach <id> - --name 'условие.pdf' < ~/условие.pdf
$J item link <id> https://example.org --title 'Пример'
$J item show <id>                                         # блок attachments
$J item attach <id> - --name x.svg < /dev/null            # file_type_not_allowed, выход 1
# материалы и Справочник (4в.4)
echo 'Цикл for. См. [[памятка:proverka-nope]]' | $J material create proverka-4v --title 'Проверка 4в' --body-file - \
    --library groups --group 'проверка 4в'                # version 1; warning: proverka-nope
$J material attach proverka-4v - --name 'памятка.pdf' < ~/условие.pdf   # version 2
$J material show proverka-4v                              # версия 2 из 2, вложение, ссылка unknown
$J section create 'Проверка 4в'                           # печатает id раздела
$J section place <section-id> proverka-4v
$J material search 'проверка'                             # находит «Проверка 4в» (регистр не важен)
$J item create --kind material --material proverka-4v --group 'проверка 4в'   # название — материала
$J material where proverka-4v                             # material items: этот элемент
$J section delete <section-id> --yes
$J material archive proverka-4v
# уборка
$J item archive <id>
$J group archive 'проверка 4в'                            # в stderr — элементы ленты группы
```

Файл работы или материала — `-o -` с перенаправлением (`judgectl work file <id> <логин> <file_id> -o - > файл`,
`judgectl material file <slug> <att> -o - > файл`): `judge` не может писать в домашний каталог владельца, двоичное в
терминал не выводится.

**Проверка сброса на временных базах** (D-073; ничего не трогает в `judge`, сравнивает сброшенную базу со свежей и
теперь заполняет и данные 4в.4 — материалы, версии, их вложения, разделы, ссылки):

```bash
sudo ./deploy/dev/check-reset-db.sh                       # выход 0: базы одинаковы
```

**Данные, которые остаются в рабочей базе** после smoke и ручной проверки (плюс то, что оставлял smoke 4а):
- группы `smoke <время>`, `smoke <время> b` (откатывается с импортом), `smoke <время> feed`, `… comments`, `… files`
  (архивные) и `проверка 4в`; `dev-student-1` и `-2` — члены этих групп;
- элементы ленты (архивные): задания с работами, сдачами (`answer.md`, `main.py`, `report.pdf`, ссылка), оценками и
  продлением `dev-student-2`; задание `Вопросы <время>` с общей и личной веткой (комментарий с `<script>` и ESC,
  удалённый ответ админа), отметка прочтения админа; вложения заданий (PDF и ссылка);
- материалы `smoke-<время>-memo` (3 версии, PDF) и `smoke-<время>-loops`, `proverka-4v` — архивные (материал не
  удаляется); разделы удалены;
- блобы всего этого в `/var/lib/judge/blobs`; записи аудита `item.*`, `work.*`, `comment.*`, `material.*`,
  `library.*`; посылки `--count 10`.

Всё это убирает `deploy/dev/reset-production.sh` перед учениками (§6 «Сброс тестовых данных», D-073).

**Откат** — только из дампа (D-064), по шаблону ниже: дамп `~/judge-before-4v.dump`, код — `git switch master`.
`alembic downgrade` здесь не годится: миграции 4в отказывают, ничего не меняя, пока в базе есть их данные
(элементы ленты, комментарии, вложения, материалы и ссылки — D-079, D-084), а после smoke они есть.

### Откат обновления: пересоздание базы из дампа (D-064)

Любое обновление начинается с дампа (шаг 0: `(umask 077; sudo -u postgres pg_dump -Fc judge > ~/judge-before-<что>.dump)`,
файл вне репозитория). Откат — **пересоздание базы** из этого дампа, а не `pg_restore --clean`: `--clean` удаляет только
то, что есть в дампе, а таблицы, типы и индексы, которые добавило обновление, остаются и мешают старому коду и его
миграциям.

```bash
sudo systemctl stop judge-worker judge-reaper judge-api-internal              # и публичное приложение (Ctrl-C)
git switch <прежняя ветка>                                                    # код до обновления, например master
sudo -u postgres dropdb judge                                                  # отказ, если к judge кто-то подключён
sudo -u postgres pg_restore --create -d postgres < ~/judge-before-<что>.dump    # CREATE DATABASE judge из дампа, через stdin
sudo $P ./deploy/install-wsl.sh                                                # прежний код; база уже на его миграции
sudo systemctl start judge-api-internal judge-reaper judge-worker
j .venv/bin/python -m app.judgectl status                                      # миграция = head прежнего кода
```

`pg_restore --create` подключается к служебной базе `postgres`, создаёт `judge` с владельцем, правами и настройками
из дампа (роль `judge` уже есть) и восстанавливает в неё данные. Файл дампа открывает ваша оболочка, `postgres`
читает его из stdin — ему не нужен доступ к вашему домашнему каталогу.

**`dropdb` отвечает «database "judge" is being accessed by other users».** Это не повод добавлять `--force`: где-то
ещё работает процесс с подключением (публичное приложение в другом терминале, `judge-dev`, ручной `psql`, воркер,
который не остановился). Найти его:

```bash
sudo -u postgres psql -X -c "SELECT pid, usename, application_name, client_addr, backend_start, state, left(query, 60) AS query
                             FROM pg_stat_activity WHERE datname = 'judge' ORDER BY backend_start"
```

Остановите найденные процессы штатно (сервис — `systemctl stop`, приложение — Ctrl-C в его терминале) и повторите
`dropdb`. Только если процесс уже недоступен, его подключение закрывается так:
`sudo -u postgres psql -X -c "SELECT pg_terminate_backend(<pid>)"`.

### Сброс тестовых данных перед учениками (D-073)

`deploy/dev/reset-production.sh` удаляет всё, что накопилось на установке за время проверок, и оставляет её как после
свежей установки. Отменить нельзя: если что-то может понадобиться, сначала дамп (шаг 0 из D-064).

```bash
(umask 077; sudo -u postgres pg_dump -Fc judge > ~/judge-before-reset.dump)   # по желанию: дамп вне репозитория
sudo ./deploy/dev/check-reset-db.sh          # по желанию: сброс на временных базах, сравнение со свежей (ниже)
sudo ./deploy/dev/reset-production.sh --yes-wipe-production
```

| Удаляется | Остаётся |
|---|---|
| база `judge` целиком: пользователи (и админы с их 2FA), сессии, группы, задачи, контесты, посылки, лента, работы, комментарии, токены воркеров, `audit_log` | `/etc/judge/*.env`: `JUDGE_SECRET_KEY`, `DATABASE_URL` с паролем роли `judge` (токен воркера заменяется новым) |
| `/var/lib/judge/blobs` — исходники и ответы учеников, файлы задач; после удаления скрипт проверяет, что там пусто | роль `judge`, `/opt/judge`, юниты systemd, настройки PostgreSQL, isolate |
| `/var/cache/judge/worker` — кэш задач воркеров | |

Это и задумано обязательством ROADMAP «перед учениками»: в базе только тестовые данные (в том числе задачи `check-*` из
smoke), а ключ `JUDGE_SECRET_KEY` остаётся на месте — его нужно лишь сохранить вне машины. Админов после сброса нет:
первый создаётся заново (§2 «Первый администратор», с 2FA), затем запускаются службы (§4).

Порядок: проверки (ничего не меняется) → остановка `judge-worker`, `judge-reaper`, `judge-api-internal` → проверка, что
к базе никто не подключён (публичное приложение или `psql`, запущенные вручную, — остановить и повторить; `FORCE` не
используется, D-064) → пересоздание базы → `alembic upgrade head` под `judge` → удаление файлов → новый токен воркера
(та же `deploy/lib/worker-token.sh`, что у `install-wsl.sh`, D-089).
База пересоздаётся **той же функцией**, которой её создаёт `deploy/setup-postgres.sh` (`judge_db_create` из
`deploy/lib/pg-database.sh`): владелец базы и схемы `public` — `judge`, у `PUBLIC` нет `CONNECT` и `TEMP`. Отката
миграций (`downgrade base`) больше нет: откаты 4а.3, 4а.4, 4в.1 и 4в.2 отказывают при их данных (группы, сроки задач,
проверки версий, лента, комментарии), и прежний скрипт останавливался на группах. При ошибке скрипт пишет, на каком шаге
остановился, что уже изменено и что делать дальше; службы остаются остановленными.

**Проверка без риска для `judge`** — `deploy/dev/check-reset-db.sh` (root, из корня репозитория, с `.venv`): создаёт
роль `judge_reset_check` и её базу теми же функциями, накатывает миграции, кладёт данные, при которых `downgrade base`
отказывает (`tools/reset_check.py seed`), пересоздаёт базу как сброс, создаёт рядом свежую `judge_reset_check_fresh` и
сравнивает их по каталогу: владелец и права базы и схемы `public`, владельцы таблиц, строки, модели = миграции. Вывод
`OK: the reset database is the same as a fresh installation`; роль и обе базы удаляются в конце.

## 7. Внешний доступ (Caddy)

Caddy (собственный домен `.ru`, модуль 6) проксирует только публичный API (порт 8000, модуль 4). **Порт 8001 в Caddyfile не добавлять.**
Публичное приложение запускается как `python -m app.public_main` (127.0.0.1:`JUDGE_PUBLIC_PORT`, по умолчанию 8000; юнит
systemd — модуль 6). Оно не стартует, пока `JUDGE_SECRET_KEY` пуст, равен заглушке из `backend.env.example` или не является
ключом Fernet, пока не задан `JUDGE_PUBLIC_ORIGIN` (ровно то, что браузер шлёт в `Origin`: `https://<домен>`), и если
`JUDGE_PUBLIC_PORT` совпадает с `JUDGE_INTERNAL_PORT`. `X-Forwarded-For` оно учитывает только от 127.0.0.1/::1 (последний
элемент — его добавляет Caddy); собственная обработка прокси-заголовков uvicorn выключена.
Проверка входа curl-ом против запущенного приложения: `tools/smoke-public-auth.sh http://127.0.0.1:8000 <ORIGIN>`
(спрашивает админа, код TOTP, ученика и QR-токен; секреты не печатает; код выхода = число провалов).
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
(с паролем) — §6 «Сброс тестовых данных».

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
export DATABASE_URL=$JUDGE_TEST_DATABASE_URL JUDGE_BLOB_DIR=$HOME/.cache/judge-blobs PYTHONPATH=.:backend
alembic upgrade head && alembic check                            # схема public базы judge_test
uvicorn app.internal_main:app --host 127.0.0.1 --port 8002 --timeout-keep-alive 75 &   # 8001 занят сервисом
JUDGE_DEV=1 python -m app.dev_seed --count 3                     # посылки в очередь без публичного API
JUDGE_DEV=1 python -m app.dev_seed --package problems-examples/sum-of-two --source sol.cpp --language cpp --user 2
python -m app.judgectl queue judgements --last 20
python -m app.judgectl worker token create smoke --out ~/.config/judge/smoke-worker-token   # новый файл 0600
tools/smoke-internal-api.sh ~/.config/judge/smoke-worker-token http://127.0.0.1:8002   # протокол воркера curl-ом, код выхода = число провалов
```
Токен воркера для проверок — в `~/.config/judge/smoke-worker-token` (каталог 700, файл 600), не в `/tmp`.
`worker token create` пишет токен **только в новый файл** `--out` (0600, существующий не перезаписывается) и не
печатает его нигде — ни в stdout, ни в stderr, ни в `--json` (D-089; раньше токен был последней строкой вывода, и в
сессии 4в.4b фильтр вывода показал его). Перед новым smoke старый файл удалить (`shred -u`) или взять другое имя.
Если токен всё же оказался на экране — сразу `judgectl worker token revoke <имя>` и новый.

**Уборка после smoke** (приватный стек разработчика: публичное приложение, внутреннее API, воркер, админ smoke).
Остановить процессы стека по их PID (не `pkill -f` по шаблону: он находит и собственную оболочку команды). Отзыв токена
воркера и отключение админа smoke — **только с явно загруженным тестовым окружением**, никогда из окружения по
умолчанию (там может оказаться `DATABASE_URL` рабочей базы); первой строкой печатается URL базы без пароля — его
и записать в отчёт:

```bash
unset DATABASE_URL JUDGE_TEST_DATABASE_URL
set -a; . ~/.config/judge/test.env; set +a
export DATABASE_URL=$JUDGE_TEST_DATABASE_URL PYTHONPATH=.:backend
python -c "import os; from sqlalchemy.engine import make_url; print('database:', make_url(os.environ['DATABASE_URL']).render_as_string(hide_password=True))"
python -m app.judgectl worker token revoke <имя-токена>
python -m app.judgectl admin disable <логин-smoke>
shred -u ~/.config/judge/smoke-worker-token <файлы пароля и секрета TOTP админа smoke>
```
Воркер на dev-бэкенде (`JUDGE_BACKEND=local`, без песочницы) для такой проверки запускается вручную, с
`JUDGE_ALLOW_UNSAFE_LOCAL=1 JUDGE_DEV=1`; под systemd он не стартует никогда, даже с этими флагами в
`/etc/judge/worker.env` (D-065).

**Хранилище блобов разработчика** — `~/.cache/judge-blobs` (каталог создаётся сам при первой записи), не `/tmp`:
`/tmp` очищается при перезапуске WSL, а `judge_test` продолжает ссылаться на блобы, и после перезапуска версии задач и
ответы ручных заданий оставались без файлов. Блобы из прежнего `/tmp/judge-blobs`, если он ещё есть, можно перенести
(`cp -an /tmp/judge-blobs/. ~/.cache/judge-blobs/`: имена — хеши, совпадающие файлы одинаковы); если его уже нет —
заново положить файлы пакетов-примеров (`dev_seed --package …`, адресация по содержимому) или перепроверить версии.
Рабочая установка этим не затрагивается: её хранилище — `/var/lib/judge/blobs` (`/etc/judge/backend.env`).

`dev_seed` и smoke-скрипт меняют данные: только для `judge_test`. Ручные `runner.cli` (box 100–101) можно запускать
при работающих сервисах; два ручных запуска одновременно конфликтуют между собой.

**Схемы прерванных прогонов `tests/pg`.** Каждый класс тестов работает в своей одноразовой схеме и удаляет её в конце;
прерванный прогон (Ctrl-C, зависание, убитый процесс) удалить её не успевает. Имена схем несут время создания
(`t_<unixtime>_<hex>`, у миграционных тестов `m_<unixtime>_<hex>`), и первый класс каждого прогона удаляет такие схемы
старше 6 часов (`tests/pg/pgbase.py`, сообщение `pgbase: dropped N stale test schemas` в stderr); идущий параллельно
прогон и схемы с другими именами не затрагиваются. Схемы старого формата без времени (`t_<10 hex>`, `m_<10 hex>`,
до 4б.0) так не удаляются — один раз вручную, когда pg-тесты не идут:

```bash
set -a; . ~/.config/judge/test.env; set +a
.venv/bin/python -c "import os, sqlalchemy as sa
e = sa.create_engine(os.environ['JUDGE_TEST_DATABASE_URL'])
with e.begin() as c:
    names = c.execute(sa.text(\"SELECT schema_name FROM information_schema.schemata WHERE schema_name ~ '^[tm]_[0-9a-f]{10}\$'\")).scalars().all()
    [c.execute(sa.text(f'DROP SCHEMA \"{n}\" CASCADE')) for n in names]
    print('dropped', len(names))"
```
