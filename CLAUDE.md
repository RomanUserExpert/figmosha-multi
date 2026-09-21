# Figmosha 3.0 — Claude Code instructions

Drive Figma by sending JS through a local bridge connected to a plugin running
inside Figma Desktop.

This copy serves **one project** — its name and port live in `project.json` —
and, inside it, **any number of open Figma files**, one session each.

Full reference (every helper, every flag, install, troubleshooting): `README.md`.
This file is the part you need loaded while working.

## How to send code

There is a shim next to `figmosha.py`, so `.\figmosha <cmd>` (bash:
`./figmosha <cmd>`) works instead of `python figmosha.py <cmd>` — it finds this
copy's venv itself. Both spellings appear below; they are the same thing.

```bash
python figmosha.py exec "return figma.currentPage.name"
python figmosha.py exec --file script.js
python figmosha.py exec --file scan.js --set ROOT_ID=185:21880   # const, before the code
python figmosha.py "return figma.currentPage.name"     # exec is implied
python figmosha.py text 185:21880 "Привіт"             # subcommands, see below
python figmosha.py status
```

Port and project come from `project.json`; never hardcode 8787.
Bridge down: `.\start-bridge.ps1` (bash: `./start-bridge.sh`).
Plugin not connected: ask the user — Plugins → Development → `Figmosha · <project>` → Run.
Anything unclear about the chain: `python figmosha.py doctor` names the fix.

`--set NAME=value` defines a const before your code. The value is **JSON when
it parses and a string when it does not**, which is what makes `--set N=[0,4,8]`
an array and `--set ID=185:21880` not a syntax error — and what makes
`--set KEYS={"a":"b"}` an *object*, so a script calling `JSON.parse(KEYS)` throws
on its first line. Two spellings remove the guess:

```bash
--set-str  KEYS='{"a":"b"}'     # always a string — for ids, and for JSON your script parses
--set-json SCALE='[0,4,8]'      # always JSON — an unparseable value is an error here
--set      KEYS=@keys.json      # read the value from a file (long key lists, Windows quoting)
```

Exit codes, so a runner need not read prose: **0** ok · **1** the script threw ·
**2** bad arguments · **3** no plugin (`--wait-plugin N` waits for it) ·
**4** timed out or the file is busy.

Output is deliberately compact: one line per node, sizes rounded, results
capped at 64 KB and logs at 200 lines with an explicit truncation marker. Add
`--raw` when you need the untouched JSON — full precision, `value`, full stack.

## Several Figma files open at once

`figmosha sessions` lists what's connected. One session — nothing to think
about. Two or more, and a command without a target comes back **409 with the
list of candidates**:

```
figmosha: 2 Figma files are connected - say which one
   s-mt0d2eic-sg28fw  «Aurora» — page «Page 1»
   s-mt0d4mg4-rvv9h8  «Kite folio» — page «8 projects»
```

**Never retry a 409 by picking one yourself.** Editing the wrong Figma file is
silent and expensive to undo. Either the user already told you which file this
thread is about — then set it once and keep it:

```powershell
$env:FIGMOSHA_SESSION = "Aurora"     # id, alias, file name, or a name prefix
```

— or ask them. Per call it's `--session <name>`.

Rules that follow from how Figma works, not from Figmosha:

- **One session is one document, not one page.** `figma.currentPage` is
  document-wide, so two threads cannot split a single file by page — they would
  switch pages under each other. Work across pages by node id with
  `await h.node(id)`, which loads that node's page for you; `h.sel()` and the
  `page` alias are off-limits in such scripts.
- **One synchronous JS thread, and nothing can interrupt it.** This is the fact
  the rest of the page is downstream of. A walk that needs five minutes holds
  the thread for five minutes; giving up on the request does not stop it.
- **Scripts in one file run one at a time.** The bridge serialises them, so a
  long script makes the next one wait rather than interleave. Different files
  run in parallel. A 504 tells you which half of the time was which:
  `waited_ms` vs `ran_ms`.
- **Your timeout is not the plugin's.** `-t N` says how long *you* wait. After
  that the script keeps running, the bridge remembers it, and the next request
  to that file is refused with `busy` rather than stacked onto a thread that is
  still working. `figmosha sessions` shows `BUSY` and for how long; `pending`
  and `queued` are not busy signals.
- **The plugin is bound to the file it was run in.** Switched files? The user
  runs the plugin again there; it becomes another session, it does not move.

## CLI subcommands — use them instead of hand-written exec

| Command | Use case |
|---|---|
| `figmosha doctor` | Diagnose bridge → plugin → Figma, with the fix for each break |
| `figmosha sessions` | Which Figma files are connected right now |
| `figmosha sel` | What the user has selected — one line per node |
| `figmosha vars [filter] [--type T]` | Which variables exist and what they resolve to, per mode. No filter — a map of collections, not a dump of the file. `--library` for collections this file consumes |
| `figmosha styles [filter]` | Local paint / text / effect / grid styles with their values |
| `figmosha where <id>` | Path from the page down, with each ancestor's size and sizing mode — the answer to «why did this move» |
| `figmosha overrides <id>` | What an instance overrides against its main component, layer by layer |
| `figmosha props <id> [--all] [-c]` | Everything set on one node, with variables and styles **by name** — the fastest answer to "is this bound or hardcoded?". `-c` adds a row per child with its sizing, for "why did this move" |
| `figmosha tree <id> [--depth N] [--layout]` | Explore structure. **Depth 3 by default**; a cut branch says `… +N deeper`, and three or more identical siblings collapse into one row (`--no-collapse` to see them all) |
| `figmosha find <id> name=Button` | Locate by exact name (`name~Btn` = substring) |
| `figmosha find <id> type=INSTANCE` | Filter by type (also `text=`, `text~`). Prints 100 rows, then says how many were left (`--limit N`). **Does not descend into instances** — `--nested` does; `--count` skips building the list |
| `figmosha each <id> -f s.js --split 2 --state run.jsonl` | Run one script over a subtree, a unit at a time, writing state as it goes. `--resume` continues an interrupted run. This is how a big file is scanned |
| `figmosha sessions [--reset SID]` | Which files are connected and which are `BUSY`. `--reset` only makes the bridge forget — it cannot stop a running script |
| `figmosha set <id> gap=16 fill=#f5f5f5` | Change literal values — the alternative to a hand-written exec |
| `figmosha bind <id> gap=space/md fill=surface/bg` | Bind the same keys to variables **by token name** |
| `figmosha text <id> "новий"` | Edit text, fonts loaded for you |
| `figmosha variant <id> "Property 1=Default"` | Switch variant |
| `figmosha clone <id> --right --gap 100` | Duplicate adjacent |
| `figmosha rm <id> [<id>…]` | Delete; `rm sel` takes the **whole** selection |
| `figmosha icomp <key>` | Import a library component and instantiate it |
| `figmosha init --name X` | Claim this copy for a project (port + plugin identity) |
| `figmosha update` | `git pull` **and** stamp the project's port and plugin id back into the plugin — one step instead of three, and it names the right follow-up (re-Run vs re-Import) |

Anywhere an id is taken, `page` and `sel` work too — `figmosha tree sel --layout`
dumps the selected subtree without hunting for its id first. In `set`, `bind`
and `rm`, `sel` means the **whole** selection, not its first node.

### `set` / `bind` — the keys

```
set   name text fill stroke sw radius gap pad w h x y opacity visible layout align
bind  fill stroke sw gap pad(+padTop…padLeft) radius(+radiusTL…radiusBL) w h opacity text
```

`fill=#rrggbb@50` and `fill=none`; `pad=n | v,h | t,r,b,l`; `w=fill|hug|<число>`;
`align=CENTER/MIN`; `layout=v|h|none|wrap`. In `bind`, a value of `none` removes
the binding. Keys are applied in a fixed order (layout → size → spacing → align),
not in the order typed, because auto-layout drops whatever is set too early.

Every mutation prints **before → after**, and a second arrow names the token:

```
94:12  QA-card [FRAME]
  gap       16  →  8 → spacing/sp-4
  radius    ! bind: no variable named «radius/rounded-md» — see: figmosha vars rounded-md
```

A key the node refuses is one line, not a failed command — the other keys and
the other nodes still go through. `--dry-run` prints the same table and writes
nothing. That output is the only undo there is: Figma's own history is not
reachable from here, so a value you may want back has to be read off the diff.

When the user says "this frame" or "the selected one", call `figmosha sel` —
don't ask them to find an id by hand.

## Helpers (`h.*`, available in every exec)

```
h.bF(node, idx, var) · h.bS(node, idx, var) · h.bN(node, prop, var)  bind fill / stroke / number
     var — token name («space/md», «Semantics/color/bg/default»), local id, or library key
     an ambiguous name throws with the candidates; it never picks one
h.applyStyle(node, kind, name)  kind: fill | stroke | text | effect | grid
h.findByName(root, name) · h.findAllByName(root, name)
h.dumpTree(node, {maxDepth, showSize, showText, showLayout})         indented tree string
h.withFonts(root, fn) · h.setText(node, text)                        font loading, done right
h.cloneNext(node, {direction, gap, name})
h.variant(inst, props) · h.variantsOf(inst)                          {current, groups, all}
h.sel() · h.resolve(id | "page" | "sel") · h.node(id) · h.var_(idOrKey)
h.hex("#1a2b3c") · h.solid(hex, opacity?) · h.frame(parent, {layout, spacing, padding, fill, radius, name})
h.importComp(key, {timeout}) · h.importVar(key, {timeout})   raced against a timer
h.walk(root, visit, {pruneInstances, budgetMs, maxNodes, maxDepth, maxHits, includeRoot, cursor})
     -> {found, visited, pruned, partial, cursor, reason}   the way to cross a big subtree
h.mainOf(instance)                                          the only supported route to a component
h.loadPageOf(node) · h.pages()                              dynamic-page loading, done for you
h.tick() · h.left() · h.stats()                             budget inside your own loops
```

`h.findByName`, `h.findAllByName` and `h.dumpTree` are **async** — they may have
to load a page first. `await` them.

All are `await`-able where they touch Figma's async API. Using them instead of
inline boilerplate saves ~70% of a script and avoids the classic mistakes —
frozen `node.fills`, missing `loadFontAsync`, auto-layout set in the wrong order:

```js
// Bad — node.fills is frozen, and this silently mutates a copy
const f = JSON.parse(JSON.stringify(node.fills));
f[0] = figma.variables.setBoundVariableForPaint(f[0], "color", v);
node.fills = f;

// Good
await h.bF(node, 0, v);
```

Same story for `await h.setText(node, "new")` instead of `loadFontAsync` +
`node.characters`, and `h.withFonts(root, fn)` before bulk-editing text.

## How exec evaluates code

```js
new Function("figma", "print", "h", `return (async () => { <YOUR CODE> })();`)(figma, print, HELPERS)
```

- `return ...` becomes the `result` field. **Forgot `return`? That's why the
  result looks empty.**
- `await` works everywhere.
- `print(...)` collects log lines (capped at 200).
- Exceptions → `{ok:false, error, hint?, stack, logs}` with HTTP 500. The bridge
  **adds a `hint`** for common errors (paint binding, frozen array, font not
  loaded, missing permission, appendChild order, variant typo). Read it first.

## Conventions

**Async APIs.** This plugin runs with `documentAccess: "dynamic-page"`, so the
synchronous lookups throw — see *Dynamic pages* below for the full table:

```js
const node = await h.node(id)                        // figma.getNodeByIdAsync
const main = await h.mainOf(instance)                // NOT instance.mainComponent
const comp = await h.importComp(key)                 // raced against a timer
```

**Auto-layout: order matters.** `resize()` / spacing / sizing are ignored if set
before `layoutMode`: into the tree → `layoutMode` → size → sizing mode →
spacing/padding. `h.frame` does that for you — prefer it.

**Two-stage for big builds.** Step 1: build the structure with hardcoded RGB and
*named* nodes. Step 2: walk by name and bind via `h.bF`/`h.bS`/`h.bN`. Verify
each step on its own.

**Don't take screenshots for verification.** The bridge returns data — check it:

```js
return (await h.node("...")).width
return root.findAll(n => n.type === "TEXT").map(t => t.characters)
```

`exportAsync({format:"PNG"})` exists for when you genuinely need pixels; it is
not an "is my code working" check.

## When something looks wrong

- **503 `plugin not connected`**: plugin window closed. Ask the user to Run it
  again. Exit code 3, so a runner can tell this from a broken script. For a long
  run, `--wait-plugin 300` pauses instead of failing.
- **504 timeout**: exit code 4. **Do not ask for a re-Run, and do not retry.**
  The script is still going inside Figma; a re-Run costs the session and a retry
  costs the thread twice. Check `figmosha sessions` — a `BUSY` row with a rising
  `running_ms` is the script still working, and it usually returns on its own.
  The answer is a smaller subtree (`each --split`), not a bigger timeout. Only
  if it truly never returns: `figmosha sessions --reset <sid>`, which makes the
  bridge forget without stopping anything.
- **504 `is busy`**: an earlier script whose caller gave up still owns this
  file's thread. Wait for it. Only this file is blocked.
- **409 with a list of files**: several files open, no target. Set the session; never guess.
- **`… permission not specified`**: edit `plugin/manifest.json` here, then ask the
  user to **re-import** (Manage plugins → remove → Import again). A manifest
  change needs a re-import, not just a re-Run.
- **`… truncated: N more characters`**: the result hit the cap. Narrow it
  (`tree --depth`, `find`, fewer fields) rather than re-running the same thing.
- **Switched Figma file → plugin disconnects**: it is bound to the file it
  started in, and switching closes the plugin window — taking *every* session
  with it, not just that file's. Start the plugin in the file you are about to
  work on and leave it in the foreground. Long runs must be resumable, because
  this will happen: `each --state … --resume`.
- **`did not settle in Ns`** from `importComp`: that key is unpublished or
  belongs to another file, and the call would never have returned. Resolve from
  the consuming side instead — compare `(await h.mainOf(inst)).key` against the
  keys you are looking for.
- **An out-of-memory crash**: the tab dies and the session disappears from
  `figmosha sessions` entirely. Nothing is at risk — the document is on the
  server and a read-only scan writes nothing. Read *Big files* before trying
  again; the same scan will do the same thing.


## Big files

Measured on a 15-page mockup file: one frame in it holds **53 439** instances,
and a scan that walked the whole thing resolved **876 849** main components
before the tab ran out of memory. Everything below follows from that run.

**Never walk a whole file, or a whole page, in one request.** A subtree per
request. `h.pages()` lists pages without loading them; `figmosha tree <id>
--depth 1` lists children without walking them. Both are cheap on purpose.

**Descend before you overrun, not after.** Splitting ahead of time is free;
splitting after a timeout costs the file's JS thread for minutes (see *504*
below). `figmosha each <id> -f scan.js --split 2 --state run.jsonl` does the
descending, runs one unit at a time, writes a line per unit and splits any unit
that times out instead of retrying it. `--resume` picks the run back up.

**Prune at `INSTANCE`.** An instance's children arrived with its component:
they are copies, not placements anyone made. In the measured run **0 of 339**
real hits were nested inside another instance, so pruning cost nothing and cut
the walk by orders of magnitude. `figmosha find` and `h.walk` prune by default;
`--nested` / `pruneInstances: false` looks inside.

**`mainComponent` is the expensive call, not the search.** Resolving one loads
the backing component — remote library ones included — and nothing releases it.
Filter on something cheap first, then resolve only the survivors. `h.mainOf`
counts them and warns through `print()` while the run can still be narrowed.

**`h.walk` over `findAll`.** `findAll` is synchronous, materialises every match
as a live node proxy and descends into instances. `h.walk` prunes, checks the
caller's deadline between nodes, and returns `{partial, cursor}` so an
interrupted walk can carry on:

```js
const r = await h.walk(root, n => n.type === "INSTANCE" ? n.id : undefined,
                       { pruneInstances: true, maxNodes: 20000 });
if (r.partial) print("stopped after " + r.visited + " (" + r.reason + ")");
```

**`h.tick()` in your own loops.** Nothing outside the plugin can interrupt a
running script, so a time limit only exists where the code looks at it.

**Write state after every unit.** Every long run here has been interrupted at
least once. `each --state` is that, done for you.

**One heavy file open at a time.** Pages, components and images share the tab's
memory with the document.

An out-of-memory crash puts nothing at risk: the document lives on the server,
and a read-only scan writes nothing. It costs the tab and the run.

## Dynamic pages

The plugin declares `"documentAccess": "dynamic-page"`. Without it, Figma
guarantees the **whole document is loaded** before the plugin's first line runs
— every page, every node, 20–30 s of it, never released. On the measured file
that was most of an out-of-memory budget spent before a single command was
sent. With it, a page loads when something asks for it.

The price is that the synchronous lookups throw. Everything under `h.*` is
already async, so this only affects hand-written `exec` code:

| Instead of | Write |
|---|---|
| `instance.mainComponent` | `await h.mainOf(instance)` |
| `component.instances` | `await component.getInstancesAsync()` |
| `figma.getNodeById(id)` | `await h.node(id)` |
| `figma.getStyleById(id)` | `await figma.getStyleByIdAsync(id)` |
| `figma.variables.getVariableById(id)` | `await h.var_(id)` |
| `figma.getLocalPaintStyles()` (and Text/Effect/Grid) | `await figma.getLocalPaintStylesAsync()` |
| `node.fillStyleId = id` (and stroke/effect/grid/text) | `await node.setFillStyleIdAsync(id)` |
| `figma.currentPage = page` | `await figma.setCurrentPageAsync(page)` |
| `style.consumers` | `await style.getStyleConsumersAsync()` |
| `page.children` / `page.findAll` on another page | `await h.loadPageOf(node)` first |
| `figma.on("documentchange")` | `page.on("nodechange")` / `figma.on("stylechange")` |

`h.resolve` — which every CLI subcommand goes through — loads the page of the
node it returns, so `tree`, `find`, `props`, `where`, `set`, `bind` and the rest
work on any page without you doing anything.

`figma.root.findAll` / `findAllWithCriteria` / `findOne` need
`await figma.loadAllPagesAsync()` first, **and that brings the whole legacy cost
back**. It is a last resort. The supported shape for a scan is one subtree per
request: `h.pages()` → `h.loadPageOf(page)` → `h.walk(...)`, or just
`figmosha each`.

A project whose scripts have not been migrated yet can stay on the old
behaviour with `python figmosha.py init --document-access legacy` (sticky in
`project.json`), then re-import the plugin. It is deprecated: Figma has required
the field for every new published plugin since April 2024.

## Local setup

`project.json` holds this copy's name and port — single source of truth for the
bridge and the CLI. Machine-specific notes go in `CLAUDE.local.md` (gitignored).

Figma imports the plugin **from this folder**. After editing `plugin/code.js` or
`plugin/ui.html`, ask the user to re-Run the plugin; a running plugin keeps the
code it started with. After `manifest.json`, they must re-Import.

Updating Figmosha overwrites `plugin/manifest.json` and `plugin/ui.html`, which
carry this project's id and port. `python figmosha.py update` does the whole
ritual — release those two, pull, re-stamp — and refuses if anything else is
uncommitted. By hand it is `git checkout -- plugin/manifest.json plugin/ui.html`,
`git pull`, `python figmosha.py init`, in that order.
