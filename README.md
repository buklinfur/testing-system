# Judge: тестирующая система для школьных контестов

Статус: модули 0-3 (среда, формат задачи и модель данных, раннер, очередь и воркеры). Среда: WSL2 + Ubuntu + `isolate`.

```
docs/
  PROBLEM_FORMAT.md           формат пакета задачи (для людей и LLM)
  RUNNER.md                   устройство раннера, запуск, что проверено и что нет
  DATA_MODEL.md               модель данных и инварианты
  QUEUE.md                    очередь на PostgreSQL: выдача, справедливость, зависшие задания, перепроверка
  WORKER.md                   воркеры: параллельность, ядра, дисковый кэш, остановка
  WORKER_PROTOCOL.md          внутренний API воркера (контракт)
  DEPLOY_WSL2.md              systemd-юниты, первый запуск, наблюдаемость
  schemas/problem.schema.json JSON Schema для problem.yaml
backend/app/models.py         модели SQLAlchemy 2.0 (PostgreSQL)
runner/                       раннер: песочница, компиляция, чекеры, интерактив, вердикты (docs/RUNNER.md)
worker/                       воркер: supervisor, процессы, дисковый кэш, клиент API (docs/WORKER.md)
protocol/                     общий код бэкенда и воркера (манифест задачи)
backend/app/                  models, queue, worker_api, internal_main, maintenance (reaper), admin_cli
deploy/                       systemd-юниты, примеры env, install-wsl.sh, setup-postgres.sh
backend/migrations/           миграции Alembic
tests/                        автотесты (python -m unittest discover -s tests -t .); tests/pg нужна PostgreSQL (docs/DEPLOY_WSL2.md, раздел 6)
problems-examples/            пять примеров: batch, custom checker, interactive, choice, text_answer
tools/validate_problem.py     проверка пакета задачи
tools/smoke-internal-api.sh   проверка внутреннего API curl-ом (dev-база)
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
