# API ученика (модули 4б.1 и 4б.2)

Статус: 4б.1 — правила (этап 4, D-091), сервисы (этап 5, D-093) и HTTP (этап 6, D-095, §6) готовы. 4б.2 (класс:
лента, работы, черновики, комментарии, материалы) — сервисы чтения готовы (§5 «Класс», D-100), HTTP — этапы 5–6.
Планы — сессии 4б.1 и 4б.2, ROADMAP. Машиночитаемое описание — [`openapi-student.json`](openapi-student.json) (маршруты ученика и
`/api/auth`; пишет `tools/gen-openapi.py`, по сети приложение его не отдаёт).

Ученик — всегда `User` из сессии: идентификаторов пользователя, участия или группы API не принимает. Время `now` —
момент получения запроса (до чтения тела), UTC.

## 1. Приём посылки (`app/submissions/intake.py`)

Проверяется до того, как что-либо записано (блоб, строка), после дешёвых отказов доступа (`access.can_submit`).
Отказ — `IntakeError` с постоянным кодом; HTTP 422, у размеров 413. Раннер сохраняет свои лимиты как страховку;
сервер никогда не принимает больше, чем он (`RUNNER_SOURCE_MAX_BYTES` / `RUNNER_ARCHIVE_MAX_BYTES` = `runner/config.py`,
тест; значения переменных больше — приложение не запускается).

| Что | Правило | Код |
|---|---|---|
| язык | языки контеста ∩ языки задачи ∩ включённые (`null` списка — без сужения) | `language_not_allowed` |
| один файл | не больше `JUDGE_SOURCE_MAX_KB` (256; BOM считается) | `source_too_large` (413) |
| | **строго UTF-8**; BOM снимается; переводы строк (CRLF) не трогаются | `source_encoding` («сохраните файл в UTF-8») |
| | не пустой (только пробельные символы — пустой) | `source_empty` |
| архив | только при `submission.allow_archive` задачи (по умолчанию да) | `archive_not_allowed` |
| | не больше min(`JUDGE_ARCHIVE_MAX_KB` 1024, `submission.max_archive_kb`) | `source_too_large` (413) |
| | читаемый zip с файлами; имена — относительный путь без `..`, без `\`; без символических ссылок и шифрования; имя не повторяется (после снятия единственной общей папки, как в раннере). Распаковки нет: читается только оглавление; имена и содержимое — как есть | `archive_invalid` |
| | файлов не больше `submission.max_files` (20) | `archive_too_many_files` |
| | у интерпретируемого языка файл запуска (`entry`, иначе `submission.entry`, иначе `main.py` — `protocol/languages.ENTRY_DEFAULTS`) есть в архиве; названный `entry` — у любого языка | `entry_not_found` |
| | `entry` — относительный путь до 255 символов | `entry_invalid` |
| задача без кода | ответы — объект известных id вопросов; `choice` — список разных вариантов этого вопроса, `text_answer` — строка до 1000 символов без NUL и одиночных суррогатов | `answers_invalid` |
| | программа задаче без кода / ответы задаче с кодом | `answers_expected` / `source_expected` |

## 2. Частота (`app/rates.py`)

Скользящее окно над моментами недавних действий (сервис читает их под блокировкой ученика): отказ `rate_limited` с
`retry_after_s` — когда самое старое действие окна выйдет из него.

* посылки — `JUDGE_SUBMIT_RATE_PER_MIN` (6; 0 — без лимита) за 60 с, по всем контестам вместе; пауза контеста
  `submit_cooldown_sec` — сверх этого, по задаче (`can_submit`);
* вход в контест — `JUDGE_ENTER_RATE` (10) попыток за 10 минут, верных и неверных: код не подобрать перебором.

## 3. Вид посылки ученику (`app/submissions/student_view.py`)

Чистая функция над строками; раннер не импортируется (тексты вердиктов и подсказок — `protocol/verdicts.py`, общий
с раннером). **Что показывать, решается при чтении** (`effective_policy`, D-094):
* **открытость** групп и тестов — только по **версии, на которой шла проверка** (её группы и тесты, без сопоставления
  с группами другой версии по имени), и только пока эта версия закреплена за задачей контеста: открыта в ней и открыта
  ученику сейчас (`access.visible_tests`: открытые; все — после конца при `tests_after_end`). Проверка старой версии
  ни одной группы открытой не показывает;
* **уровень `feedback`** — строже из двух: уровень группы в версии проверки и самый строгий уровень версии,
  закреплённой сейчас (`meet`: `full` уступает любому, `first_fail` с `groups_only` дают `verdict_only`).
`test_results.visible_to_student`, записанный воркером при проверке, источником не является: политика, ужесточённая
потом (`full` → `verdict_only`), скрывает подробности и старых посылок.

* статус: `queued` | `judging` (с `progress`) | `done`; вердикт, его текст и подсказка — только у `done`;
* баллы — в баллах контеста (`scores.contest_score`, D-090), у групп тоже; `counted`, `late`, `rechecking`;
* `CE` — лог компиляции (пути сервера убраны раннером), до 4000 символов;
* `IE` — только вердикт и общая подсказка: `judgements.error` ученику не уходит никогда (D-069);
* группы — открытые (или открытые сейчас) и при `feedback` `full` / `groups_only`;
* тесты: группы, открытой сейчас, — все, с входом, ответом, выводом программы и сообщением чекера; закрытой группы —
  при `full` все, при `first_fail` первый проваленный тест посылки, иначе ни одного, и **никогда** их содержимое и
  сообщение чекера. Время и память — кроме `verdict_only`; вид TL — кроме `verdict_only`; подсказка RE — только по
  сигналу или коду выхода (stderr не показывается никогда: трассировка закрытого теста может напечатать его данные);
* задача без кода: по каждому вопросу «верно / нет», пояснение — только при `full`, при `verdict_only` — ничего.

## 4. Таблица результатов

Правила — `CONTESTS.md` §4 («Таблица результатов», `standings_open_for`).

## 5. Сервисы (`app/student/`, D-093)

Функции не делают commit (транзакция — вызывающего); ученик — `User` из сессии, иначе `student_required`. Отказ —
`StudentError(code, retry_after_s)`; коды → HTTP — §6.

| Функция | Что | Отказы |
|---|---|---|
| `contests.my_contests` | контесты, которые ученик видит (`mode: full`) или где участвовал (`own_results`); строка: `slug`, `title`, `mode`, `phase` (`before` / `running` / `over`), `state`, времена, `timing_mode`, `personal_duration`, `access_mode`, `has_access`, `joined`, `joined_via`, `started_at`, `window`, `standings_open` | — |
| `contests.contest_view` | строка + `server_time`; при `full` — `description_md`, группы контеста (для фильтра таблицы), `problem_count`, `problems`: до начала времени ученика — **пустой список** (D-093), потом по `problem_visible`: метка, `titles`, `max_score` в баллах контеста, `available_from`, свой срок с продлениями, политика после срока, `statement_open`, свои `submissions` и `best` | `contest_not_found` |
| `contests.enter` | попытка входа (с кодом или без): `EnterOutcome(ok, code, participation)`; записывается **всегда** (`contest_entry_attempts`), вызывающий делает commit и при отказе | `rate_limited` (`JUDGE_ENTER_RATE` за 10 минут, без записи); в `code` — отказы `access.can_enter`, `contest_not_found` |
| `contests.start` | личный старт | отказы `access.can_start` |
| `problems.problem_view` | условие версии, закреплённой сейчас: `titles`, `statements`, `limits`, `note_md`, `max_score`, сроки, примеры (тесты открытых групп с `show_in_statement`, до 64 КБ каждый), языки, лимиты файла и архива, свои `counted_submissions` / `limit` / `cooldown_left_s`; задача без кода — вопросы **без ответов** | `contest_not_found`, `no_access`, `problem_not_found` (скрыта, до `available_from`, нет метки), `not_participant`, `press_start`, `not_started_yet`, `statement_hidden` |
| `problems.statement_file` | картинка `statement/images/…` из `version.files` после открытого условия | те же, `file_not_found` (тест, чекер, `..`, чего нет) |
| `submissions.precheck` | дешёвые отказы до чтения тела (вызывающий затем делает rollback); возвращает лимит тела для этой задачи (`source` — `JUDGE_SOURCE_MAX_KB`, `archive` — min(`JUDGE_ARCHIVE_MAX_KB`, `max_archive_kb`), `answers` — 64 КБ) | доступ, вид тела (`archive_not_allowed` уже здесь), язык, `can_submit` |
| `submissions.submit` | одна посылка: исходник, архив или ответы; порядок — приём (§1), `SUBMIT_RATE`, частота (§2), `submission_decision` (участие `FOR UPDATE`), блоб, строка, очередь; отказ ничего не оставляет | коды §1, `rate_limited`, отказы `can_submit` (`cooldown` с `retry_after_s`, `submission_limit`, `deadline_passed`, `contest_over`, …), `invalid_request` |
| `submissions.my_submissions` | свои посылки, новые первыми, курсор; `contest` (видимый, в том числе только для своих результатов), `label` | `contest_not_found`, `label_needs_contest` |
| `submissions.my_submission` | вид §3 текущей проверки, `rechecking`, без истории | `submission_not_found` (чужая — так же) |
| `submissions.my_source` | свой исходник | `submission_not_found`, `file_not_found` (ответы, не код) |
| `standings.standings` | таблица §4, `group` — неархивная группа контеста | `standings_closed`, `group_not_found` |

`counted` посылки не хранится: считается при чтении по окну участника (как лимит посылок). `dev_seed --contest`
отправляет посылки через `submissions.submit` (много посылок в минуту — `JUDGE_SUBMIT_RATE_PER_MIN=0`).

`problems.problem_view` с 4б.2 отдаёт ещё `links` — ссылки `[[памятка:…]]` всех условий версии, по одной на slug, как
`materials.service.resolve` видит их этому ученику (`{slug, available: false}` без основания — как нет материала).

### Класс (4б.2, D-100)

Модули [`student/feed.py`](../backend/app/student/feed.py) (чтение) и
[`student/classroom.py`](../backend/app/student/classroom.py) (тонкие обёртки сервисов класса). Правила — все из
`CLASSROOM.md` (`classroom/access.py`, `deadline_rules`, `grading.student_view`, `comment_rules`, `materials/rules.py`);
здесь только сбор и вид. Отказ — `FeedError` с кодом сервиса класса (у `rate_limited` — `retry_after_s`), HTTP —
этапы 5–6. Ученику **никогда** не уходят sha256 файлов, автор вложения, неопубликованная оценка, чужое.

**Элемент существует** для ученика только по `items.student_sees` (`feed.seen_item`): в его ленте или на нём его
работа; иначе `item_not_found` — одинаково для «нет», черновика, чужого класса и до `available_from`. Отсюда начинается
каждая функция с элементом.

| Функция | Что | Отказы |
|---|---|---|
| `feed.my_feed(cursor, limit)` | лента (`items.student_feed`: свои группы и личная адресация, без дублей), новые первыми по `at` = `available_from` / `published_at` / `created_at`, затем id; курсор — id последнего элемента страницы (позиция в порядке: ушедший между страницами элемент ничего не сдвигает), `limit` до 100. Строка: `id`, `kind`, `title`, `at`, `archived`, `unread` (общая и своя личная ветка, `comments.unread_by_item`); у задания `assignment` — `answer_kind`, `grading`, `max_points`, `available_from`, свой `due_at` с продлениями, `after_due`, `status` (`null` — работы нет), `grade`, `late`, `missing`, `submissions`, `returned_comment`; у контест-элемента `contest` — строка `contests.my_contests` (**доступа не даёт**, D-059); у материала `material` — `{id, title}` | — |
| `feed.item_page(item, blobs, ttl_days)` | страница: строка ленты + `statement_hidden`, `body_md`, `attachments` (без sha256 и автора), `links` (`resolve` по тексту) — только там, где ученик видит условие (`attachments.can_view`: у задания скрыто после срока при `after_due_statement = hidden` и в архиве — тогда `null`, `null`, `[]`); `comments`: `enabled`, `shared_unread`, `private_unread` (`null` — ветку не читает). У задания всегда: `assignment`, `can_submit` (`{allowed, reason, late}` — `can_submit_work` с подставным ответом нужного вида: код о задании, сроке и оценке, не об ответе), `hand_ins` (свои сдачи по порядку: `at`, `late`, `text`, `files` `{id, name, size}` без `answer.md`, `links`; рецензий нет — их видимое в `status` и `grade`), `draft` (`drafts.draft_view` с `expires_at`) | `item_not_found` |
| `feed.my_works` | все свои работы (`can_see_own_work`: и в архиве, и после исключения из группы), новые сдачи первыми — порядок только по своим сдачам, рецензия (выпущенная или нет) ничего не двигает: `item` `{id, title, archived}`, срок, `status`, `grade`, `late`, `submissions`, `last_hand_in_at` | — |
| `classroom.item_file` / `work_file` | файл преподавателя (где видно условие) / файл своей работы, в том числе `answer.md` | `item_not_found`, `attachment_not_found` / `file_not_found` |
| `classroom.shared_thread` / `my_thread` | общая ветка / своя личная (`comments.*_thread`, ученик — сам) | `item_not_found`, `thread_not_found` |
| `classroom.post_shared` / `post_private` | комментарий → `comment_card` (имя автора и `teacher`, `mine`; логинов нет) | `item_not_found`, коды `comment_rules`, `rate_limited` + `retry_after_s` |
| `classroom.read_shared` / `read_mine` | отметка прочитанного (`up_to` — комментарий этой ветки, который ученик видит) → сколько осталось | `item_not_found`, `thread_not_found`, `comment_not_found` |
| `classroom.delete_comment` | своё | `comment_not_found` (не видит), `not_your_comment` |
| `classroom.material` / `material_file` | последняя версия материала с основанием, вложения без sha256 и автора / её файл | `material_not_found`, `attachment_not_found` |
| `classroom.library(q)` | Справочник (`library.library_view`), с `q` — поиск | — |

## 6. HTTP (`app/student_api/`, D-095)

Публичное приложение, те же cookie-сессии, что у `/api/auth`. Формат ошибок, время (UTC с `Z`), курсоры — как в
админском API (`ADMIN_API.md` §2): `{"detail": "<код>", "message"?, "problems"?}`; у 429 ещё `retry_after_s` и
заголовок `Retry-After`.

**Доступ.** Маршруты `/api/contests…` и `/api/submissions…` — только ученик с полной сессией (`student_session`):
без сессии — 401 `not_authenticated`; админ между паролем и кодом TOTP — 401 `mfa_required`; админ — 403
`student_required`. `/api/load` — любая полная сессия, и админ. Небезопасные методы — `Origin` ==
`JUDGE_PUBLIC_ORIGIN` (иначе 403 `origin_not_allowed`, до всего) и `X-CSRF-Token` == токен сессии (иначе 403
`csrf_token_required`). Идентификаторов пользователя API не принимает: в телах лишнее поле — 422, лишний параметр
query ничего не значит; чужое и несуществующее отвечают одинаково (404).

| Метод и путь | Что | Ответ |
|---|---|---|
| `GET /api/contests` | мои контесты (§5 `my_contests`) | `{items, server_time}` |
| `GET /api/contests/{slug}` | страница контеста (§5 `contest_view`) | строка + `server_time`, … |
| `POST /api/contests/{slug}/enter` | вход, тело `{code?}` (необязательно) | страница + `entered`: `ok` \| `rejoin_with_code` |
| `POST /api/contests/{slug}/start` | личный старт | страница |
| `GET /api/contests/{slug}/standings?group=` | таблица (§4) | `rows`, `problems`, `groups`, `server_time`, … |
| `GET /api/contests/{slug}/problems/{label}` | условие и мои данные (§5 `problem_view`) | |
| `GET /api/contests/{slug}/problems/{label}/files/{path}` | картинка условия (`statement/images/…`) | файл, `ETag`, 304 по `If-None-Match` |
| `POST /api/contests/{slug}/problems/{label}/submissions` | посылка (ниже) | 201 `{id, judgement_id, counted, late}` |
| `GET /api/submissions?contest=&label=&cursor=&limit=` | свои посылки, новые первыми (`limit` до 100) | `{items, next_cursor}` |
| `GET /api/submissions/{id}` | вид посылки (§3) + `poll_after_s`, пока что-то проверяется | |
| `GET /api/submissions/{id}/source` | свой исходник: файл — текстом, архив — zip | файл |
| `GET /api/load` | индикатор загрузки, снимок одного момента (D-067) | `level`, `queue_length`, `workers_online`, `workers_busy`, `typical_seconds`, `my_position`, `my_wait_seconds`, `server_time` |

**Посылка.** Тело — сырое, вид по `Content-Type`: `text/plain` — один файл (`?language=`; `charset` не важен, файл
обязан быть UTF-8), `application/zip` — архив (`?language=`, `?entry=`), `application/json` — `{"answers": {…}}`
задачи без кода; иначе 415 `unsupported_media_type` до чтения тела. `entry` не у архива и `language` у ответов — 422
`invalid_request`. Порядок: Origin → сессия и CSRF → `Content-Type` (415) → объявленный `Content-Length` больше
границы вида (`JUDGE_SOURCE_MAX_KB` / `JUDGE_ARCHIVE_MAX_KB` / 64 КБ) — 413 → `precheck` (контест, задача, вид тела,
язык, `can_submit`), затем rollback: **ни транзакции, ни блокировки, пока тело идёт** → тело потоком с лимитом
этой задачи, 413 сразу при превышении (с `Content-Length` и без него) → `submit` в новой транзакции (приём §1,
частота, решение снова, блоб, строка, очередь). Отказ ничего не оставляет. 413 исходника и архива —
`source_too_large`, ответов — `too_large`. Момент посылки — момент получения запроса: медленная загрузка не делает её
поздней. Маршрут освобождён от общего лимита JSON (1 МБ) точным шаблоном пути
(`student_api.UPLOAD_PATTERNS`: слаг и метка — шаблоны `contest.schema.json`); соседние пути (слэш в конце, двойной
слэш, другой регистр, другой метод) лимит сохраняют. Контест со слагом не по схеме (только руками в базе) посылки
принимает, но тело тогда сначала читает общий лимит.

**Опрос.** `poll_after_s` есть, пока у посылки есть незавершённая проверка (в том числе перепроверка); у завершённой
поля нет. Проверяется — 2 с; в очереди — `min(10, 2 + 2·⌊(место − 1) / воркеров онлайн⌋)` по месту в честном
порядке (`queue.poll_inputs`); ни одного воркера онлайн — сразу 10 с.

**Коды → HTTP** (`student_api/deps.py` `STUDENT_STATUS`; тест полноты — `tests/test_student_api_units.py`, тест «каждый
код достижим запросом» — `tests/pg/test_student_api_pg.py`):

| HTTP | Смысл | Коды |
|---|---|---|
| 404 | ученику не видно (одинаково «чужое» и «нет такого») | `contest_not_found`, `problem_not_found`, `submission_not_found`, `file_not_found`, `group_not_found` |
| 403 | видно, но закрыто | `no_access`, `standings_closed`, `wrong_code`, `student_required` |
| 409 | состояние или время | `not_participant`, `press_start`, `not_started_yet`, `statement_hidden`, `contest_over`, `deadline_passed`, `submission_limit`, `already_joined`, `already_started`, `not_personal`, `not_published`; недостижимы запросом ученика: `admins_do_not_join`, `problem_hidden`, `not_available_yet` (скрытая задача и задача до `available_from` — уже 404) |
| 422 | ввод | коды приёма §1 (кроме размера), `label_needs_contest`, `invalid_request` |
| 413 | размер | `source_too_large` (и `too_large` у тела ответов) |
| 415 | тип тела | `unsupported_media_type` |
| 429 + `Retry-After` | подождать | `rate_limited` (посылки, попытки входа), `cooldown` (пауза контеста) |

Вход по коду: каждая попытка записывается (и неверная, и к несуществующему контесту), ответ — по коду отказа; сверх
`JUDGE_ENTER_RATE` за 10 минут — 429 без записи.

Тесты: обход всех маршрутов ученика из самого приложения (`tests/pg/test_student_api_pg.py`: аноним, админ, админ на
полпути, без CSRF, чужой Origin; новый префикс приложения без объявления — ошибка), подмена на каждом эндпоинте,
поиск строк-маркеров закрытых тестов, `error`, `stderr` во всём JSON, тело без транзакции, int4 — общий обход
`tests/pg/test_int_bounds_pg.py`.
