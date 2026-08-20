# Figmosha в новом проекте — пошагово

Одна копия Figmosha = один проект = один порт = один плагин в меню Figma.
Внутри проекта можно держать сколько угодно открытых файлов Figma — они
разъезжаются по сессиям.

---

## 1. Скопировать папку

Взять нулевую (чистую, неинициализированную) копию и положить в проект:

```powershell
Copy-Item -Recurse "D:\tools\figmosha" "D:\Work\Northwind\figmosha"
```

Нулевая копия — та, в которой **нет** `project.json`. Не копируй проектную:
утащишь чужие имя и порт.

Папку `venv` копировать не обязательно — если её нет, создай:

```powershell
cd D:\Work\Northwind\figmosha
python -m venv venv
.\venv\Scripts\pip install aiohttp
```

---

## 2. Инициализировать проект

```powershell
cd D:\Work\Northwind\figmosha
python figmosha.py init --name Northwind
```

Что произойдёт:

```
  ✓  project.json           Northwind, port 8803 (derived from the name)
  ✓  plugin/manifest.json   id figmosha-northwind · «Figmosha · Northwind»
  ✓  plugin/ui.html         ws://localhost:8803/plugin

  →  import the plugin in Figma: Plugins → Development →
     Import plugin from manifest… → …/Northwind/figmosha/plugin/manifest.json
  →  start the bridge:  .\start-bridge.ps1   (bash: ./start-bridge.sh)
```

Порт выводится из имени и записывается в `project.json` числом — дальше он не
меняется сам, даже если проект переименовать: плагин уже импортирован против
этого номера. Если порт занят другим *запущенным* мостом, `init` назовёт
владельца и возьмёт следующий свободный. Без `--name` имя берётся из папки
проекта (родительской, если Figmosha лежит в подпапке).

Имя проекта видно потом везде: в меню плагинов Figma, в `doctor`, в чипе плагина.
Делай его коротким и узнаваемым.

---

## 3. Импортировать плагин в Figma

В Figma Desktop: **Plugins → Development → Import plugin from manifest…** и
выбрать `D:\Work\Northwind\figmosha\plugin\manifest.json`.

Импортируем **прямо из папки проекта** — никуда ничего копировать не нужно.
В меню появится отдельная запись «Figmosha · Northwind» рядом с плагинами других
проектов.

> Импорт нужен один раз на проект. Дальше при изменениях `code.js` / `ui.html`
> хватает **re-Run**. Re-**Import** нужен только если поменялся `manifest.json`.

---

## 4. Запустить мост

```powershell
cd D:\Work\Northwind\figmosha
.\start-bridge.ps1
```

Порт мост берёт из `project.json` сам. Логи — `bridge.out.log` рядом со скриптом.
`-Restart` перезапускает, `-Stop` останавливает.

---

## 5. Запустить плагин в нужном файле Figma

Открыть файл → **Plugins → Development → Figmosha · Northwind → Run**.

В чипе плагина появится имя проекта и файла. Открыл второй файл этого же
проекта — запусти плагин и там: это будет вторая сессия того же моста.

> Плагин привязан к тому файлу, в котором запущен. Переключил файл — запусти
> плагин заново в новом.

---

## 6. Проверить

```powershell
python figmosha.py doctor
```

Должно быть:

```
  ✓  project «Northwind», порт 8803
  ✓  bridge answering on localhost:8803 — «Northwind»
  ✓  plugin connected
  ✓  round trip works (3ms)
  ✓  editing «Northwind DS» — page «Tokens» of 4
```

Со списком сессий (`✓ сессий: 2`) `doctor` заговорит после шага 5 ребилда.

---

## 7. Сказать Клоду

В `CLAUDE.md` проекта (или в корневом, если Figmosha лежит подпапкой) — одна
строка, чтобы агент знал, где мост:

```markdown
Figma управляется через Figmosha в `./figmosha`. Все команды запускать
из этой папки — она сама знает свой порт. Подробности: `./figmosha/CLAUDE.md`.
```

Если в проекте открыто больше одного файла Figma, каждый тред задаёт себе
адресата один раз:

```powershell
$env:FIGMOSHA_SESSION = "DS"     # тред A
$env:FIGMOSHA_SESSION = "Web"    # тред B
```

Один открытый файл — ничего задавать не надо.

---

## Несколько файлов одного проекта

Открыл второй файл этого же проекта — запусти в нём тот же плагин. Это вторая
сессия того же моста, никакого «Slot busy»:

```powershell
python figmosha.py sessions
#   s-mt0d2eic-sg28fw  «Aurora» — page «Page 1»       pending 0  143s
#   s-mt0d4mg4-rvv9h8  «Kite folio» — page «8 projects»  pending 0  39s
```

Команда без указания файла в такой ситуации вернёт 409 со списком — мост
никогда не выбирает за тебя. Указывай файл так:

```powershell
$env:FIGMOSHA_SESSION = "aurora"          # на весь терминал
python figmosha.py --session kite sel    # на один вызов
```

Совпадение ищется по id, алиасу, полному имени файла и префиксу — `--session ds`
находит «Northwind DS». Если префикс подходит двум файлам, будет 409 со списком
кандидатов.

**Чего так не сделать:** развести двух агентов по *страницам* одного файла.
`figma.currentPage` общедокументный, они будут переключать страницу друг под
другом. С несколькими страницами работать можно, но адресуя узлы по id
(`getNodeByIdAsync` после `loadAllPagesAsync`).

---

## Обновление Figmosha в проекте

**Нельзя** просто перезаписать папку целиком: `plugin/manifest.json` и
`plugin/ui.html` сгенерированы под этот проект, перезапись вернёт им порт и id
нулевой копии.

Правильно:

```powershell
# 1. обновить файлы (кроме project.json)
# 2. восстановить проектную идентичность
python figmosha.py init          # без --name: берёт имя и порт из project.json
# 3. перезапустить мост и re-Run плагина
```

Если копия заведена клоном этого репозитория, шаг 1 — это `git pull`, и перед
ним надо отпустить два файла, которые `init` правил под проект: иначе pull
откажется их перезаписывать.

```powershell
python figmosha.py update      # или короче:  .\figmosha update
```

Это и есть те три шага одной командой — она отпускает два проектных файла,
делает `git pull --ff-only`, возвращает порт и id на место и говорит, что
именно теперь нужно: re-Run или re-Import. Если в копии есть незакоммиченные
правки где-то ещё — откажется, а не затрёт их. Руками то же самое:

```powershell
git checkout -- plugin/manifest.json plugin/ui.html
git pull
python figmosha.py init
```

Если при обновлении поменялся `manifest.json` — не re-Run, а **re-Import**.

---

## Если что-то не так

| Симптом | Причина | Что делать |
|---|---|---|
| Мост не стартует, «неинициализированная копия» | нет `project.json` | `python figmosha.py init --name …` |
| «порт занят проектом X» | два проекта на одном порту | `init` подберёт свободный; либо задать `--port` вручную |
| В Figma два плагина с одинаковым именем | старые импорты с общим `id` | удалить старые записи в Plugins → Development, импортировать заново |
| Плагин: «Connecting…» и не зеленеет | мост не запущен или другой порт | `.\start-bridge.ps1`, потом сверить порт в `project.json` и в чипе |
| `409` со списком сессий | в проекте открыто несколько файлов | `python figmosha.py sessions`, потом `--session <имя>` |
| Плагин отвалился после переключения файла | плагин привязан к документу | запустить его заново в нужном файле |

---

## Что где лежит

| Файл | Что это |
|---|---|
| `project.json` | имя и порт этой копии. Единственный источник правды |
| `project.py` | читает его; общий для моста, CLI и лаунчеров |
| `plugin/manifest.json` | `init` правит в нём id, имя и порт |
| `plugin/ui.html` | `init` правит в нём адрес моста |
| `bridge.out.log` | лог моста |
| `CLAUDE.local.md` | машинные пути, гитигнорится |

---
