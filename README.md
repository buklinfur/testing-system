# Judge: тестирующая система для школьных контестов

Статус: модули 0-3 готовы и проверены на реальной установке (systemd, PostgreSQL 16, `isolate` от пользователя `judge`, нагрузка 60 посылок на 5 воркерах). Модуль 4а готов и проверен на установке: аккаунты с 2FA для админов, импорт задач с безопасной распаковкой и версиями по содержимому, контесты, группы и сроки, консоль `judgectl`, админский HTTP API, проверка задач эталонами через воркер. Модуль 4в готов и слит в основную ветку: лента класса с ручными заданиями и оценкой, комментарии, вложения и сдача файлов, библиотека материалов со Справочником и ссылками `[[памятка:slug]]`. API ученика для контестов (4б.1) готов и слит: контесты и вход по коду, личный старт, условия с картинками, посылки с проверкой до сохранения и опросом по месту в очереди, свои посылки, таблица с фильтром по группе, индикатор загрузки. API ученика для класса (4б.2) готов (тесты на PostgreSQL, сценарии smoke на тестовой базе; проверка на установке — следующий шаг): лента и страница задания, «Мои работы», черновик сдачи из файлов, загружаемых по одному (фото с телефона, HEIC — только скачиванием), сдача, пересдача после возврата и до публикации оценки, общая и личная ветки комментариев, материалы и Справочник. Дальше — веб-интерфейс (5) и деплой (6). Среда: WSL2 + Ubuntu 24.04 + `isolate`.

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
  PLAN_4A_DELTA_4V.md         дополнение к 4а: группы, сроки, модуль 4в (важнее MODULE_4A.md, где они расходятся)
  PROBLEM_IMPORT.md           импорт задач: безопасная распаковка, проверка, версии по содержимому
  CONTESTS.md                 контесты: contest.yaml, apply и pin, вступление, личный таймер, правила доступа
  CLASSROOM.md                модуль 4в: лента класса, ручные задания, комментарии, вложения, библиотека материалов
  FILES.md                    отдача файлов: тип по имени и содержимому, заголовки безопасности, ETag
  ROADMAP.md                  где проект сейчас, порядок модулей, обязательства следующим модулям
  DECISIONS.md                журнал решений D-001…
  openapi-admin.json          OpenAPI админского и auth API (tools/gen-openapi.py)
  openapi-student.json        OpenAPI API ученика и auth API (tools/gen-openapi.py)
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
tools/smoke-admin-api.sh      сценарий админского API curl-ом: группы, контесты, лента, комментарии, файлы, материалы
tools/smoke-student-api.sh    сценарий API ученика curl-ом: вход, контесты, OK/WA/CE, 413/415/429, таблица с замером,
                              лента, черновик с HEIC, сдачи и пересдачи, комментарии, материалы
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
