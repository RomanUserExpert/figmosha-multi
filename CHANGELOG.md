# Changelog

Notable changes, newest first. Versions follow [semver](https://semver.org/):
the bridge's HTTP contract and the `h.*` helper surface are what's being
versioned, since those are what scripts depend on.

After upgrading, **re-run the plugin in Figma** — a running plugin keeps the
code it started with, so new helpers won't exist until you do. If
`plugin/manifest.json` changed, re-*import* it rather than just re-running, and
re-run `figmosha init` so the copy's project identity survives the update.

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
