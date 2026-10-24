# Развёртывание в WSL2 (systemd)

Сервисы: `judge-api-internal` (API для воркеров, 127.0.0.1:8001), `judge-reaper`, `judge-worker`; публичный API (модуль 4) и `cloudflared` добавятся рядом.

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
4. Автозапуск самого WSL при входе в Windows: планировщик заданий Windows, действие `wsl.exe -d Ubuntu-22.04 -u root -- /bin/true` при входе (WSL стартует вместе с первым вызовом, systemd поднимет юниты).

## 1. Установка

```bash
sudo ./deploy/install-wsl.sh     # пользователь judge, каталоги, venv в /opt/judge, юниты, шаблоны /etc/judge/*.env
sudo nano /etc/judge/backend.env # DATABASE_URL
```

## 2. Первый запуск

```bash
# БД и таблицы (миграции генерируются по README), затем:
sudo -u judge bash -c 'set -a; . /etc/judge/backend.env; set +a; cd /opt/judge; \
    PYTHONPATH=/opt/judge:/opt/judge/backend .venv/bin/python -m app.admin_cli create-worker-token main'   # токен покажется один раз
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

`git pull`, снова `sudo ./deploy/install-wsl.sh`, затем `sudo systemctl restart judge-api-internal judge-reaper judge-worker`.
Перезапуск воркеров корректный: текущие проверки дорабатываются (до 2 минут), потом процессы выходят.

## 5. Cloudflare Tunnel

Туннель должен указывать только на публичный API (порт 8000, модуль 4). **Порт 8001 (внутренний API) в туннель не добавлять.**
