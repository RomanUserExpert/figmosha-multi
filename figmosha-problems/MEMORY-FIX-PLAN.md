# MEMORY-FIX-PLAN — потолок памяти вкладки на тяжёлом файле

Анализ 2026-09-24 по прогону `custom-control-usage\v3-2026-09-24\` (Figmosha 3.0.1,
`documentAccess: "dynamic-page"`, `h.walk` с обрезкой на `INSTANCE`, `h.mainOf`,
`each --split 2 --state --resume`). Только чтение и план; в репозитории ничего не
правилось. Ссылки `file:line` — на `E:\Work\Personal\figmosha multi\`, если не
сказано иначе. Данные прогона — `E:\Work\Compatibl\related-projects\custom-control-usage\v3-2026-09-24\`.

---

## 1. Проблема

**Что падает.** Read-only скан 15-страничного файла «UIR - FM - Controls» три раза
подряд убивает вкладку Figma Desktop сообщением *«This file has run out of browser
memory»*. Плагин исчезает из `figmosha sessions`, `each --resume` продолжает после
переоткрытия файла. Прогон — не одна сессия вкладки, а три с половиной.

**Ключевые числа** (из `memory-ceiling-2026-09-24.md` и файлов состояния прогона):

| Что | Число | Откуда |
|---|---|---|
| Лимит памяти вкладки | **2 ГБ**, действует и в Desktop-приложении | help.figma.com «Reduce memory usage in files» (§2, F1) |
| `getNodeByIdAsync` на незагруженную страницу | 11.3 с | memory-ceiling §«Where the time…» |
| `loadAsync` страницы 10 (14 детей, 81 табличный экран) | **< 1 с** (`warm` wall 938 мс, `ms: 0`) | `data/events.jsonl:31`, `run.log:28` |
| Один юнит страницы 10 (6–9 узлов, 5–8 `mainOf`, ключи уже прогреты) | **3.3–5 с**, лёгкие юниты без инстансов 15–125 мс | `data/page-10.jsonl` (81 записей) |
| Первый `mainOf` на ключ / повторный | 9.0 с / 0–1 мс | memory-ceiling |
| Страница 2 холодная / тёплая | 175 с / 1 с | `events.jsonl:2`, memory-ceiling |
| Сессия 1 до OOM | стр. 0–9 (≈283 с юнит-времени) + 9 тяжёлых юнитов стр. 10 (79 с) ≈ **362 с** | `page-*.jsonl`, `events.jsonl` |
| Сессия 2 до OOM | записи 11–76 `page-10.jsonl` = **66 юнитов, ≈214 с** (в memory-ceiling написано «1 + 25» — по файлу состояния это 66: следующая сессия пишет `5 ok, 75 skipped`, `events.jsonl:38`) | `page-10.jsonl`, `events.jsonl:36-38` |
| Сессия 3 до OOM | 5 юнитов стр. 10 (51 с) + 64 юнита стр. 11 (326 с) ≈ **377 с** | `page-10.jsonl:77-81`, `page-11.jsonl` |
| `mainOf` за весь прогон | несколько сотен, не 876 849 | memory-ceiling |

Вывод из таблицы, которого в memory-ceiling нет явно: **загрузка страницы дешёвая,
дорогое — первое касание каждой секции**. `loadAsync` страницы 10 — меньше секунды,
а каждый её юнит — 3–5 секунд при 6–9 посещённых узлах и уже прогретых ключах
компонентов. То есть содержимое инстансов (таблицы) не приходит вместе со страницей,
а **гидратируется в момент, когда скрипт касается инстанса**, и остаётся в куче
движка до закрытия файла. Три сессии умерли при ≈360 / 214 / 377 с накопленного
юнит-времени — это и есть грубая мера «сколько гидратированного контента влезает в
2 ГБ» (сессия 2 меньше: файл переоткрыли на тяжёлой странице 10, и она отрисовалась
во вьюпорте).

**Почему предыдущий `FIX-PLAN.md` не решил это.** Он искал память в трёх местах
(§0, §2):

| Что предполагал FIX-PLAN | Что показал прогон 2026-09-24 |
|---|---|
| Слой 1а: legacy-плагин предзагружает все 15 страниц — «большая часть OOM-бюджета до первой команды» (`FIX-PLAN.md:35-44`, `:168-171`) | Верно, но недостаточно. `dynamic-page` внедрён (`plugin/manifest.json:8`), страницы грузятся по требованию — и **сама страница дешёвая**. Бюджет съедает не список страниц, а гидратация секций при касании |
| §2 п.2–3: число `mainComponent`-резолвов (876 849) и массивы живых прокси | Резолв стоит **на ключ**, не на вызов (9 с / 0–1 мс). Сотни вызовов, 6–9 узлов на юнит — и всё равно OOM |
| §2b/§3: «меньше юнит — меньше памяти» | Размер юнита меняет, сколько делает один запрос, а не сколько держит вкладка в конце (memory-ceiling §«What does not help») |
| Подразумевалось: то, что загружено плагином, можно ограничить плагином | В Plugin API **нет выгрузки** ни страницы, ни компонента (§2, F3). Всё, чего скрипт коснулся, живёт до закрытия файла |

Итого 3.0 сделал ровно то, что обещал: поток не блокируется, прогон продолжаемый,
предзагрузки нет. Но любая архитектура, в которой плагин **должен коснуться каждого
экрана**, упирается в 2 ГБ кучи документа примерно на 60–70 табличных экранах. Дальше
есть только два хода: не касаться (REST) или планово рвать сессию до OOM (бюджет +
переоткрытие), и оба надо сделать инструментом, а не ритуалом.

---

## 2. Что прочитал и что нашёл

### 2.1 Файлы

- `figmosha-problems/memory-ceiling-2026-09-24.md`, `README.md`, `FIX-PLAN.md` — целиком.
- `CHANGELOG.md:12-120` (что вошло в 3.0/3.0.1), `:200-202` (`figma.fileKey` недоступен dev-плагину).
- `CLAUDE.md` разделы «Big files», «Dynamic pages»; `README.md` оглавление.
- `plugin/manifest.json` (`dynamic-page`, `teamlibrary`).
- `plugin/code.js`: `:129-152` кэши и `clearExecCaches` (сбрасывает `MAIN_CACHE`/`MAIN_RESOLVED` каждый exec — счётчик не переживает запрос), `:432-440` `loadPageOf`, `:445-447` `pages`, `:455-472` `mainOf`, `:495-590` `walk` (**`:583` читает `child.children` у каждого INSTANCE ради счётчика `pruned`**), `:610-682` `dumpTree`, `:879-886` `stats`, `:905-1001` очередь и `runExec`.
- `bridge.py`: `:62-124` `Session`/`describe` (`started`, `orphaned`), `:324-353` ping/pong отвечает iframe, `:439-445` приём `started`, `:476` `heartbeat=20`, `:660-684` 504 `busy`, `:689-736` диспатч и таймаут.
- `figmosha.py`: `:179-193` коды выхода, `:227-246` `_exec` + `--wait-plugin`, `:1603-1614` вывод `sessions` («dispatched, not started»), `:1968-1986` `_children_of`, `:1989-2001` `_split_ahead`, `:2004-2021` `_load_state` (**любая `ok`-запись = сделано, порядок не учитывается, `partial` не смотрится**), `:2024-2178` `cmd_each` (`:2095-2100` ветка ok без проверки `partial`; `:2102-2145` split только по коду 4), `:2353-2377` флаги `each`.
- `tests/test_cli.py:1151-1330` — тесты `each` на фейковом бридже; `tests/helpers.test.js:366` — стаб `getMainComponentAsync`.
- Прогон: `v3-2026-09-24/run.py` (драйвер: warm → each → redo partial), `scan-unit.js` (что именно трогает юнит: `h.node` → `loadPageOf` → `h.walk` prune → `h.mainOf` на каждом инстансе → `parent`-цепочка → `visible`), `keys.json` (12 ключей вариантов + ключ сета), `data/events.jsonl`, `data/run.log`, `data/page-10.jsonl`, `page-11.jsonl`, `page-02.jsonl`.
- `E:\Work\Personal\Figmosha Dev Seat\FigDev\docs\why-dev-mode.md` — измерения на Dev-seat: `figma.fileKey === undefined` у dev-плагина, REST Tier 1 = 15/мин (Pro) / 20/мин (Org), variables по REST только Enterprise.
- `uspec/README.md:124-134` — уже есть `file-keys.json` (имя файла → key) как обход отсутствия `fileKey`.

### 2.2 Веб-источники и выводы

**F1. Лимит 2 ГБ на вкладку, действует в Desktop. Порог 90 % — красная плашка, 100 % — файл блокируется. Recovery mode загружает ВСЕ страницы.** — *проверено*:
[Reduce memory usage in files](https://help.figma.com/hc/en-us/articles/360040528173-Reduce-memory-usage-in-files):
«active memory limit of 2GB per browser tab», «Memory usage is only tracked for pages that have loaded», recovery mode: «All pages of the file will load… must be reduced to below 90 % to exit».
Форум: WASM поднял лимит до 4 ГБ в 2020, Figma осталась на 2 ГБ; запрос от марта 2026 всё ещё открыт —
[thread 14662](https://forum.figma.com/t/webassembly-boosted-memory-caps-from-2gb-to-4gb-in-2020-why-hasnt-figma/14662),
[feature request 51649](https://forum.figma.com/share-your-feedback-26/allow-users-to-remove-built-in-figma-s-2-4-gb-ram-limit-and-use-of-multi-thread-cores-51649).
Следствие: 4 ГБ ждать не стоит, recovery mode — худшее место для плагина (грузит всё).

**F2. Индикатор памяти есть, и он по слоям.** — *проверено* (там же): Main menu → View → **Memory usage** (метр в левой панели) → **Manage memory** → **Show memory in layers panel** показывает, сколько памяти держит каждый слой/компонент. Программного API нет. Это единственный честный измеритель для протокола §4.6.

**F3. Выгрузки нет.** — *проверено*: [Migrating to dynamic loading](https://developers.figma.com/docs/plugins/migrating-to-dynamic-loading/) и [PageNode](https://developers.figma.com/docs/plugins/api/PageNode/) описывают только `loadAsync`/`loadAllPagesAsync` («may be slow… memory limit when all pages are loaded»); ни unload, ни флага «страница загружена», ни памяти в [figma global](https://developers.figma.com/docs/plugins/api/figma/). `closePlugin` закрывает UI и таймеры — про документ ни слова.

**F4. Плагин исполняется в QuickJS, скомпилированном в WASM; узлы — хэндлы в движок документа.** — *проверено* ([An update on plugin security](https://www.figma.com/blog/an-update-on-plugin-security/): «We now use QuickJS… cross-compiled to WebAssembly», «object representations are too different»). *Гипотеза* (сильная): куча плагина отдельна от кучи документа, поэтому закрытие/перезапуск плагина не освобождает гидратированные экраны. Измерить: F2-метр до/после закрытия плагина (§4.6, M1).

**F5. Инстанс в файле хранится как ссылка на компонент + overrides; дочерние слои гидратируются движком в памяти.** — *проверено по сторонним разборам формата* ([Grida: fig.kiwi](https://grida.co/docs/wg/feat-fig/glossary/fig.kiwi), [openfig research](https://github.com/OpenFig-org/openfig-core/blob/main/docs/research.md): «instance stores only its bounds… override properties, and the parent-component reference ID… Figma Web uses its WASM core engine… in-memory hydration»), не по документации Figma. Это объясняет измеренное: страница (список верхних фреймов) — дёшево, секция с таблицами — 3–5 с и десятки мегабайт при касании. *Гипотеза* (к измерению, M2): гидратацию запускает чтение `.children` у INSTANCE (именно это делает `h.walk` на `plugin/code.js:583`), а не `type`/`name`/`id`. Если так — обход, не читающий детей инстансов, будет холодно стоить миллисекунды.

**F6. `figma.skipInvisibleInstanceChildren`** — *проверено* ([docs](https://developers.figma.com/docs/plugins/api/properties/figma-skipinvisibleinstancechildren/)): при `true` `children`/`findAll`/`findOne`/`findAllWithCriteria` пропускают невидимые узлы внутри инстансов и их потомков; «up to several times faster in large documents»; по умолчанию `true` в Dev Mode, `false` в Figma. Help-статья F1 отдельно называет скрытые слои главным потребителем памяти. *Гипотеза* (M3): с флагом гидратируется меньше и юнит стоит меньше памяти. Цена: невидимые узлы внутри инстансов становятся невидимы и для скрипта — для скана по `mainOf` верхних инстансов это не мешает (`scan-unit.js` читает `visible` самого инстанса, не его детей).

**F7. Через InstanceNode нет способа узнать ключ main-компонента без его загрузки.** — *проверено* ([InstanceNode](https://developers.figma.com/docs/plugins/api/InstanceNode/)): `mainComponent`/`getMainComponentAsync` — единственный путь к ключу; `componentProperties` даёт значения свойств (дёшево, 5 мс измерено), но не ключ. То есть предфильтр по осям вариантов возможен, а ключ — только через загрузку компонента (один раз на ключ за сессию).

**F8. `figma.fileKey` — только приватным плагинам с `enablePrivatePluginApi`.** — *проверено* ([figma global](https://developers.figma.com/docs/plugins/api/figma/)) + измерено в FigDev (`undefined`). Ключ файла для REST берётся из URL / `file-keys.json` (`uspec/README.md:124-134`) — механизм уже есть.

**F9. REST API покрывает read-only скан целиком.** — *проверено*:
- [File endpoints](https://developers.figma.com/docs/rest-api/file-endpoints/): `GET /v1/files/:key` (`ids`, `depth` — «setting this to 1 returns only Pages», `geometry`, `version`, `branch_data`, `plugin_data`); `GET /v1/files/:key/nodes?ids=…&depth=N` («depth will be counted starting from the desired node», `depth=1` — только прямые дети). Ответ: `document`, `components` (map id → Component), `componentSets`, `styles`, `version`. Tier 1, scope `file_content:read`.
- Типы ([figma/rest-api-spec `api_types.ts`](https://raw.githubusercontent.com/figma/rest-api-spec/main/dist/api_types.ts)): `InstanceNode { componentId, componentProperties?, overrides, … } & FrameTraits` (**дети инстанса включаются в ответ — копии**); `Component { key, name, componentSetId?, remote: boolean /* doesn't live in this file */ }`; `ComponentSet { key, remote? }`. То есть матчинг `scan-unit.js` (ключ варианта ∈ `KEYS` или `componentSets[componentSetId].key == SET_KEY`) делается из одного ответа, без загрузки библиотек.
- Rate limits ([docs](https://developers.figma.com/docs/rest-api/rate-limits)): Tier 1 для Dev/Full seat — Pro 15/мин, Org/Enterprise 20/мин; View/Collab — **20 запросов в месяц**. `429` + `Retry-After`.
- Ошибки ([docs](https://developers.figma.com/docs/rest-api/errors)): `400` «requested resources are too large… results in a timeout. Please reduce the number and size of objects requested». Форум/сообщества: ответы 280–320 МБ на `/v1/files` начали отбиваться Cloudflare ([поиск](https://community.latenode.com/t/large-figma-file-retrieval-causing-timeout-error-with-rest-api/25735), треды форума по «request too large»), рабочий приём — `/nodes` с `depth` и пачками `ids`.
- Дети инстансов в REST имеют id вида `I5912:74596;5912:74456` и по ним нельзя запросить `/nodes` ([форум 19465](https://forum.figma.com/ask-the-community-7/can-t-get-file-nodes-of-component-instance-children-19465)) — для нас неважно: внутрь инстансов мы не ходим.
- Ограничение: variables по REST — только Enterprise (why-dev-mode.md). Для скана инстансов не нужно.

**F10. Переоткрытие файла и перезапуск плагина автоматизируемы только через UI.** — *проверено частично*: шорткат **Run last plugin = Ctrl+Alt+P** (Win) / ⌥⌘P (Mac), а также Quick actions Ctrl+/ → «Run last plugin» ([iorad](https://www.iorad.com/player/2000815/Figma---Shortcut---Run-last-plugin), [johanronsse.be](https://johanronsse.be/2019/09/05/figma-plugin-shortcuts)). Deep-link `figma://file/<key>` существует (community-плагины «Deep Link Converter», «Copy Deep Link»); официальная help-статья [Open links in the desktop app](https://help.figma.com/hc/en-us/articles/360039824334-Open-links-in-the-desktop-app) формат не описывает. *Гипотезы* (M6): (а) `Start-Process "figma://file/<key>"` фокусирует уже открытую вкладку, а не открывает вторую; (б) Ctrl+Alt+P после переоткрытия запускает именно Figmosha (память «последнего плагина» — на приложение, не на вкладку); (в) вкладку можно закрыть Ctrl+W. Никакого API запуска плагина нет ([figma global](https://developers.figma.com/docs/plugins/api/figma/)).

**F11. `performance.memory` в iframe плагина.** — *гипотеза с низким приором*: 2 ГБ — это WASM-память движка (ArrayBuffer вне V8-кучи), а `usedJSHeapSize` считает V8-кучу; плюс iframe плагина может жить в другом процессе. Проверить стоит 5 строк в `ui.html` (M5), но рассчитывать на это нельзя.

**F12. Что в `each` и бридже подтверждено кодом (4 бага из memory-ceiling):**
1. `partial: true` внутри `value` не проверяется — юнит пишется как `ok` (`figmosha.py:2095-2100`); split только по коду 4 (`:2102`).
2. `_load_state` (`:2004-2021`) берёт любую `ok`-запись, порядок не важен: `ok` → потом `failed` (повторный прогон) всё равно «сделано», и `partial` не учитывается.
3. Записи `started` нет; в записях нет времени (`write` в `:2076-2079`); убийцу вкладки видно только по «следующей строке, которой нет».
4. `sessions` печатает `BUSY 0s (dispatched, not started)` (`:1610-1611`) — это `running`-запись с `started_at is None` (`bridge.py:711`), которую бридж держит, пока сокет открыт; замёрзшая от OOM вкладка держит сокет до `heartbeat=20` (`bridge.py:476`) и дольше, если pong отвечает живой iframe (`bridge.py:324-353` — pong шлёт `ui.html`, не главный поток). «Не стартовал за N секунд» как признак мёртвого потока не используется.

---

## 3. Варианты решения

### A. REST для read-only обнаружения — `figmosha rest …` (рекомендуется, основной)

**Как.** Новый модуль `figmosha_rest.py` (stdlib `urllib`, без зависимостей). Токен — `FIGMA_TOKEN` (env) или `~/.figmosha/token`; ключ файла — `--file-key`, либо по имени через `file-keys.json` (уже есть в `uspec/`). Команды:

- `figmosha rest file <key|name>` — `GET /v1/files/:key?depth=1`: имя, `version`, список страниц с id (аналог `h.pages()`), проверка токена/доступа.
- `figmosha rest walk <key> [--page ID|--ids a,b] [--depth 3] [--nested] --out nodes.jsonl` — итеративное углубление через `/nodes?ids=…&depth=D`: узел пишется строкой (`id, name, type, visible, page, path, componentId, componentKey, componentSetKey, remote`); INSTANCE — терминал (ключ берётся из `components`/`componentSets` того же ответа); контейнеры на срезе глубины собираются в пачки `ids` (≤ 50 на запрос, длина URL) и запрашиваются снова. `400 too large` → делить пачку / уменьшать `depth`. `429` → спать `Retry-After`. Ответы кэшируются на диск по `(key, version, ids, depth)`, повторный прогон бесплатен.
- `figmosha rest find <key> --component-key K [--set-key S] [--name~…]` — то же, с фильтром; для нашего скана это ровно `scan-unit.js` без плагина.
- `figmosha rest tree <key> <node> --depth N` — для глаз.

**Что чинит.** Read-only скан **не трогает вкладку вообще**: ноль памяти Figma, ноль сессий, никакого плагина, можно гонять ночью и по файлу, который никто не открыл. Даёт всё, что выводит `scan-unit.js` (путь, секция, фрейм, `visible`, `insideInstance` по построению `false`).

**Что не чинит.** Запись (нужен плагин — но по списку id из REST, см. E); variables по имени (Enterprise-only — не нужно скану); ответы включают копии детей инстансов — трафик, не память вкладки; лимит 15–20 запросов/мин → страница 10 (81 секция) ≈ 5–20 запросов ≈ 1–2 мин, весь файл — порядка 10–15 мин без присмотра.

**Усилие M. Риск низкий-средний:** нужен Dev/Full seat с доступом к файлу (у пользователя Dev seat, why-dev-mode.md); `400`/Cloudflare на слишком глубоких ответах — адаптивное деление; `depth`-семантика для `/nodes` подтверждена документацией.

**Доказательства.** F8, F9. Проверить за 10 минут — M4.

### B. Меньше гидратировать при касании (плагин)

B1. **`h.walk` не читает `.children` у INSTANCE** (`plugin/code.js:583`). Счётчик `pruned` считать по типу (`child.type === "INSTANCE"`), без обращения к детям. *Гипотеза F5*: именно это чтение гидратирует таблицы. Если M2 подтвердит — холодный юнит с 3–5 с падает до сотен мс и **не оставляет в куче содержимое инстансов**; останется только стоимость `mainOf` на ключ (12 ключей на весь файл). Усилие S, риск нулевой (поведение `pruned` сохраняется).

B2. **`figma.skipInvisibleInstanceChildren = true` как опция** — `h.walk({skipInvisible: true})`, флаг `--skip-invisible` у `exec`/`each`/`find`, установка в начале `runExec` и сброс в `finally`. *Гипотеза F6*: меньше гидратации скрытых слоёв. Усилие S. Риск: скрипт, которому нужны скрытые узлы внутри инстансов, обязан не включать флаг — потому опция, не дефолт.

B3. **Предфильтр по `componentProperties` перед `mainOf`** (F7): оси `content-qty, direction, istitle` отсекают чужие инстансы за 5 мс. На страницах 10–11 отсекает мало (3 из 5 — попадания), на остальных — большинство. Усилие S (в `scan-unit.js` и в `ds-review/handoff-spec` скриптах), это не фикс Figmosha, а рекомендация в `CLAUDE.md`.

**Что не чинит:** накопление всё равно есть (компоненты по ключу, страницы), потолок не отменяется — B только отодвигает его, возможно сильно (если F5 подтвердится — на порядок).

### C. Бюджет сессии вкладки в `each` — остановка до OOM

**Как.** `each --tab-budget <секунды юнит-времени|N units>` (дефолт — из измерений, консервативно ≈ 150 с накопленного юнит-времени или 40 «тяжёлых» юнитов > 1 с). Счётчик привязан к сессии вкладки: бридж уже знает `age_s`/`opened` (`bridge.py:85`, `:110`) — новый `opened` = новая вкладка = счётчик в ноль. При исчерпании: `each` останавливается с новым кодом выхода **5 `EXIT_TAB_BUDGET`** и текстом «tab budget reached after N units / T s: close the file, reopen it on a light page, re-Run the plugin, then `--resume`»; с `--wait-plugin` (у `each` он уже 300 с по умолчанию, `figmosha.py:2055-2057`) — просто ждёт новую сессию и продолжает сам. Дополнительно плагин копит **сквозные** счётчики холодного времени (`loadMs`, `walkMs`, `mainOfMs`, `heavyUnits`) на уровне модуля (сейчас `clearExecCaches` сбрасывает всё, `code.js:145-152`) и отдаёт их в `h.stats()` и в `identity`/`pong` — `figmosha sessions` показывает «touched: 41 heavy units, 180 s cold since this tab opened».

**Что чинит.** OOM превращается в плановую паузу: нет recovery-mode диалога, нет риска потерять запись в полёте (для write-прогонов это главное), нет «мёртвого» сокета на минуты. Ноль изменений в Figma.

**Что не чинит.** Сессий по-прежнему несколько; порог — эвристика, пока не откалиброван метром (M1). Усилие S. Риск низкий: слишком низкий порог = лишнее переоткрытие, слишком высокий = как сейчас.

### D. Автоматическое переоткрытие (Windows) — `figmosha reopen`

**Как.** После C (или после потери сессии): найти окно Figma (`pywinauto`/`ctypes` + `SetForegroundWindow`), `Ctrl+W` (закрыть вкладку), `Start-Process "figma://file/<key>?node-id=<лёгкая страница>"`, дождаться окна, `Ctrl+Alt+P` (Run last plugin), ждать сессию через `/sessions`. `each --reopen auto` вызывает это на исчерпании бюджета. Все три шага — гипотезы F10, проверяются руками за 2 минуты (M6).

**Что чинит.** Прогон без присмотра при любом числе сессий. **Что не чинит.** Хрупко: фокус, диалоги Figma (после OOM — окно recovery, потому D имеет смысл только вместе с C), только Windows/Desktop. Усилие M (S на код, M на отладку), риск высокий. Отдельная опциональная зависимость, вне ядра.

### E. Гибрид: плагин только для того, что нашёл REST

`each --ids-from nodes.jsonl [--group-by page]` — юниты берутся из результата `rest walk`/`rest find`, группируются по странице (страница касается один раз), тяжёлые страницы — в начало свежей сессии (сочетание с C: бюджет переворачивается перед тяжёлой страницей, а не посреди неё). Для записи по 225 попаданиям страницы 10 плагин загрузит ровно 225 инстансов, а не 81 экран целиком. Усилие S поверх A и C.

### F. Порядок обхода, минимизирующий пик (без кода)

- Страница касается один раз; лёгкие страницы — вместе, тяжёлые — каждая с начала сессии (по данным: свежая вкладка вмещает ≈ 65 табличных экранов, значит стр. 10 (81) и стр. 11 (64) — по сессии на каждую, стр. 0–9 — в третью).
- Переоткрывать на лёгкой странице (COVER): отрисованная тяжёлая страница сама ест бюджет (сессия 2).
- Один тяжёлый файл в приложении.
- Не трогать дважды: `run.py` делает `warm` (11 с холодной страницы) и потом `each` — при B1/A предварительный `warm` не нужен.

### G. Что **не работает** (проверено или с сильным основанием), чтобы не тратить время

| Идея | Почему нет |
|---|---|
| Закрыть/перезапустить плагин, чтобы освободить память | Куча документа — в движке, не в QuickJS (F4). Измерить один раз (M1), дальше не возвращаться |
| Переключить страницу / убрать её из вьюпорта | Память считается по загруженным страницам, выгрузки нет (F1, F3). Помогает только для *рендера* текущей страницы |
| Мельче юниты, больше сплитов | Меняет размер запроса, не остаток в куче (memory-ceiling) |
| Recovery mode | Грузит все страницы (F1) |
| `loadAllPagesAsync` | Обратно к legacy-расходу (F3) |
| Ждать 4 ГБ | Figma на 2 ГБ, запрос от 2026-03 открыт (F1) |
| Приватный плагин ради `figma.fileKey` | Не нужен: `file-keys.json` (F8); публикация требует Org + админа (why-dev-mode.md) |
| Плагин в Dev Mode ради экономии | Тот же движок; единственная разница — `skipInvisibleInstanceChildren=true` по умолчанию — доступна и в Design (B2) |
| Читать память из плагина | API нет (F3); `performance.memory` в iframe — низкий приор (F11) |
| Более дешёвый путь к ключу main-компонента, чем `getMainComponentAsync` | Нет (F7). Есть только предфильтр по свойствам (B3) |

### Сравнение

| Вариант | Убирает OOM для read-only | Убирает OOM для write | Без присмотра | Усилие | Риск | Статус доказательств |
|---|---|---|---|---|---|---|
| A REST discovery | **да, полностью** | нет (но сокращает касания через E) | да | M | низкий-средний | документация + спецификация; M4 — 10 мин |
| B1 walk без `.children` инстансов | возможно на порядок | то же | — | S | нулевой | гипотеза F5; M2 — 15 мин |
| B2 skipInvisibleInstanceChildren | частично | частично | — | S | низкий (опция) | документация F6; M3 |
| B3 предфильтр по свойствам | мало на табличных страницах | — | — | S | нулевой | измерено (5 мс) |
| C бюджет сессии | превращает в паузу | превращает в паузу | нет (ждёт человека) | S | низкий | 3 точки OOM; калибровка M1 |
| D авто-переоткрытие | — | — | **да** (с C) | M | высокий | гипотеза F10; M6 |
| E гибрид ids-from | — | сокращает касания до попаданий | с D | S | низкий | следует из A |
| F порядок обхода | отодвигает | отодвигает | — | 0 | — | данные прогона |

---

## 4. План фикса

Порядок — по «убранный класс отказов на единицу усилия», с условием, что каждый
шаг проверяется на тяжёлом файле по §4.6 до следующего.

### Шаг 0 (день 0, без кода): протокол M1–M4 из §4.6

Четыре измерения, ≈ 40 минут на файле «UIR - FM - Controls». Они решают, что
делает B1/B2 (гидратация) и стоит ли A вообще (доступ по REST). Без них план
ниже — упорядоченные гипотезы.

### Шаг 1 (S) — четыре бага `each` и «stalled» в бридже

`figmosha.py`:
- `_load_state` (`:2004-2021`) → **последняя запись на id**: читать по порядку в `dict`, сделанным считать `status == "ok"` **и** не `partial` (см. ниже). Вернуть также `suspects` — id со `started` без терминальной записи.
- `cmd_each` (`:2081-2100`): перед `_exec` писать `{"id", "name", "status": "started", "t": iso}`; во всех записях поле `t`. После `ok`: распарсить `value` (если строка — `json.loads` с fallback), при `value.partial === true` → та же ветка, что `EXIT_BUSY` (`:2102-2145`): не повторять, сплитить; запись `{"status": "partial", "cursor": value.cursor}`. Флаг `--partial-is-ok` для скриптов, где `partial` означает другое.
- На `--resume` печатать «unit X was in flight when the previous session died» по `suspects` и писать это в запись `started`→`ok` как `"crashed_before": N`.
- `sessions` (`:1603-1614`): «dispatched Ns ago, never started» вместо `0s`; при `not started` и `dispatched_ms > 10 000` — метка **STALLED** и подсказка «main thread never picked the request up: the tab is frozen or out of memory; `return 1 -t 15` is the test, then reopen the file».

`bridge.py`:
- `describe()` (`:102-124`): добавить `dispatched_ms`, `stalled: started_at is None and dispatched > STALL_S` (10 с).
- Диспатч (`:689-712`): если предыдущая `running`-запись `stalled` — не ждать её `_wait_idle` до таймаута вызывающего, а сразу 504 с `stalled: true` и хинтом про переоткрытие. Опционально: по `stalled` бридж шлёт `ping` и, если pong отвечает (iframe жив, главный поток нет), помечает сессию `frozen`.

Тесты (`tests/test_cli.py` рядом с `:1166-1330`): `test_each_treats_partial_as_not_done_and_splits`, `test_load_state_takes_the_last_record_per_id`, `test_each_writes_started_before_running`, `test_resume_names_the_unit_that_was_in_flight`; `tests/test_bridge.py`: `test_sessions_marks_a_never_started_dispatch_as_stalled`.

### Шаг 2 (S) — плагин: не гидратировать лишнего, считать холодное время

`plugin/code.js`:
- `walk` `:583`: `if (prune && child.type === "INSTANCE") pruned++;` — без `child.children`. (Решает M2; даже если гипотеза F5 не подтвердится, чтение лишнее.)
- `walk` opts `skipInvisible` → в начале `figma.skipInvisibleInstanceChildren = true`, в `finally` вернуть прежнее; то же по полю `msg.skip_invisible` в `runExec` (`:931`), которое бридж прокидывает из `POST /exec {skip_invisible}`; CLI-флаг `--skip-invisible` у `exec`, `each`, `find`, `tree`.
- Сквозные счётчики (`:129-152`): `SESSION_STATS = {loadMs, walkMs, mainOfMs, mainKeys: Set, heavyExecs, execs}` — не сбрасываются в `clearExecCaches`, наполняются в `loadPageOf` (`:432`), `walk` (`:495`), `mainOf` (`:455`, ключ считать по `main.key`, не по `node.id`). `h.stats()` (`:879`) отдаёт их; `identity()` (`:49`) и ответ на `ping` включают краткую сводку → бридж кладёт в `describe()` → `sessions` печатает «cold 180s · 41 heavy · 12 keys».
- `MAIN_WARN_AT` (`:139`) `[5000, …]` — порог на вызовы устарел (F7: цена на ключ). Заменить на предупреждение по `mainKeys.size` (например, 50 ключей) и по накопленному `mainOfMs`.

Тесты: `tests/helpers.test.js` — стаб, у которого `children` у INSTANCE — геттер со счётчиком, `walk` с prune его не дёргает; `stats` переживает два `runExec`.

### Шаг 3 (S) — `each --tab-budget`, код выхода 5

`figmosha.py`:
- `EXIT_TAB_BUDGET = 5` (`:179-182`); `_exit_code` не трогать (это решение `each`, не бриджа).
- `cmd_each`: перед циклом `GET /sessions` → запомнить `opened`/`age_s` целевой сессии; после каждого юнита прибавлять `ms` в `tab_spent` (и, если бридж отдаёт — брать `cold_ms` из шага 2 как более точную меру); если `age_s` уменьшился — новая вкладка, счётчик в ноль. При `tab_spent ≥ --tab-budget` (дефолт `150` с; `0` — выключить): записать `{"status":"paused","reason":"tab-budget", …}`, напечатать инструкцию (закрыть файл, открыть на лёгкой странице, Run плагин, `--resume`), и **с `--wait-plugin` продолжить самому**, когда появится сессия с новым `opened`; без него — выйти с 5.
- Записи `page`-группировки: `--order light-first|as-is` (F): лёгкость известна из state прошлого прогона или из `rest walk` (число INSTANCE в поддереве).

Тесты: `test_each_pauses_on_tab_budget_and_resumes_on_a_fresh_session` (фейковый `/sessions` с меняющимся `opened`).

Документация: `CLAUDE.md` «Big files» — заменить абзац про `mainComponent` («цена на ключ, не на вызов; дорого — первое касание секции; бюджет вкладки — `each --tab-budget`»), добавить код 5 в таблицу кодов выхода; `README.md` то же; `figmosha-problems/README.md` §2 — сноска уже есть, добавить ссылку на этот файл.

### Шаг 4 (M) — `figmosha rest`

Новый `figmosha_rest.py` (stdlib) + подкоманда `rest` в `figmosha.py` (`build_parser`, `:2204`):
- Auth: `FIGMA_TOKEN` → `~/.figmosha/token` → ошибка с текстом «Settings → Security → Personal access tokens, scope `file_content:read`». Никогда не в `project.json`.
- Ключ: `--file-key`, иначе `file-keys.json` в корне проекта (перенести шаблон из `uspec/file-keys.example.json`, сделать общий), иначе по имени сессии: если плагин подключён, `figmosha sessions` даёт имя → `file-keys.json`.
- `rest file`, `rest walk`, `rest find`, `rest tree` (§3A). Дисковый кэш `.figmosha-cache/rest/<key>/<version>/<sha(ids,depth)>.json.gz`; `--no-cache`.
- Ограничитель: токен-бакет 15/мин по умолчанию (`--rpm 20` для Org), `429` → `Retry-After`, `400 too large` → делить пачку пополам, при одном id — `depth-1`, минимум `depth=1`.
- Формат `nodes.jsonl` совместим с `each --ids-from` (шаг 5) и с `_load_state` (поле `id`).
- Вывод `rest find` для скана custom-control: те же поля, что в `scan-unit.js` (`layer, variant, page, topSection, section, frame, path, visible`), чтобы `report.py` прогона читал его без правок.

Тесты: `tests/test_rest.py` с фейковым `urlopen` — итеративное углубление, терминальность INSTANCE, деление на `400`, `429`, кэш, матчинг по `components[componentId].key` и по `componentSets[componentSetId].key`.

Документация: раздел «REST for discovery» в `CLAUDE.md` и `README.md`: «read-only скан большого файла — `rest walk`/`rest find`, плагин — для записи и для того, чего REST не отдаёт (variables по имени вне Enterprise, выделение, `getCSSAsync`)».

### Шаг 5 (S) — гибрид `each --ids-from`

`cmd_each`: вместо `_split_ahead` читать юниты из `nodes.jsonl` (`id, name, page`), группировать по `page`, тяжёлые страницы (по числу юнитов) — первыми после переоткрытия (в связке с шагом 3: перед сменой страницы, если оставшийся бюджет меньше её оценки, — пауза/переоткрытие сейчас, а не посреди страницы). Тест: `test_each_ids_from_groups_by_page_and_pauses_before_a_heavy_page`.

### Шаг 6 (M, опционально, после M6) — `figmosha reopen` и `each --reopen auto`

Отдельный модуль `figmosha_desktop_win.py` (только Windows, зависимость `pywinauto` опциональна, без неё команда говорит, что поставить). Последовательность из §3D; каждый шаг с таймаутом и проверкой (`/sessions` → новый `opened`). В `each` — вызов на паузе шага 3. Не включать по умолчанию.

### Шаг 7 — перевод прогона

`custom-control-usage\v3-2026-09-24\run.py` → `figmosha rest find <key> --component-keys @keys.json --set-key … --out data/rest-hits.jsonl` и сверка с `page-10.jsonl`/`page-11.jsonl` (225 + N попаданий). Это и есть приёмочный тест шага 4.

### 4.6 Протокол измерений (тяжёлый файл, до и после)

Все измерения — на «UIR - FM - Controls», файл открыт заново на странице COVER,
плагин запущен, метр включён: **Main menu → View → Memory usage**, затем
**Manage memory → Show memory in layers panel**. Записывать процент метра.

**M1. Что стоит один экран и что освобождается (15 мин; калибрует шаг 3, закрывает F4).**
1. Метр после открытия на COVER: `m0`.
2. `figmosha each 84:312628 -f scan-unit.js --split 2 --state m1.jsonl -t 60 …` — остановить (Ctrl+C) после 10 юнитов. Метр: `m10`. В панели слоёв — память одного `tree-table-*`-фрейма: `per_screen`.
3. Закрыть плагин (крестик). Метр: `m_closed`. **Решает:** `m_closed ≈ m10` → F4 верна, закрытие не помогает.
4. Перейти на COVER, подождать 30 с. Метр: `m_page`. **Решает:** страница не выгружается.
5. Бюджет для шага 3: `(0.85 − m0) / ((m10 − m0)/10)` юнитов. Сверить с 60–70 из прогона.

**M2. Что именно гидратирует (15 мин; решает B1).** Три холодные секции страницы 11 (ещё не тронутые: взять id из `page-11.jsonl` нельзя — они тронуты; взять из страницы 12+ через `figmosha tree <page> --depth 2`). На каждой — один `exec` с таймером и метром до/после:
- (a) обход без детей инстансов: `const r = await h.walk(root, n => n.type, {pruneInstances:true}); return {ms: Date.now()-t0, visited: r.visited}` **на сборке с правкой `:583`** — либо, до правки, свой цикл по `root.children`, читающий только `type/name/id`;
- (b) текущий `h.walk` (читает `child.children`);
- (c) (b) + `h.mainOf` на каждом инстансе.
**Число, которое решает:** если (a) — миллисекунды и метр не сдвинулся, а (b) — секунды и +`per_screen`, гипотеза F5 подтверждена, шаг 2 даёт порядок. Если (a) ≈ (b) — гидратацию вызывает сам проход по узлам, и B1 бесполезен; тогда весь вес на A + C.

**M3. `skipInvisibleInstanceChildren` (10 мин; решает B2).** Ещё три холодные секции, вариант (b) с `figma.skipInvisibleInstanceChildren = true` в первой строке. Сравнить время и дельту метра с M2(b). Решает: меньше на ≥ 30 % → опция в шаг 2 и рекомендация по умолчанию для сканов.

**M4. REST (10 мин; решает A).**
```
curl -H "X-Figma-Token: $FIGMA_TOKEN" "https://api.figma.com/v1/files/<KEY>?depth=1"            # доступ, version, страницы
curl -H "X-Figma-Token: $FIGMA_TOKEN" "https://api.figma.com/v1/files/<KEY>/nodes?ids=84:207462&depth=3" -o p11.json
```
Записать: код ответа, размер `p11.json` (МБ), время; `jq` — число узлов `type=="INSTANCE"`, у которых `components[componentId].key` ∈ `keys.json` или `componentSets[components[componentId].componentSetId].key == SET_KEY`. **Число, которое решает:** совпадает с числом попаданий страницы 11 в `page-11.jsonl` (посчитать `found` по `report.py`) → A даёт тот же результат без вкладки. `400 too large` → повторить с `depth=2` и пачками `ids` детей — это ровно то, что реализует шаг 4.

**M5. `performance.memory` (5 мин; F11).** В `plugin/ui.html` на `pong` добавить `mem: performance.memory && performance.memory.usedJSHeapSize`; сравнить до/после M1. Если не растёт вместе с метром — закрыть тему.

**M6. Переоткрытие руками (5 мин; решает D).** При открытом файле: `Start-Process "figma://file/<KEY>"` — открылась вторая вкладка или сфокусировалась первая? `Ctrl+W`, снова deep-link, после загрузки `Ctrl+Alt+P` — запустился Figmosha? `figmosha sessions` — новый `age_s`? Три «да» → шаг 6 реализуем.

**Критерий «починено».** Скан custom-control по всему файлу: (1) через `rest find` — без открытой вкладки, за ≤ 15 мин, с тем же множеством попаданий, что и три сессии 2026-09-24; (2) через плагин с шагами 2–3 — ноль OOM, паузы по бюджету вместо падений, число сессий ≤ 3 (или, если M2 подтвердит F5, — 1).
