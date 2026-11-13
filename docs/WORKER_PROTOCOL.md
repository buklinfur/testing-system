# Протокол воркер ↔ бэкенд (внутренний API, версия 1)

Базовый путь `/internal/worker/v1`, JSON. Внутренний API слушает **отдельный порт** (`127.0.0.1:8001`) и не публикуется
через обратный прокси (Caddy). Второй рубеж: запрос с любым заголовком, который добавляет прокси, получает **403** ещё до
проверки токена. Это `X-Forwarded-For`, `X-Forwarded-Proto`, `X-Forwarded-Host` (Caddy ставит их всегда), `Forwarded`,
`X-Real-IP` (другие прокси) и `cf-ray` / `cf-connecting-ip` (Cloudflare). Воркер ходит на порт напрямую и таких заголовков не шлёт.
Список: `PROXY_HEADERS` в `backend/app/internal_main.py`, тесты: `tests/test_internal_api_units.py`.
Воркер знает только URL и токен и не ходит в БД, поэтому его можно вынести на другую машину (тогда нужен VPN/LAN и firewall; токен даёт доступ ко всем тестам).

Заголовки: `Authorization: Bearer <token>`; после регистрации ещё `X-Worker-Id`, `X-Worker-Instance`.
Токен хранится в БД как sha256, один токен на машину, у него есть список разрешённых `capabilities`.

| Запрос | Назначение |
|---|---|
| `POST /register` `{name, capabilities, info}` → `{worker_id, instance}` | создаёт/перехватывает воркера по имени, новый `instance`; возвращает прошлые задания в очередь |
| `POST /claim` → `{job: {...} \| null}` | следующее задание: посылка в справедливом порядке с учётом `capabilities`, иначе проверка задачи (ниже) |
| `POST /heartbeat` `{state, judgement_id \| check_id, attempt, phase, tests_done, tests_total, stats}` → `{cancel, reclaimed}` | жив / прогресс; `cancel=true`: задание отобрано; `reclaimed: [{kind: judgement \| check, id, attempt, result: requeued \| gave_up}]` — задания, которые числились за воркером, но heartbeat их не назвал, и они выданы дольше `heartbeat_timeout_s` назад: возвращены в очередь как провал попытки (D-069). Поле новое, старый воркер его игнорирует |
| `GET /problem-versions/{id}/manifest` → `{manifest_sha256, manifest}` | описание задачи, файлы по sha256 |
| `GET /blobs/{sha256}` | содержимое файла (потоком); воркер сверяет хеш |
| `POST /judgements/{id}/result` | результат; повтор безопасен |
| `POST /judgements/{id}/release` `{attempt, reason}` | вернуть задание (остановка), не считается провалом |
| `POST /problem-checks/{id}/result` `{attempt, outcome, report}` или `{attempt, error}` | итог проверки версии задачи; повтор безопасен |
| `POST /problem-checks/{id}/release` `{attempt, reason}` → `{released}` | вернуть проверку (остановка), не считается провалом |
| `POST /deregister` | воркер остановился: задания возвращаются в очередь (не провал), экземпляр заканчивается — дальше все запросы этого `instance` получают 409 `deregistered` |

Коды ответа:

| Код | Когда | Что делает воркер |
|---|---|---|
| 401 | токен неверен или отозван | выход **78** (ошибка конфигурации, без перезапусков; D-066) |
| 403 | запрос пришёл через прокси, чужое имя или токен не разрешает capability | — |
| 409 `deregistered` | этот экземпляр сам сказал `deregister` (`workers.instance_id` пуст); проверяется **раньше** `superseded`, на всех маршрутах, кроме `register`, до разбора тела (D-068) | при остановке — молча; иначе выход **75** и новая регистрация |
| 409 `superseded` | зарегистрировался другой процесс с этим именем | выход **75** и новая регистрация через минуту |
| 409 `lease_lost` | результат или `release` от чужой или старой попытки; сервер уже завершил задание сам — проверку по таймауту или после `max_attempts`, посылку после `max_attempts` (D-069) | результат не доставляется, в журнал «rejected» |
| 422 | результат не сходится с версией задачи или отчёт проверки неверен | — |

**После `deregister`.** Поздний heartbeat (запрос был в пути во время остановки) больше не возвращает воркеру состояние
`idle`: сервер отвечает 409 `deregistered`, `state` остаётся `stopped`. Воркер и сам ждёт поток heartbeat (не дольше
таймаута запроса) прежде чем сказать `deregister`. Воркер, которого reaper пометил `stopped` молча (heartbeat пропал),
экземпляр не теряет и **оживает** своим следующим heartbeat. `register` возможен всегда и выдаёт новый `instance`.
Тесты: обход всех маршрутов роутера (`tests/pg/test_worker_api_pg.py`, новый маршрут проверяется сам) и
`tests/test_worker_e2e.py`.

**Совместимость.** Старый воркер (до 4б.0) кода `deregistered` не знает: для него это обычный 409 (`ApiConflict`).
На claim он выходит с 75 и через минуту регистрируется заново, на heartbeat считает задание потерянным, результат —
«rejected»; то есть ведёт себя как прежде. Новый воркер со старым сервером `deregistered` не получает никогда.
Обновлять воркер и сервер одновременно не нужно.

Версия протокола остаётся `v1`: поля только добавляются. Воркер разбирает задание по известным ему полям и лишние
игнорирует (`worker/client.py` отдаёт словарь как есть; тесты: `tests/pg/test_worker_api_pg.py`,
`tests/test_worker_e2e.py`). Воркер без capability `problem_check` проверок не получает никогда, поэтому воркер
версии 4а.3 работает с бэкендом 4а.4 без изменений.

## job (ответ claim)

Поле `kind` различает задания: `"judgement"` (посылка) или `"problem_check"` (проверка версии задачи, ниже).
Поле `release` — путь, по которому задание возвращается (`/judgements/{id}/release`, `/problem-checks/{id}/release`):
задание незнакомого вида воркер не выполняет, а возвращает по этому пути (D-056). Путь принимается только
относительный и только под `/internal/worker/v1/` (буквы, цифры, `_ - .` в сегментах; без схемы, хоста, `//`, `..`,
`%`, `?`, `#`): запрос несёт токен, поэтому полный URL или выход за префикс — отказ с записью в лог, запрос не
отправляется (`worker/client.py`, `release_path`).

```json
{"kind": "judgement", "release": "/judgements/12/release", "judgement_id": 12, "attempt": 1, "reason": "initial", "submission_id": 7,
 "problem_version_id": 3, "manifest_sha256": "…64 hex…",
 "language": "cpp", "source_sha256": "…", "is_archive": false, "entry_file": null,
 "answers": null, "deadline_s": 1800}
```
Для `choice`/`text_answer` `language` и `source_sha256` пусты, ответы в `answers`; воркер оценивает их без песочницы.

## manifest

`protocol/manifest.py` (общий код бэкенда и воркера): `format`, `version_id`, `slug`, `type`, `titles`, `config`, `groups[]`
(с `tests[]`: `id, position, name, input_sha256, answer_sha256`), `programs[]` (checker/interactor: `role, name, language, source_sha256`).
`config` = нормализованный `problem.yaml` **без** `test_groups` (тесты лежат в `groups`; формат пакета не менялся).
`manifest_sha256` = sha256 канонического JSON; это ключ кэша воркера. Для опубликованной версии он хранится в `problem_versions.manifest_sha256`.

## result

```json
{"attempt": 1, "verdict": "PARTIAL", "score": "40.00", "max_score": "100.00",
 "compile_log": null, "error": null,
 "summary": {"first_fail": "WA", "groups": [{"name": "small", "points": "40.00", "score": "40.00", "scoring": "sum", "visibility": "hidden"}]},
 "tests": [{"test_case_id": 101, "index": 1, "verdict": "OK", "time_ms": 12, "memory_kb": 3100, "exit_code": 0, "signal": null, "tl_kind": null,
            "message": "", "stderr_excerpt": "", "output_excerpt": "…", "visible_to_student": true}],
 "timing": {"fetch_ms": 40, "judge_ms": 900}}
```
`tl_kind` бывает только у `TL`: `"cpu"` или `"wall"` (иначе 422); у других вердиктов бэкенд его не сохраняет.
`visible_to_student` вычисляет воркер тем же `runner.explain.student_view`, который уже проверен тестами раннера (одна логика, а не две).
Вердикт `IE` бэкенд повторяет до `max_attempts`, потом фиксирует (`score = 0`, `max_score` бэкенд считает сам по версии).
`score` и `max_score` — в баллах версии задачи; должно быть `0 ≤ score ≤ max_score ≤ 999999.99`, иначе 422 (D-090).

## Проверка версии задачи (`kind: "problem_check"`, сессия 4а.4)

Выдаётся только воркеру с capability `problem_check` и только когда для него нет ни одной посылки; одновременно идёт не
больше `JUDGE_MAX_CONCURRENT_CHECKS` проверок (порядок и квота — `QUEUE.md`, «Проверки задач»).

```json
{"kind": "problem_check", "release": "/problem-checks/5/release", "check_id": 5, "attempt": 1, "reason": "import", "problem": "sum-of-two@3",
 "problem_version_id": 3, "manifest_sha256": "…64 hex…",
 "solutions": [{"name": "solutions/ok.py", "language": "python", "source_sha256": "…", "expect": "OK"},
               {"name": "solutions/wa.cpp", "language": "cpp", "source_sha256": "…", "expect": "WA"}],
 "deadline_s": 600}
```
* Манифест тот же, что для посылок (его дайджест — ключ кэша воркера, эталонов в нём нет); эталоны приходят в самом
  задании и скачиваются как блобы. `reason`: `import` или `recheck`. `deadline_s` = `JUDGE_CHECK_TIMEOUT_SEC`.
* Воркер компилирует чекер и интерактор (если есть), затем прогоняет эталоны и сверяет вердикт первого неудачного
  теста с `expect` (`PROBLEM_FORMAT.md` §9).
* Heartbeat: `{"state": "busy", "check_id": 5, "attempt": 1, "tests_done": 1, "tests_total": 2, …}`;
  `tests_done`/`tests_total` считают эталоны. `cancel=true` — проверка отобрана, воркер её бросает.
* `release` — при остановке воркера: проверка возвращается в очередь, провал не засчитывается.

### Итог проверки

```json
{"attempt": 1, "outcome": "failed", "report": {
  "outcome": "failed", "reason": "solution_mismatch", "worker": "judge-0", "toolchain_id": "…", "duration_ms": 8100,
  "programs": [{"role": "checker", "name": "checker.cpp", "ok": true, "log": ""}],
  "solutions": [{"name": "solutions/wa.cpp", "language": "cpp", "expected": "WA", "got": "OK", "ok": false,
                 "score": "100", "first_fail": null, "message": "", "error": null}]}}
```
* `outcome` — `ready` или `failed`, он становится статусом версии; `report` копируется в `problem_versions.check_report`
  (история — в `version_checks`).
* `report.outcome` обязан совпадать с `outcome`. `report.reason` у `failed` обязателен, у `ready` — `null` или
  `nothing_to_check`. Допустимые причины: `checker_compile_failed`, `interactor_compile_failed`, `solution_mismatch`,
  `timeout` (воркер остановился по `deadline_s`, отчёт частичный), `internal_error`, `worker_lost`, `nothing_to_check`.
* Поле `by` ставит только сервер (`"by": "server"` — проверку завершил сервер: таймаут или `max_attempts`), отчёт с
  ним отклоняется. `programs` и `solutions` — списки; отчёт больше 512 КиБ отклоняется (логи компиляции обрезает
  воркер). Нарушение любого правила — **422**, ничего не меняется.
* `{"attempt": 1, "error": "…"}` — сбой самого воркера, а не задачи: проверка возвращается в очередь
  (`{"status": "requeued"}`), после `JUDGE_MAX_ATTEMPTS` сбоев завершается `failed` с причиной `internal_error`
  (`{"status": "recorded"}`).
* Ответы: `recorded`, `requeued`, `already_recorded` (тот же итог прислан повторно). **409** `lease_lost`: попытка или
  воркер не те, проверки нет, или сервер уже завершил её сам.
