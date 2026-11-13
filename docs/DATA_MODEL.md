# Модель данных

Определена в [`backend/app/models.py`](../backend/app/models.py) (SQLAlchemy 2.0, только PostgreSQL).

## Схема связей

```
users ──< problems ──< problem_versions ──< test_groups ──< test_cases
                              │  ├──< programs (checker, interactor, validator, solutions)
                              │  └──< version_checks ──> workers   (проверки версии воркером, 4а.4)
                              │
contests ──< contest_problems ┘            (контест привязан к конкретной версии)
   │               │  └──< deadline_extensions ──> users | groups   (продления срока задачи или элемента ленты)
   │               └──< submissions ──< judgements ──< test_results ──> test_cases
   ├──< participations                      │
   └──< contest_groups ──> groups           └──> workers
groups ──< group_members ──> users   (классы; состав с историей)
feed_items ──> contests?                     (лента класса, 4в: контест / ручное задание / материал)
   ├──< feed_item_audience ──> groups | users  (адресаты)
   ├──< deadline_extensions                    (продления ручного задания)
   ├──< attachments ──> blobs?                 (файлы и ссылки преподавателя, 4в.3)
   └──< assignment_works ──> users             (работа ученика по заданию)
          ├──< assignment_submissions ──< assignment_submission_files ──> blobs   (текст ответа и файлы)
          │                         └──< assignment_submission_links            (ссылки сдачи, 4в.3)
          └──< assignment_reviews              (оценки, возвраты, публикации — история)
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

## Изменения модуля 4а.2 (миграции `6bc3b8bd1c68` → `9e2f4a7c1b30` → `4d8a1c2e9f57`)

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
* `problems.owner_id` и `contests.created_by` могут быть пустыми: задача или контест из консоли не принадлежат
  пользователю веба (миграции `9e2f4a7c1b30` и `4d8a1c2e9f57`; откат отдаёт такие строки первому админу, а без админа
  отказывает). Как контесты используют эти поля — [`CONTESTS.md`](CONTESTS.md).
* Откат (`downgrade`) возвращает то, что умеет старая схема (`personal` → `timed` + минуты с округлением вверх,
  `ready` → `published`, `failed` → `archived`, …); состояние, настройки посылок, скрытость и баллы задач теряются.
  Если в контесте одна метка у скрытой и видимой строки, откат отказывает со списком.

## Изменения 4а.3a (миграция `6fca2cdc55b2`)

Решения — D-020…D-024 в [`DECISIONS.md`](DECISIONS.md), ТЗ — [`PLAN_4A_DELTA_4V.md`](PLAN_4A_DELTA_4V.md) §3.2;
перенос данных и отказы проверяет `tests/pg/test_migrations_pg.py`.

* `contests.access_mode` (`open` / `code` / `groups` / `code_or_groups`) вместо `join_policy`; `CHECK`: в режимах
  с кодом (`code`, `code_or_groups`) код задан. Перенос: `open` → `open`, `code` → `code`.
* `participations.joined_via` (`code` / `group` / `open`) — как ученик впервые вошёл. Сам по себе доступ даёт только
  `code`; остальное решают текущий режим и текущий состав групп. Перенос: `code` в контестах с кодом, иначе `open`.
* `groups` — классы: `name` (уникально без учёта регистра среди неархивных: индекс по `lower(name) WHERE archived_at
  IS NULL`; не пустое), `archived_at` (группы не удаляются), `created_at`, `created_by` (пусто — из консоли).
* `group_members` — состав: `added_at/by`, `removed_at/by`. Удаление из группы ставит `removed_at`, строка остаётся
  (кто был в классе); повторное добавление — новая строка. Текущая строка на пару группа–ученик одна (частичный
  уникальный индекс `WHERE removed_at IS NULL`).
* `contest_groups` — группы, которым адресован контест: PK `(contest_id, group_id)`, `added_at/by`.
* `contest_problems`: `available_from`, `due_at` (`CHECK available_from < due_at`), `after_due_statement`
  (`visible` / `hidden`, по умолчанию `visible`), `after_due_submit` (`no` / `late` / `yes`, по умолчанию `late`),
  `note_md` (пояснение преподавателя, его видят ученики с доступом к задаче).
* `deadline_extensions` — продление срока задачи контеста ученику **или** группе (`CHECK`: ровно одно), `due_at`,
  `reason`, `created_at/by`; на задачу — одно продление на ученика и одно на группу (частичные уникальные индексы).
* `users.timezone` — пояс IANA для интерфейса (пусто — пояс браузера); хранение времени не меняется (`timestamptz`,
  тест проверяет по метаданным, что все столбцы времени с поясом).
* `submissions.late` — отметка «с опозданием», ставится при приёме и потом не пересчитывается.
* Откат отказывает, ничего не меняя и со списком, пока в базе есть то, чего старая схема не выразит: контест
  в режиме с группами, группы, продления, сроки / политики / пояснения задач, посылки с опозданием, пояса
  пользователей, участия, которые сейчас доступа не дают (вошёл при `open`, потом контест стал `code`: при
  `join_policy` такого ученика снова пустили бы).

## Изменения 4а.4 (миграция `b7e3d91a4c62`)

Решения — D-048…D-055 в [`DECISIONS.md`](DECISIONS.md), §5.9 в [`MODULE_4A.md`](MODULE_4A.md); миграцию проверяет
`tests/pg/test_migrations_pg.py`.

* `version_checks` — проверки версий задач воркером (компиляция чекера и интерактора, прогон эталонов). **Не
  judgements**: вне справедливой очереди и счётчика загрузки учеников. Столбцы: `problem_version_id`, `reason`
  (`import` / `recheck`), `status` (`queued` → `running` → `done`), `outcome` (`ready` / `failed`, задан ровно тогда,
  когда `done` — `CHECK`), `worker_id`, `attempt` (fencing, как у judgements), `failures` (потерянный воркер,
  внутренняя ошибка; на `JUDGE_MAX_ATTEMPTS` — `failed`), `heartbeat_at`, `progress_done/total` (эталонов прогнано),
  `report` (`check_report`), `error`, `requested_by` (кто попросил перепроверку; пусто — консоль), `created_at`,
  `started_at`, `finished_at`. Индексы: одна незавершённая проверка на версию (`uq_version_checks_active`, частичный
  `WHERE status IN ('queued', 'running')`), очередь `(created_at, id) WHERE status = 'queued'`, история версии
  `(problem_version_id, id)`.
* Итог законченной проверки становится `problem_versions.status`, её отчёт копируется в
  `problem_versions.check_report` (его показывают API и judgectl); строки `version_checks` — история.
* `version_checks.requested_by` — след пользователя: удалить его нельзя (как с `judgements.requested_by`).
* `worker_tokens.capabilities`: миграция добавляет `problem_check` всем неотозванным токенам с `cpu`; новые токены
  по умолчанию `["cpu", "problem_check"]`. Статусы существующих версий не меняются (остаются `ready`).
* Откат отказывает, пока в `version_checks` есть строки; иначе снимает `problem_check` с токенов и воркеров.

## Изменения 4в.1 (миграция `b32303b5df22`)

Решение — D-059 в [`DECISIONS.md`](DECISIONS.md), правила и статусы — [`CLASSROOM.md`](CLASSROOM.md). Миграцию
проверяет `tests/pg/test_migrations_pg.py`, ограничения — `tests/pg/test_feed_schema_pg.py` (каждая `CHECK` и каждый
частичный уникальный индекс: отказ и успешный вариант).

* `feed_items` — элемент ленты класса.
  * `kind`: `contest` (ссылка `contest_id`), `assignment` (ручное задание), `material` (до 4в.4 запрещён `CHECK`);
    `state`: `draft` / `published` / `archived`; `title`, `body_md`.
  * `available_from` — отложенная публикация: до этого момента элемента у ученика нет. `due_at`, `after_due_statement`,
    `after_due_submit` — те же типы и смысл, что у `contest_problems` (правила — `deadline_rules.py`).
  * Только у задания: `grading` (`points` / `pass_fail` / `none`), `max_points` (ровно при `points`, > 0),
    `answer_kind` (`mark` / `text`; `files` и `text_or_files` — с 4в.3, до тех пор запрещены `CHECK`),
    `grades_release` (`immediate` / `manual`).
  * `created_by`, `created_at`, `updated_at`, `published_at` (первая публикация).
  * `CHECK`:
    - `contest_link` — `contest_id` ровно у `contest`;
    - `assignment_settings` — настройки оценки и ответа ровно у `assignment`;
    - `due_only_assignment` — срок только у задания;
    - `max_points`, `max_points_positive`, `answer_kind_before_4v3`, `material_after_4v4`, `title_not_blank`;
    - `deadline` — `available_from < due_at`, только когда заданы оба;
    - `published_at` — не черновик ⇒ `published_at` задан.
* `feed_item_audience` — адресаты: одна строка — одна группа **или** один ученик (`CHECK one_subject`); частичная
  уникальность `(item_id, group_id)` и `(item_id, user_id)`. Группа даёт элемент тем, кто в ней сейчас.
* `assignment_works` — работа ученика по заданию, одна на пару (`uq_assignment_works_item_user`). Создаётся первой
  сдачей или первой оценкой. Её строку блокирует (`FOR UPDATE`) каждое изменение: сдача, оценка, возврат, публикация.
  `status` (`submitted` / `graded` / `returned`) и `status_at` — кэш истории, пишется в той же транзакции; «назначено»
  не хранится (строки работы нет).
* `assignment_submissions` — сдачи, только добавление: `created_at` (время приёма), `late` (фиксируется при приёме),
  `ip`. Засчитывается последняя. В `judgements` не попадают никогда.
* `assignment_submission_files` — файлы сдачи по хешу (`blob_sha256`, `size`, `position`, `path`; уникален в сдаче).
  До 4в.3 — только `answer.md` с текстом ответа.
* `assignment_reviews` — история проверки, только добавление:
  - `action`: `graded` (`score` или `passed` или ничего — шкала `none`), `returned` (на доработку),
    `released` (публикация оценки `release_of`);
  - `comment` — до 2000 символов, `reviewer_id` (пусто — консоль), `at`;
  - `CHECK`: `release_of` ровно у `released`, отметка только у `graded`, не больше одной отметки, `score ≥ 0`;
  - одна публикация на оценку — частичный уникальный индекс по `release_of`.
* `deadline_extensions` — вторая цель: `contest_problem_id` стал nullable, добавлен `feed_item_id`, `CHECK one_target`
  (ровно одно из двух). Существующие продления задач контеста проходят его без изменений. Частичная уникальность
  `(feed_item_id, user_id)` и `(feed_item_id, group_id)`.
* **`ondelete` у внешних ключей новых таблиц:**
  - `CASCADE` — только `feed_item_audience.item_id` и `assignment_submission_files.submission_id`: адресаты и файлы
    внутри своего родителя;
  - `RESTRICT` — всё остальное:
    - `feed_items.contest_id`, `.created_by`;
    - `feed_item_audience.group_id`, `.user_id`, `.added_by`;
    - `assignment_works.item_id`, `.user_id`;
    - `assignment_submissions.work_id`;
    - `assignment_submission_files.blob_sha256`;
    - `assignment_reviews.work_id`, `.reviewer_id`, `.release_of`;
    - `deadline_extensions.feed_item_id`.

    Контесты, группы и пользователи архивируются, а не удаляются; элемент с работами или продлениями удалить нельзя.
* Откат отказывает, ничего не меняя, пока есть хоть один элемент ленты или продление элемента; иначе удаляет таблицы
  и их типы, `deadline_extensions.contest_problem_id` снова `NOT NULL`.


## Изменения 4в.2 (миграция `e73284028c20`)

Решение — D-072 в [`DECISIONS.md`](DECISIONS.md), правила — [`CLASSROOM.md`](CLASSROOM.md) §9 (с этапа 3 сессии
4в.2). Миграцию проверяет `tests/pg/test_migrations_pg.py`, ограничения и следы — `tests/pg/test_comment_schema_pg.py`.

* `feed_items.comments_enabled` — `BOOLEAN NOT NULL DEFAULT false`: пишут ли ученики в общую ветку элемента.
  Постоянное значение по умолчанию PostgreSQL хранит в каталоге, поэтому существующие элементы получают `false` без
  перезаписи таблицы и без `UPDATE` строк.
* `comment_threads` — ветка комментариев элемента ленты. `student_id` пуст — общая ветка (одна на элемент,
  `uq_comment_threads_shared`), задан — личная ветка ученика и преподавателей (одна на пару «элемент — ученик»,
  `uq_comment_threads_private`; только у задания — это проверяет сервис). Ветка создаётся первым комментарием и не
  удаляется, поэтому ветки без комментариев не бывает. Ключ — пара «элемент — ученик», а не работа: вопрос задают до
  первой сдачи, а работа появляется только с ней.
* `comments` — комментарии, только добавление, без правок:
  - `author_id` (пусто — консоль), `body` — исходный Markdown как данные, `created_at` (время приёма);
  - мягкое удаление: `deleted_at`, `deleted_by` (пусто при `deleted_at` — консоль), `delete_reason` (до 200);
  - `CHECK`:
    - `body_length` — не больше 4000 символов (`char_length`: кодовые точки, как `len` в Python);
    - `body_not_blank` — не пусто и не только пробелы, табуляции и переводы строк ASCII. Правила сервиса строже
      (любые пробельные символы Unicode, NUL, одиночные суррогаты): всё, что они пропускают, база принимает;
    - `deleted_fields` — `deleted_by` и `delete_reason` только у удалённого.
  - индексы: `(thread_id, id)`; `(author_id, created_at)` для авторов-пользователей (частота, следы).
* `thread_reads` — отметки прочтения: PK `(user_id, thread_id)`, `last_read_comment_id` (ссылка на комментарий),
  `updated_at`. Своя у каждого пользователя, у каждого админа тоже; отметка только растёт (держит сервис).
* **`ondelete` у внешних ключей новых таблиц** — все `RESTRICT`, каскадов нет:
  - `comment_threads.item_id` → `feed_items`, `.student_id` → `users`;
  - `comments.thread_id` → `comment_threads`, `.author_id`, `.deleted_by` → `users`;
  - `thread_reads.user_id` → `users`, `.thread_id` → `comment_threads`, `.last_read_comment_id` → `comments`.

  Комментарии и ветки не удаляются; пользователи и элементы архивируются (элемент удаляется только черновиком, а
  в черновике комментариев нет). Сброс установки (`deploy/dev/reset-production.sh`) пересоздаёт базу целиком (D-073)
  и от этих правил не зависит.
* **Следы пользователя** (`users.traces`): «comments written» (`author_id`), «comments deleted» (`deleted_by`),
  «private comment threads» (`student_id`). Отметки прочтения — не след: удаление пользователя стирает их
  (`thread_reads_deleted` в аудите `user.delete`), как сессии.
* Откат отказывает, ничего не меняя, пока есть хоть одна ветка или комментарий; иначе удаляет три таблицы и столбец
  `comments_enabled` — теряются только значения этого флага у элементов.


## Изменения 4в.3 (миграция `32cd8723a2bc`)

Решения — D-078 (правила) и D-079 (схема) в [`DECISIONS.md`](DECISIONS.md), правила — [`CLASSROOM.md`](CLASSROOM.md)
§10. Миграцию проверяет `tests/pg/test_migrations_pg.py`, ограничения и следы — `tests/pg/test_attachment_schema_pg.py`
(каждая `CHECK` и каждый частичный уникальный индекс: отказ по имени и успешный вариант).

* `feed_items`: снят `CHECK answer_kind_before_4v3`. Метки `files` и `text_or_files` были в типе `answer_kind` с 4в.1,
  тип не меняется.
* `assignment_submission_files.kind` — новый тип `submission_file_kind`: `text` (текст ответа, `answer.md`) или `file`
  (файл, загруженный учеником). Столбец добавлен `NOT NULL` с постоянным значением по умолчанию `'text'`: PostgreSQL
  хранит его в каталоге, поэтому строки 4в.1 (все — `answer.md`) читаются как `text` без перезаписи таблицы и без
  `UPDATE` строк (на judge_test — 24 строки, файл таблицы тот же). Сразу после этого значение по умолчанию снято:
  каждая новая строка называет свой вид. Если до миграции в таблице есть файл не `answer.md`, миграция отказывает,
  ничего не меняя (4в.1 таких не пишет).
  * Вместо `UNIQUE(submission_id, path)` — частичные уникальные индексы:
    - `uq_assignment_submission_files_text` — не больше одного `text` на сдачу;
    - `uq_assignment_submission_files_name` — `(submission_id, lower(path))` среди `file`: `A.py` и `a.py` в одной
      сдаче база отвергает. `lower()` сворачивает и кириллицу (`Отчёт.pdf` / `отчёт.PDF`): базы установки созданы с
      `C.UTF-8`. Правила сравнивают через `casefold` раньше базы (D-078), индекс — последняя защита;
    - файл ученика с именем `answer.md` (`file`) не сталкивается с текстовым ответом (`text`).
  * Индекс `(submission_id, position, id)` — файлы сдачи по порядку (раньше эту роль играла уникальность).
  * `CHECK`: `text_is_answer_md` — `text` только с путём `answer.md`; `file_not_empty` — у `file` размер > 0
    (`file_empty`).
* `assignment_submission_links` — ссылки сдачи: `submission_id`, `url`, `title` (необязателен), `position`. Хранится
  как прислано после `strip`; сервер ссылки не открывает. `CHECK`: `url_valid` — не длиннее 2000 символов и начинается
  с `http://` или `https://` (любой регистр); `title_length` — до 200; `position_non_negative`. Индекс
  `(submission_id, position, id)`.
* `attachments` — файлы и ссылки преподавателя у элемента ленты (задание или контест-элемент), в порядке `position`.
  - Новый тип `attachment_kind`: `file` | `link`. У файла — `name`, `blob_sha256`, `size`; у ссылки — `url` и
    необязательный `title`. `created_by` (пусто — консоль), `created_at`.
  - `CHECK`:
    - `kind_shape` — у файла есть блоб, имя и размер и нет `url` и `title`; у ссылки наоборот;
    - `name_valid` — имя не пустое (и не только пробелы, табуляции, переводы строк ASCII), до 150 символов;
    - `size_positive` — размер > 0;
    - `url_valid` и `title_length` — как у ссылок сдачи;
    - `position_non_negative`.
  - Индексы: `(item_id, position, id)`; `uq_attachments_item_name` — `(item_id, lower(name))` среди файлов. Одинаковые
    ссылки база не запрещает.
  - Удаление вложения — удаление строки; блоб остаётся (`RESTRICT`), в аудите — имя, sha и размер (этап 3).
  - В 4в.4 у материала появится второй владелец: `item_id` станет nullable с `CHECK num_nonnulls(...) = 1`, как у веток
    комментариев.
* **`ondelete` у новых внешних ключей** (проверяет каталог, `test_the_new_foreign_keys_say_their_on_delete`):
  - `assignment_submission_links.submission_id` → `assignment_submissions` — `CASCADE`, как у файлов сдачи (сдачи
    сервисы не удаляют);
  - `attachments.item_id` → `feed_items` — `CASCADE`, как у адресатов: элемент удаляется только черновиком, и его
    вложения уходят с ним;
  - `attachments.blob_sha256` → `blobs` — `RESTRICT`;
  - `attachments.created_by` → `users` — `RESTRICT`.
* **Следы пользователя** (`users.traces`): «feed attachments added» (`attachments.created_by`). Тест проверяет, что
  каждый внешний ключ на `users` либо след (`users.TRACES`), либо стирается вместе с пользователем (`users.ERASED`:
  сессии, QR-токены, членство в группах, отметки прочтения).
* Ключ блокировки `locks.STUDENT_FILES` (пара с id ученика): сдачи с файлами одного ученика идут по одной, и квота
  (различные блобы всех его сдач) видит каждую прежнюю (этап 3).
* Откат отказывает, ничего не меняя, пока есть хоть один загруженный файл сдачи (`file`), ссылка сдачи, вложение или
  элемент с `answer_kind` `files` / `text_or_files`; сообщение называет число каждого. Иначе он **ничего не теряет**:
  все оставшиеся строки файлов — текст ответа, а их вид следующий `upgrade` восстанавливает. Откат возвращает
  `UNIQUE(submission_id, path)` и `CHECK answer_kind_before_4v3`, удаляет обе таблицы и оба типа.

## Изменения 4в.4 (миграция `b8e0cbcc4cda`)

Решения — D-083 (правила) и D-084 (схема) в [`DECISIONS.md`](DECISIONS.md), правила — [`CLASSROOM.md`](CLASSROOM.md)
§11. Миграцию проверяет `tests/pg/test_migrations_pg.py`, ограничения, внешние ключи и следы —
`tests/pg/test_material_schema_pg.py` (каждая `CHECK` и каждый частичный уникальный индекс: отказ по имени и успешный
вариант).

* `materials` — материал библиотеки: `slug` (уникален; `CHECK slug_format` — формат slug задачи
  `^[a-z0-9][a-z0-9-]{1,62}$`), `title` (до 200, `title_not_blank`), `library` — новый тип `material_library`
  (`none` по умолчанию / `all_students` / `groups`), `archived_at`, `created_by` (пусто — консоль), `created_at`,
  `updated_at`. Удаления нет, только архив. **slug не меняется** после создания — это правило сервиса
  (`slug_immutable`, этап 3), не `CHECK`: ограничение не видит прежнего значения, а триггеров в схеме нет.
* `material_versions` — неизменяемые версии: `material_id`, `version` (с 1, `version_positive`; уникален с
  материалом — `uq_material_versions_material_version`), `body_md` (Markdown, `body_length` — до 100 000 символов),
  `created_by`, `created_at`. Все видят последнюю.
* `material_groups` — группы Справочника (`library = groups`): PK (`material_id`, `group_id`), `added_by`, `added_at`.
  Индекс по группе (для `groups.why_kept_after_rollback`, `groups.materials_of`).
* `library_sections` — разделы Справочника: `parent_id` (пусто — верхний уровень), `title` (`title_not_blank`),
  `position` (`position_non_negative`); `not_own_parent` — не родитель самому себе. Глубину два уровня держит сервис
  (`materials/rules.check_section_place`): `CHECK` не видит строку родителя. Индекс `(parent_id, position, id)`.
* `material_sections` — материал в разделе: PK (`section_id`, `material_id`), `position` (`position_non_negative`).
  Материал может быть в нескольких разделах. Индекс по материалу.
* `feed_items.material_id` — материал элемента `material`. `CHECK material_after_4v4` (4в.1) снят, вместо него
  `material_link`: у элемента `material` материал есть, у остальных видов его нет. Срок и шкалу у материала и так
  запрещают `due_only_assignment` и `assignment_settings`. Индекс `ix_feed_items_material` — частичный, по непустым.
* `attachments` — второй владелец, `material_version_id`. `item_id` стал nullable, `CHECK one_owner` —
  `num_nonnulls(item_id, material_version_id) = 1`. Имена файлов уникальны в версии без учёта регистра —
  `uq_attachments_material_version_name` (`(material_version_id, lower(name))` среди файлов версии); у элемента —
  прежний `uq_attachments_item_name`. Индекс `(material_version_id, position, id)` — частичный. Версия не меняется:
  новая версия копирует строки вложений, блобы общие.
* `material_refs` — ссылки `[[памятка:slug]]` текста, производные: пишутся при сохранении текста (этап 3) и этой
  миграцией для текстов до неё. Ровно один источник — `CHECK one_source` (`num_nonnulls(problem_version_id,
  feed_item_id, material_version_id) = 1`): версия задачи (все её условия), элемент ленты (`body_md`) или версия
  материала. Хранится **slug**, а не id: ссылка на ещё не созданный материал оживает, когда его создадут
  (`slug_format` — тот же формат). Одна строка на источник и slug — частичные уникальные индексы
  `uq_material_refs_problem_version`, `uq_material_refs_feed_item`, `uq_material_refs_material_version`; индекс по
  `slug` — для доступа и «где используется».
* **Заполнение `material_refs`** — миграция разбирает условия каждой версии задачи и текст каждого элемента. Грамматика
  скопирована в миграцию, а не импортирована (миграция должна делать то же, что делала); `tests/test_material_rules_units.py`
  сверяет копию с `materials/links.py` по шаблонам и по поведению. Тест на данных — префикс в разном регистре, текст
  вместо названия, один slug дважды и в двух языках условия (одна строка), неверные записи (не ссылки). На judge_test
  миграция дала **0 строк**: в 17 версиях задач и 14 элементах ленты нет ни одной ссылки.
* **`ondelete` новых внешних ключей** (проверяет каталог, `test_the_new_foreign_keys_say_their_on_delete`):
  - `materials.created_by`, `material_versions.created_by`, `material_groups.added_by` → `users` — `RESTRICT`;
  - `material_versions.material_id` → `materials` — `RESTRICT` (ни версии, ни материалы не удаляются);
  - `material_groups.material_id` → `materials` — `CASCADE`; `material_groups.group_id` → `groups` — `RESTRICT`;
  - `library_sections.parent_id` → `library_sections` — `RESTRICT`: раздел с подразделами не удаляется;
  - `material_sections.section_id` → `library_sections` — `CASCADE` (удалённый раздел отпускает материалы);
    `material_sections.material_id` → `materials` — `RESTRICT`;
  - `material_refs.*` → источник — `CASCADE`: производные строки уходят с источником (удаляется только черновик
    элемента; версии не удаляются);
  - `attachments.material_version_id` → `material_versions` — `RESTRICT`;
  - `feed_items.material_id` → `materials` — `RESTRICT`.
* **Следы пользователя** (`users.traces`): «materials created», «material versions written», «library groups added»;
  метка вложений стала «attachments added» (элементы и версии материалов). Тест «каждый внешний ключ на `users` — след
  или стирается» (`tests/pg/test_attachment_schema_pg.py`) видит новые столбцы.
* **Группы:** `groups.why_kept_after_rollback` оставляет группу, которой Справочник показывает материалы; аудит
  `group.archive` называет их (`materials`, slug), `groups.materials_of(group)`.
* `tools/reset_check.py` сеет и данные 4в.4 (материал с двумя версиями, вложение версии, группа, разделы, элемент
  `material`, ссылки из задачи, элемента и материала): `downgrade base` первым отказывает откат 4в.4.
* Откат отказывает, ничего не меняя, пока есть материал, раздел Справочника, элемент `material`, вложение версии
  материала или ссылка (`material_refs`, в том числе заполненная миграцией); сообщение называет число каждого. Иначе
  он ничего не теряет и возвращает `CHECK material_after_4v4` и `attachments.item_id NOT NULL`, удаляет таблицы и тип.

## Изменения 4б.1 (миграция `f2e12ff59f1c`)

Решение — D-090 в [`DECISIONS.md`](DECISIONS.md). Миграцию и перенос данных туда и обратно проверяет
`tests/pg/test_migrations_pg.py`.

* `judgements.max_score Numeric(8,2)` — сколько могла дать проверка, **в баллах версии задачи**, на которой она шла:
  сумма `test_groups.points`, у `choice` / `text_answer` — баллы вопросов из `config` (1, если не указаны, как считает
  `runner/quiz.py`). `NULL` — только у незавершённой проверки; `CHECK max_score_non_negative`. Пишет `queue.finish`
  (присланный воркером, при `0 ≤ score ≤ max_score`) или, у внутренней ошибки, сервер по версии
  (`scores.version_max_score`). Миграция заполняет его у **всех** завершённых проверок (`finished` и `internal_error`)
  по версии проверки — правило переписано в ней на SQL, тест сверяет копию с `scores.version_max_score` на версиях
  всех видов.
* **Баллы контеста** — одна функция `scores.contest_score(score, max_score, points)`: без `contest_problems.points` —
  балл как есть, иначе `score × points / max_score`, округление до сотых вверх от половины; `max_score` пуст или 0 —
  0 баллов, деления на ноль нет. Максимум задачи в контесте — `scores.contest_max` (`points` или `max_score`). Через
  них показывают баллы все виды: `submissions/view.py` (список и посылка в `/api/admin`), `judgectl submission
  list/show`, `judgectl queue judgements`; таблица результатов и вид посылки ученика в 4б.1 — тоже. Пример: группы на
  60, `points: 100` — полное решение 100, половина групп 50.
* `judgements.server_gave_up_at timestamptz` — проверку завершил сервер (сдался после `max_attempts`, D-069). Заменяет
  текстовую отметку — префикс `the server gave up after ` в `error`: миграция ставит у таких строк (`internal_error`)
  `server_gave_up_at = finished_at` и убирает префикс; текст воркера, сохранённый как `worker: the server gave up…`,
  остаётся как был. `CHECK gave_up_is_internal_error`. Пишет только `queue._fail_attempt`.
* `ix_participations_user` (`participations.user_id`) — «мои контесты» ученика.
* Откат ничего не теряет из того, что держит старая схема: префикс возвращается в `error` строк с
  `server_gave_up_at`; сообщение воркера, записанное после обновления и начинающееся как старая отметка, получает
  `worker: ` (как делал старый `finish`), чтобы старый код не принял его за отметку сервера; `max_score` удаляется.
