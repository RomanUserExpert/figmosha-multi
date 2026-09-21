# FIX-PLAN — Figmosha после скана «UIR - FM - Controls»

План исправлений по шести режимам отказа из `figmosha-problems/README.md`
(измерения 2026-09-18). Анализ кода на 2026-09-21, версия
`project.VERSION = "2.4.0"`. Ничего не правилось — только чтение.

Ревизия 2: учтены решения пользователя по спорным вопросам (§9). Директива:
**плагин должен работать и не падать на больших файлах и сканах**; спорное
решается в пользу устойчивости, даже ценой совместимости — с миграционным путём.

Все ссылки `file:line` — на файлы в корне `E:\Work\Personal\figmosha multi\`.

Категории:

- **[код]** — правится в этом репозитории (CLI / bridge / plugin).
- **[док]** — платформенное ограничение Figma; ответ — правило, дефолт или предупреждение.
- **[решено]** — было спорным, решение принято, см. §9.

---

## 0. Диагноз: что общего у шести отказов

README называет общей причиной «один синхронный JS-поток, делящий память
вкладки с документом». Как описание платформы — верно. Но по коду общий корень
распадается на **четыре** слоя, и два из них — целиком в нашей власти.

**Слой 1 — платформа (не лечится, обходится).** Один поток; `findAll` /
`findAllWithCriteria` синхронны; разрешённые `mainComponent` не освобождаются;
`importComponentByKeyAsync` для чужого неопубликованного ключа не settle'ится
(`uspec/docs/import-by-key.md`); переключение файла закрывает плагин. Это
режимы 4 и 6 целиком и часть режима 2.

**Слой 1а — legacy-предзагрузка документа (лечится одним полем манифеста).**
`plugin/manifest.json` не объявляет `documentAccess: "dynamic-page"`. Для
legacy-плагина Figma при первом запуске в свежеоткрытом файле **загружает все
страницы и все узлы документа целиком** (20–30 с задержки на старте, потом
мгновенно в той же сессии). Значит 15 страниц «UIR - FM - Controls» оказались
в памяти **не потому, что скан их обошёл, а потому, что плагин запустился**
— до первой команды. Объяснение README «страницы не выгружаются» неточное:
их не выгружали, потому что их предзагрузили. Часть OOM-бюджета тратится
до того, как bridge получил хоть один `POST /exec`, и `dynamic-page` —
единственное, что это убирает. При этом `CLAUDE.md:179` уже пишет
«Dynamic-page documentAccess makes lookups async» — документация описывает
режим, в котором плагин не работает. Подробно — §7.

**Слой 2 — bridge забывает о запросе раньше, чем плагин его закончил.**
Главный исправимый корень режима 1. `exec_handler` (`bridge.py:486-597`)
держит `session.lock` (`bridge.py:68`) и отпускает его в `finally`
(`bridge.py:573`) **в том числе по таймауту** (`bridge.py:559-567`),
одновременно выкидывая запрос из `session.pending` (`bridge.py:561`). Поздний
ответ плагина потом молча отбрасывается (`bridge.py:341-344`). После одного
504 bridge считает файл свободным и шлёт следующий `exec` в плагин, где поток
всё ещё занят. Отсюда:

- «`pending: 0, queued: 0`, а `return 1` таймаутится» — `pending` =
  `len(session.pending)` (`bridge.py:87`, `:97`), запись уже удалена; `queued`
  — число ждущих lock (`bridge.py:69`), а lock свободен.
- «`ran_ms: 100017` при `waited_ms: 0`» — `ran_ms` считается от `t0` момента
  *отправки* в плагин (`bridge.py:550`, `:562`), не от начала исполнения.
- «две большие прогулки одновременно» (README §2) — в плагине нет очереди:
  `figma.ui.onmessage` (`plugin/code.js:583`) — `async` без флага занятости;
  второй `exec` начинает исполняться на ближайшем `await` первого, а его
  `finally` (`plugin/code.js:638-641`) сбрасывает `CURRENT_PRINT` и кэши
  первого.

**Слой 3 — нет примитива «единица работы».** Ни бюджета, ни дедлайна внутри
цикла обхода, ни курсора, ни обрезки на `INSTANCE`, ни раннера с
пред-разбиением. `README.md:124`: «Long operations are not resumable».
Каждый потребитель пишет своё (`scan_cc.py`, `.claude/skills/ds-review/scripts/scan.js`).
Это режимы 2 и 3.

Режим 5 (`--set`) — просто соглашение парсера.

Вывод: тезис README принять, но с поправкой на слой 1а. Порядок отдачи:
слой 2 (дёшево, только `bridge.py`, убирает каскад «504 → второй скрипт →
минуты мёртвого файла → OOM») → слой 1а (одно поле, убирает базовый расход
памяти до первой команды) → слой 3 (дедлайн в цикле, prune, курсор, раннер)
→ слой 1 (документация и дефолты).

---

## 1. Таймаут CLI ≠ таймаут плагина

### Текущее поведение

- CLI: `--timeout/-t`, дефолт 60 с (`figmosha.py:1700`). `_exec`
  (`figmosha.py:190-201`) кладёт `timeout` в тело, дедлайн сокета
  `timeout + 5`. Отмены нет; `_emit` (`figmosha.py:214-233`) печатает и
  возвращает 1.
- Bridge: `clamp_timeout` 1..600 с (`bridge.py:47-49`, `:196-208`). Дедлайн
  включает очередь (`queued_at`, `bridge.py:516`); ожидание lock —
  `bridge.py:520-528`; ожидание ответа — `bridge.py:559-567`. В обоих случаях
  lock освобождается (`bridge.py:573`), `pending` чистится (`bridge.py:561`),
  поздний ответ отбрасывается (`bridge.py:341-344`). Тест закрепляет это:
  `tests/test_bridge.py:184`.
- Plugin: **пути отмены нет.** `await fn(figma, print, HELPERS)`
  (`plugin/code.js:609-613`); ни `AbortSignal`, ни дедлайна, ни флага «занят».
  `ping`/`pong` отвечает **iframe UI** (`plugin/ui.html:116-120`), не главный
  поток; `_incumbent_answers` (`bridge.py:228-256`) использует его только при
  переподключении.
- Документация советует неверное: `CLAUDE.md:207-209` и `README.md:405` на
  504 — «закройте и перезапустите плагин». При переполнении это необязательно
  (поток вернётся) и вредно (новая сессия, §6).

### Предлагаемое исправление

**1a. Bridge помнит «сирот» и никогда молча не шлёт `exec` в занятый плагин. [код, решено]**

- В `Session` — `running: dict rid -> {"t0", "started_at", "caller_left_at"}`:
  запросы, отправленные в плагин и ещё не отвеченные, независимо от того, ждёт
  ли HTTP-клиент. По 504 запись переезжает из `pending` в `running`. Поздний
  `result`/`error` (`bridge.py:341-344`) снимает её. Дисконнект чистит
  `running` вместе с `pending` (`bridge.py:399-403`).
- В `exec_handler` после lock: если `session.running` непуст — ждать его
  очистки в пределах таймаута вызывающего; не дождались — 504
  `busy: earlier script <rid> has been running for N s (its caller gave up
  at M s)`. **Потолка по времени нет** (§9.5): пока плагин не ответил и не
  отключился, сессия `busy`, `running_ms` растёт. Принудительный сброс —
  только явный: `figmosha sessions --reset <sid>` → `POST /sessions/<sid>/reset`
  (очищает `running`, печатает предупреждение, что поток может быть занят).
- `describe()` (`bridge.py:81-90`) получает `busy`, `running_ms`,
  `running_rid`, `orphaned: bool`; `/status` (`bridge.py:621-634`) — `busy`.
  `figmosha sessions` (`figmosha.py:1436`) и `status` (`figmosha.py:255`)
  печатают их; `doctor` (`figmosha.py:1107-1180`) перед round-trip показывает
  `busy` и не советует перезапуск, если поток просто занят.

**1b. Плагин сообщает `started` и держит собственную очередь. [код]**

- `figma.ui.onmessage`: до `new Function` — `{type:"started", id}`; флаг
  `BUSY` и очередь входящих `exec`, строго по одному. Bridge по `started`
  ставит `started_at`; `ran_ms` в 504 становится честным: «dispatched N s
  ago, never started — the plugin is busy with <rid>».
- Старый плагин без `started` — bridge работает без `started_at`.

**1c. Дедлайн внутри цикла обхода, не снаружи. [код, решено]**

Синхронный JS не вытесняется, поэтому дедлайн живёт **в хелперах обхода**:
bridge передаёт в `exec` `deadline_ms`; плагин выставляет `h.deadline`;
`h.walk`, `h.dumpTree`, `h.withFonts`, `find` проверяют бюджет между узлами
и **возвращают частичный результат с курсором** (`{partial: true, visited,
cursor}`), а не держат поток. `h.tick()` — для пользовательских циклов.
Сообщение `cancel` от bridge (по 504 / Ctrl-C) ставит флаг, который
`h.tick()` тоже видит — для скриптов на `await`. Голый `findAll` это не
спасает — потому `find` переводится на `h.walk` (§3a).

**1d. Документация. [док]** В `CLAUDE.md` и `README.md`: «504 не освобождает
поток; смотрите `figmosha sessions` (`busy`, `running_ms`); ждите;
`sessions --reset` — только руками и только если понимаете, что делаете».

### Оценка и риск

- 1a — **S** (bridge; ~50 строк + 4 теста). Риск низкий; меняется поведение
  только после 504. Мёртвый без дисконнекта плагин блокирует файл до явного
  сброса — это принятая цена (§9.5).
- 1b — **S** (plugin + bridge; re-Run у пользователя).
- 1c — **M**. Скрипты без хелперов и без `h.tick()` не останавливаются —
  ожидаемо.
- 1d — **S**.

---

## 2. OOM: бюджет тратится до первой команды, а потом на `mainComponent`

### Текущее поведение

Три расхода, в порядке появления:

1. **Предзагрузка всего документа при запуске legacy-плагина** (слой 1а).
   Все 15 страниц в памяти до первого `exec`. Ни bridge, ни CLI этого не
   видят; в `figmosha doctor` это выглядит как «round trip works».
2. **Живые прокси.** `figmosha find` — `root.findAll(...)` (`figmosha.py:1544`),
   полный массив; `ROW_JS` (`figmosha.py:262-269`) `mainComponent` не трогает
   — хорошо; обрезки на `INSTANCE` нет — `find type=INSTANCE` возвращает и
   вложенные копии. `figmosha tree` **не дешёвый**: `h.dumpTree` при срезе
   вызывает `descendants(n)` (`plugin/code.js:362-366`, `:384-389`) — полный
   обход каждой срезанной ветки ради `… +N deeper`. На фрейме в 53 k нод
   `tree --depth 1` ходит по всем 53 k. «`list` без глубокого обхода» из README
   — это `scan_cc.py`, в Figmosha такого нет.
3. **`getMainComponentAsync` на каждый инстанс** —
   `.claude/skills/ds-review/scripts/scan.js:42`,
   `handoff-spec/scripts/extract.js:55` (обрезают на `INSTANCE`,
   `scan.js:23-29`, но всё поддерево). `props`/`overrides`
   (`figmosha.py:855`, `:298`) — один инстанс, безопасно.

Bridge об OOM не узнаёт: вкладка падает → сокет закрыт → `bridge.py:399-403`
удаляет сессию, in-flight получает 500 `plugin disconnected mid-request`,
следующий — 503 (`bridge.py:453-457`). CLI оба печатает и выходит с 1.

### Предлагаемое исправление

**2a. `documentAccess: "dynamic-page"`. [код, решено]** Убирает расход №1
целиком и делает загрузку страниц управляемой: грузится текущая страница и
те, к которым обратились. План миграции — §7.

**2b. `tree` без глубокого обхода. [код]** Бюджет на `descendants()` (например
2 000 узлов → `… +2000⁺ deeper`) — `tree --depth 1` становится дешёвым списком
детей, на котором строится пред-разбиение (§3).

**2c. `find` не заходит в инстансы по умолчанию. [код, решено]** Ручной обход
через `h.walk` с `pruneInstances: true`; опт-ин `--nested` возвращает старое
поведение. `--count` — счёт без материализации массива. В выводе по умолчанию
одна строка «instances not descended (--nested)», чтобы смена дефолта была
видимой. Миграция: запись в `CHANGELOG.md`, обновить `CLAUDE.md:86`.

**2d. `h.mainOf(node)`. [код]** `getMainComponentAsync` с кэшем на время
exec по `id` компонента и с `h.tick()`; единственный рекомендуемый способ
дойти до main-компонента (§7.3). Не делает резолв дешевле — но делает его
считаемым: `h.mainOf` ведёт счётчик, и `print`-предупреждение после N
резолвов («resolved 5000 main components in this exec — narrow the subtree»)
появляется до OOM, а не после.

**2e. Правила в `CLAUDE.md`. [док]** Раздел «Большие файлы»: не резолвить
`mainComponent` до дешёвого фильтра; обрезать на `INSTANCE`; один тяжёлый
файл за раз; OOM ничего не портит (документ на сервере); `ds-review` /
`handoff-spec` на файлах этого размера — только по поддеревьям.

### Оценка и риск

- 2a — см. §7.
- 2b — **S**, риск нулевой (число становится «≥»).
- 2c — **S/M**. Ломает счётчики в сценариях, которым нужны вложенные — они
  добавляют `--nested`. Принято.
- 2d — **S**.
- 2e — **S**.

Сам OOM в пределах одного скрипта — **[док]**: bridge его не предотвратит;
предотвращают дефолты (2a, 2c) и дисциплина (2e).

---

## 3. Единица работы — поддерево, выбранное до переполнения

### Текущее поведение

- Пред-разбиения, курсора, возобновляемого прогона в репозитории **нет**.
  Единственный примитив — `exec --file … --set ROOT_ID=…`
  (`figmosha.py:1472-1488`). Внешний `scan_cc.py --presplit N` — единственная
  реализация.
- Дешёвого списка детей нет (§2b).
- `--set` не читает из файла; список ключей — строкой в командной строке
  (Windows: ~8 k символов).

### Предлагаемое исправление

**3a. `h.walk(root, visit, opts)`. [код]** Итератор с `pruneInstances`
(дефолт `true`), `deadline` (§1c), `maxNodes`, и **курсором**: при исчерпании
бюджета возвращает `{partial: true, visited, cursor}`; повторный вызов с
`cursor` продолжает. Под `dynamic-page` (§7) перед обходом делает
`await pageOf(root).loadAsync()`. `h.dumpTree` и `find` — на нём.

**3b. `figmosha each <root> --file script.js [--split N] [--state run.jsonl]
[--resume]`. [код, решено — живёт в репозитории]**

1. `--split N` — спуститься на N уровней через дешёвый список детей (§2b),
   собрать листья.
2. Каждому листу — `exec --file script.js --set ROOT_ID=<leaf>`; результат и
   статус — строкой в `--state` **до** следующего листа.
3. 504 / `busy` (§1a) → не повторять, спуститься на уровень ниже, детей в
   очередь; 503 / `plugin disconnected` → `--wait-plugin` (§6b), затем
   продолжить; `--resume` — пропустить сделанное; частичный результат с
   курсором (§3a) → дозапустить с `--set CURSOR=`.
4. `scan_cc.py` переводится на `each`, чтобы третьей копии не было.

**3c. `--set NAME=@path.json`. [код]** Значение из файла.

**3d. Правила в `CLAUDE.md`. [док]** «Никогда весь файл/страницу одним
запросом; спускаться до, а не после; писать состояние после каждой единицы;
длинный прогон — только через `each`».

### Оценка и риск

- 3a — **M** (plugin + `tests/helpers.test.js`). Риск низкий.
- 3b — **L** (подкоманда, файл состояния, три ветки отказа, тесты).
- 3c, 3d — **S**.

---

## 4. `importComponentByKeyAsync` зависает

### Текущее поведение

- `h.importComp` (`plugin/code.js:577`) и `icomp` (`figmosha.py:1686-1694`) —
  голый `await` с дефолтом 60 с.
- Причина разобрана в `uspec/docs/import-by-key.md`: опубликованный ключ —
  быстро; свой неопубликованный — быстрый reject; чужой неопубликованный —
  никогда не settle'ится, таймаут не помогает. Случай README — та же таблица.
- Отличие от §1: **зависший промис не держит поток.** CLI ждёт, плагин
  свободен, `return 1` ответит. Защитный таймаут ничего не ломает.

### Предлагаемое исправление

**4a. Гонка с таймером. [код]** `h.importComp(key, {timeout: 15000})` через
`Promise.race`; по таймауту — ошибка «import of <key> did not settle in 15 s
— the key is unpublished or belongs to another file; resolve from the
consuming side: `(await h.mainOf(inst)).key` — see
uspec/docs/import-by-key.md». То же для `h.importVar`. `icomp` — тот же
хелпер, дефолт `-t 20`.

**4b. Подсказка в bridge. [код]** `ERROR_HINTS` (`bridge.py:120-150`): пара
для «did not settle».

**4c. `CLAUDE.md`. [док]** Рядом с `figma.importComponentByKeyAsync(key)`:
«не для поиска инстансов библиотечного компонента; сравнивайте
`(await h.mainOf(inst)).key` со стороны потребителя».

### Оценка и риск

- 4a — **S**. Ложный таймаут при медленной сети: 15 с против измеренных
  4–907 мс; настраиваемо.
- 4b, 4c — **S**.

Само зависание — **[док]**.

---

## 5. `--set` подставляет JSON как значение, а не как строку

### Текущее поведение

`prelude` (`figmosha.py:1447-1470`): `json.loads(raw)` (`:1465`), при ошибке
— строка (`:1466-1467`). Осознанно (docstring `:1450-1454`), закреплено
`tests/test_cli.py:438-447`, используется в `README.md:276` и
`.claude/skills/ds-review/SKILL.md:63`. Help (`figmosha.py:1742`) и
`CLAUDE.md:21` о JSON-разборе молчат.

### Предлагаемое исправление

**5a. `--set-str NAME=value` (всегда строка) и `--set-json NAME=value` (всегда
JSON, ошибка при неразборе). [код]** `--set` не трогать. Параметр `mode` в
`prelude`, три строки в `build_parser`, три теста.

**5b. Help и документация. [док]** В help `--set`: «JSON when it parses, a
string otherwise; `--set-str` to force a string». В `CLAUDE.md` — та же фраза
и идиома `typeof X === "string" ? JSON.parse(X) : X`.

**5c. `@file` (§3c).** Снимает лимит длины и проблему кавычек в PowerShell.

`NAME:=value` для JSON отклонено: ломает `--set N=12` и `SCALE=[…]` во всех
скиллах без выигрыша.

### Оценка и риск

**S**, риск нулевой.

---

## 6. Плагин привязан к файлу; переключение файла убивает все сессии

### Текущее поведение

- Дисконнект: `bridge.py:399-403` удаляет сессию, in-flight — 500
  `plugin disconnected mid-request` (`_fail_pending`, `bridge.py:216-224`);
  следующий — 503 (`bridge.py:453-457`). CLI (`figmosha.py:214-233`) — код
  выхода 1, как у ошибки скрипта. Ни ожидания, ни повтора.
- Переподключение UI (`plugin/ui.html:128-141`) спасает только от падения
  bridge; закрытое окно плагина — конец `code.js`.
- Re-Run — новая сессия: `SID` генерируется при каждом запуске
  (`plugin/code.js:11`, обоснование `:5-10`). `FIGMOSHA_SESSION=<sid>` после
  re-Run — 409 (`bridge.py:468-473`); `FIGMOSHA_SESSION=<имя файла>` переживает
  (`_match_sessions`, `bridge.py:416-432`). `alias` наследуется только при том
  же `sid` (`bridge.py:277`) и нигде не устанавливается — мёртвое поле.
- Почему падают обе сессии — поведение Figma Desktop, из репозитория не
  управляется; было ли это переключение вкладок или навигация в той же —
  неизвестно, и план устойчив к обоим (§9.6).

### Предлагаемое исправление

**6a. Различимые коды выхода. [код]** `_emit`: 1 — ошибка скрипта; 3 — 503 и
`plugin disconnected mid-request`; 4 — 504 / `busy`. Раннеры перестают гадать.

**6b. `--wait-plugin N` / `FIGMOSHA_WAIT_PLUGIN`. [код]** На 503 (и на 500
`disconnected mid-request`) CLI опрашивает `GET /sessions`
(`bridge.py:600-618`) до появления сессии, совпадающей с целью, до N с, затем
выполняет запрос. `each` (§3b) включает его по умолчанию.

**6c. Стабильный `sid` на файл через `figma.clientStorage`. [код, решено]**
`clientStorage` — хранилище **на плагин и пользователя, не на файл**, поэтому
ключ — имя файла: `clientStorage.getAsync("sid:" + figma.root.name)`; нет —
сгенерировать и сохранить. Это асинхронно, поэтому `identity()`
(`plugin/code.js:13-20`) и ответ на `whoami` (`:586-589`) становятся
`async`; `ui.html` уже ждёт identity до подключения (`plugin/ui.html:56-59`,
`:185-188`), менять там нечего. Коллизия (два разных файла с одним именем в
одном аккаунте) → bridge отвечает 1008 (`_claim_session`, `bridge.py:266-292`);
плагин на 1008 **регенерирует `sid`**, перезаписывает `clientStorage` и
переподключается сразу, вместо ожидания 15 с (`plugin/ui.html:129-136`).
`alias` при этом переживает re-Run (`bridge.py:277` уже так делает для того же
`sid`). Документ не трогается — `clientStorage` не пишет в файл, возражение из
`code.js:5-10` снято.

**6d. Адресация по имени файла — второй путь, не альтернатива. [код/док, решено]**
`figmosha sessions` печатает имя файла первым столбцом; 409
(`figmosha.py:216-221`) советует `--session <имя файла>`; `CLAUDE.md:53`
уже показывает имя — оставить как основной пример.

**6e. `CLAUDE.md`: раздел «Длинный прогон». [док]** Запускать плагин в целевом
файле, держать его в фокусе, работать через `each`, состояние — после каждой
единицы. Формулировка нейтральна к причине потери сессии: «плагин может
исчезнуть при любом переключении файла; `each --wait-plugin` это переживает».

### Оценка и риск

- 6a — **S**. Скрипты с `== 1` — единичны.
- 6b — **S/M** (тест с фейковым плагином, который уходит и возвращается).
- 6c — **S/M**. Риск: одноимённые файлы → один лишний реконнект; и
  `clientStorage` не разделяется между Figma stable и Beta — там будут два
  `sid`, что нормально.
- 6d, 6e — **S**.

Само закрытие плагина — **[док]**.

---

## 7. Миграция на `documentAccess: "dynamic-page"` [код, решено]

### 7.1 Что меняется в манифесте

- `plugin/manifest.json`: `"documentAccess": "dynamic-page"`.
- `_write_manifest` (`figmosha.py:1224-1245`) патчит манифест на месте с
  `setdefault` — добавить `m["documentAccess"] = "dynamic-page"` туда же, чтобы
  `figmosha init` / `figmosha update` (`figmosha.py:1279`) переносили поле во
  все существующие копии. Манифест — в `STAMPED` (`figmosha.py:1262`), так что
  `update` уже умеет его переписать и сказать «re-Import».
- Изменение манифеста требует **re-import** плагина в каждом проекте
  (`CLAUDE.md:210-213`, `:223-225`). `update` печатает именно это.

### 7.2 Что переписать в `plugin/code.js`

Синхронных API, которые под `dynamic-page` бросают, в плагине **нет**:
`getNodeByIdAsync` (`code.js:556`, `:574`), `getVariableByIdAsync` (`:178`),
`getStyleByIdAsync` (`:240`), `getLocal*Async` (`:105-108`, `:208-212`),
`set*StyleIdAsync` через `STYLE_KINDS` (`:207-213`, `:569`),
`getMainComponentAsync` (`:474`). `figma.currentPage` на чтение (`:18`, `:25`,
`:485`, `:550`, `:552`) остаётся валидным.

Ломается **зависимость от загруженности страницы** — всё, что обходит
поддерево узла, полученного по id с другой страницы:

| Место | Что | Правка |
|---|---|---|
| `code.js:549-557` `h.resolve` | возвращает узел с любой страницы | после `getNodeByIdAsync` — подняться до `PAGE` и `await page.loadAsync()`; идемпотентно, для загруженной страницы бесплатно; guard `typeof page.loadAsync === "function"` на переходный период |
| `code.js:574` `h.node` | то же | то же (делегировать в `h.resolve`) |
| `code.js:338-345` `h.findByName` / `h.findAllByName` | `findOne`/`findAll` на незагруженной странице | `async`, `await h.loadPageOf(root)` перед обходом — **меняет сигнатуру** (были sync); в `CLAUDE.md:139` пометить `await` |
| `code.js:353-413` `h.dumpTree` | `n.children` на незагруженной странице | `async`, load перед `walk`; плюс бюджет из §2b |
| `code.js:416-419` `h.withFonts` | `findAll` | уже `async`; добавить load |
| `code.js:362-366` `descendants()` | глубокий обход | §2b |
| новое | `h.loadPageOf(node)`, `h.pages()` (список `figma.root.children` без загрузки), `h.mainOf(node)` (§2d) | добавить; `figma.root.children` под `dynamic-page` доступен без загрузки |

Генерируемый CLI JS: всё идёт через `node_expr` → `h.resolve`
(`figmosha.py:159-161`), так что `tree`, `find` (`figmosha.py:1544`,
`root.findAll`), `props`, `where`, `overrides`, `set`, `bind`, `text`,
`variant`, `clone`, `rm` получают загрузку страницы автоматически. `sel` и
`icomp` работают на `figma.currentPage` — загружена всегда. `doctor`
(`figmosha.py:1171-1172`) читает `figma.root.children.length` — допустимо.

Тесты: `tests/helpers.test.js` и `run_js` в `tests/test_cli.py` стабят
Figma — добавить `loadAsync` на стаб страницы и тест «`h.resolve` грузит
страницу узла».

### 7.3 Что ломается в пользовательских скриптах `exec` (главная ломающая часть)

Это скрипты вне репозитория (`custom-control-usage\`, `migration-to-slots\`,
`corner-radius-binding\`) и любой inline `exec`. Под `dynamic-page` бросают:

- `instance.mainComponent` → `await h.mainOf(instance)` / `getMainComponentAsync()`.
  README §2 прямо говорит, что скан читал `instance.mainComponent` — значит
  `scan_cc.py` ломается первым.
- `component.instances` → `await component.getInstancesAsync()`.
- `figma.getNodeById` → `await h.node(id)`.
- `figma.getStyleById`, `figma.variables.getVariableById` /
  `getVariableCollectionById`, `figma.getLocal{Text,Paint,Effect,Grid}Styles`,
  `figma.variables.getLocalVariables` / `getLocalVariableCollections` →
  `*Async` (или `h.var_`, `h.style_`).
- `node.fillStyleId = …` и родня → `await node.setFillStyleIdAsync(…)` /
  `h.applyStyle`.
- `figma.currentPage = page` → `await figma.setCurrentPageAsync(page)`.
- `style.consumers` → `await style.getStyleConsumersAsync()`.
- `figma.root.findAll` / `findOne` / `findAllWithCriteria` → сначала
  `await figma.loadAllPagesAsync()` (что возвращает legacy-расход памяти —
  в `CLAUDE.md` пометить как «последнее средство») или обход по
  `figma.root.children` + `page.loadAsync()` по одной странице —
  рекомендуемый путь для сканов, и именно то, что даёт `each` (§3b).
- `page.children` / `page.findAll` для не-текущей страницы →
  `await page.loadAsync()` первым.
- `figma.on("documentchange")` → `page.on("nodechange")` /
  `figma.on("stylechange")`. В репозитории не используется.

Шим для мягкого перехода: **`h.mainOf(node)`** (§2d) + подсказка в
`ERROR_HINTS` (`bridge.py:120-150`): текст ошибки Figma при синхронном вызове
под `dynamic-page` упоминает `documentAccess` и `*Async` — по этой подстроке
bridge отвечает: «this plugin runs with documentAccess: dynamic-page —
replace `.mainComponent` with `await h.mainOf(n)`, `getNodeById` with
`await h.node(id)`, `findAll` on another page with `await h.loadPageOf(n)`
first; see CLAUDE.md → Dynamic pages». Так каждый сломанный скрипт сам
называет свою правку при первом запуске.

### 7.4 `uspec/`, скиллы, документация

- `uspec/extract/extract.py:152,311`, `uspec/templates/capture.js:21`,
  `replay.js:18` — уже `loadAllPagesAsync()`: работают без правок, но грузят
  весь документ; допустимо для файла с одним компонент-сетом, отметить в
  `uspec/README.md`.
- `uspec/templates/verify-all.py:72,81` — `p.findAll` по каждой странице →
  `await p.loadAsync()` перед. **S.**
- `uspec/apply-adapter.py:154` — `cell.findOne` внутри созданного фрейма на
  текущей странице — работает.
- `uspec/adapter.md:41` упоминает `figma.root.findAll(…)` — добавить
  «после `loadAllPagesAsync`».
- `.claude/skills/*/scripts/*.js` — корень через `h.node(ROOT_ID)` или
  `figma.currentPage.selection[0]`; с правкой `h.resolve` работают без
  изменений. `SKILL.md` ds-review / handoff-spec — добавить «на больших файлах
  — по поддеревьям через `each`».
- `CLAUDE.md`: раздел «Conventions → Async APIs» (`:179-185`) становится
  правдой; добавить подраздел «Dynamic pages»: таблица sync → async из §7.3,
  `h.loadPageOf`, `h.pages`, `h.mainOf`, `loadAllPagesAsync` как последнее
  средство. Строка `:63` («`getNodeByIdAsync` (`loadAllPagesAsync` first)») —
  переписать: `loadAllPagesAsync` больше не нужен для доступа по id.
- `README.md:387,391` — те же правки. `CHANGELOG.md` — запись «Breaking».

### 7.5 Переходный период: можно ли держать оба режима

Режим — свойство манифеста, а манифест — на копию проекта. Значит **оба
режима сосуществуют естественно, по проектам**: копия с `dynamic-page` и
копия без него — разные плагины в меню Figma. Внутри одной копии оба режима
одновременно невозможны.

Предлагаемый путь:

1. Код плагина (§7.2) пишется **совместимым с обоими режимами**: `loadAsync`
   под guard, `h.mainOf` работает и в legacy. Это первый шаг, без манифеста;
   после него любая копия может перейти в любой момент.
2. `figmosha init --document-access legacy|dynamic-page` (дефолт
   `dynamic-page`), значение — в `project.json`. `_write_manifest` пишет его.
   Проект с внешними скриптами, которые ещё не переписаны, ставит `legacy`
   явно, и `doctor` печатает `documentAccess: legacy (deprecated — see
   CLAUDE.md → Dynamic pages)`.
3. `doctor` под `dynamic-page` показывает, сколько страниц загружено
   (`figma.root.children.length` против числа с `loadAsync` уже сделанным — по
   документации у `PageNode` нет публичного флага загруженности; считать по
   собственному журналу `h.loadPageOf`), чтобы OOM-бюджет был виден до
   скана.
4. Первый проект на `dynamic-page` — тот, где идёт скан
   `custom-control-usage\` (`scan_cc.py` переводится на `each` + `h.mainOf`
   в том же заходе). После него — дефолт для всех.
5. Через один релиз `legacy` остаётся как флаг, но `CHANGELOG` объявляет его
   устаревшим; Figma требует поле для всех новых публикуемых плагинов с
   апреля 2024 — направление одно.

### 7.6 Оценка и риск

- Манифест + `_write_manifest` + `init --document-access` — **S**.
- `code.js` (§7.2: `h.resolve`/`h.node` с load, `findByName`/`findAllByName`/
  `dumpTree` async, `h.loadPageOf`, `h.pages`, `h.mainOf`, hint) — **M**.
- `uspec/` + скиллы + `CLAUDE.md`/`README.md`/`CHANGELOG.md` — **S**.
- Внешние скрипты — **зависит от их числа**; шим и hint сводят каждую
  поломку к одной строке правки при первом запуске.

Риски: (1) внешние скрипты с `.mainComponent` — ломаются сразу и громко, с
подсказкой; принято. (2) `h.findByName`/`h.findAllByName` меняют сигнатуру на
`async` — скрипты без `await` получат Promise вместо узла, молча; смягчение:
в legacy-режиме тоже вернуть Promise (одно поведение в обоих режимах),
предупредить в `CHANGELOG`, и в hint на «is not a function» / «undefined»
рядом с `findByName` упомянуть `await`. (3) `getNodeByIdAsync` на узел
незагруженной страницы — по документации узел возвращается, но обход требует
`loadAsync`; правка `h.resolve` закрывает это для всего CLI. (4) Один
re-import на проект — разово, `update` подсказывает.

---

## 8. Приоритеты

Порядок — по «предотвращённый класс отказов на единицу усилий», с поправкой
на директиву «не падать на больших файлах».

| # | Что | Из | Усилие | Что предотвращает / почему здесь |
|---|-----|----|--------|-----------------------------------|
| 1 | Bridge: `running`, `busy`/`running_ms` в `/sessions` и `/status`, никакого молчаливого диспатча в занятый плагин, `sessions --reset` | §1a | S | Каскад «504 → второй скрипт в занятом потоке → минуты мёртвого файла / OOM». Только `bridge.py`, без re-Run и re-import у пользователей — поэтому **перед** `dynamic-page`: даёт эффект сегодня, ничего не ломает, и без него `dynamic-page` не спасает от второго скрипта в потоке |
| 2 | `dynamic-page`: код плагина совместимый с обоими режимами (`h.resolve` + `loadAsync`, `h.loadPageOf`, `h.pages`, `h.mainOf`, async `findByName`/`dumpTree`), hint в bridge, манифест, `init --document-access`, `CLAUDE.md` «Dynamic pages» | §7 | M | Единственное, что убирает предзагрузку всего документа до первой команды — самый большой рычаг по памяти. Второе, а не первое, потому что требует re-import во всех проектах и ломает внешние скрипты; шаг 1 к тому моменту уже защищает от каскада |
| 3 | Плагин: `started`, `BUSY`, очередь; `deadline_ms` в `exec`, `h.tick()`, `h.walk` с `pruneInstances`/`deadline`/курсором и частичным результатом | §1b, §1c, §3a | M | Переплетение скриптов на `await`; честный `ran_ms`; дедлайн **внутри** цикла обхода вместо удержания потока. Идёт в одном re-Run с шагом 2 |
| 4 | `find` без спуска в инстансы (`--nested` опт-ин), `--count`; `tree` с бюджетом на `descendants()` | §2b, §2c | S | Живые массивы из 53 k прокси; скрытый полный обход в «дешёвом» инструменте; открывает пред-разбиение |
| 5 | S-пакет: `--set-str`/`--set-json`/`@file`; таймер в `h.importComp`/`h.importVar`, `icomp -t 20`, hint; коды выхода 1/3/4; `--wait-plugin` | §5a, §3c, §4a, §4b, §6a, §6b | S | Ошибки типа в каждом новом скрипте; 60–180 с на чужом ключе; раннеры, не отличающие «плагин исчез» от «скрипт упал» |
| 6 | Стабильный `sid` через `clientStorage`, регенерация на 1008; имя файла первым столбцом в `sessions` | §6c, §6d | S/M | `FIGMOSHA_SESSION`/`alias`, не переживающие re-Run; 15 с ожидания на коллизии |
| 7 | `figmosha each --split/--state/--resume`; перевод `scan_cc.py` на него | §3b | L | Третья копия раннера; прогон, который нельзя продолжить. После 3–6, потому что опирается на `busy`, коды выхода, `--wait-plugin`, дешёвый `tree`, курсор |
| 8 | Документация: `CLAUDE.md` «Большие файлы», «Длинный прогон», «Dynamic pages»; исправить совет про 504; `--set`; `importComponentByKeyAsync`; `uspec/`, `SKILL.md`; `CHANGELOG` «Breaking» | §1d, §2e, §3d, §4c, §5b, §6e, §7.4 | S | Повтор всех шести ошибок следующим агентом. Не зависит от кода — может идти параллельно с любым шагом; разделы про `dynamic-page` и `find` публикуются вместе с шагами 2 и 4 |

Шаги 1, 4, 5 — независимы и все S; их можно сделать одной итерацией до
шага 2. Шаги 2 и 3 — один re-Run + один re-import у пользователей, потому
лучше выпускать вместе. Шаг 7 — последний из кода, замыкает всё.

---

## 9. Принятые решения

1. **`documentAccess: "dynamic-page"` — принят.** Факты: под `dynamic-page`
   страницы грузятся по обращению (`PageNode.loadAsync()` перед `children` /
   `findAll` / `findOne` / `appendChild` / export); `DocumentNode.findAll` /
   `findAllWithCriteria` / `findOne` требуют `figma.loadAllPagesAsync()`,
   `findChild` / `findChildren` — нет; синхронные API бросают (список в §7.3);
   `documentchange` недоступен без `loadAllPagesAsync`, замены —
   `PageNode.on('nodechange')`, `figma.on('stylechange')`; поле обязательно для
   всех новых публикуемых плагинов с апреля 2024. **Главное:** legacy-плагин
   при первом запуске в свежеоткрытом файле получает от Figma гарантию, что
   весь файл загружен (20–30 с) — то есть 15 страниц загрузил не скан, а
   запуск плагина. План: §7; в §0 и §2 диагноз исправлен.

2. **`find` не спускается в инстансы по умолчанию;** старое поведение —
   явный `--nested`. Основание: 0 из 339 попаданий были вложенными, цена
   пруна нулевая, выигрыш — порядки. §2c.

3. **`sid` стабилен на файл (`figma.clientStorage`, ключ — имя файла) и
   адресация по имени файла — оба пути.** Резюмирование переживает повторный
   Run. §6c, §6d.

4. **Раннер с пред-разбиением живёт в репозитории** (`figmosha each`);
   `scan_cc.py` переводится на него. Резюмируемость — часть продукта. §3b.

5. **`running` без потолка:** никогда молча не диспатчить в занятый плагин;
   держать `busy`, отдавать `running_ms`; сброс — только явный,
   `figmosha sessions --reset <sid>`. §1a.

6. **Режим 6 — устойчиво к обоим сценариям** (переключение вкладки и
   навигация в той же): детект потери сессии, код выхода 3, `--wait-plugin`,
   резюмируемость через `each`. Причину не выясняем. §6.

7. **Дефолт `--timeout 60` остаётся;** дедлайн живёт **внутри** цикла обхода
   (`h.walk` и родня проверяют бюджет между шагами и возвращают частичный
   результат с курсором), потому что синхронный JS не вытесняется. §1c, §3a.

Общая директива ко всем спорным местам: **устойчивость на больших файлах
важнее обратной совместимости, но у каждого слома есть миграционный путь** —
hint в bridge, шим `h.mainOf`, `init --document-access legacy` на переходный
период, запись «Breaking» в `CHANGELOG.md`.
