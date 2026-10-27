# Модель данных

Определена в [`backend/app/models.py`](../backend/app/models.py) (SQLAlchemy 2.0, только PostgreSQL).

## Схема связей

```
users ──< problems ──< problem_versions ──< test_groups ──< test_cases
                              │  └──< programs (checker, interactor, validator, solutions)
                              │
contests ──< contest_problems ┘            (контест привязан к конкретной версии)
   │               │
   │               └──< submissions ──< judgements ──< test_results ──> test_cases
   └──< participations                      │
                                            └──> workers
blobs (sha256)   — файлы: тесты, исходники, архивы, пакеты
languages        — профили языков (конфиг, не код)
audit_log        — кто, что и откуда делал (веб: actor_user_id, консоль: actor_cli)
users ──< sessions          (серверные сессии входа)
users ──< login_tokens      (одноразовые QR-токены учеников)
auth_failures, auth_lockouts — неудачные попытки входа и блокировки перебора
```

## Инварианты

1. **Опубликованная `ProblemVersion` неизменяема.** Любая правка создаёт новую версию. `ContestProblem` хранит `problem_version_id`, поэтому старые результаты не меняются.
2. **Посылка и проверка разделены.** `Submission` это то, что прислал ученик. `Judgement` это одна попытка проверки. Перепроверка добавляет новый `Judgement`, а `is_current` указывает актуальный (уникальный частичный индекс: один актуальный на посылку).
3. **Очередь это `judgements` со статусом `queued`.** Для неё есть частичный индекс `ix_judgements_queue`. Воркер забирает
   задания только через внутренний API; захват (`UPDATE ... FOR UPDATE SKIP LOCKED` под advisory lock, справедливый порядок
   между учениками), возврат зависших заданий и перепроверки описаны в [`QUEUE.md`](QUEUE.md).
4. **Файлы хранятся по хешу** (`blobs`), в БД только `sha256`. Одинаковые тесты в разных версиях не дублируются.
5. **`checker_message` сохраняется всегда**, а ученику показывается по политике `feedback` и флагу `visible_to_student`. Преподаватель видит всё.

## Таблица результатов

Считается из актуальных проверок (`is_current`), отдельной таблицы пока нет.

| Режим (`contests.scoring_mode`) | Правила |
|---|---|
| `points` | По каждой задаче берётся лучший балл; итог это сумма. Ничья: раньше достигнут последний прирост баллов. |
| `icpc` | Решено = есть посылка с вердиктом `OK`. Штраф = минуты от начала (контеста или индивидуального старта) до первого `OK` плюс `icpc_penalty_minutes` за каждую неудачную посылку до него. `CE` не штрафуется. Сортировка: больше решённых, затем меньше штраф. |

Заморозка (`freeze_at`): после этого момента ученики видят таблицу такой, какой она была в момент заморозки; преподаватель видит актуальную.

## Что намеренно отложено

- Антиплагиат: есть флаг `contests.plagiarism_check`, исходники хранятся пригодными для сравнения.
- Частичные баллы от чекера (`scoring: min`): нужен расширенный протокол чекера.

## Изменения модуля 3

* `judgements`: `problem_version_id`, `priority`, `requires` (capabilities воркера), `attempt` (fencing), `failures`, `error`
  (текст внутренней ошибки, только преподавателю), `summary` (JSONB: баллы по группам / разбор теста), `tests_done/tests_total`
  (живой прогресс), `requested_by`. Индексы: очередь `(priority, created_at)` где `queued`; `ix_judgements_inflight`;
  уникальный `uq_judgements_active` (одно незавершённое задание на посылку) рядом с прежним `uq_judgements_current`.
* `test_results`: `signal`, `stderr_excerpt`, `output_excerpt`; `tl_kind` (`cpu` / `wall` у вердикта TL, миграция `4499f58e60ca`).
* `workers`: вместо `token_hash` ссылка `token_id`; добавлены `instance_id`, `state`, `info`, `stats`, `started_at`.
* Новые таблицы `worker_tokens` (один токен на машину, хранится sha256, список разрешённых capabilities, отзыв) и `queue_events` (журнал инцидентов очереди).
* `problem_versions.manifest_sha256`: хеш манифеста опубликованной версии (ключ кэша воркера).
* Формат пакета задачи не менялся. Для воркера `problem_versions.config` должен быть нормализованным `problem.yaml` без `test_groups`.

## Изменения модуля 4а.1 (миграция `c13b55a3e4d5`)

* Роль `teacher` переименована в `admin` (`ALTER TYPE role RENAME VALUE`, строки сохраняют роль). Ролей две: `admin` и `student`.
* `users.login`: `varchar(32)`, обычный `UNIQUE` и `CHECK` по формату `^[a-z0-9][a-z0-9._-]{2,31}$` вместо индекса по
  `lower(login)`; логин хранится в нижнем регистре (миграция приводит существующие; если какой-то логин и после
  `trim`+`lower` не проходит формат, миграция останавливается со списком таких логинов и ничего не меняет). Решение: без `citext`, см.
  `MODULE_4A.md`. Новые поля: `password_changed_at`, `last_login_at`, `last_login_ip`, `totp_secret_enc` (Fernet),
  `totp_enabled_at` (2FA подтверждена кодом), `totp_last_step` (последний принятый 30-секундный шаг: код не принимается дважды).
* `sessions`: `token_hash` (sha256 токена из cookie), `state` (`mfa_pending` / `active`), `csrf_token`, `last_seen_at`,
  `expires_at` (скользящий срок), `absolute_expires_at` (жёсткий предел, только админы), `ip`, `user_agent`, `revoked_at`.
* `auth_failures`: каждая неудача входа (`password` / `totp` / `qr`) с логином (для QR — пусто) и IP; индексы `(login, at)`, `(ip, at)`.
* `auth_lockouts`: блокировка логина **или** IP (`CHECK`: ровно одно из двух) до `until`; снятие заполняет `cleared_at` и
  `cleared_by`, неудачи до снятия больше не считаются. История неудач при этом не удаляется.
* `login_tokens`: QR-токены (sha256), `expires_at`, `used_at`, `revoked_at`.
* `audit_log`: колонки переименованы (`user_id` → `actor_user_id`, `created_at` → `at`, `ip_address` → `ip`),
  добавлены `actor_cli` (`cli:<unix-user>`), `object_type`, `object_id`; индексы по `at`, по актору и по объекту.
* `tests/pg/test_migrations_pg.py` теперь сравнивает с `create_all` ещё и все ограничения (`CHECK`, `UNIQUE`, `FK`),
  а шаги миграции с данными (переименование роли, логины, `audit_log`) проверяются отдельным тестом вверх и вниз.
