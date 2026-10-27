# judgectl — консоль владельца сервера

Код: [`backend/app/judgectl/`](../backend/app/judgectl). Решения: Р2, Р33–Р36 в [`MODULE_4A.md`](MODULE_4A.md);
как устроены аккаунты, пароли и 2FA — [`AUTH.md`](AUTH.md).

## Запуск и права

judgectl работает через сервисный слой **прямо с БД**, не через HTTP. Аутентификация — доступ к ОС: возможность
запустить его от `judge` с `/etc/judge/backend.env` (там `DATABASE_URL` и `JUDGE_SECRET_KEY`).

```bash
# на установке (функция j из docs/DEPLOY_WSL2.md, §3)
j() { sudo -u judge bash -c 'set -a; . /etc/judge/backend.env; set +a; cd /opt/judge; PYTHONPATH=/opt/judge:/opt/judge/backend "$@"' _ "$@"; }
j .venv/bin/python -m app.judgectl queue status

# разработчик, база judge_test
PYTHONPATH=.:backend python -m app.judgectl --help
```

* **Каждая команда — одна транзакция**: либо всё, либо ничего. Пока человек вводит пароль или код, транзакция не
  открыта.
* **Аудит:** каждое изменение пишется в `audit_log` с `actor_cli = cli:<unix-user>` (обычно `cli:judge`), а имя
  человека из `SUDO_USER` — в `details.sudo_user`. Пароли и токены в аудит не попадают.
* **Вывод:** таблица для человека; `--json` (после имени команды: `judgectl admin list --json`) — для скриптов.
  Пароли и токены печатаются **один раз**; пачка паролей — только в новый файл 0600 (или явно в stdout).
* **Коды выхода:** 0 — успех; 1 — ошибка (`judgectl: <сообщение>` в stderr, ничего не изменено); 2 — неверные
  аргументы; 130 — прервано (Ctrl-C / конец ввода).
* Команды, где вводится пароль или код 2FA (`admin add`, `admin reset-password`, `admin reset-2fa`), требуют
  терминал и без него отказывают, ничего не спросив. `admin add` и `admin reset-2fa` без рабочего
  `JUDGE_SECRET_KEY` отказывают тоже до первого вопроса.

## Группы и команды

```
judgectl admin  add LOGIN --name NAME | list | disable LOGIN | enable LOGIN
                | reset-password LOGIN | reset-2fa LOGIN | unlock LOGIN
judgectl user   add (--surname S --first-name F | --login L) [--name NAME]
                | import FILE --out (NEW_FILE | -) | list [--all] | reset-password LOGIN
                | disable LOGIN | enable LOGIN | unlock (LOGIN | --ip ADDR)
                | set-login LOGIN NEW_LOGIN | qr-token LOGIN [--no-qr]
judgectl queue  status | reap | judgements [--last N] | events [--last N]
                | rejudge (--submission ID | --contest-problem ID [--verdicts WA,TL])
judgectl worker list | token create NAME [--caps cpu,gpu] | token revoke NAME
```

В 4а.2 добавятся `problem`, `contest`, `submission` (в том числе `submission rejudge` вместо `queue rejudge`) и
общий `status`.

### admin — администраторы (только здесь, Р2)

| Команда | Что делает |
|---|---|
| `add LOGIN --name NAME` | Пароль вводится дважды без эха (≥ 12 символов, правила — [`AUTH.md`](AUTH.md) §3; 3 попытки, потом отказ без изменений). Админ создаётся, затем подключается 2FA: QR в терминале + строка `otpauth://…`, код из приложения (3 попытки). Если код так и не подтверждён, админ остаётся с `2FA = pending`, войти не может, команда завершается с кодом 1 и подсказкой `reset-2fa` |
| `list` | Логин, имя, активен ли, **состояние 2FA** (`confirmed` / `pending` — секрет выдан, код не подтверждён / `none`), последний вход и IP, блокировка до |
| `reset-2fa LOGIN` | Новый секрет с QR, подтверждение кодом; все сессии админа отзываются. Это же — **повтор неподтверждённого подключения** |
| `reset-password LOGIN` | Новый пароль дважды без эха; сессии отзываются, блокировка снимается |
| `disable` / `enable LOGIN` | Отключение отзывает все сессии; удаления нет |
| `unlock LOGIN` | Снять блокировку перебора и обнулить счётчик |

Команды группы `admin` отказывают для ученика и наоборот (с подсказкой, какой группой пользоваться).

Код TOTP, которым подтвердили подключение, второй раз не принимается: для первого входа дождитесь следующего кода.

### user — ученики

| Команда | Что делает |
|---|---|
| `add --surname S --first-name F [--login L] [--name NAME]` | Логин — `L` или `фамилия.инициал` (см. [`AUTH.md`](AUTH.md) §2), имя в рейтинге — `NAME` или «Имя Ф.». Сгенерированный пароль печатается один раз |
| `import FILE --out NEW_FILE` | Пачка из CSV (ниже) |
| `list [--all]` | Активные ученики (`--all` — и отключённые), последний вход, блокировка |
| `reset-password LOGIN` | Новый сгенерированный пароль (печатается один раз); сессии и QR-токены отзываются, блокировка снимается |
| `disable` / `enable LOGIN` | Отключение отзывает сессии и QR-токены |
| `unlock LOGIN` | Снять блокировку логина — **любого, в том числе несуществующего** (неудачи считаются и для них); для админа отказ с подсказкой `admin unlock` |
| `unlock --ip ADDR` | Снять блокировку IP (например, весь класс за одним школьным адресом); аудит `ip.unlock` |
| `set-login LOGIN NEW_LOGIN` | Сменить логин (по просьбе ученика); формат как у любого логина |
| `qr-token LOGIN [--no-qr]` | Одноразовая ссылка входа на 7 дней (`<JUDGE_PUBLIC_ORIGIN>/login/qr#<токен>`) и её QR; новый токен отменяет прежний. Без `JUDGE_PUBLIC_ORIGIN` печатается только токен |

#### Импорт CSV (`user import`)

* Строки: `фамилия;имя[;логин][;display_name]` — так сохраняет Excel. Кодировка определяется сама: UTF-8 с BOM, UTF-8,
  cp1251 (Excel на Windows по умолчанию); разделитель — `;` или `,`; поля в кавычках допустимы.
* Первая строка — заголовок, если её первая ячейка «фамилия», `surname`, `last_name`, `lastname` или `last name` (в любом регистре); иначе это данные.
  Пустые строки и пустые хвостовые ячейки пропускаются.
* Пустой логин — сгенерировать; пустой display_name — «Имя Ф.». Явные логины создаются первыми, поэтому
  сгенерированный логин не займёт логин, указанный ниже в файле.
* **Атомарно:** сначала разбирается весь файл, и обо **всех** ошибках сообщается сразу с номерами строк (не то
  число полей, нет фамилии или имени, плохой или повторяющийся в файле логин, слишком длинное имя, логин уже занят
  в базе). Если ошибка хоть одна, не создаётся никто.
* **Результат** — новый файл `--out`: создаётся с `O_EXCL` (существующий не перезаписывается) и правами 0600,
  UTF-8 с BOM и `;` (Excel открывает как есть), столбцы `фамилия;имя;логин;пароль;display_name`. Файл создаётся до
  изменений в базе и удаляется, если импорт не прошёл. Пароли больше нигде не хранятся: файл — единственная копия.
  `--out -` печатает то же в stdout (с `--json` — массив объектов).
* Пример: [`deploy/dev/students-30-cp1251.csv`](../deploy/dev/students-30-cp1251.csv) (30 учеников, cp1251).

```bash
j .venv/bin/python -m app.judgectl user import /var/lib/judge/7b.csv --out /var/lib/judge/7b-passwords.csv
```

### queue — очередь проверки

| Команда | Что делает |
|---|---|
| `status` | Снимок загрузки (длина очереди, воркеры онлайн, ожидание); `--json` — ещё и метрики. Все числа из одного момента |
| `reap` | Вернуть в очередь задания потерянных воркеров сейчас, не дожидаясь reaper |
| `judgements [--last N]` | Последние проверки: вердикт, первый провал, вид TL, попытки, воркер, внутренняя ошибка |
| `events [--last N]` | Журнал очереди (регистрации, `released`, `requeued`, `gave_up`; 14 дней) |
| `rejudge --submission ID` | Перепроверить посылку |
| `rejudge --contest-problem ID [--verdicts WA,TL]` | Перепроверить все посылки задачи контеста (или только с этими текущими вердиктами) |

Перепроверки пишутся в аудит (`submission.rejudge`, `contest_problem.rejudge`). Поведение очереди — [`QUEUE.md`](QUEUE.md).

### worker — воркеры

| Команда | Что делает |
|---|---|
| `list` | Воркеры: состояние, возраст heartbeat, текущее задание, ядра, число проверок, capabilities |
| `token create NAME [--caps cpu,gpu]` | Новый токен воркера. Токен — **последняя строка** stdout (скрипты берут его `| tail -1`), показывается один раз |
| `token revoke NAME` | Отозвать живой токен; если такого нет — ошибка |

## Старое имя: `app.admin_cli`

`python -m app.admin_cli …` (синтаксис модуля 3) остаётся алиасом до модуля 6: переводит аргументы, пишет в stderr
`admin_cli is deprecated, use: judgectl …` и выполняет новые команды. На нём держится `deploy/dev/judge-dev admin …`.

| admin_cli | judgectl |
|---|---|
| `create-worker-token NAME [--caps]` | `worker token create NAME [--caps]` |
| `revoke-worker-token NAME` | `worker token revoke NAME` |
| `status` | `queue status` + `worker list` |
| `reap`, `judgements`, `events`, `rejudge …` | `queue reap`, `queue judgements`, `queue events`, `queue rejudge …` |
