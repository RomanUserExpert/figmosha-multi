# extract — uSpec's extraction, driven through the Figmosha bridge

`_base.json` is normally produced by the **uSpec Extract** Figma plugin, driven by its own
UI. This folder produces the same file by running uSpec's phase code as exec scripts sent
through `POST /exec`, so the extraction half of the pipeline needs no plugin but Figmosha's.

Neither plugin is modified. The only divergence from uSpec's source is a read-only patch to
phases F and G, applied at build time and described below.

Origin: a 2026-09-07 investigation that ran the whole pipeline this way, end to end,
against a copy of a real design-system library.

## The pipeline

```bash
# once — a scratch clone, not vendored here (127 MB)
git clone --filter=blob:none https://github.com/redongreen/uSpec.git
cd uSpec && git checkout 1e25e9b8bc2e694689ba0489594f38807a341f14
cd figma-plugin && npm install

# build the bundle (re-run after a uSpec bump)
node build-bundle.mjs --src=<path to the clone>

# extract — read-only, nothing is written to Figma
python extract.py --set-id <component-set-id> \
    --expect-file "<your Figma file>" \
    --out <slug>-_base.json

# check it against uSpec's own validator, then stage it
node validate.mjs <slug>-_base.json --src=<path to the clone>
npx uspec-skills@0.3.3 component-md prepare --base <slug>-_base.json --json
```

| File | What it is |
|---|---|
| `build-bundle.mjs` | Asserts the pinned commit, applies the read-only patch, bundles `entry/figmosha-entry.ts` with esbuild → `uspec-extract.bundle.js` (gitignored — a build output). |
| `entry/figmosha-entry.ts` | The bundle entry: re-exports uSpec's phases as `USPEC.*`. Adds nothing. |
| `extract.py` | The orchestrator. Guards the open file, runs the phases in three calls, assembles `_base.json` on disk exactly as `figma-plugin/src/code.ts` assembles it. |
| `validate.mjs` | Wrapper around uSpec's `validate-base.mjs` — see *Two traps*, below. |
| `bench*.js`, `run_exec.py`, `probe*.js`, `findkey.py`, `bench/` | The 2026-09-07 investigation material. `bench/bench_n160.json` is the original proof: 160 variants, one call, 1.78 MB. |

## Why the phases run read-only

uSpec's phases F and G are the pipeline's **only Figma writers**: each creates a temporary
instance, turns every BOOLEAN on, measures it and removes it again — 22 instances for
`<slug>`. Under the plugin those writes are the designer's; under Figmosha they would be
the agent's. Read-only is the default because most extractions do not need them at all, not
because the instances are dangerous — see the rule below.

So `build-bundle.mjs` patches both phases to honour `globalThis.USPEC_READONLY` and measure
the **variant node** instead. For a component with no BOOLEAN properties and no SLOTs the two
measurements are the same thing — `setProperties({})` is a no-op and `createInstance()`
reproduces the variant's geometry — so `<slug>` loses nothing. With booleans present,
read-only mode measures the un-revealed state and `extract.py` says so in
`_extractionNotes.warnings`.

`--reveal` runs the phases the plugin's way. **It does not ask per run.** The designer's rule,
2026-09-07: *the component itself must never change; on a temporary instance anything goes* —
and phases F and G only ever touch instances they created. `--yes-write` is still accepted so
older invocations keep working, but it is ignored.

The rule is enforced, not remembered. Every reveal run counts the nodes on the current page, on
the component's page, and in the component set itself, before and after, and checks that every
`createInstance` has a matching `remove`. A run that leaves one node behind or moves the set
**fails and writes no `_base.json`**. That check is why the permission could be given at all —
do not relax it to make a run pass.

The patch is anchored on exact source text. When a uSpec upgrade moves those lines the build
**fails** rather than quietly producing a bundle that writes to Figma — that failure is the
tripwire, do not work around it by loosening the anchor.

## The three calls

The plugin is stateless between exec calls, so all state lives in `extract.py`.

| Call | Phases | Notes |
|---|---|---|
| 1 | guard, A, B, H | Asserts `figma.root.name` against `--expect-file` before anything else. |
| 2…n | E | One call per `--batch N` variants; default is one call for all of them. |
| n+1 | C, D, F, G, F′, I | Receives the style/variable id sets and a trimmed variant list. |

`buildFirstGuess` needs every variant's walked tree but never its colour or layout walk — the
bulk of phase E's output — so only `id`, `name`, `variantProperties` and `treeHierarchical` go
back up in the last call.

Measured on `<slug>` [`<component-set-id>`], 160 variants, 1 089 nodes: **3 calls, 16 s, 3.47 MB**,
zero Figma mutations.

## The classification checklist

The plugin shows the designer a list of INSTANCE children and stamps every row
`user-selected` — meaning *shown to the designer and not disputed*, not *clicked*.
`extract.py` prints the same list and takes the answers as
`--classify "<name>=constitutive|referenced|decorative"`.

- Answered → `classificationEvidence: ["user-selected"]`, reason *"Set by designer via
  figmosha-extract."*
- Unanswered and ambiguous → `["headless-default"]`, defaulted to `referenced`, which is the
  plugin UI's own default.

Both pass `prepare`. The difference is one readiness flag: `childCompositionUserSelected`.
`create-component-md` Step 4.5 resolves headless-default entries itself with one warning, so
the answer is a quality choice, not a gate.

### Standing answer: shared icons are `referenced`

The designer settled this on 2026-09-07, as a rule rather than a one-off: **an icon that comes
from a shared set is `referenced`, always.** Do not ask again per component; pass
`--classify "<name>=referenced"` and move on.

The reasoning it came from, measured in the sandbox fork rather than argued: the `icon` set
[`<icon-set-id>`] has **4 650 instances across 40 distinct components** — `input`, `date-picker`,
`action-overlay`, `button-ghost` and the rest — and `arrow-down` [`<remote-comp-id>`] has **453 across 10**.
A component used that widely has an identity of its own, so its property surface belongs in its
own spec. Copying it into every parent's API guarantees those copies drift.

The rule covers shared icons. A child that only exists inside its parent is still a judgement
call — run Q1/Q2 from `create-component-md` Step 3.5.

## Where a composed child's original lives

A node id is not navigable. Every entry in `_childComposition` that names a component set now
also carries where its original sits, so a reader of the spec can go and look at it instead of
hunting by id. `extract.py` resolves this after composition, read-only, and writes both a
sentence into `classificationReason` (the only free-text channel the renderer carries into the
`.md`) and structured fields for anything that wants to build its own link:

| Field | Meaning |
|---|---|
| `originResolved` | whether the node could be found at all |
| `originRemote` | `true` when the component belongs to another file |
| `originPageName` | the page it sits on, for a local component |
| `originUrl` | a link to that page — `null` for a remote one |
| `originKey` | the published component key |
| `originFile` | the file the page belongs to |

**The local / remote split is the whole point.** A local child gets a page and a link. A remote
one has no parent and no page in this file, and the plugin API cannot name the file it came
from — so it is told that it is remote and given its key, rather than pointed at a page that
does not contain it. `<slug>` has one of each: `icon` is on page `↳ Icons` here, while
`arrow-down` is remote and lives elsewhere.

## `fileKey`

`figma.fileKey` is `undefined` under a development plugin, so the key comes from the
`FILE_KEYS` map in `extract.py`, looked up by the file the bridge reports — add a row rather
than passing `--file-key` twice. An unknown file yields `"unknown-file"`, a null `figmaUrl`
and a warning; the validator accepts it and only the provenance links stop resolving.

`_meta.extractionSource` is `"mcp"` — the validator's enum is `plugin | mcp`, and JS executed
through a bridge is what "mcp" names here. `pluginVersion` records what actually ran.

## Two traps

- **uSpec's validator silently passes everything when run as a CLI on Windows.** Its entry
  point is guarded by ``import.meta.url === `file://${process.argv[1]}` ``, which is never true
  here (`file:///C:/…` vs `C:\…`), so `node scripts/validate-base.mjs <file>` exits 0 without
  validating — including on a file with a required key deleted. Use `validate.mjs`, which
  imports the exported function. Confirmed both ways against a deliberately broken file.
- **The clone checks out with CRLF on Windows.** `build-bundle.mjs` normalises before matching
  its anchors; anything else reading that source must do the same.

## Where the output lives

`extract.py --out` writes wherever it is told. `prepare` then stages its own copy under
`.uspec-cache\<slug>\` together with five evidence dumps — 21 MB for `<slug>` — and
plans `components\<slug>.md`. Both paths are gitignored: they regenerate from the base file
and are never reviewed by hand. Where the `_base.json` itself should live is still open —
question 6 of the plan.
