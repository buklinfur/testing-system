# API ученика (модуль 4б.1)

Статус: правила (этап 4, D-091) и сервисы (этап 5, D-093) готовы; HTTP — этап 6 (этот документ дополнится маршрутами,
кодами ответов и опросом). План — сессия 4б.1, ROADMAP «4б.1».

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
`StudentError(code, retry_after_s)`; коды → HTTP — этап 6.

| Функция | Что | Отказы |
|---|---|---|
| `contests.my_contests` | контесты, которые ученик видит (`mode: full`) или где участвовал (`own_results`); строка: `slug`, `title`, `mode`, `phase` (`before` / `running` / `over`), `state`, времена, `timing_mode`, `personal_duration`, `access_mode`, `has_access`, `joined`, `joined_via`, `started_at`, `window`, `standings_open` | — |
| `contests.contest_view` | строка + `server_time`; при `full` — `description_md`, группы контеста (для фильтра таблицы), `problem_count`, `problems`: до начала времени ученика — **пустой список** (D-093), потом по `problem_visible`: метка, `titles`, `max_score` в баллах контеста, `available_from`, свой срок с продлениями, политика после срока, `statement_open`, свои `submissions` и `best` | `contest_not_found` |
| `contests.enter` | попытка входа (с кодом или без): `EnterOutcome(ok, code, participation)`; записывается **всегда** (`contest_entry_attempts`), вызывающий делает commit и при отказе | `rate_limited` (`JUDGE_ENTER_RATE` за 10 минут, без записи); в `code` — отказы `access.can_enter`, `contest_not_found` |
| `contests.start` | личный старт | отказы `access.can_start` |
| `problems.problem_view` | условие версии, закреплённой сейчас: `titles`, `statements`, `limits`, `note_md`, `max_score`, сроки, примеры (тесты открытых групп с `show_in_statement`, до 64 КБ каждый), языки, лимиты файла и архива, свои `counted_submissions` / `limit` / `cooldown_left_s`; задача без кода — вопросы **без ответов** | `contest_not_found`, `no_access`, `problem_not_found` (скрыта, до `available_from`, нет метки), `not_participant`, `press_start`, `not_started_yet`, `statement_hidden` |
| `problems.statement_file` | картинка `statement/images/…` из `version.files` после открытого условия | те же, `file_not_found` (тест, чекер, `..`, чего нет) |
| `submissions.precheck` | дешёвые отказы до чтения тела (вызывающий затем делает rollback) | доступ, вид тела, язык, `can_submit` |
| `submissions.submit` | одна посылка: исходник, архив или ответы; порядок — приём (§1), `SUBMIT_RATE`, частота (§2), `submission_decision` (участие `FOR UPDATE`), блоб, строка, очередь; отказ ничего не оставляет | коды §1, `rate_limited`, отказы `can_submit` (`cooldown` с `retry_after_s`, `submission_limit`, `deadline_passed`, `contest_over`, …), `invalid_request` |
| `submissions.my_submissions` | свои посылки, новые первыми, курсор; `contest` (видимый, в том числе только для своих результатов), `label` | `contest_not_found`, `label_needs_contest` |
| `submissions.my_submission` | вид §3 текущей проверки, `rechecking`, без истории | `submission_not_found` (чужая — так же) |
| `submissions.my_source` | свой исходник | `submission_not_found`, `file_not_found` (ответы, не код) |
| `standings.standings` | таблица §4, `group` — неархивная группа контеста | `standings_closed`, `group_not_found` |

`counted` посылки не хранится: считается при чтении по окну участника (как лимит посылок). `dev_seed --contest`
отправляет посылки через `submissions.submit` (много посылок в минуту — `JUDGE_SUBMIT_RATE_PER_MIN=0`).
