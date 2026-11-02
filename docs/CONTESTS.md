# Контесты

Задачи выдаются ученикам только через контесты (Р19); тренировочный архив — контест без даты окончания. Решения —
Р19–Р25, Р35 и §5.6 в [`MODULE_4A.md`](MODULE_4A.md); таблицы — [`DATA_MODEL.md`](DATA_MODEL.md).

| Где | Что |
|---|---|
| [`protocol/schemas/contest.schema.json`](../protocol/schemas/contest.schema.json) | JSON Schema для `contest.yaml` |
| [`backend/app/contests/spec.py`](../backend/app/contests/spec.py) | разбор `contest.yaml` и правила, которых нет в схеме |
| [`backend/app/contests/service.py`](../backend/app/contests/service.py) | `apply`, `pin`, состояния, коды приглашения, вступление, личный старт |
| [`backend/app/contests/access.py`](../backend/app/contests/access.py) | правила доступа — чистые функции (Р25) |
| [`contests-examples/`](../contests-examples) | примеры: архив-тренировка, олимпиада с личным таймером, урок с заморозкой |

Из веба — `PUT /api/admin/contests/<slug>` с тем же файлом в виде JSON ([`ADMIN_API.md`](ADMIN_API.md) §5.3).
Команды — `judgectl contest …` ([`JUDGECTL.md`](JUDGECTL.md)): `apply` (файл или stdin, `--dry-run`), `show` (с
кодом приглашения), `publish`, `archive`, `pin … --rejudge | --no-rejudge`, `code regenerate`, `participants`.
Посылку «как ученик» до API 4б делает `dev_seed --contest <slug> --label <метка>` (только dev): вступает с кодом
контеста, нажимает «Начать» в личном контесте и проходит `can_submit`; отказ правил останавливает его с причиной.

## 1. `contest.yaml`

Контест описывается файлом, его можно хранить в git; `apply` приводит контест к файлу и повторно ничего не меняет (Р35).

```yaml
format_version: 1
slug: olymp-7b-autumn
title: "Осенняя олимпиада, 7Б"
description: |
  Три задачи, 90 минут с момента нажатия «Начать».
starts_at: 2026-11-10 09:00        # без смещения — время JUDGE_TIMEZONE
ends_at: 2026-11-10 18:00
timing_mode: personal
personal_duration: 1h30m
scoring_mode: icpc
leaderboard: after_end
access: {mode: code}
languages: [python, cpp]
submit_cooldown_sec: 30
problems:
  - {label: A, problem: sum-of-two, version: 1}
  - {label: B, problem: pair-with-sum}
  - {label: C, problem: guess-number, points: 150}
```

| Поле | По умолчанию | Смысл |
|---|---|---|
| `slug`, `title` | обязательно | идентификатор (`[a-z0-9-]`) и название |
| `description` | пусто | Markdown для участников |
| `starts_at` | обязательно | начало; ISO 8601, без смещения — в `JUDGE_TIMEZONE`, `Z` или `+07:00` — как написано |
| `ends_at` | нет | конец; без него контест бессрочный (архив-тренировка) |
| `timing_mode` | `fixed` | `fixed` — общее окно `[starts_at, ends_at)`; `personal` — личный таймер |
| `personal_duration` | — | только и обязательно при `personal`: `90m`, `1h30m`, `2h`; больше нуля |
| `freeze_at` | нет | заморозка таблицы; нужен `ends_at`, внутри окна, не при `leaderboard: hidden`, не при `personal` |
| `scoring_mode` | `points` | `points` или `icpc`; `icpc_penalty_minutes` (20) — штраф за попытку |
| `leaderboard` | `hidden` | `hidden`, `visible`, `after_end` (после `ends_at`) |
| `access.mode` | `code` | `open` — кнопкой; `code` — по коду приглашения (группы — 4а.3a, этап 3). Ключ `join_policy` из 4а.2 больше не принимается, ошибка подсказывает замену |
| `allow_upsolving` | `true` | дорешивание после окна (посылки принимаются, в зачёт не идут) |
| `languages` | все | список разрешённых языков из включённых в системе |
| `submit_cooldown_sec` | 15 | пауза между посылками одного ученика |
| `max_submissions_per_problem` | нет | лимит зачётных посылок на задачу |
| `tests_after_end` | `false` | после окончания показать ученикам все тесты |
| `plagiarism_enabled` | `false` | флаг для будущего хука антиплагиата |
| `problems` | обязательно | `label` (`A`, `B1`; `[A-Z][A-Z0-9]{0,7}`), `problem` (slug), `version` (необязательно), `points` (переопределение итога задачи; баллы групп масштабируются пропорционально) |

В файле нет состояния контеста и кода приглашения: их меняют команды (`publish`, `archive`, `code regenerate`),
иначе повторный `apply` откатывал бы публикацию, а код попадал бы в git. Даты YAML читаются как текст, кавычки не нужны.
Все ошибки файла выдаются разом.

## 2. `apply`

* Новый контест создаётся в состоянии `draft`; в режиме `code` сразу получает код приглашения.
* Задача в контесте определяется **по задаче**, метка — её изменяемый атрибут: метки можно переставлять и менять.
* **Версия.** `version` указана — закрепляется она (только `ready`). Не указана — остаётся текущее закрепление, а у
  новой задачи берётся последняя `ready`. Если указанная версия отличается от закреплённой, а посылки по задаче уже
  есть, `apply` отказывает и подсказывает `judgectl contest pin <контест> <метка> <версия> --rejudge` (или
  `--no-rejudge`): смена версии с посылками — явное решение (Р22). Без посылок `apply` меняет версию сам.
* **Задача убрана из файла:** если по ней есть посылки — строка становится скрытой (`hidden`: ученики её не видят, в
  таблице её нет, посылки сохраняются), иначе удаляется.
* **Задача вернулась в файл:** снова видимой становится **последняя** скрытая строка этой задачи, если её версия
  совпадает с версией в файле (или версия в файле не указана); иначе создаётся новая строка, скрытые остаются
  скрытыми со своими посылками.
* Задача в архиве, задача без `ready`-версии, несуществующая версия, неизвестный язык — ошибка; все ошибки разом, и
  при ошибке не меняется ничего.
* **Предупреждения** — не ошибки: при смене режима доступа с `open` на `code` снова начинает действовать код, который
  был у контеста раньше (при `open` он хранится, но не используется). `apply` об этом предупреждает и подсказывает
  `judgectl contest code regenerate <контест>`, если старый код мог разойтись.
* Каждое изменение — строка в отчёте и в аудите `contest.apply`; без изменений аудит не пишется.

`pin` — сменить закреплённую версию задачи (только `ready`); с `--rejudge` все посылки задачи перепроверяются на новой
версии (новые проверки, история сохраняется, [`QUEUE.md`](QUEUE.md)). Аудит `contest.pin`.

## 3. Состояния, код, вступление, старт

* `draft` — видит только админ; `published` — видят ученики; `archived` — только для чтения и только участникам.
  `publish` / `archive` (аудит `contest.publish` / `contest.archived`).
* **Код приглашения** — 6 символов из `ACDEFGHJKMNPQRTUVWXY3479` (без похожих 0/O, 1/I/L, 2/Z, 5/S, 8/B), вводится
  в любом регистре, пробелы и дефисы игнорируются. `code regenerate` выдаёт новый (аудит `contest.code_regenerate`).
* **Вступление** (Р23): только опубликованный контест; `open` — без кода, `code` — с кодом; после окончания — только
  при `allow_upsolving`. Отдельно от личного старта.
* **Личный старт** (Р21, `personal`): один раз, внутри `[starts_at, ends_at)`; личное окно — `[started_at,
  min(started_at + personal_duration, ends_at))`. Без `ends_at` — тренировка на время: начать можно в любой день после
  `starts_at`.

## 4. Доступ (Р24, Р25)

Чистые функции в `access.py`; эндпоинты 4б и админского API вызывают только их. Админ может всё (его посылки не
идут в зачёт). Для ученика:

| Функция | Правило |
|---|---|
| `contest_window_for` | `fixed`: `[starts_at, ends_at)`; `personal`: личное окно после старта, до старта — нет окна |
| `can_view_contest` | `published`; `archived` — только участнику; `draft` — никогда |
| `can_join` | см. §3; ответы: `not_published`, `already_joined`, `contest_over`, `wrong_code` |
| `can_start` | см. §3; ответы: `not_personal`, `not_participant`, `not_started_yet`, `contest_over`, `already_started` |
| `can_view_problem` | участнику, задача не скрыта, его окно началось (после конца — при дорешивании) |
| `can_submit` | см. ниже |
| `can_see_leaderboard` | участнику: `visible` — всегда, `after_end` — после `ends_at`, `hidden` — никогда |
| `leaderboard_cutoff` | с `freeze_at` до `ends_at` ученик видит таблицу на момент заморозки, админ — живую; после конца — живая |
| `visible_tests` | содержимое только открытых групп; все — при `tests_after_end` после `ends_at` |

`can_submit` проверяет по порядку: состояние `published` (`not_published`), участие (`not_participant`), скрытость
задачи (`problem_hidden`), язык (`language_not_allowed`), время — до окна `not_started_yet`, личный контест без старта
`press_start`, в окне — посылка **в зачёт**, после окна — **дорешивание** (не в зачёт) или `contest_over` без
`allow_upsolving`; затем лимит зачётных посылок (`submission_limit`, на дорешивание не действует) и пауза (`cooldown` с
`retry_after_s`). Таблица случаев — `tests/test_contest_access_units.py`.
