# Judge: тестирующая система для школьных контестов

Статус: модули 0-3 готовы (среда, формат задачи и модель данных, раннер, очередь и воркеры) и проверены на реальной установке: systemd, PostgreSQL 16, `isolate` от пользователя `judge`, нагрузка 60 посылок на 5 воркерах. Модуль 4а в работе: готовы сессии 4а.1 (аккаунты, вход с 2FA для админов, сессии, консоль `judgectl`) и 4а.2 (импорт задач с безопасной распаковкой и версиями по содержимому, контесты из `contest.yaml` с личным таймером, правила доступа, консольные команды для задач, контестов и посылок). Среда: WSL2 + Ubuntu 24.04 + `isolate`.

```
docs/
  PROBLEM_FORMAT.md           формат пакета задачи (для людей и LLM)
  RUNNER.md                   устройство раннера, запуск, что проверено и что нет
  DATA_MODEL.md               модель данных и инварианты
  QUEUE.md                    очередь на PostgreSQL: выдача, справедливость, зависшие задания, перепроверка
  WORKER.md                   воркеры: параллельность, ядра, дисковый кэш, остановка
  WORKER_PROTOCOL.md          внутренний API воркера (контракт)
  DEPLOY_WSL2.md              systemd-юниты, первый запуск, наблюдаемость
  AUTH.md                     аккаунты, логины, пароли, сессии, 2FA, QR-вход, перебор, API /api/auth
  ADMIN_API.md                админский HTTP API /api/admin: доступ, формат ответов, загрузки, эндпоинты
  JUDGECTL.md                 консоль владельца сервера: админы, ученики, импорт CSV, очередь, воркеры
  MODULE_4A.md                спецификация модуля 4а и история решений
  PROBLEM_IMPORT.md           импорт задач: безопасная распаковка, проверка, версии по содержимому
  CONTESTS.md                 контесты: contest.yaml, apply и pin, вступление, личный таймер, правила доступа
backend/app/models.py         модели SQLAlchemy 2.0 (PostgreSQL)
runner/                       раннер: песочница, компиляция, чекеры, интерактив, вердикты (docs/RUNNER.md)
worker/                       воркер: supervisor, процессы, дисковый кэш, клиент API (docs/WORKER.md)
protocol/                     общий код бэкенда, воркера и инструментов: манифест, проверка пакета задачи,
                              языки, schemas/problem.schema.json (JSON Schema для problem.yaml)
backend/app/                  models, queue, worker_api, internal_main, public_main + auth_api (вход), auth/ (аккаунты,
                              сессии, 2FA), problems/ (импорт задач), contests/ (контесты и правила доступа), judgectl/ (консоль; admin_cli — старый алиас),
                              maintenance (reaper)
deploy/                       systemd-юниты, примеры env, install-wsl.sh, setup-postgres.sh
backend/migrations/           миграции Alembic
tests/                        автотесты (python -m unittest discover -s tests -t .); tests/pg нужна PostgreSQL (docs/DEPLOY_WSL2.md, раздел 9)
problems-examples/            пять примеров: batch, custom checker, interactive, choice, text_answer
contests-examples/           примеры contest.yaml: архив-тренировка, олимпиада с личным таймером, урок с заморозкой
tools/validate_problem.py     проверка пакета задачи (те же проверки, что у импорта)
tools/pack-problem.sh         zip папки задачи в stdout (тот же фильтр мусора, что у импорта)
tools/make-bad-archives.py    вредоносные архивы для ручной проверки импорта
tools/smoke-internal-api.sh   проверка внутреннего API curl-ом (dev-база)
tools/smoke-public-auth.sh    проверка входа (пароль, TOTP, QR, CSRF) curl-ом против публичного приложения
```

## Проверка примеров

```bash
pip install pyyaml jsonschema
for p in problems-examples/*/; do python tools/validate_problem.py "$p"; done
```

## Миграции

Alembic, каталог `backend/migrations`. URL берётся из `DATABASE_URL` (как у бэкенда), а не из `alembic.ini`.

```bash
DATABASE_URL=postgresql+psycopg://... alembic upgrade head
# после изменения models.py:
DATABASE_URL=... alembic revision --autogenerate -m "что изменилось"
```

Сгенерированный файл надо проверить глазами. Autogenerate не создаёт общие enum-типы один раз и не удаляет их в
`downgrade` (см. начальную миграцию), а условия `WHERE` частичных индексов не сравнивает.
`tests/pg/test_migrations_pg.py` проверяет, что миграции строят ту же схему, что `models.py`, включая частичные индексы, и что `downgrade` чистый.
Схему руками не менять.
