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

1. **`ProblemVersion` неизменяема с момента импорта.** Любая правка пакета создаёт новую версию (одинаковое содержимое — та же версия, `content_sha256`); статус (`pending_check` / `ready` / `failed`) говорит только, можно ли её закреплять в контестах. `ContestProblem` хранит `problem_version_id`, поэтому старые результаты не меняются.
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

Заморозка (`freeze_at`): после этого момента ученики видят таблицу такой, какой она была в момент заморозки; преподаватель видит актуальную. С личным таймером (`timing_mode = personal`) заморозки нет.

## Что намеренно отложено

- Антиплагиат: есть флаг `contests.plagiarism_enabled`, исходники хранятся пригодными для сравнения.
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
* Как этим пользуются (сроки сессий, лимиты перебора, 2FA, QR, список действий аудита): [`AUTH.md`](AUTH.md).
  Reaper раз в час удаляет из `auth_failures`, `auth_lockouts`, `sessions`, `login_tokens` строки старше
  `JUDGE_AUTH_RETENTION_DAYS` (30 дней); `audit_log` не чистится никогда.

## Изменения модуля 4а.2 (миграция `6bc3b8bd1c68`)

Решения — [`MODULE_4A.md`](MODULE_4A.md) §5.6; перенос существующих данных проверяет `tests/pg/test_migrations_pg.py`
(вверх и вниз по каждому случаю).

* `problems.archived` — архивация задачи (вместо статуса версии).
* `problem_versions`: статус `pending_check` / `ready` / `failed` вместо `draft` / `published` / `archived`
  (перенос: `draft` → `pending_check`, остальные → `ready`); `published_at` удалён, `created_at` — время импорта;
  `content_sha256` + `unique(problem_id, content_sha256)` — идентичность содержимого (у версий, созданных до 4а.2,
  пусто); `files` (`{путь: sha256}` всех файлов пакета), `imported_by`, `check_report` (проверка воркером, 4а.4).
  `manifest_sha256` записывается для любой версии: версии неизменяемы.
* `contests`: вместо `mode`, `duration_minutes`, `leaderboard_enabled`, `join_code`, `plagiarism_check` —
  `state` (`draft` / `published` / `archived`), `timing_mode` (`fixed` / `personal`) + `personal_duration` (`interval`),
  `leaderboard` (`hidden` / `visible` / `after_end`), `join_policy` (`open` / `code`) + `invite_code` (уникальный),
  `allow_upsolving`, `languages` (null — все), `submit_cooldown_sec`, `max_submissions_per_problem`, `tests_after_end`,
  `plagiarism_enabled`. `starts_at` обязателен; `ends_at` пуст у бессрочного контеста (архив-тренировка).
  `CHECK`: личный таймер ⇔ задана длительность, длительность > 0, при личном таймере нет заморозки, конец позже начала,
  при входе по коду код задан, неотрицательные кулдаун и штраф, лимит посылок > 0.
  Перенос: `duration_minutes` → `personal`, иначе `fixed`; `leaderboard_enabled` → `visible` / `hidden`; `join_code` →
  `code` + `invite_code`, пустой — `open`; `state = published`; пустой `starts_at` ← `created_at`. Противоречивые
  контесты (заморозка при длительности, длительность ≤ 0, конец не позже начала, один код у двух контестов)
  останавливают миграцию со списком, ничего не меняя.
* `contest_problems`: `problem_id` (задача определяется по ней, метка — изменяемый атрибут), `points` (переопределение
  итога задачи, > 0), `hidden` (задача убрана из контеста, но посылки по ней есть). Уникальность метки и задачи —
  частичные индексы `WHERE NOT hidden`. Перенос: если в одном контесте несколько версий одной задачи (так делал
  `dev_seed`), видимой остаётся последняя строка, остальные скрываются.
* `participations`: `joined_at` (вступление; перенос — из `started_at`), `started_at` — только личный старт
  (`NULL` до нажатия «Начать»; в `fixed`-контестах при переносе очищен).
* `submissions`: индекс `(contest_problem_id, user_id, created_at)` вместо `(contest_problem_id)`.
* `languages`: миграция добавляет недостающие строки из `protocol/languages.py` (единый источник; существующие не
  меняются).
* `problems.owner_id` может быть пустым: задача, импортированная из консоли, не принадлежит пользователю веба
  (миграция `9e2f4a7c1b30`; откат отдаёт такие задачи первому админу, а без админа отказывает).
* Откат (`downgrade`) возвращает то, что умеет старая схема (`personal` → `timed` + минуты с округлением вверх,
  `ready` → `published`, `failed` → `archived`, …); состояние, настройки посылок, скрытость и баллы задач теряются.
  Если в контесте одна метка у скрытой и видимой строки, откат отказывает со списком.
