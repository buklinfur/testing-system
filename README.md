# Judge: тестирующая система для школьных контестов

Статус: модули 0-2 (среда, формат задачи и модель данных, раннер). Среда: WSL2 + Ubuntu + `isolate`.

```
docs/
  PROBLEM_FORMAT.md           формат пакета задачи (для людей и LLM)
  RUNNER.md                   устройство раннера, запуск, что проверено и что нет
  DATA_MODEL.md               модель данных и инварианты
  schemas/problem.schema.json JSON Schema для problem.yaml
backend/app/models.py         модели SQLAlchemy 2.0 (PostgreSQL)
runner/                       раннер: песочница, компиляция, чекеры, интерактив, вердикты (docs/RUNNER.md)
tests/                        автотесты (python -m unittest discover -s tests -t .)
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
