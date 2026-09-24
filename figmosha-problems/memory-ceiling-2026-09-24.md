# Figmosha 3.0 on a heavy file: a memory ceiling the script can't avoid

Measured on **2026-09-24** during a clean re-scan of **UIR - FM - Controls** for
`custom-control` (`custom-control-usage\v3-2026-09-24\`). This was the first big run on
**Figmosha 3.0**: `documentAccess: "dynamic-page"`, `h.walk` pruned at `INSTANCE`, `h.mainOf`,
`figmosha each --split 2 --state … --resume`. Everything the README recommends was in place.

**The short version.** Figmosha 3.0 fixes the thread and resume failures. It does not fix the
memory ceiling. The Figma tab still runs out of memory on the table pages, even though
the script makes few calls and only a handful of `mainOf`. Merely *touching* a heavy
screen loads it into the tab, and nothing loaded is released. The only thing that frees memory is
reopening the file. The working tactic is **crash → reopen → `--resume`**, and a run over this
file is **several tab sessions**, not one.

This corrects §2 of `README.md`, which pins the out-of-memory crash on the *number* of
`mainComponent` resolutions (876 849 on 2026-09-18). That number is not required. See below.

---

## What happened

Three tab sessions, three out-of-memory crashes (*"This file has run out of browser memory …
Open in recovery mode"*). Each time the plugin disappeared from `figmosha sessions`.

| Tab session | Done before the crash | Heavy units (table screens) |
|---|---|---|
| 1 | pages 0–9 (light) + 9 units of page 10 *Tree table control* | ~9 |
| 2 | 1 + 25 units of page 10 | ~26 |
| 3 | last 5 units of page 10 + 64 units of page 11 *Table control* | ~69 |

A "unit" is one level-2 subtree from `each --split 2`, usually one screen section of 5–10
top-level instances. After pruning, the script resolved **5–9 main components per unit**,
a few hundred in all, not hundreds of thousands. The tab died anyway.

The ceiling is not a fixed number of units: 9, 26 and 69 heavy units per session. It depends on
what else the tab held: session 1 had already walked ten pages, and session 3 started on a
file freshly reopened from a light page.

---

## Where the time and memory actually go

Measured on cold units of page 10, in a freshly opened tab:

| Step | Cost | Note |
|---|---|---|
| `figma.getNodeByIdAsync` on an unloaded page | **11.3 s** | the page loads on demand, as dynamic-page intends |
| `h.walk` over a 6-node section, pruned at `INSTANCE` | **6.8 s cold**, 1–7 ms warm | touching the frame loads its content, including the tables inside the instances |
| `n.variantProperties` on the 5 instances found | **5 ms** | cheap, no load |
| `h.mainOf` on the same 5, first time per component key | **9.0 s** | loads the remote library component |
| `h.mainOf` again, same keys, other units | **0–1 ms** | the loaded component stays; per-key cost, not per-call |

Unit durations on the table pages: **median 3.6 s, max 27 s** (page 10: 81 units; page 11: 64).
Light pages ran in 15–45 ms per unit.

**Cold vs warm is the whole story.** Page 2 *Action overlay* took **175 s** on its first pass.
The same page, same script, straight afterwards: **1 s**. The test page's first unit hit
the 58 s deadline cold and ran in 0.2 s warm. Whatever gets loaded stays, which makes the second
pass fast. The same thing fills the tab.

So the cost is **how heavy each thing you touch is**, not how many calls you make. A
`custom-control` holding a tree-table is a big component, and every screen with tables is big
content. `pruneInstances` stops the *walk* at the instance, but reading the instance and resolving its
main component still makes Figma load what is behind it.

---

## What does *not* help

- **Pruning harder.** The walk visits 5–10 nodes per unit already.
- **A cheaper pre-filter before `mainOf`.** `variantProperties` is cheap (5 ms) and does narrow it:
  a `custom-control` has the axes `content-qty, direction, istitle`. But on these pages 3 of every 5
  top-level instances *are* hits, so they must be resolved anyway, and the frame content is
  already loaded by the walk before the filter runs.
- **Smaller units.** Splitting changes how much each request does, not how much the tab holds
  at the end.
- **Recovery mode.** It exists to reduce memory use. It is not a place to run the plugin from.
  Reopen the file normally.

## What does help

- **Resumability, fully.** `each --state … --resume` lost nothing across three crashes. The
  unit in flight at the crash is recorded as `failed: plugin disconnected mid-request`, and
  `--resume` **retries it** (it did, 14 ms on the next session). Everything already `ok` is skipped.
- **Leaving a light page in the viewport** when reopening, so Figma doesn't also render the
  heavy page in the tab.
- **Budgeting sessions, not requests.** Plan a heavy file as N tab sessions. On this file, roughly
  25–70 table screens per session.

---

## Things the run exposed in the tooling

1. **`each` treats a partial result as `ok`.** When `h.walk` or the script stops at its own
   deadline and returns `partial: true`, `each` records the unit as done and moves on. It splits
   only on a CLI timeout (exit 4). A driver has to read `partial` out of the value itself and
   re-run those units smaller. `v3-2026-09-24\run.py` does that.
2. **The state file keeps every attempt.** After a crash and a resume, the same unit appears as
   `failed` and then `ok`. A reader must take the **last** record per id. `run.py` at first
   read "any `failed`" and stopped on a page that was in fact complete.
3. **`each` output goes to stderr only at the end** from a driver's point of view. The id of the
   unit that was running when the tab died is visible only from the state file (the next line
   that never got written), not from a "started" record. A `started` line per unit would name the
   killer directly.
4. **`figmosha sessions` said `BUSY 0s (dispatched, not started)`** for a request that had
   been dispatched several minutes earlier, while the tab was already failing. Not a reliable
   liveness signal either. `return 1` remains the test.

## Rules that follow

- A heavy mockup file is **several tab sessions**. Plan for it; don't fight it.
- `mainOf` costs **per distinct component key**, and touching a frame costs **per screen, cold**.
  Neither is released until the file is reopened.
- After an out-of-memory crash, reopen **normally**, stay on a light page, re-Run the plugin,
  `--resume`.
- Read the state file **last record per id**, and treat `partial: true` as not done.
