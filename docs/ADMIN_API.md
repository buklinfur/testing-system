# Админский HTTP API (`/api/admin`)

API для админского веб-интерфейса (модуль 5). Решения — [`MODULE_4A.md`](MODULE_4A.md) §5.7–§5.8; вход, сессии и
профиль — [`AUTH.md`](AUTH.md). Тот же процесс, что `/api/auth` (публичное приложение, `python -m app.public_main`), за
тем же Caddy. Код: [`backend/app/admin_api/`](../backend/app/admin_api); сервисы — общие с `judgectl`.

**Машиночитаемое описание** — [`openapi-admin.json`](openapi-admin.json) (`/api/admin` и `/api/auth`). По сети приложение
OpenAPI не отдаёт (`openapi_url=None`); файл пишет `python tools/gen-openapi.py`, а `tests/test_openapi_units.py`
падает, если файл разошёлся с приложением. Этот документ — описание для людей: смысл, правила и коды ошибок.

Разделы: пользователи (§5.1), задачи (§5.2), контесты (§5.3), группы (§5.4), продления (§5.5), посылки (§5.6),
система, аудит и языки (§5.7), лента класса и ручные задания (§5.8), комментарии (§5.9).

## 1. Доступ

* Нужна **полная сессия** (cookie `__Host-session` после пароля и кода TOTP) **активного админа**:

  | Кто | Ответ |
  |---|---|
  | нет сессии, она истекла или отозвана, пользователь отключён | 401 `not_authenticated` |
  | админ ввёл пароль, но не код TOTP (`mfa_pending`) | 401 `mfa_required` |
  | ученик | 403 `admin_required` |

* Небезопасные методы (`POST`, `PUT`, `PATCH`, `DELETE`): `Origin` равен `JUDGE_PUBLIC_ORIGIN` (иначе 403
  `origin_not_allowed`) и `X-CSRF-Token` равен `csrf_token` сессии (иначе 403 `csrf_token_required`).
* **Операций с админами и токенами воркеров здесь нет и не будет** (Р2): создавать, отключать, удалять админов,
  сбрасывать им пароль и 2FA, выдавать токены воркеров можно только из консоли ([`JUDGECTL.md`](JUDGECTL.md)).
* Каждое изменение пишется в `audit_log` с `actor_user_id` админа и IP клиента.
* `tests/pg/test_admin_routes_pg.py` берёт **все** маршруты `/api/admin` из самого приложения и проверяет каждый:
  без сессии — 401, ученику — 403, админу без TOTP — 401, небезопасный метод без CSRF или с чужим `Origin` — 403;
  маршрутов для админов, 2FA и токенов воркеров нет. Новый эндпоинт попадает в проверку сам.

## 2. Формат ответов

* **Ошибки** — один формат для всего публичного приложения:

  ```json
  {"detail": "<код>", "message": "<текст для человека>", "problems": ["...", "..."]}
  ```

  `detail` — постоянный код для программ (`snake_case`); `message` и `problems` — необязательны. Ошибки разбора тела
  — 422 `invalid_request`, по строке на поле в `problems` (`"password: Field required"`); неизвестный путь — 404
  `not_found`; не тот метод — 405 `method_not_allowed`.
* **Числа на входе ограничены** (D-088): id в пути, в query и в теле — от 1 до 2147483647 (столбцы `int4`; типы
  `app/ids.py`), `cursor`, `limit`, номера версий и счётчики — тоже; баллы — по правилам D-061 (до `999999.99`, иначе
  `score_invalid` / `max_points_invalid`). Число
  вне границ — 422 `invalid_request` с полем в `problems`, до обработчика; число внутри текста (`sum@99999999999` в
  перепроверке) — «не найдено», как любой несуществующий номер. Значение, которое всё же отвергла база
  (`DataError`: переполнение, слишком длинная строка), — тоже 422 `invalid_request` (`"value out of range or too
  long"`), никогда не 500; в журнал оно пишется ошибкой уровня `ERROR` (метод и шаблон пути, без значений): это
  забытая граница в нашем коде. Проверяют `tests/test_int_bounds_units.py` (каждое число каждого маршрута в OpenAPI
  приложения имеет границы) и `tests/pg/test_int_bounds_pg.py` (2^31, 2^63 и −1 в каждый маршрут) — новые маршруты,
  в том числе ученика 4б, попадают в проверку сами.
* **Время** — всегда UTC, ISO 8601 с `Z`.
* **Списки** — курсор по `id`, новые первыми: `?cursor=<id>&limit=<n>` (по умолчанию 50, больше 200 урезается до 200).
  Ответ `{"items": [...], "next_cursor": <id или null>}`; следующая страница — тот же запрос с `cursor=next_cursor`.
* **Кэш** — все ответы с `Cache-Control: no-store` (пароли и QR-токены показываются один раз).

## 3. Тело запроса

* JSON — не больше **1 МБ**. Больший `Content-Length` отвергается сразу (413 `too_large`, тело не читается);
  тело без длины (`chunked`) считается и обрывается, как только превысит лимит.
* Загрузки — сырое тело без `multipart`: `application/zip` (архив задачи, лимит `JUDGE_IMPORT_MAX_ARCHIVE_MB`) и
  `text/csv` (класс учеников, 1 МБ). Тело пишется потоком во временный файл со счётчиком: превышение — 413 сразу, другой
  `Content-Type` — 415 `unsupported_media_type`. От лимита в 1 МБ освобождены **только две точные пары** «метод + путь»:
  `POST /api/admin/problems` и `POST /api/admin/users/import`. Слэш в конце, двойной слэш, другой регистр, `%2F`
  (если сервер его не раскодировал) или другой метод не освобождают: тело режется по 1 МБ, потом отвечает роутер.
  Освобождение сверяется с тем же путём, по которому выбирается обработчик, поэтому освобождённый запрос всегда
  попадает к обработчику загрузки с его собственным лимитом.
* Загрузка вложения элемента (§5.10, 4в.3) — сырое тело `application/octet-stream`, лимит `JUDGE_ATTACHMENT_MAX_MB`.
  Её путь содержит id, поэтому она освобождена **шаблоном**: метод `POST` и путь, **целиком** совпадающий с
  `/api/admin/items/[1-9][0-9]{0,9}/attachments` (`re.fullmatch`, `admin_api/routes.py` `UPLOAD_PATTERNS`). Id без
  ведущего нуля и не длиннее 10 цифр (id — int4), поэтому `/items/007/attachments`, слэш в конце, двойной слэш,
  `/attachments/order`, другой метод — под лимитом 1 МБ (тест соседних путей `tests/test_http_body_units.py`).

## 4. Файлы из загруженного контента

Файлы версии задачи и исходники посылок приходят от людей, поэтому отдаются так, чтобы браузер не исполнил их как
страницу (XSS). Правила — [`FILES.md`](FILES.md) (D-070), кратко:

* тип решают **имя и первые 8 КБ содержимого вместе**: показываются (`inline`) только картинки, у которых и байты, и
  имя — PNG, JPEG или WebP (тип — по байтам); HTML в `pic.png`, SVG, GIF и всё прочее — `application/octet-stream`
  вложением (D-026);
* тексты (тесты, исходники) — `text/plain; charset=utf-8` вложением, только если это UTF-8 без NUL; cp1251 и двоичное —
  `application/octet-stream`; архив посылки — `application/zip` только с сигнатурой zip;
* на каждом ответе, 200 и 304: `X-Content-Type-Options: nosniff`, `Content-Security-Policy: sandbox; default-src 'none'`,
  `Cache-Control: private, no-cache`, `ETag: "<sha256>"`, `Content-Disposition` с очищенным именем; `If-None-Match`
  даёт 304 только после проверки доступа;
* путь файла ищется только среди файлов версии (`files`), к файловой системе по пути из URL обращения нет.

То же правило обязательно для картинок условия в API ученика (4б.1) и вложений (4в.3): тот же `file_response`.

### 4.1. Недоверенный текст

Часть полей в ответах API написали ученики или их программы, часть — люди без особых прав (названия, причины). Сервер
отдаёт их **как есть** (не экранирует и не чистит: экранировать — задача того, кто выводит), поэтому фронтенд
(модуль 5) обязан:

* **только как текст** — без `innerHTML`, `dangerouslySetInnerHTML`, `v-html`, `document.write`, без рендера Markdown и
  без «умных» ссылок:
  - исходник посылки (`source.text`) и имена файлов архива (`source.files[].name`, `entry_file`);
  - `compile_log`, `error` проверки, `checker_message`, `stderr_excerpt`, `output_excerpt` тестов;
  - ответы посылок без кода (`answers`);
  - `display_name`, логины, названия групп, `reason` продлений, имена файлов задач и тестов;
  - лента (§5.8): `title` элементов (и `item_title` в очереди), **текст ответа** ученика (`events[].text`),
    `comment` оценок и возвратов (`returned_comment`), `warning` контест-элемента;
  - сообщения ошибок (`message`, `problems`) и предупреждения `apply` (в них встречаются названия из файлов);
* **Markdown — только через санитайзер** (разрешённый список тегов и атрибутов, без `script`, `style`, `iframe`,
  обработчиков `on*`, ссылок `javascript:` / `data:`): `note_md` задачи контеста, `description` контеста, условия
  задач, `body_md` элемента ленты;
* картинки — только с разрешённых путей этого же API (§4), никогда по URL из текста.

В тестах API такие значения нарочно содержат `<script>` и теги: сервер возвращает их байт в байт. Обязательство
модуля 5 записано в [`ROADMAP.md`](ROADMAP.md).

## 5. Эндпоинты

| Метод и путь | Ответ |
|---|---|
| `GET /api/admin/me` | `{state, csrf_token, expires_at, user}` — админ этой сессии; первый запрос админского интерфейса |

### 5.1. Пользователи

Всё идёт через `app.auth.users` — те же функции, что у `judgectl user …` ([`JUDGECTL.md`](JUDGECTL.md)). **Админы
здесь только для чтения**: любое действие над админом — 403 `admin_in_console`. Пользователь в ответах:
`{id, login, display_name, role, active, created_at, last_login_at, last_login_ip, failures, locked_until,
two_factor (только у админов)}`; хешей и секретов нет.

| Метод и путь | Тело / параметры | Ответ | Ошибки |
|---|---|---|---|
| `GET /users` | `q` (часть логина или имени, без учёта регистра; `%` и `_` — обычные буквы), `role=student\|admin`, `active`, `locked`, курсор | список пользователей | 422 |
| `GET /users/{id}` | — | пользователь | 404 `user_not_found` |
| `GET /users/{id}/failures` | `limit` (до 200) | `{login, counting, limit, locked_until, items: [{at, kind, ip}]}` | 404 |
| `POST /users` | `{surname, first_name, login?, display_name?, timezone?}` | 201 `{user, password}` — **пароль только здесь** | 409 `login_taken`, 422 `login_invalid` / `display_name_required` / `timezone_invalid` / `invalid_request` |
| `POST /users/import` | тело — список класса (`text/csv`, ≤ 1 МБ, формат и кодировки — [`JUDGECTL.md`](JUDGECTL.md)), `?dry_run=true` | 201 `{import_id, users: [{surname, first_name, login, password, display_name, group}]}` (столбец «класс» — группы, `JUDGECTL.md`); при `Accept: text/csv` — тот же CSV, что пишет judgectl (BOM, `;`, `attachment; filename="students-import-<id>.csv"`); `dry_run` — 200 `{dry_run, users}` без паролей, ничего не пишется | 413, 415, 422 `csv_invalid` + `problems` с номерами строк (в том числе занятые в базе логины), ничего не создано |
| `GET /users/imports` | курсор | `{items: [{id, at, file, count, by_user_id, by_console, rolled_back}]}` | — |
| `POST /users/imports/{id}/rollback` | `{confirm_import_id}` = id | `{import_id, deleted: [logins], groups_deleted: [названия], groups_kept: ["название: почему"]}` (К3) | 404 `import_not_found`, 409 `already_rolled_back` / `users_logged_in` (+ `logins`) / `has_traces` (+ `traces`), 422 `confirmation_mismatch` |
| `POST /users/{id}/reset-password` | — | `{user, password}` — новый пароль один раз; сессии и QR-токены отозваны, блокировка снята | 403, 404 |
| `POST /users/{id}/disable` / `enable` | — | пользователь (отключение отзывает сессии и QR-токены) | 403, 404 |
| `PUT /users/{id}/login` | `{login}` | пользователь | 403, 404, 409 `login_taken`, 422 `login_invalid` |
| `PUT /users/{id}/timezone` | `{timezone: "<IANA>" \| null}` (ключ обязателен) | пользователь (`timezone` в нём) | 403 `admin_in_console` (свой пояс админ меняет через `PUT /api/auth/profile`), 404, 422 `timezone_invalid` |
| `POST /users/{id}/unlock` | — | `{login, was_locked}` | 403, 404 |
| `POST /logins/{login}/unlock` | — | `{login, was_locked}` — любой логин, и несуществующий | 403 для логина админа |
| `POST /ips/unlock` | `{ip}` | `{ip, was_locked}` | 422 `invalid_ip` |
| `POST /users/{id}/delete` | `{confirm_login}` = логин | 204 | 403, 404, 409 `has_traces` + `traces` (`{"submissions": 1, …}`: тогда только отключить), 422 `confirmation_mismatch` |
| `POST /users/qr-tokens` | `{user_ids: [...]}` (1–200, повторы убираются) | `{tokens: [{user_id, login, display_name, url, expires_at}]}`; `url` = `<JUDGE_PUBLIC_ORIGIN>/login/qr#<токен>`; новый токен отменяет прежний; **одна запись аудита `user.qr_tokens` на пачку** со списком id | 404 `user_not_found` + `problems`, 403 (админ в списке), 422 `qr_not_allowed` (отключённый ученик) — ни одного токена не выдано |

Пароли и QR-токены есть только в ответе, который их создал (`no-store`); в `audit_log` и в логи они не попадают —
это проверяет тест вместе с кодами TOTP.

### 5.2. Задачи

Загрузка идёт через тот же `import_package`, что и `judgectl problem import` ([`PROBLEM_IMPORT.md`](PROBLEM_IMPORT.md)),
списки — через `problems/catalog.py`, общий с `judgectl problem list / versions / show`.

| Метод и путь | Тело / параметры | Ответ | Ошибки |
|---|---|---|---|
| `POST /problems` | тело — zip пакета (`application/zip`, лимит `JUDGE_IMPORT_MAX_ARCHIVE_MB`), `?dry_run=true` | 201 `{slug, result: "created", version, content_sha256, warnings, dry_run, status, check_id}` (`status` — `pending_check` и номер проверки воркером, если у версии есть свой чекер, интерактор или эталоны; иначе `ready` и `check_id: null`); то же содержимое — 200 `result: "exists"`; `dry_run` — 200 `result: "new"` или `"exists"`, ничего не пишется | 413 `too_large` (по `Content-Length` сразу или по счётчику), 415, 422 `package_invalid` + `problems` (все причины разом), 409 `problem_archived` |
| `GET /problems` | `q` (часть slug), `archived=false\|true\|all` (по умолчанию `false`), курсор | `{items: [{id, slug, title, type, archived, versions, latest: {version, status}}]}` | — |
| `GET /problems/{slug}` | — | то же + `versions: [{version, status, check_report, imported_at, content_sha256, files, pinned_in, from_zip}]` | 404 `problem_not_found` |
| `GET /problems/{slug}/versions/{n}` | — | `{titles, statements, config, files: [пути], groups: [{name, points, scoring, visibility, feedback, depends_on, show_in_statement, tests: [{name, visibility, has_answer}]}], programs: [{role, name, language, expect}], check_report, checks: [{id, reason, status, outcome, report_reason, attempt, failures, worker_id, progress, requested_by, error, created_at, started_at, finished_at}], …}` — скрытые тесты тоже; `checks` — проверки версии воркером, новые первыми (отчёт последней законченной — `check_report`, формат — [`WORKER_PROTOCOL.md`](WORKER_PROTOCOL.md)) | 404 `version_not_found` |
| `GET /problems/{slug}/versions/{n}/files/{путь}` | — | сам файл по правилам §4: картинки `inline` (тип по расширению), тесты и тексты `text/plain` + `attachment`, остальное (SVG, HTML, …) `application/octet-stream` + `attachment`; всегда `nosniff` и `sandbox`-CSP | 404 `file_not_found` — путь ищется только среди файлов версии (`..`, `%2e%2e`, абсолютные пути, несуществующие имена — 404) |
| `POST /problems/{slug}/archive` / `unarchive` | — | задача | 404 |
| `POST /problems/{slug}/versions/{n}/recheck` | — | 202 `{slug, version, result: "created", status, check}`; проверка уже в очереди или идёт — 200 с `result: already_queued \| already_running` и той же проверкой (ничего нового). `status` версии меняется только по итогу (D-054). Аудит `problem.recheck` | 404 `problem_not_found` / `version_not_found`, 409 `problem_archived` |

Ответ после отказа импорта ничего не оставляет: ни строк, ни файлов в хранилище блобов (проверки идут до записи).

### 5.3. Контесты

Контест — это `contest.yaml` в виде JSON: та же схема (`protocol/schemas/contest.schema.json`), тот же разбор
(`contests/spec.py`, `from_data`) и тот же `apply`, что у `judgectl contest apply` ([`CONTESTS.md`](CONTESTS.md)).
`GET /contests/{slug}` отдаёт `spec` в форме `contest.yaml` (время — UTC с `Z`, длительность — `1h30m`): отправленный
обратно через `PUT`, он ничего не меняет. Доступ — `access: {mode, groups}` (4а.3a; `join_policy` из 4а.2 — 422
`contest_invalid` с подсказкой); неизвестная или архивная группа — 409 `contest_conflict`. У задачи — `available_from`, `due_at`, `after_due:
{statement, submit}`, `note_md` (`CONTESTS.md` §1); предупреждения о сроках — в `warnings`. Группы — §5.4,
продления — §5.5. `judgectl contest show --json` выдаёт ту же структуру.

| Метод и путь | Тело / параметры | Ответ | Ошибки |
|---|---|---|---|
| `GET /contests` | `state=draft\|published\|archived`, курсор | `{items: [{id, slug, title, state, timing_mode, starts_at, ends_at, problems, participants}]}` | — |
| `GET /contests/{slug}` | — | `{spec, state, created_at, invite_code, participants, rows: [{id, label, problem, version, status, points, hidden, warning}]}`; `warning` — текст, если закреплённая версия `failed` (проверка не прошла: посылки принимаются, задачу стоит исправить), иначе `null` | 404 `contest_not_found` |
| `PUT /contests/{slug}` | JSON `contest.yaml` (`slug` = путь; время без смещения — в `JUDGE_TIMEZONE`), `?dry_run=true` | 201 (создан) или 200: `{created, changes, warnings, dry_run, contest}`; без изменений — `changes: []`; `dry_run` показывает изменения и ничего не пишет | 422 `slug_mismatch`, 422 `contest_invalid` + `problems` (схема и правила файла), 409 `contest_conflict` + `problems` (нет задачи, версия не `ready`, «версия с посылками меняется через pin»: подсказка `judgectl contest pin …`) |
| `POST /contests/{slug}/publish` / `archive` | — | `{slug, state, changed}` | 404 |
| `POST /contests/{slug}/pin` | `{label, version, rejudge}` — `rejudge` **обязателен** | `{label, from, to, rejudge}` (`rejudge` — число созданных перепроверок или `null`) | 422 без `rejudge`, 404 `contest_problem_not_found` / `version_not_found`, 409 `version_not_ready` / `problem_archived` |
| `POST /contests/{slug}/code` | — | `{slug, invite_code}` — новый код, старый перестаёт работать | 409 `contest_is_open` |
| `GET /contests/{slug}/participants` | курсор | `{items: [{user_id, login, display_name, joined_at, started_at, joined_via, access_now}]}` — `joined_via`: как ученик впервые вошёл (`code` / `group` / `open`); `access_now`: даёт ли это доступ сейчас (режим и текущий состав групп, `access.has_access`) | 404 |

Предупреждения `apply` (например, «при `open → code` снова действует старый код») приходят в `warnings` и не
останавливают изменение.

### 5.4. Группы

Тот же сервис, что `judgectl group` (`app/groups.py`, [`JUDGECTL.md`](JUDGECTL.md)). Группа здесь — по `id`, ученики —
по `user_id`. Каждое изменение — в аудите от имени админа сессии.

| Метод и путь | Тело / параметры | Ответ | Ошибки |
|---|---|---|---|
| `GET /groups` | `q` (часть названия), `archived=false\|true\|all` (по умолчанию `false`), курсор | `{items: [{id, name, archived_at, created_at, members, contests}]}` (`members`, `contests` — числа) | — |
| `POST /groups` | `{name}` | 201: группа (как `GET /groups/{id}`) | 409 `group_name_taken` (без учёта регистра среди неархивных), 422 `group_name_invalid` |
| `GET /groups/{id}` | `?history=true` | `{id, name, archived_at, created_at, members, contests: [{slug, title, state}], students: [{user_id, login, display_name, added_at, removed_at}]}`; с `history` — и убранные | 404 `group_not_found` |
| `PUT /groups/{id}` | `{name}` | группа | 404, 409 `group_name_taken`, 422 `group_name_invalid` |
| `POST /groups/{id}/archive` | — | `{group, contests: [slug], feed_items: [{id, kind, state, title}]}` — контесты, к которым группа подключена, и элементы ленты, где она в адресатах (К6): она больше не даёт к ним доступа. `feed_items` — с 4в.1b, прежние поля не изменились | 404 |
| `POST /groups/{id}/unarchive` | — | группа | 404, 409 `group_name_taken` |
| `POST /groups/{id}/members` | `{user_ids: [...]}` (1–200) | `{added: [логины], already: [логины]}` | 404 `user_not_found` + `problems` (все отсутствующие), 409 `group_archived`, 422 `not_a_student` + `logins` |
| `POST /groups/{id}/members/remove` | `{user_ids: [...]}` | `{removed: [...], not_members: [...]}` — строки состава остаются с `removed_at` | 404 |

Доступ к контестам меняется сразу (правила — [`CONTESTS.md`](CONTESTS.md) §3–§4): добавленный ученик видит контесты
группы, убранный теряет условия и сдачу, свои посылки и оценки видит всегда.

### 5.5. Продления сроков

Тот же сервис, что `judgectl extension` (`contests/deadlines.py`, [`CONTESTS.md`](CONTESTS.md) §5).

| Метод и путь | Тело / параметры | Ответ | Ошибки |
|---|---|---|---|
| `GET /contests/{slug}/extensions` | `?label=` | `{items: [{id, label, user, group, due_at, base_due_at, reason, created_at}]}` | 404 `contest_not_found` / `contest_problem_not_found` |
| `GET /contests/{slug}/problems/{label}/extensions` | — | то же для одной задачи | 404 |
| `POST /contests/{slug}/problems/{label}/extensions` | `{user_id \| group_id, due_at, reason?}` — ровно одно из двух; `due_at` как в `contest.yaml` (без смещения — `JUDGE_TIMEZONE`) | 201 `{extension, replaced: null}`; для того же ученика или группы — 200 `{extension, replaced: <прежний срок>}` (D-038) | 404 `user_not_found` / `group_not_found` / `contest_problem_not_found`, 409 `no_deadline` / `group_archived`, 422 `extension_not_later` / `extension_subject` / `not_a_student` / `due_at_invalid` |
| `POST /contests/{slug}/problems/{label}/extensions/{id}/delete` | — | 204 | 404 `extension_not_found` — в том числе для продления другой задачи или другого контеста |

### 5.6. Посылки

Тот же сервис, что `judgectl submission` (`app/submissions/view.py`, `rejudge.py`). Это вид преподавателя: все
тесты, включая скрытые, внутренние ошибки, вывод программы — ученику его не отдавать никогда (4б делает свой).
Почти всё здесь — недоверенный текст (§4.1).

| Метод и путь | Тело / параметры | Ответ | Ошибки |
|---|---|---|---|
| `GET /submissions` | `contest`, `label` (только с `contest`), `problem` (slug), `user_id`, `group_id` (**текущие** члены группы), `verdict`, `status` (вердикт и статус **текущей** проверки), `late`, курсор | `{items: [{id, user: {id, login, display_name}, contest, label, problem, version, language, archive, created_at, late, judgement: {id, status, verdict, score} \| null}]}` | 404 `contest_not_found` / `contest_problem_not_found` / `problem_not_found` / `group_not_found`, 422 `label_needs_contest`, 422 `invalid_request` (неизвестный вердикт или статус) |
| `GET /submissions/{id}` | `?judgement_id=` (тесты другой проверки этой посылки) | то же + `entry_file`, `ip`, `answers`, `source: {name, size, missing, text, truncated}` (текст — до 256 КБ, байты не UTF-8 заменены) или `{name, size, files: [{name, size}], entry_file}` для архива; `judgements` — **вся история** (`reason`, `status`, `verdict`, `score`, `version`, `current`, `attempt`, `failures`, `priority`, `worker`, `requested_by`, времена, `tests_done/total`, `compile_log`, `error`, `summary`); `tests_of` и `tests` — **все** тесты (`test`, `group`, `verdict`, `time_ms`, `memory_kb`, `exit_code`, `signal`, `tl_kind`, `visible_to_student`, `checker_message`, `stderr_excerpt`, `output_excerpt`) | 404 `submission_not_found` / `judgement_not_found` |
| `GET /submissions/{id}/source` | — | файл: один исходник — `text/plain; charset=utf-8`, архив — `application/zip`; всегда `attachment` (`submission-<id>.py` / `.zip`), `nosniff`, `sandbox`-CSP (§4) | 404 `submission_not_found` / `no_source` (посылка с ответами) / `file_missing` |
| `POST /submissions/{id}/rejudge` | — | `{submission, kind: created \| retargeted \| already_running, judgement}` | 404 |
| `POST /submissions/rejudge` | `{contest+label \| contest_problem_id \| problem_version: "<slug>@<n>" \| submission_ids, verdicts?, user_id?, group_id?}` + **`dry_run: true` или `expect: N`** | `dry_run` → `{affected, dry_run: true}`, ничего не меняется; `expect` → `{affected, contest_problems, created, retargeted, already_running, dry_run: false}` | 422 `rejudge_needs_filter` (нет ни задачи, ни версии, ни id посылок: «перепроверить всё» нельзя), 422 `dry_run_or_expect` (нужно ровно одно), 409 `count_changed` + `affected` (фильтр выбирает уже другое число: ничего не сделано), 404 как у списка, `version_not_found` |

Перепроверка — в два шага (MODULE_4A §5.7): сначала `dry_run` показывает, сколько посылок затронет фильтр, потом тот же
фильтр с `expect` равным этому числу. Новые проверки встают в очередь с приоритетом перепроверки, история не теряется
([`QUEUE.md`](QUEUE.md)); уже ждущая проверка не удваивается, а нацеливается заново (`retargeted`). Аудит:
`submission.rejudge` (одна посылка), `submissions.rejudge` (пачка: фильтр и числа).

### 5.7. Система, аудит, языки

| Метод и путь | Параметры | Ответ |
|---|---|---|
| `GET /system` | — | `{migration: {current, head, ok}, load: {level, queue_length, workers_online, workers_busy, typical_seconds, …}, metrics: {queue_length, oldest_queued_age_s, in_flight, workers_by_state, typical_seconds, median_wait_s, finished_by_verdict, requeued_total}, workers: [{id, name, state, heartbeat_age_s, judgement_id, check_id, capabilities, info, stats}], workers_known, checks: {queued, running, pending_versions, recent_failed: [проверка], pinned_failed: [{contest, label, problem, version, reason, warning}]}, config_problems: [...], timezone}` — всё на один момент базы (`read_one_moment`); тот же сервис `app/system.py`, что `judgectl status`. Хранилища блобов здесь нет: оно на диске, его показывает только консоль |
| `GET /audit` | `action` (точное имя или префикс с точкой на конце: `contest.` — все `contest.*`), `object_type`, `object_id`, `actor_user_id`, `console=true\|false` (только консоль / только веб), `since`, `until` (`since ≤ at < until`; ISO 8601, без смещения — `JUDGE_TIMEZONE`), курсор | `{items: [{id, at, action, actor_user_id, actor_cli, ip, object_type, object_id, details}]}`; 422 `time_invalid` |
| `GET /languages` | — | `{items: [{key, name, enabled}]}` |

В `details` аудита нет паролей, QR-токенов и кодов TOTP: тест создаёт их все (пароли учеников и админа, сброс,
импорт класса, QR-токены, вход с кодом) и ищет их в ответе `/audit`. `details` — недоверенный текст (§4.1): в нём
бывают названия и логины.

### 5.8. Лента класса и ручные задания

Тот же сервис, что `judgectl item` и `judgectl work` (`app/classroom/items.py`, `works.py`; [`CLASSROOM.md`](CLASSROOM.md),
D-059…D-063). Элемент — по `id`, группа и ученик — по `id`. Время на входе — как в `contest.yaml` (без смещения —
`JUDGE_TIMEZONE`, или с `Z` / `+07:00`), на выходе — UTC с `Z`. Баллы (`max_points`, `score`) — строка или число JSON,
обычная запись с точкой, не больше двух знаков после неё (D-061); в ответах — строка из базы с двумя знаками
(`"8.50"`). Работа — пара «элемент — ученик» из пути: `work_id` и `submission_id` API не принимает, работа ученика на
другом элементе через этот путь не видна (404). Эндпоинтов сдачи здесь нет: сдаёт только ученик (4б.2; до него —
`dev_seed --item`).

**Элементы**

| Метод и путь | Тело / параметры | Ответ |
|---|---|---|
| `GET /items` | `group_id` (адресован группе), `kind=assignment\|contest`, `state=draft\|published\|archived`, курсор | `{items: [{id, kind, state, title, contest_id, available_from, due_at, published_at, created_at, comments_enabled, audience: {groups: [id], users: [id]}, works: {статус: число}, warning}], next_cursor}` |
| `POST /items` | `{kind, title, body_md?, contest?: slug, material?: slug, available_from?, due_at?, after_due_statement?, after_due_submit?, grading?, max_points?, answer_kind?, grades_release?, comments_enabled?, group_ids?, user_ids?}` | 201: элемент (как `GET /items/{id}`), черновик; адресаты — в той же транзакции (отказ адресата — нет и элемента). `kind: material` (4в.4) — с `material` (slug); пустой `title` — название материала; неизвестный slug — 404 `material_not_found`, архивный — 409 `material_archived`, без `material` — 422 `material_required` |
| `GET /items/{id}` | — | то же, что в списке, + `body_md`, `after_due_statement`, `after_due_submit`, `grading`, `max_points`, `answer_kind`, `grades_release`, `updated_at`, `created_by`, `contest: {slug, title, state} \| null`, `audience: {groups: [{id, name, archived}], users: [{id, login, display_name}]}`, `extensions` (число); с 4в.4 — `material_id`, `material: {id, slug, title, archived} \| null`, `link_warnings` (предупреждения о ссылках `[[памятка:slug]]` текста, `CLASSROOM.md` §11) |
| `PUT /items/{id}` | только меняемые поля; меняются **присланные ключи**, `null` очищает (`due_at: null`); неизвестный ключ — 422 | `{item, changed: [поля]}`. Новая шкала `pass_fail` / `none` без `max_points` очищает его; с `max_points` не `null` — 422 `max_points_invalid`; `points` без `max_points` — тоже. После первой оценки шкала не меняется (`grading_locked`) |
| `POST /items/{id}/publish` · `archive` · `unarchive` | — | `{item, changed}` |
| `POST /items/{id}/delete` | `{confirm_id}` = id | 204; только черновик без работ и продлений |
| `PUT /items/{id}/audience` | `{group_ids, user_ids}` | `{item, added, removed}` — адресаты становятся ровно такими |
| `GET /groups/{id}/feed` | `drafts=true\|false` (по умолчанию `true`) | `{items}` в порядке ленты (`available_from`, иначе `published_at`; новые сверху), без курсора |
| `GET /items/{id}/extensions` | — | `{items: [{id, item, user, group, due_at, base_due_at, reason, created_at}]}` |
| `POST /items/{id}/extensions` | `{user_id \| group_id, due_at, reason?}` | 201 `{extension, replaced: null}`; для того же ученика или группы — 200 `{…, replaced: <прежний срок>}` |
| `POST /items/{id}/extensions/{eid}/delete` | — | 204; продление другого элемента или задачи контеста — 404 |

У контест-элемента `warning` — текст, если адресаты контеста не увидят (группы не подключены, вход по коду, личные
адресаты в режиме `groups`). Элемент доступа к контесту не выдаёт.

**Работы**

| Метод и путь | Тело / параметры | Ответ |
|---|---|---|
| `GET /works` | `item_id`, `group_id` (**текущие** члены), `status=assigned\|submitted\|graded\|returned`, `late`, `missing`, курсор | `{items: [{item, item_title, user_id, login, display_name, work_id, status, status_at, submissions, last_submitted_at, late, missing, grade: {score, passed, released} \| null}], next_cursor}`. С `item_id` — все текущие адресаты и все, у кого есть работа (без работы — `assigned`), курсор — id ученика; без `item_id` — только существующие работы, курсор — id работы (D-060) |
| `GET /items/{id}/works/{user_id}` | — | `{work_id, item, user_id, login, display_name, status, status_at, missing, events: [{kind: submission, id, at, late, text, files: [{id, kind: text\|file, name, size, sha256}], links: [{id, url, title}]} \| {kind: review, id, at, action, score, passed, comment, reviewer_id, release_of}], student_sees: {status, grade, returned_comment, submissions, late, missing}}`; текст ответа (`answer.md`) — строкой `text`. Адресат без работы — 200, `status: assigned`, событий нет |
| `POST /items/{id}/works/{user_id}/grade` | `{score? \| passed?, comment?, release?}` — `score` для `points`, `passed` для `pass_fail`, ничего для `none`; можно без сдачи | `{result: {work_id, review_id, status, released, seen_at_once}, work}` |
| `POST /items/{id}/works/{user_id}/return` | `{comment}` | `{result: {work_id, review_id, status}, work}` — ученик видит возврат сразу |
| `POST /items/{id}/release` | `{user_ids?}` (1–200) | `{results: [{user_id, login, result}]}` — по ученику: `released`, `nothing_to_release` (работы нет или она не оценена), `already_released`. Неизвестные `user_ids` — 404 `user_not_found` (все отсутствующие), ничего не опубликовано |

**Коды отказов.** Одна таблица `STATUS` в `admin_api/feed.py`; `tests/test_admin_feed_units.py` сверяет её со всеми
кодами сервисов и правил (новый код без статуса или код, которого никто не выдаёт, — падение теста), а
`tests/pg/test_admin_feed_pg.py` получает каждый код со статусом хотя бы одним запросом. Правило: 404 — нет объекта;
422 — ввод недопустим при любом состоянии; 409 — мешает текущее состояние.

| Статус | Коды |
|---|---|
| 404 | `item_not_found`, `user_not_found`, `group_not_found`, `contest_not_found`, `material_not_found`, `extension_not_found`, `work_not_found` (ученик не адресат и работы на этом элементе нет) |
| 422 | `invalid_request` (тело или параметр), `title_invalid`, `material_required`, `contest_required`, `item_invalid`, `grading_invalid`, `max_points_invalid`, `answer_kind_invalid`, `deadline_invalid`, `due_at_invalid`, `available_from_invalid`, `score_invalid`, `score_required`, `score_out_of_range`, `grade_kind_mismatch`, `passed_required`, `grade_not_expected`, `comment_too_long`, `comment_required`, `not_a_student`, `extension_subject`, `extension_not_later`, `extension_before_available`, `missing_needs_item`, `confirmation_mismatch` |
| 409 | `grading_locked`, `no_audience`, `item_archived`, `item_not_published`, `item_not_draft`, `item_has_works`, `item_has_extensions`, `group_archived`, `not_published`, `not_addressed`, `not_an_assignment`, `no_deadline`, `nothing_to_return`, `already_returned`, `release_not_needed`, `material_archived` |
| в ответе 200 | `nothing_to_release`, `already_released` — результаты массовой публикации по ученику |

Не достигаются админским запросом (D-063): коды сдачи ученика — `already_graded`, `deadline_passed`,
`not_available_yet`, `no_access` (403), `answer_required`, `text_not_expected`, `text_too_long` (422; в таблице
`STATUS` для 4б.2); защитные — `extension_before_available` (срок элемента и так позже `available_from`, это
`CHECK`) и `item_has_works` (у черновика не бывает работ).

Аудит: `item.*` и `work.*` ([`CLASSROOM.md`](CLASSROOM.md) §7) от имени админа сессии. Сдачи учеников в аудит не
пишутся.

### 5.9. Комментарии

Тот же сервис, что `judgectl comment` (`app/classroom/comments.py`, правила `comment_rules.py`;
[`CLASSROOM.md`](CLASSROOM.md) §9, D-072…D-077). Общая ветка — у элемента; личная — у пары «задание — ученик» и
находится **только по этой паре из пути** (`/items/{id}/students/{user_id}/…`): id ветки API не принимает, `up_to`
из другой ветки — 404 `comment_not_found` (отметка не меняется). Ученика в пути называет только преподаватель:
у эндпоинтов ученика (4б.2) `user_id` в пути не будет, ученик берётся из сессии. Читатель — **админ этой сессии**:
непрочитанное (`unread`) и отметки прочтения у каждого админа свои, отметки другого не меняются. Консоль
(`judgectl`) отметок не имеет. Комментарии — недоверенный текст (§4.1): уходят как есть; время — UTC с `Z`.

| Метод и путь | Тело / параметры | Ответ |
|---|---|---|
| `GET /items/{id}/comments` | `after` (id, ≥ 0), `limit` (1–200, по умолчанию 50) | `{thread_id \| null, item, kind: shared, student: null, comments_enabled, can_post, cannot_post, comments: [...], next_after, unread}` — старые сверху; удалённые — с текстом и причиной |
| `POST /items/{id}/comments` | `{body}` | 201: комментарий (ниже) |
| `POST /items/{id}/comments/read` | `{up_to?}` или без тела | `{unread}` — оставшееся непрочитанным у этого админа |
| `GET /items/{id}/students/{user_id}/comments` | `after`, `limit` | то же, `kind: private`, `student: {id, login, display_name}` |
| `POST /items/{id}/students/{user_id}/comments` | `{body}` | 201: комментарий — ответ ученику или первое сообщение (ветка создаётся им) |
| `POST /items/{id}/students/{user_id}/comments/read` | `{up_to?}` или без тела | `{unread}` |
| `POST /comments/{comment_id}/delete` | `{reason?}` или без тела | 200: удалённый комментарий |
| `GET /comments/threads` | `item_id`, `group_id`, `awaiting`, `unread` (`true\|false`), `cursor`, `limit` | `{items: [{thread_id, kind, item: {id, title, kind, state}, student, comments, deleted, last_id, last: {id, at, by_student} \| null, awaiting_reply, unread}], next_cursor}` |

Комментарий: `{id, at, body, author: {id, login, display_name, teacher, console}, deleted, deleted_at, deleted_by:
{id, login, console} | null, delete_reason, thread_id, item, kind, student}` (в ветке — без последних четырёх полей).
Комментарий консоли — `author.console: true`, `id`/`login` — `null`.

* **Курсоры.** Ветка читается по возрастанию id, поэтому её курсор — `after` / `next_after` (id последнего
  комментария страницы); входящие — новые сверху, как все списки, `cursor` / `next_cursor` (id самого нового
  комментария последней ветки страницы).
* **Отметки.** `GET` отметку не ставит, только явный `…/read`. Отметка только растёт: `up_to` старше прежней ничего
  не меняет. Свой комментарий админа непрочитанным для него не бывает; удалённые не считаются.
* **Кто пишет.** Преподаватель пишет в общую ветку и при выключенных комментариях, и в контест-элементе
  (объявления), но не в черновике (`not_published`); личная ветка — только у задания (`not_an_assignment`), только
  ученику (`not_a_student`), который адресат сейчас или имеет работу или ветку (`not_addressed`; читать чужую —
  404 `thread_not_found`). У архивного элемента писать можно. Частоты у преподавателя нет.
* **Удаление мягкое**: ученики комментарий больше не видят (даже заглушки), преподаватели видят текст, кто удалил и
  причину (≤ 200 символов после схлопывания пробелов). Повтор — 409 `already_deleted`. Удаление ответа
  преподавателя возвращает ветку в «ждёт ответа».
* **`comments_enabled`** — поле элемента в `POST /items` и `PUT /items/{id}` (`true`/`false`; `null` или у
  контест-элемента — 422 `item_invalid`); включает запись учеников в общую ветку задания.
* **Тело, которое база не может хранить.** NUL (`"\u0000"`) доходит до правил — 422 `comment_invalid`. Одиночный
  суррогат (`"\ud800"`) отвергает уже разбор тела (Pydantic, `string_unicode`) — **422 `invalid_request`** с
  проблемой `body: …`, никогда не 500 (проверено тестом для общей и личной ветки и в smoke; так же для `reason`).
  Пустое или только невидимое (пробелы любого вида, нулевой ширины, BOM) — `comment_required`; больше 4000
  символов — `comment_too_long` (предел тела в схеме — 100 000, чтобы до правила доходило).

**Коды отказов** (та же таблица `STATUS`, тот же тест полноты: сканер видит и отказы `_no("…")` правил комментариев):

| Статус | Коды |
|---|---|
| 404 | `thread_not_found` (ветка не читается: личная — ученик не адресат, без работы и ветки; не задание; не ученик), `comment_not_found` (нет такого; `up_to` не из этой ветки), `item_not_found`, `user_not_found`, `group_not_found` |
| 422 | `comment_invalid`, `comment_required`, `comment_too_long`, `reason_too_long`, `not_a_student`, `invalid_request` |
| 409 | `not_published`, `not_an_assignment`, `not_addressed`, `already_deleted` |

Не достигаются админским запросом (D-077, множество `COMMENT_STUDENT_ONLY`; статусы — для 4б.2):
`comments_disabled` (409), `comments_teacher_only` (409), `not_your_comment` (403), `rate_limited` (429; частота —
`JUDGE_COMMENT_RATE_PER_MIN`, по умолчанию 10 в минуту, `Retry-After` — в 4б.2). Тест достижимости 4б.2 должен
получить их запросами ученика.

Аудит: `comment.post` и `comment.delete` (без текста; у удаления — автор, ветка и причина) от имени админа сессии.
Комментарии учеников в аудит не пишутся.

### 5.10. Вложения и ссылки, файлы работ

Те же сервисы, что `judgectl item attach | link | attachments | detach | attachment-order` и `work file`
(`app/classroom/attachments.py`, `works.work_file`, правила `attachment_rules.py`; [`CLASSROOM.md`](CLASSROOM.md) §10,
D-078…D-082). Отдача — `content_files.file_response(…, request=request)` ([`FILES.md`](FILES.md): тип по имени и
содержимому, `nosniff`, CSP `sandbox`, `ETag`, 304) и **только после** проверки, что файл принадлежит элементу или работе
из пути. Имена, URL и заголовки — недоверенный текст (§4.1), уходят как есть. Id в путях этих маршрутов — int4
(1…2 147 483 647): больше — 422 `invalid_request`, а не ошибка базы.

| Метод и путь | Тело / параметры | Ответ |
|---|---|---|
| `GET /items/{id}/attachments` | — | `{items: [вложение]}` по порядку |
| `POST /items/{id}/attachments?name=ИМЯ` | сырое тело, `Content-Type: application/octet-stream` | 201: вложение-файл, в конце списка |
| `POST /items/{id}/links` | `{url, title?}` | 201: вложение-ссылка, в конце списка |
| `PUT /items/{id}/attachments/order` | `{ids}` — **все** id вложений элемента ровно по разу | `{items}` в новом порядке |
| `GET /items/{id}/attachments/{att_id}` | — | файл (`file_response`); ссылка, чужой или удалённый id — 404 `attachment_not_found` |
| `POST /items/{id}/attachments/{att_id}/delete` | — | 204; блоб остаётся, аудит хранит имя, sha256 и размер (у ссылки — URL и заголовок) |
| `GET /items/{id}/works/{user_id}/files/{file_id}` | — | файл работы: загрузка ученика или текст ответа `answer.md` (id — в карточке работы, `events[].files`); файл чужой работы (другого ученика или элемента) — 404 `file_not_found` |

Вложение: `{id, kind: file | link, position, created_by, created_at}` и у файла `name, size, sha256`, у ссылки
`url, title`. В карточке работы (§5.8) у каждой сдачи — `files: [{id, kind: text | file, name, size, sha256}]` и
`links: [{id, url, title}]`.

* **Загрузка: сначала всё дешёвое, потом тело.** Порядок проверок до первого прочитанного байта тела:
  1. `Origin` (публичное приложение, 403 `origin_not_allowed`);
  2. сессия админа (401 `not_authenticated` / `mfa_required`, 403 `admin_required`) и CSRF-токен (403
     `csrf_token_required`);
  3. `Content-Type` — только `application/octet-stream` (415 `unsupported_media_type`);
  4. имя `?name=` — UTF-8 в процентной кодировке (`encodeURIComponent`), ровно один раз, не пустое. Разбирается
     строго из сырой строки запроса: битая кодировка — 422 `invalid_request`, а не подмена на U+FFFD; `+` — пробел;
  5. элемент существует (404 `item_not_found`), и то, что решается без тела (`attachments.precheck_file`): архивный —
     409 `item_archived`, лимит числа — 409 `too_many_attachments`, тип по имени — 422 `file_type_not_allowed`,
     повтор имени без учёта регистра — 422 `duplicate_name`.

  Только после этого тело пишется потоком во временный файл `<корень блобов>/tmp` (`blobstore.Incoming`,
  `http_body.save_body`). Больше `JUDGE_ATTACHMENT_MAX_MB` — **413 `too_large`** сразу (по `Content-Length`, ничего не
  прочитав, или как только счётчик превысит лимит). Затем сервис `add_file` берёт строку элемента `FOR UPDATE`, ещё раз
  проверяет всё под блокировкой (тип по содержимому — `file_content_mismatch`, пустой — `file_empty`) и последним
  шагом переносит файл в хранилище. Пока тело принимается, запрос **не держит ни транзакции, ни блокировки** (после
  ранних проверок — `rollback`). Отказ или обрыв не оставляют ни блоба, ни временного файла. Тесты: на каждый ранний
  отказ — тело не прочитано (0 кусков из 2,5 МБ, т. е. ранний код, а не 413) и `tmp` пуст; во время приёма тела
  другое соединение блокирует строку элемента с `NOWAIT`.
* **Имя** приводится к базовому (`C:\…\рисунок.PNG` → `рисунок.PNG`, `content_files.clean_name`) и проверяется
  правилами (белый список по последнему расширению, CLASSROOM §10). Кириллица допустима.
* **Ссылка** хранится как прислана (без пробелов по краям), сервер её не открывает. Пределы схемы (URL 10 000,
  заголовок 1 000 символов) выше пределов правил (2000 и 200), поэтому длинное значение — `link_invalid` /
  `link_title_invalid`, а не `invalid_request`.
* **Порядок** (`PUT …/order`) — ровно текущий набор: иначе 422 `attachment_order_invalid` с `missing`, `extra`,
  `repeated`. Тот же порядок — без записи в аудит.
* **Архивный элемент** только читается: список и скачивание работают, изменения — 409 `item_archived`.

**Коды отказов** (таблица `STATUS`; сканер теста полноты видит и `Refusal("…")` правил вложений):

| Статус | Коды |
|---|---|
| 404 | `attachment_not_found`, `file_not_found`, `item_not_found`, `user_not_found` |
| 422 | `file_type_not_allowed`, `file_content_mismatch`, `file_empty`, `duplicate_name`, `link_invalid`, `link_title_invalid`, `attachment_order_invalid`, `invalid_request` |
| 409 | `item_archived`, `too_many_attachments` |
| 413 / 415 | `too_large` (тело больше лимита), `unsupported_media_type` |

Защитные (`DEFENSIVE`, админским запросом не достигаются): `file_too_large` — загрузку сверх лимита раньше отвергает
413 `too_large` (`judgectl item attach` получает именно `file_too_large`); `file_name_invalid` — имя сначала очищается
(`clean_name`: никогда не пустое, без разделителей и управляющих символов, до 150 символов). Только сдача ученика
(`HAND_IN_ONLY`, статусы для 4б.2): `too_many_files`, `submission_too_large` (422), `quota_exceeded` (409),
`files_required`, `files_not_expected` (422). `NO_ROUTE_YET` пуст: у каждого кода сервисов есть маршрут.

Аудит: `item.attachment_add`, `item.attachment_remove`, `item.attachment_order` (без содержимого) от имени админа
сессии.

### 5.11. Библиотека материалов и Справочник

Те же сервисы, что `judgectl material …` / `section …` (`app/materials/service.py`, `library.py`, `refs.py`;
[`CLASSROOM.md`](CLASSROOM.md) §11, D-083…D-087). Материал — по id (int4, больше — 422 `invalid_request`), раздел — по
id. Ученических эндпоинтов здесь нет (4б.2): админ видит всё, архивное тоже, а ссылки разрешаются как для админа —
`ok` / `archived` / `unknown` (ученику 4б.2 отдаст только `available`, не различая «нет» и «закрыт»). Названия, тексты,
имена файлов и URL — недоверенный текст (§4.1), уходят как есть.

**Материалы и версии**

| Метод и путь | Тело / параметры | Ответ |
|---|---|---|
| `GET /materials` | `?archived=true` — и архивные; `cursor`, `limit` | `{items: [сводка], next_cursor}`, новые сверху |
| `POST /materials` | `{slug, title, body_md?, library?: none \| all_students \| groups, group_ids?}` | 201: материал как `GET` и `warnings` |
| `GET /materials/{id}` | — | последняя версия: `version`, `body_md`, `attachments`, `links` (разрешённые ссылки текста), `link_warnings`; сводка: `slug`, `title`, `library`, `groups`, `sections`, `versions` (число), `archived_at` |
| `PATCH /materials/{id}` | `{title?, library?, slug?}` — только присланные ключи | `{material: сводка, changed}`; другой slug — **422 `slug_immutable`** (тот же — без изменений); `null` и другие ключи — 422 `invalid_request` |
| `POST /materials/{id}/versions` | `{body_md}` | версия (ниже); тот же текст — `created: false` и последняя версия |
| `GET /materials/{id}/versions` | — | `{items: [{version, created_at, created_by, body_chars, attachments}]}`, новые сверху |
| `GET /materials/{id}/versions/{n}` | — | `{slug, version, created_at, created_by, body_md, attachments, links}`; нет — 404 `version_not_found` |
| `PUT /materials/{id}/groups` | `{group_ids}` — ровно эти | сводка; новая группа — неархивная (409 `group_archived`), нет — 404 `group_not_found` |
| `POST /materials/{id}/archive` / `unarchive` | — | `{material, changed}`; повтор — `changed: false`, не ошибка |
| `GET /materials/{id}/where` | — | `{material_items, linking_items, problems: ["slug@v"], materials: [slug]}` |
| `POST /materials/resolve` | `{text}` | `{links: [{slug, status, id?, title?}], warnings}`; ничего не пишет |

Версия в ответе изменения: `{material_id, slug, version, created, warnings, attachments}`. **Каждое изменение
содержимого — новая версия**: текст, файл, ссылка, удаление, порядок; вложения копируются строками (новые id, общие
блобы). Название и аудитория библиотеки меняются на месте (`PATCH`), без версии. Ссылки `[[памятка:slug]]` —
неверная запись, неизвестный slug, архивный материал — никогда не отказ, а `warnings` / `link_warnings`; те же поля —
у элемента ленты (`link_warnings` в `GET /items/{id}`).

**Вложения версии** — по образцу §5.10:

| Метод и путь | Тело / параметры | Ответ |
|---|---|---|
| `POST /materials/{id}/attachments?name=ИМЯ` | сырое тело, `application/octet-stream` | 201: новая версия с файлом в конце |
| `POST /materials/{id}/links` | `{url, title?}` | 201: новая версия со ссылкой в конце |
| `PUT /materials/{id}/attachments/order` | `{ids}` — все вложения **последней** версии по разу | версия (`created: false` — тот же порядок); иначе 422 `attachment_order_invalid` с `missing`, `extra`, `repeated` |
| `GET /materials/{id}/attachments/{att_id}` | `?version=N` — файл старой версии | файл (`file_response`, [`FILES.md`](FILES.md)); не файл этой версии (другой версии, ссылка, нет id) — 404 `attachment_not_found`; нет версии — 404 `version_not_found` |
| `POST /materials/{id}/attachments/{att_id}/delete` | — | новая версия без вложения; не вложение последней версии — 404 `attachment_not_found`. Блоб остаётся у старых версий |

Загрузка — точно как в §5.10: до первого байта тела — `Origin` → сессия и CSRF → `Content-Type` (415) → `?name=` (422)
→ материал (404 `material_not_found`) и то, что решается без тела (`materials.precheck_file`: архивный — 409
`material_archived`, лимит — 409 `too_many_attachments`, тип по имени и повтор имени в последней версии — 422), затем
`rollback`. Тело — потоком в `<корень блобов>/tmp`, сверх `JUDGE_ATTACHMENT_MAX_MB` — 413 `too_large`. Строку материала
`FOR UPDATE` берёт только сервис `add_file` после тела и решает всё заново (архив, повтор имени, лимит, тип по
содержимому). Путь `POST /api/admin/materials/<id>/attachments` освобождён от лимита JSON шаблоном
`routes.UPLOAD_PATTERNS` (id без ведущего нуля, до 10 цифр); соседние пути (`/links`, `/attachments/order`,
`/versions`, со слэшем на конце, другой метод) — нет (тест `test_http_body_units`). Лимиты — те же, что у вложений
элемента (`JUDGE_ATTACHMENT_MAX_MB`, `JUDGE_ITEM_ATTACHMENTS_MAX`).

**Справочник**

| Метод и путь | Тело / параметры | Ответ |
|---|---|---|
| `GET /library` | `?q=` — поиск (до 200 символов) | `{sections: [{id, title, materials, sections}], unsorted}` — все разделы и материалы (архивные с `archived: true`); с `q` — `{query, materials}`: по названию без учёта регистра (кириллица тоже), `%`, `_`, `\` — буквально |
| `POST /library/sections` | `{title, parent_id?}` | 201: `{id, title, parent_id}` в конце уровня; под подразделом — 409 `section_too_deep` |
| `PUT /library/sections/order` | `{parent_id?, ids}` — все разделы уровня по разу (`parent_id` нет / `null` — верхний) | `{parent_id, ids}`; иначе 422 `order_invalid` с `missing`, `extra`, `repeated` |
| `PATCH /library/sections/{id}` | `{title}` | `{id, title, parent_id, changed}`; 422 `section_title_invalid` |
| `POST /library/sections/{id}/move` | `{parent_id}` — раздел верхнего уровня или `null` | `{…, changed}`; под себя — 422 `section_invalid`; под подраздел или раздел с подразделами под другой — 409 `section_too_deep` |
| `POST /library/sections/{id}/delete` | `{confirm_id}` — тот же id (422 `confirmation_mismatch`) | что удалено и slug материалов, которые в нём были; с подразделами — 409 `section_not_empty` |
| `POST /library/sections/{id}/materials` | `{material_id}` | `{section_id, material_id, changed}` — в конец раздела; архивный — 409 `material_archived` |
| `PUT /library/sections/{id}/materials/order` | `{ids}` — id материалов раздела по разу | `{section_id, ids}`; иначе 422 `order_invalid` |
| `POST /library/sections/{id}/materials/{material_id}/delete` | — | `{…, changed}`: убрать из раздела (материал остаётся) |

**Коды отказов** (таблица `STATUS`; сканер теста полноты читает `materials/*.py`, а код, переданный правилу
аргументом, — `section_title_invalid` — тест требует литералом в `materials/rules.py`):

| Статус | Коды |
|---|---|
| 404 | `material_not_found`, `version_not_found`, `section_not_found`, `attachment_not_found`, `group_not_found` |
| 422 | `slug_invalid`, `slug_immutable`, `title_invalid`, `body_invalid` (NUL), `body_too_long` (> 100 000 символов), `library_invalid`, `section_title_invalid`, `section_invalid`, `order_invalid`, `attachment_order_invalid`, коды файлов и ссылок §5.10, `confirmation_mismatch`, `invalid_request` (в том числе одиночный суррогат в JSON) |
| 409 | `slug_taken` (архивным тоже), `material_archived`, `section_too_deep`, `section_not_empty`, `group_archived`, `too_many_attachments` |
| 413 / 415 | `too_large`, `unsupported_media_type` |

Защитный (`DEFENSIVE`): `material_invalid` — отказ `update_material` для поля, которое он не меняет; `PATCH`
отвергает любой ключ, кроме `title`, `library`, `slug`, раньше (`invalid_request`). `NO_ROUTE_YET` пуст.

Аудит: `material.create`, `material.update`, `material.version`, `material.attachment_add`,
`material.attachment_remove`, `material.attachment_order`, `material.library_groups`, `material.archive`,
`material.unarchive`, `library.section_create`, `library.section_rename`, `library.section_move`,
`library.section_delete`, `library.section_order`, `library.place`, `library.unplace`, `library.order` — без текста
(длины, имена, sha256 и размер, URL и заголовки) от имени админа сессии.

## 6. Проверка на работающем приложении

`tools/smoke-admin-api.sh [BASE_URL] ORIGIN` — curl-сценарий проверки 4а.3 и 4а.4 через этот API (для 4а.4: загруженные задачи ждут `ready` до 120 с, пакет `tests/problems/check-broken-checker` получает `failed` с логом компиляции, закрепить его нельзя, `recheck` создаёт одну проверку и заканчивается `ready`; нужен работающий воркер с `problem_check`): класс из CSV со столбцом
«Класс» (две группы), загрузка задач, контест только для группы (сроки с политиками `late` и `no`), добавление
учеников, продление одному ученику, удаление из группы, посылки (список, фильтры `late` и `group_id`, вид
преподавателя, исходник вложением), перепроверка в два шага, откат импорта (К3: группа контеста остаётся, другая
удаляется), архивация с контестами (К6), часовой пояс, `/system`, `/audit`, `/languages`; лента 4в.1b (отдельный
блок): задание группе с `<script>` в названии, карточка адресата без работы (`assigned`, «не сдано»), сдачи через
`SEED_CMD` (`dev_seed --item`: с опозданием и с продлением вовремя), очередь проверки с фильтрами, `score_invalid`,
оценка, возврат и пересдача, переоценка, публикация по ученикам (неизвестный `user_id` — 404 без изменений),
архивация группы с `feed_items`. Блок картинок 4б.0 (отдельно, после ленты): пакет с картинкой под кириллическим именем, PNG под `.jpg`
и HTML под `.png` — предупреждение импорта о подделке, `inline` и `image/png` для настоящей картинки (имя целиком в
`filename*`, транслит в `filename`), `nosniff`, CSP, `private, no-cache`, `ETag`, 304 с теми же заголовками, подделка —
вложением, без сессии `If-None-Match` ничего не меняет (401). Блок комментариев 4в.2 (§5.9, после картинок):
комментарий в черновике — 409, админ в пути личной ветки — 404 `thread_not_found`, удаление несуществующего — 404,
NUL — `comment_invalid`, одиночный суррогат — 422 `invalid_request`, личная ветка контест-элемента —
`not_an_assignment`; с `SEED_CMD` (`dev_seed --item … --comment`): ученик при выключенных комментариях —
`comments_disabled`, после `comments_enabled: true` — общий комментарий и личный с `<script>` и ESC, входящие «ждёт
ответа» (обе ветки), тело байт в байт, `GET` отметку не ставит, ответ преподавателя убирает ветку из «ждёт ответа»,
`…/read` — `unread 0`, удаление с причиной, повтор — 409, ветка снова ждёт ответа. `rate_limited` не проверяется (он
зависит от `JUDGE_COMMENT_RATE_PER_MIN` установки). Блок файлов 4в.3 (§5.10, после комментариев): вложение-PDF под
кириллическим именем и картинка; SVG, HTML под `.png`, `.exe`, имя не в UTF-8, повтор имени в другом регистре — 422,
другой `Content-Type` — 415; ссылка с недоверенным заголовком, `javascript:` — `link_invalid`; скачивание теми же
байтами, вложением с именем в `filename*`, `nosniff`, CSP `sandbox`, 304 по `ETag`, картинка — `inline`; ссылка как
файл — 404; порядок и неполный порядок, удаление; с `SEED_CMD` (`dev_seed --item … --file … --link …`): `.exe`
ученика — отказ, сдача текста, двух файлов и ссылки, карточка работы с файлами, скачивание `main.py` (304) и
`answer.md`, тот же файл как файл другого ученика или под другим элементом — 404 `file_not_found`; архивный элемент —
409. Блок материалов 4в.4 (§5.11, после файлов): материал со ссылкой на ещё не существующий slug — предупреждение,
ссылка `unknown`; новая версия со ссылкой на другой материал — без предупреждений; тот же текст — `created: false`;
другой slug — 422 `slug_immutable`; название — на месте, без версии; задание со ссылками на материал и на неизвестный
slug — одно `link_warnings`, `where` его видит (черновик затем удаляется); `resolve` — `ok` и `unknown`, два
предупреждения; раздел с подразделом, третий уровень — 409 `section_too_deep`; материал в подразделе виден в
`GET /library`; поиск «циклы <время>» в нижнем регистре находит «Циклы <время>»; PDF в новую версию (версия 3), SVG —
422; файл скачивается теми же байтами с `nosniff`, как файл версии 2 — 404; история 3, 2, 1; архив дважды
(`changed` true, затем false), загрузка в архивный — 409; разделы удаляются. Остаются в базе архивными материалы
`smoke-<время>-memo` и `-loops` (материал не удаляется) и их блобы. Файлы для `dev_seed` лежат в отдельном каталоге 755 (их читает `judge` через `seed-as-judge.sh`). Спрашивает админа и код
TOTP (или `ADMIN_LOGIN`, `ADMIN_PASSWORD`, `TOTP_CODE` из окружения); секреты передаются curl только через файлы во
временном каталоге 700 и не печатаются. Всё создаётся с именами `smoke-<время>`, повторный запуск не мешает
прежнему. Код выхода — число провалов.

Посылать как ученик до API 4б можно только через `dev_seed`: `SEED_CMD=tools/seed-as-judge.sh` (dev,
от `judge` через ваш `sudo`, ученики `dev-student-1` и `-2`). Без `SEED_CMD` проверки посылок пропускаются (`SKIP`),
и каждая строка называет команду `dev_seed` и ожидаемый результат. Это только описание: к концу прогона контест
архивирован, а импорт откачен, поэтому сроки и доступ через группу по-настоящему проверяются только с `SEED_CMD`.

