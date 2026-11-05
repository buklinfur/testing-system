# Тестовые пакеты проверки задач (сессия 4а.4)

Пакеты для проверки версий задач воркером ([`docs/WORKER.md`](../../docs/WORKER.md), «Проверка версии задачи»).
Здесь только намеренно сломанные или учебные случаи; исправные примеры — в `problems-examples/`.

| Пакет | Что внутри | Итог проверки |
|---|---|---|
| `check-verdicts` | эталоны OK, WA, TL (бесконечный цикл), RE (код выхода 3), CE (C++ с синтаксической ошибкой); все ожидания верные | `ready` |
| `check-wrong-expect` | неверное решение и медленное (5 с при лимите 0,5 с) ожидают `OK` | `failed`, `solution_mismatch`: у каждого эталона `expected`, `got`, первый неудачный тест |
| `check-broken-checker` | свой чекер с синтаксической ошибкой | `failed`, `checker_compile_failed`, лог компилятора; эталоны не запускаются |
| `check-broken-interactor` | интерактор с синтаксической ошибкой | `failed`, `interactor_compile_failed`, лог компилятора |

Проверить вручную до импорта: `python -m runner.cli selfcheck tests/problems/<пакет>` (два последних дают IE с
логом компиляции — так раннер показывает несобравшуюся программу жюри). Через воркер: импорт и
`judgectl problem show <slug>@<v>`. Используются в `tests/pg/test_problem_check_e2e_pg.py` и
`tools/smoke-admin-api.sh` (`check-broken-checker`).
