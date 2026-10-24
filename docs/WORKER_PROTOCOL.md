# Протокол воркер ↔ бэкенд (внутренний API, версия 1)

Базовый путь `/internal/worker/v1`, JSON. Внутренний API слушает **отдельный порт** (`127.0.0.1:8001`) и не публикуется
через Cloudflare Tunnel; запросы с заголовками `cf-ray` / `cf-connecting-ip` дополнительно отклоняются.
Воркер знает только URL и токен и не ходит в БД, поэтому его можно вынести на другую машину (тогда нужен VPN/LAN и firewall; токен даёт доступ ко всем тестам).

Заголовки: `Authorization: Bearer <token>`; после регистрации ещё `X-Worker-Id`, `X-Worker-Instance`.
Токен хранится в БД как sha256, один токен на машину, у него есть список разрешённых `capabilities`.

| Запрос | Назначение |
|---|---|
| `POST /register` `{name, capabilities, info}` → `{worker_id, instance}` | создаёт/перехватывает воркера по имени, новый `instance`; возвращает прошлые задания в очередь |
| `POST /claim` → `{job: {...} \| null}` | следующее задание в справедливом порядке с учётом `capabilities` |
| `POST /heartbeat` `{state, judgement_id, attempt, phase, tests_done, tests_total, stats}` → `{cancel}` | жив / прогресс; `cancel=true`: задание отобрано |
| `GET /problem-versions/{id}/manifest` → `{manifest_sha256, manifest}` | описание задачи, файлы по sha256 |
| `GET /blobs/{sha256}` | содержимое файла (потоком); воркер сверяет хеш |
| `POST /judgements/{id}/result` | результат; повтор безопасен |
| `POST /judgements/{id}/release` `{attempt, reason}` | вернуть задание (остановка), не считается провалом |
| `POST /deregister` | воркер остановился |

Коды: 401 токен неверен/отозван; 403 чужое имя/токен не разрешает capability; **409** `superseded` (зарегистрировался другой
процесс с этим именем) или `lease_lost` (результат от чужой/старой попытки); 422 результат не сходится с версией задачи.

## job (ответ claim)

```json
{"judgement_id": 12, "attempt": 1, "reason": "initial", "submission_id": 7,
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
 "tests": [{"test_case_id": 101, "index": 1, "verdict": "OK", "time_ms": 12, "memory_kb": 3100, "exit_code": 0, "signal": null,
            "message": "", "stderr_excerpt": "", "output_excerpt": "…", "visible_to_student": true}],
 "timing": {"fetch_ms": 40, "judge_ms": 900}}
```
`visible_to_student` вычисляет воркер тем же `runner.explain.student_view`, который уже проверен тестами раннера (одна логика, а не две).
Вердикт `IE` бэкенд повторяет до `max_attempts`, потом фиксирует.
