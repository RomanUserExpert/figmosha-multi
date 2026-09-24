# Changelog

Notable changes, newest first. Versions follow [semver](https://semver.org/):
the bridge's HTTP contract and the `h.*` helper surface are what's being
versioned, since those are what scripts depend on.

After upgrading, **re-run the plugin in Figma** — a running plugin keeps the
code it started with, so new helpers won't exist until you do. If
`plugin/manifest.json` changed, re-*import* it rather than just re-running, and
re-run `figmosha init` so the copy's project identity survives the update.

## [3.2.0] — 2026-09-24

The rest of the memory story, from the same file: the tab still dies near 2 GB,
and now `each` sees that coming.

- **`each` pauses before the Figma tab runs out of memory.** Before every unit,
  and before splitting, it reads the private bytes of the tab's renderer
  process (Windows). Past `--tab-limit` (default 1700 MB) it writes a `paused`
  record, tells you to reopen the file on a light page, and waits up to
  `--reopen-wait` seconds (default 1800) for a new plugin session — then
  carries on by itself. Not reopened: exit code **5**, `--resume` later. Every
  `ok` record carries `tab_mb`, which is what the next calibration is made of.
  `--tab-pid` picks the tab by hand; `--tab-limit 0` turns the guard off.
  Live check: on a tab at 1974 MB, `each` sent nothing to Figma and exited 5.
  Before that, the same tab answered a plain child listing with Figma's own
  "Unable to establish connection to Figma".
- **`figmosha mem`** — private memory of each Figma tab, largest first.
- **`each` no longer splits instances.** Splitting one handed its sublayers to
  the script and never the instance itself: a `custom-control` placed loose on
  a page became three units and was not counted. Instances are units of their
  own now; one that overruns fails with a pointer to `--split-instances`, which
  restores the old behaviour.
- **Hidden layers inside instances are no longer listed as units.** With
  invisible instance children skipped (3.1), Figma answers "does not exist" for
  them, so they could only ever fail.

## [3.1.0] — 2026-09-24

3.0 fixed the thread and the resume. It did not fix the memory: on the same
file the tab still ran out, three times, with only a few hundred `mainOf` calls
(see [`figmosha-problems/memory-ceiling-2026-09-24.md`](figmosha-problems/memory-ceiling-2026-09-24.md)).
Measured afterwards in the tab's own process: a heavy page costs hundreds of
MB to load, a new library component tens, a section of an already loaded page
a few — and nothing is released until the file is reopened. The plan is in
[`figmosha-problems/MEMORY-FIX-PLAN.md`](figmosha-problems/MEMORY-FIX-PLAN.md);
this release is its first step.

- **`h.walk` skips hidden layers inside instances.** It turns on
  `figma.skipInvisibleInstanceChildren` for the walk and restores it after. A
  cold section of a table-heavy page went from 3.1–3.7 s to 0.2–0.7 s.
  `skipInvisible: false` opts out; `find --hidden` does the same for `find`.
- **`each` no longer records a `partial: true` result as done.** It is split
  and run smaller, the same as a timeout. `--partial-is-ok` keeps the old
  behaviour for scripts where `partial` means something else.
- **`each` writes `started` before every unit**, and `--resume` reads the
  **last record per id**: a unit it already split is not run again (its
  children are), and one that was running when the tab died is named.
- **`figmosha sessions` says `STALLED`** for a request the plugin never
  started — a frozen or out-of-memory tab — instead of `BUSY 0s`.

Re-Run the plugin after updating; the manifest did not change.

## [3.0.1] — 2026-09-21

`update` told you two different things in a row. `init`, which it calls, can
only see that the plugin's identity did not change, so it said "re-Run the
plugin"; `update` then looked at what the pull had actually touched and said
"re-IMPORT". Following the first one leaves Figma running the old manifest —
which, on this release, means the whole document is still preloaded — and looks
exactly like the update not working. `init` now leaves that line to the caller
that knows.

## [3.0.0] — 2026-09-21

2.4 made the everyday task cheap. This release is about the file that defeats
all of it: a 15-page mockup where one frame holds **53 439** instances, a scan
resolved **876 849** main components, and the tab ran out of memory. The
failure modes are written up in
[`figmosha-problems/README.md`](figmosha-problems/README.md); what follows is
the answer to them.

The measure of this release is one thing: a scan that used to end in a crash
should end in a result, and an interruption should cost a delay rather than a
restart.

**Upgrading: re-import the plugin**, do not just re-Run it. `manifest.json`
gained `documentAccess`, and a manifest change is not picked up by a Run
however many times you press it. `python figmosha.py update` says so on its
own.

### Surviving a big file

**Breaking.** Four changes, each with a migration:

- `plugin/manifest.json` now declares `"documentAccess": "dynamic-page"`, so
  Figma no longer preloads the entire document before the plugin's first line.
  That preload was 20–30 s and most of an out-of-memory budget spent before a
  single command had been sent. In exchange the synchronous lookups throw:
  `instance.mainComponent`, `figma.getNodeById`, `component.instances`,
  `getLocal*Styles`, `node.fillStyleId = …`, `figma.currentPage = …`,
  `style.consumers`. The replacements are in `CLAUDE.md → Dynamic pages`, and
  the bridge names them in a hint the first time a script trips over one.
  **Re-import the plugin** (a manifest change is not picked up by a re-Run).
  A project that is not ready: `figmosha init --document-access legacy`, which
  sticks in `project.json`.
- `figmosha find` no longer descends into instances. An instance's children are
  copies that arrived with its component, not placements anyone made; in the
  measured run 0 of 339 real hits were nested inside one. `--nested` restores
  the old behaviour, `--count` skips building the list at all.
- `find --raw` returns the walk — `{found, visited, pruned, partial, cursor}` —
  rather than a bare array, because on a big subtree "were there more?" is the
  difference between a short answer and a wrong one.
- `h.findByName`, `h.findAllByName` and `h.dumpTree` are now `async`: they may
  have to load the page they were handed. `await` them.

**A caller's timeout is no longer mistaken for the plugin's.** A 504 used to
release the file's lock while the abandoned script kept running inside Figma,
so the next request was dispatched into a busy JS thread — which is how one
overrun became minutes of a dead file, and twice an out-of-memory crash. The
bridge now keeps abandoned requests in `Session.running`, waits for the thread,
and refuses with `busy` instead of stacking. `GET /sessions` gained `busy`,
`running_ms` and `orphaned`; `GET /status` gained `busy`. `pending` and
`queued` never were busy signals and still are not.

`POST /sessions/<sid>/reset` (`figmosha sessions --reset <sid>`) makes the
bridge forget a wedged session. It cannot stop a running script — nothing can —
and it is a last resort, not a retry.

**The plugin runs one exec at a time and says when it starts.** A `started`
message makes `ran_ms` mean what it says; before, it was time since dispatch.
An internal queue stops two scripts interleaving at their `await` points and
wiping each other's per-exec caches.

**A deadline that lives inside the loop.** Figma cannot interrupt a running
plugin script, so a limit only exists where the code checks it. The bridge
passes the caller's remaining time to the plugin, and `h.walk` and friends stop
between nodes and hand back a cursor.

Two details of that, both found by running it against a real file rather than a
stub, and both the difference between the mechanism working and merely
existing:

- The plugin stops **short** of the caller's deadline — 10% of the budget,
  between 250 ms and 2 s — because a partial result that arrives after the 504
  is the same as no result at all.
- The budget is checked at **every** node, not every 256th. A stride looks like
  a saving and is not: measured inside Figma, `Date.now()` costs 0.19 µs while
  reading one node's `type` and `children` costs 17.63 µs, so checking always
  adds about 1%. A stride, meanwhile, multiplies the overshoot by the cost of
  the visit — and the visit is the expensive part of any real scan. At 10 ms a
  node it walked 2.5 s past the deadline and the answer arrived too late.

### New

- `figmosha each <id> -f script.js --split N --state run.jsonl [--resume]` —
  run one script over a subtree, a unit at a time. It descends before it runs
  anything (listing children is free; discovering the right size after a
  100-second overrun is not), writes one JSON line per unit as it finishes, and
  splits a unit that times out instead of retrying it. `--resume` picks the run
  back up. Long runs get `--wait-plugin` by default.

  The listing that decides how to split a unit is asked with a generous budget
  of its own, because the unit that just overran still owns the file's thread:
  asking with the unit's own timeout hits the same busy plugin and reports
  "cannot be split", which is a wrong answer to a question that was never
  asked.
- `h.walk(root, visit, opts)` — the way to cross a big subtree: prunes at
  `INSTANCE` by default, checks the deadline between nodes, stops on
  `budgetMs` / `maxNodes` / `maxHits`, and returns `{found, visited, pruned,
  partial, cursor, reason}`. Feed the cursor back to carry on: over a real
  page, 410 + 214 nodes across two separate `exec` calls is exactly the 624 a
  single uninterrupted walk finds — no gaps, no repeats.
- `h.mainOf(instance)` — the only supported route to a main component under
  dynamic-page, with a per-exec cache and a counter that warns through
  `print()` at 5 000 resolves, while the run can still be narrowed.
- `h.loadPageOf(node)`, `h.pages()`, `h.tick()`, `h.left()`, `h.stats()`.
- `--set-str NAME=value` and `--set-json NAME=value`. `--set` guesses — JSON
  when it parses, a string when it does not — which makes
  `--set KEYS='{"a":"b"}'` an object and throws inside any script that calls
  `JSON.parse(KEYS)`. `--set-str` is the fix. `NAME=@file` reads the value from
  a file, for key lists past the command-line length limit.
- `--wait-plugin N` / `FIGMOSHA_WAIT_PLUGIN`: on a lost plugin, wait for it to
  come back rather than failing. Switching files in Figma closes the plugin
  window and takes every session with it, so a long run meets this.
- Exit codes a runner can branch on: **1** script error · **2** usage ·
  **3** no plugin · **4** timeout or busy. They used to all be 1.

### Fixed

- `h.importComp` / `h.importVar` race the import against a timer. Importing a
  key that belongs to another file and was never published never settles — no
  result, no error — and the 180 s in the field report was the CLI giving up,
  not the API answering. `icomp` defaults to 20 s and points at the way that
  works: compare `(await h.mainOf(inst)).key` from the consuming side.
- `figmosha tree` no longer hides a full walk. The `… +N deeper` count called
  `descendants()`, which walked every cut branch — so `tree --depth 1` on a
  53 000-node frame walked all 53 000. It is bounded now, and says `≥N` when it
  stops counting.
- The session id survives a re-Run: it is kept in `figma.clientStorage`, keyed
  by file name, so `--session` and a resumed run still find the same file after
  the plugin is started again. Two same-named files in one account share the
  key; the bridge refuses the second with 1008 and the plugin regenerates
  immediately instead of waiting 15 s.
- `CLAUDE.md` and `README.md` no longer advise re-running the plugin on a 504.
  It is unnecessary — the thread nearly always returns — and costs the session.


### uSpec runs on the bridge — `uspec/`

**uSpec** documents a Figma component: it reads a component set out of the file,
writes a Markdown spec, and renders seven annotation frames — anatomy, structure,
property, color, API, motion, screen reader — back beside the component. Stock
uSpec needs two things to do it: its own **Extract plugin**, and a **Figma MCP
server** for every write. Both exist to execute JavaScript inside the open file.
That is what `POST /exec` has always been, so neither is necessary here.

`uspec/` is the adapter. It makes `figmosha` a third `mcpProvider`, and the whole
pipeline — extraction, spec, contract, all seven renders — runs over the bridge.
Nothing in uSpec is forked: `npx uspec-skills init` installs the skills
unmodified and `uspec/apply-adapter.py` patches them in place afterwards, four
marked substitutions per tree, idempotent, re-runnable after every upgrade.
`--check` reports an unpatched tree and exits non-zero, so an upstream move
surfaces as `UNPATCHED` rather than as nothing.

Three things were worth the trouble to get right:

- **Extraction is read-only.** uSpec's phases F and G instantiate every variant
  to measure it and then delete the instances. Under an MCP that write belongs to
  a plugin the designer ran; here it would be the agent's, into the designer's
  file, unasked. `extract/build-bundle.mjs` patches both phases at build time to
  measure in place, anchored on exact source text so an upstream move fails the
  build instead of passing silently. The cost is bounded and the output declares
  it in `_extractionNotes.warnings`: nothing at all without BOOLEANs or SLOTs, and
  otherwise only the reflowed geometry in `crossVariant.axisDiffs`. `--reveal`
  restores the full measurement on **temporary instances** and verifies the file
  came back to where it started before it will finish.

- **The templates are captured, not imported.** The render skills import a
  template component by key from an unpublished Community file. That key does not
  reject — it **hangs and never settles**, 15–24 s to a timeout, seven for seven.
  Publication status is the discriminator, not connection warmth, and a longer
  timeout does not help. So the seven templates are read out node-for-node into
  `templates/templates.json` (330 nodes, 147 `#anchor` layers) and replayed as a
  detached frame on demand. The skills detach the imported instance on the next
  line anyway, so nothing downstream can tell.

- **The port is not written down twice.** Every script in `uspec/` takes its
  default `--port` from this copy's `project.json`, the same number the bridge
  binds.

`uspec/file-keys.json` (gitignored, `file-keys.example.json` is the template) maps
a Figma file's name to its key — a development plugin cannot read `figma.fileKey`,
and `_meta.figmaUrl` needs it.

Proven end to end against a copy of a real design-system library. Full write-up in
[`uspec/README.md`](uspec/README.md).

## [2.4.0] — 2026-08-21

2.3 made output cheap. The unit left unoptimised was the **agent's turn** —
seconds of thinking plus a whole context read — and the way Figmosha spent it
was by making its caller write JavaScript. Ten subcommands could read the
document and four could change it; anything else meant a hand-written `exec`,
with the frozen-fills and auto-layout-order traps that cost a turn each time
they were hit.

The measure of this release is one task: binding hardcoded values to tokens
should take three turns, not five to seven.

```
before:  sel → props → vars → write 15 lines of exec → error → fix
after:   sel → props → bind
```

### Added

- **`figmosha set` and `figmosha bind`.** `set` takes literal values
  (`gap=16 fill=#f5f5f5 pad=12,16 w=fill`), `bind` takes token names
  (`gap=space/md fill=surface/canvas`); `bind key=none` removes a binding. Keys
  are applied in a fixed order — layout, size, spacing, align — regardless of
  the order typed, because auto-layout silently drops whatever is set too early.
  `sel` means the whole selection, as in `rm`. Every mutation prints
  **before → after**, with a second arrow naming the token; `--dry-run` prints
  the same table and writes nothing. That diff is the only undo there is:
  Figma's history is not reachable from a plugin.
- **Variables and styles by name.** `h.bF` / `h.bS` / `h.bN` and the new
  `h.applyStyle` accept a token name, a local id, or a library key. Names match
  most specific first: exact, then ignoring case, then qualified with the
  collection (`Semantics/color/bg/default`), then as a suffix on a `/` boundary
  (`bg/default` finds `color/bg/default`, never `bg/default-alt`). A name that
  matches more than one **throws with the candidates listed** rather than
  guessing — in a themed library every primitive exists twice.
- **`figmosha where <id>`** — the path from the page down, with each ancestor's
  size and sizing mode, which is what "why did this move" is actually about.
- **`figmosha overrides <id>`** — what an instance overrides against its main
  component, with the nested ids (`I10:239;88:9705`) resolved to layer names.
- **`exec --set NAME=value`** (repeatable) defines a const before the code —
  JSON when it parses as JSON, a string when it does not. This removes the
  assemble-a-copy-per-run step from all four skills.
- **`h.applyStyle(node, kind, name)`** and `h.style_(kind, name)` for
  `fill` / `stroke` / `text` / `effect` / `grid`.

### Changed

- **`tree` defaults to depth 3**, not 99. A cut branch prints `… +N deeper`
  with the real count, so the decision to dig is made against a number. Three or
  more siblings sharing a name **and** type collapse into one row naming the id
  range; `--no-collapse` turns that off. Measured on a real page: 424 lines
  against 1077.
- **`find` prints 100 rows** and then says how many were left, with the
  `--limit` that would show them. `--raw` stays unbounded on purpose: a
  truncated array carries no sign that a tail is missing.
- **`vars` qualifies ambiguous names.** A name living in more than one
  collection prints as `Collection/name` — the form that resolves — and the rest
  stay short. On a themed library that is about a quarter of the file.
- **The variable list is cached as a snapshot** of `{name, group}` for the
  duration of one exec, not as the Figma objects. Every property read on a Figma
  node crosses into the engine: reading `name` across 438 variables costs ~14 ms,
  and a four-rung ladder over live proxies paid that toll on every resolve. Ten
  resolves went from 73 ms to 1 ms.
- **All four skills return tables**, not nested objects, and are fed with
  `--set` instead of a temp file built per run.

### Fixed

- **`session.queued` leaked upward.** The counter came down on the timeout path
  and on success, but not on `CancelledError` — an ordinary Ctrl-C — so
  `sessions` and the 504 hint over-reported the queue for as long as the bridge
  ran. It is a `finally` now.
- **A second bridge could take a port that was already in use.** Windows reads
  `SO_REUSEADDR` as permission to bind over a live listener, and the two
  processes then split the port: the plugin holds its WebSocket in one while the
  CLI posts to the other. The symptom — a session that exists but answers
  `Cannot write to closing transport` — points nowhere near the cause.
  `reuse_address=False`.
- **`vars` repeated its truncation warning once per collection.** The row
  counter is per file but `break` only left the inner loop; a seven-collection
  file said it three times for a common filter.
- **`_incumbent_answers` polled for the pong** every 50 ms instead of waiting
  for it. It resolves a future now, which is what a reconnecting plugin waits on.

## [2.3.0] — 2026-08-20

Output is the thing an agent pays for, and Figmosha was charging about three
times what it needed to. This release is mostly about that, plus the CLI bug
that made the documented shorthand unusable in exactly the projects 2.2 was
built for.

### Changed

- **Compact results.** The plugin no longer pretty-prints: `JSON.stringify` runs
  without indentation, and Figma's float sizes (`343.99996948242188`) are rounded
  in the text. `sel` and `find` return one line per node — `id  TYPE  W×H  name`
  — assembled inside the plugin, so the rows cross the wire once in the shape
  the caller reads. Measured on 12 nodes: 435 tokens before, 127 after.
- **`--raw` is the escape hatch** for all of it: full JSON response, full
  precision, `value`, whole stack. `figmosha status` is a single line now, with
  `--raw` for the old JSON.
- **The result is no longer serialised twice.** `POST /exec` takes
  `want_value`; the CLI sets it to `false` unless `--raw` was asked for, and the
  plugin then skips the second full walk and the second copy over the socket.
  Anything that does not send the field keeps the old response shape.
- **Ceilings, with the truncation said out loud.** 64 KB per result, 200
  `print()` lines per run; both report how much was dropped. One command can no
  longer flood a context window unannounced.
- **Stack traces are cut to the first three frames** in CLI output — past that
  it is the plugin's own runtime, every time.
- **`timeout` is validated and clamped to 1..600 s.** It used to be `float()` on
  whatever arrived: `"soon"` was an empty 500, and `99999` held the document's
  lock — and every other script queued behind it — for a day.
- **GET requests get a 5 s deadline** instead of 65. `status` and the first step
  of `doctor` used to hang for a minute when the port was held by a process that
  accepted the connection and went quiet.
- **`rm sel` deletes the whole selection**, not just its first node. Everywhere
  else `sel` is "the first selected node"; deleting one of three selected layers
  and reporting success was a wrong answer nobody re-reads.
- **`start-bridge.ps1` polls for the port** instead of sleeping a flat two
  seconds, and reads it via `Get-NetTCPConnection` instead of parsing `netstat`
  text (which depended on the console locale).
- `CLAUDE.md` is ~800 tokens lighter: reference tables moved to the README,
  rules and traps stayed.

### Fixed

- **`figmosha --session X "<js>"` and `figmosha -t 5 "<js>"`** ended in
  `invalid choice`. The shorthand only looked at `argv[1]`, so any flag broke
  it — including `--session`, which is exactly what a multi-file project puts in
  front of every command. Global options are now lifted out before `exec` is
  inserted, so flags work on either side of the code.
- **The `*` in `figmosha sessions`** marked only exact matches, while routing
  accepted name prefixes: `FIGMOSHA_SESSION=kite` sent commands to «Kite
  folio» and the listing showed nothing selected. The bridge now answers the
  question itself (`GET /sessions?session=X` → `matched`), so there is one
  matcher instead of two.
- **The hint for a missing manifest permission** told you to sync the plugin to
  `/mnt/c/Users/<you>/figmosha-plugin/`, a path that stopped existing in 2.2 —
  the plugin is imported from the project folder now. It says re-import instead.
- **`tests/helpers.test.js` had been failing to load since 2.2** — the stub had
  no `figma.on`, which `code.js` calls at load time. Fixed, and extended to
  cover the result shaping.
- README no longer tells WSL users to copy the plugin folder to the Windows
  side; two copies drift apart the moment `init` stamps a port into one of them.

### Added

- **`figmosha props <id>`** — everything set on one node, with variables and
  styles resolved to names: geometry, layout, constraints, fills, strokes,
  radii, effects, text, component properties. A value with `→ token` after it
  is bound; a value without one is hardcoded, which is the question the
  command exists to answer.

  Written by hand this was two round trips — one to read `boundVariables`,
  another to turn `VariableID:1:234` into `color/bg/surface` — and a few
  hundred tokens of pretty JSON. It is now one call and about 70 tokens.
  Defaults (`opacity 1`, `constraints MIN/MIN`, `rotation 0`) are on every
  node in the file and are not printed unless `--all` asks for them.
  `--children` adds one row per direct child carrying its FILL/HUG/FIXED
  sizing and `grow`, since a layout that moved is usually a child's sizing
  rather than the parent's.
- **A shim next to `figmosha.py`** — `.\figmosha props sel` instead of
  `python figmosha.py props sel`, with the copy's venv found for you.
  `figmosha.cmd` on Windows, `figmosha` elsewhere; both resolve the copy from
  their own location rather than the working directory, so one copy on PATH
  keeps talking to its own project.
- **`figmosha update`** — `git pull` and the re-stamping that has to follow
  it, as one step. Letting go of `plugin/manifest.json` and `plugin/ui.html`,
  pulling, and running `init` is a three-part ritual whose middle step
  destroys the first one's output; performed half-way it leaves `ui.html` on
  port 8787 and a plugin that cannot find its bridge. It refuses rather than
  guesses — uncommitted changes elsewhere stop it before the pull — and it
  says which follow-up the update earned, since "re-Run the plugin" and
  "remove it and Import again" are not interchangeable.
- **A mistyped subcommand no longer travels to Figma as JavaScript.**
  `figmosha updat` became `exec "updat"` and came back as
  «'updat' is not defined» from inside the plugin runtime. A bare word is
  now read as a mistyped command, with the nearest matches offered.
- **`figmosha vars [filter]`** and **`figmosha styles [filter]`** — the other
  half of `props`, which can say which token a value is bound to but never
  whether a suitable one exists.

  `vars` without a filter prints a map rather than the file: collections,
  their modes, counts, and how many tokens sit under each name prefix
  (`color/ 98   space/ 14`). With a filter it prints rows, one column per
  mode, with aliases resolved to the token they point at. A design system is
  hundreds of variables, and dumping all of them to answer "is there a
  spacing scale" costs more than the answer is worth; listings stop at 200
  rows and say so.

  `--library` lists the collections this file consumes, and — with a filter —
  the variables inside them together with the keys `h.importVar()` takes.
  That path is a network call per collection, so it only happens when
  something is actually being looked for. A file consuming a design system
  has no local variables at all, which is exactly where the command is
  needed.
- `tests/test_cli.py` — the CLI had no tests at all, which is why the shorthand
  bug survived a release. Covers argv normalisation, and runs the JS that the
  CLI generates through Node against a stub document, so a typo in a generated
  snippet fails here rather than inside somebody's Figma file.
- `project.VERSION` — one version number for the copy, reported by `GET /`.
  The banner claimed `2.0` throughout 2.2.

## [2.2.0] — 2026-08-19

One copy of Figmosha per project, and any number of open Figma files inside it.
Both were single before: every copy was a byte-identical clone listening on
8787 with the plugin id `figmosha-bridge-dev`, so a second copy could not start,
and a second file was answered with "Slot busy".

**Upgrading an existing copy:** run `python figmosha.py init` in it, then remove
the old entry in Figma's Plugins → Development and import the manifest again —
the plugin id changes, so a re-*run* is not enough this once. Copies are no
longer updated by overwriting the folder wholesale: `plugin/manifest.json` and
`plugin/ui.html` now carry the project's identity, so re-run `init` after any
update.

### Added

- `project.json` — a copy's identity: `{name, port, created}`. Read by the
  bridge, the CLI and both launchers, so the port lives in exactly one place.
- `figmosha init [--name X] [--port N]` — claims a copy for a project: picks a
  port (derived from the name, so two people initializing the same project
  agree), writes the config, and stamps the project's id, menu name and bridge
  address into the plugin. Idempotent; a rename never moves the port, because a
  plugin was already imported against it.
- **Sessions.** The bridge keeps one per open Figma document, each with its own
  socket and its own in-flight requests. `GET /sessions` lists them; `/status`
  gained `project` and `sessions` alongside its old fields.
- `figmosha sessions` — which files are connected, with their pages.
- `--session` / `FIGMOSHA_SESSION` — which file a command means. Matches an id,
  an alias, a full file name, or a name prefix: `--session ds` finds
  "Northwind DS".
- The plugin introduces itself: a per-run session id, the file name and the
  current page, kept in memory rather than written into the document.

### Changed

- **A request that could mean two files is refused, never guessed.** With
  several files connected, a command without a target returns 409 and lists the
  candidates. Silently editing the wrong Figma file is the expensive mistake
  here; failing loudly is the cheap one.
- **Scripts in one document run one at a time.** A document is a single JS
  runtime, and two scripts interleaving at their await points would see each
  other's half-finished edits. Different documents still run in parallel.
- Timeouts now cover queueing as well as running; a 504 reports `waited_ms` and
  `ran_ms` separately, and `/sessions` shows `queued`.
- A closing file fails only its own requests. Previously one shared `PENDING`
  meant closing any file broke every in-flight request on the bridge.
- The bridge refuses to start without `project.json`. Falling back to 8787 would
  let an uninitialized copy answer for whichever project owns that port.
- Rejection with close code 1008 now means one thing only — the same document is
  already served, i.e. it is open in a second Figma window.

### Fixed

- `start-bridge.ps1` passed the script path to `Start-Process` unquoted, so a
  project folder with a space in its name never started.
- The bridge forces UTF-8 on stdout; a redirected Windows console would kill it
  on the first line when the project name was not ASCII.

## [2.1.0] — 2026-08-18

### Security

- **The bridge now refuses requests that come from a web page.** It executes
  arbitrary JS inside your open Figma file, so any tab you have open was part of
  the threat model: a page could `fetch` `localhost:8787` as a `text/plain`
  "simple request", dodge the CORS preflight, and silently edit or delete your
  work. Requests carrying an `Origin` header are now rejected, and `Host` is
  pinned to the loopback names actually served, which closes DNS rebinding.
  Local clients (curl, the CLI) are unaffected — they never send `Origin`.

### Added

- `h.sel()` and `figmosha sel` — read the current selection. Closes the gap
  between "this frame here", which you point at with a mouse, and a node id.
- `page` and `sel` work anywhere a node id is taken: `figmosha tree sel --layout`,
  `figmosha rm sel`.
- `figmosha doctor` — walks bridge → plugin → round trip → which file is open,
  naming the fix at whichever link is broken.
- `h.hex()` and `h.solid()` — hex strings instead of hand-rolled `/255` maths.
- `h.frame(parent, opts)` — creates a frame and applies auto-layout in the order
  Figma requires. Getting that order wrong fails silently, which is why it was
  worth encoding in a helper rather than documenting for a third time.
- `h.resolve(idOrAlias)` — one lookup that also understands `page` and `sel`.
- `--layout` flag on `figmosha tree` — shows layoutMode, gap, padding and sizing.
- `figmosha rm` takes several ids at once.
- `FIGMOSHA_HOST` / `FIGMOSHA_PORT` environment variables.
- `start-bridge.ps1` — detached launcher for native Windows, with `-Restart` and
  `-Stop`. `start-bridge.sh` needs bash and tmux, which a plain Windows box has
  neither of.
- Tests: the bridge is driven by a fake plugin over a real WebSocket (guard,
  exec round trip, timeouts, disconnect cleanup, slot handover, hints), and the
  pure helpers run against a stubbed Figma (hex maths, auto-layout ordering).
  Neither needs Figma.

### Fixed

- **The CLI crashed on any layer name outside cp1252** — which on a default
  Windows console means most non-English names. Output is now forced to UTF-8.
- **A reconnecting plugin could be locked out for ~20s.** A half-open socket
  (laptop slept, network changed) stayed "open" until the heartbeat gave up, and
  every 2s retry was rejected meanwhile. The bridge now pings the incumbent: no
  answer within a second and the newcomer takes over. A plugin that *is* alive
  still keeps the slot, so two Figma windows no longer evict each other forever.
  A rejected plugin shows `Slot busy` and backs off 15s instead of hammering.
- `--timeout` was ignored: the client socket deadline was hardcoded to 65s, so
  long runs died client-side while the bridge was still waiting, losing its
  error payload and hint.
- `h.var_` could not resolve a library key. `getVariableByIdAsync` rejects a
  malformed id by throwing, so the unguarded call swallowed control flow before
  the import fallback ran — the documented behaviour never worked.
- `h.bF`/`h.bS` threw an opaque `SyntaxError` from `JSON.parse(undefined)` on
  nodes with mixed fills; they now say what's wrong and which node.
- `h.withFonts` silently skipped mixed-font text nodes, so editing them failed
  later and far from the cause. It now reports what it skipped.
- Requests left in flight when the plugin disconnects are failed immediately
  rather than hanging until timeout.

### Changed

- **Plugin window is a 220×28 status bar.** Was 360×260 with a log panel. The
  background carries the state — green `Connected`, amber `Connecting…` with a
  spinner, red `Error` — using [Solar](https://www.figma.com/community/file/1166831539721848736)
  icons (CC BY 4.0). Logs moved to the plugin console.
- README rewritten: what the tool can and can't do, an HTTP API reference, a
  security section, a Mermaid architecture diagram, and installation reduced to
  handing the repo URL to Claude Code.
- Machine-specific paths and hosts moved out of `CLAUDE.md` into a gitignored
  `CLAUDE.local.md`. They had no business in a public repo that invites
  strangers to point their agent at it.

## [2.0.0] — 2026-05-20

- Replaced the Playwright + Scripter approach with the WebSocket bridge: a
  custom plugin holds a socket open to a local Python server, so Plugin API
  calls are milliseconds rather than browser automation.
- `h.*` helpers, high-level CLI subcommands, and `hint` fields on recognised
  errors.

[2.1.0]: https://github.com/denysosadchyi/figmosha2/releases/tag/v2.1.0
