# Админский HTTP API (`/api/admin`)

API для админского веб-интерфейса (модуль 5). Решения — [`MODULE_4A.md`](MODULE_4A.md) §5.7–§5.8; вход, сессии и
профиль — [`AUTH.md`](AUTH.md). Тот же процесс, что `/api/auth` (публичное приложение, `python -m app.public_main`), за
тем же Caddy. Код: [`backend/app/admin_api/`](../backend/app/admin_api); сервисы — общие с `judgectl`.

**Машиночитаемое описание** — [`openapi-admin.json`](openapi-admin.json) (`/api/admin` и `/api/auth`). По сети приложение
OpenAPI не отдаёт (`openapi_url=None`); файл пишет `python tools/gen-openapi.py`, а `tests/test_openapi_units.py`
падает, если файл разошёлся с приложением. Этот документ — описание для людей: смысл, правила и коды ошибок.

Разделы: пользователи (§5.1), задачи (§5.2), контесты (§5.3), группы (§5.4), продления (§5.5), посылки (§5.6),
система, аудит и языки (§5.7).

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

## 4. Файлы из загруженного контента

Файлы версии задачи и исходники посылок приходят от людей, поэтому отдаются так, чтобы браузер не исполнил их как
страницу (XSS):

* всегда `X-Content-Type-Options: nosniff` и `Content-Security-Policy: sandbox; default-src 'none'`;
* показываются в браузере (`inline`) **только картинки** `png`, `jpeg`, `webp` (D-026; GIF — вложением), тип — по расширению из списка
  файлов версии, не по содержимому;
* всё остальное, включая SVG и HTML, — `Content-Disposition: attachment` и `application/octet-stream`; исходники и
  тесты — `text/plain; charset=utf-8` и `attachment`;
* путь файла ищется только среди файлов версии (`files`), к файловой системе по пути из URL обращения нет.

То же правило обязательно для картинок условия в API ученика (4б).

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
  - сообщения ошибок (`message`, `problems`) и предупреждения `apply` (в них встречаются названия из файлов);
* **Markdown — только через санитайзер** (разрешённый список тегов и атрибутов, без `script`, `style`, `iframe`,
  обработчиков `on*`, ссылок `javascript:` / `data:`): `note_md` задачи контеста, `description` контеста, условия
  задач;
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
| `POST /groups/{id}/archive` | — | `{group, contests: [slug]}` — контесты, к которым группа подключена (К6): она больше не даёт к ним доступа | 404 |
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

## 6. Проверка на работающем приложении

`tools/smoke-admin-api.sh [BASE_URL] ORIGIN` — curl-сценарий проверки 4а.3 через этот API: класс из CSV со столбцом
«Класс» (две группы), загрузка задач, контест только для группы (сроки с политиками `late` и `no`), добавление
учеников, продление одному ученику, удаление из группы, посылки (список, фильтры `late` и `group_id`, вид
преподавателя, исходник вложением), перепроверка в два шага, откат импорта (К3: группа контеста остаётся, другая
удаляется), архивация с контестами (К6), часовой пояс, `/system`, `/audit`, `/languages`. Спрашивает админа и код
TOTP (или `ADMIN_LOGIN`, `ADMIN_PASSWORD`, `TOTP_CODE` из окружения); секреты передаются curl только через файлы во
временном каталоге 700 и не печатаются. Всё создаётся с именами `smoke-<время>`, повторный запуск не мешает
прежнему. Код выхода — число провалов.

Посылать как ученик до API 4б можно только через `dev_seed`: `SEED_CMD=tools/seed-as-judge.sh` (dev,
от `judge` через ваш `sudo`, ученики `dev-student-1` и `-2`). Без `SEED_CMD` проверки посылок пропускаются (`SKIP`),
и каждая строка называет команду `dev_seed` и ожидаемый результат. Это только описание: к концу прогона контест
архивирован, а импорт откачен, поэтому сроки и доступ через группу по-настоящему проверяются только с `SEED_CMD`.

