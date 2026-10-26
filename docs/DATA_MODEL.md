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
audit_log        — кто, что и откуда делал
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
- Сессии и токены доступа: появятся в модуле API и авторизации.

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
