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
deploy/                       systemd-юниты, примеры env, install-wsl.sh
tests/                        автотесты (python -m unittest discover -s tests -t .); tests/pg нужны вашей PostgreSQL
problems-examples/            пять примеров: batch, custom checker, interactive, choice, text_answer
tools/validate_problem.py     проверка пакета задачи
```

## Проверка примеров

```bash
pip install pyyaml jsonschema
for p in problems-examples/*/; do python tools/validate_problem.py "$p"; done
```

## Миграции

Файл миграции намеренно не приложен: его надо сгенерировать на вашей БД.

```bash
pip install "sqlalchemy>=2" alembic psycopg[binary]
alembic init backend/migrations
# в backend/migrations/env.py: target_metadata = Base.metadata  (from app.models import Base)
alembic revision --autogenerate -m "initial schema"
alembic upgrade head
```

Проверьте сгенерированную миграцию глазами: особенно создание типов enum и частичных индексов.
