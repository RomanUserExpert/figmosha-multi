# Figmosha — failure modes

What goes wrong when you drive Figma through the bridge, why, and what to do instead.
Not a substitute for `Figmosha\CLAUDE.md` — that file documents the API. This one documents
the ways a run dies.

Everything here was measured on **2026-09-18** during the `custom-control-usage\` scan of
**UIR - FM - Controls**, unless another date is given. Every number is from that run.

The common thread: **the plugin is one synchronous JavaScript thread inside the Figma tab,
and it shares that tab's memory with the document.** Almost every failure below is a
consequence of forgetting one of those two facts.

---

## 1. The CLI's timeout is not the plugin's timeout

**Symptom.** A request returns `timeout after 100s`. Every request after it also times out,
including `return 1`. The file looks dead.

**What actually happens.** `figmosha exec -t N` gives up after N seconds. The script inside
the plugin does not stop. `findAllWithCriteria` is synchronous, so a walk that needs five
minutes holds the thread for five minutes, and everything sent meanwhile waits behind it.

Measured: a request that walked all 15 pages of UIR - FM - Controls in one call was abandoned
by the CLI at 110 s. A trivial `return 1` sent afterwards timed out at 20 s, then again at
25 s. The thread came back on its own **~5 minutes** later. A later page-level scan reported
`ran_ms: 100017` against `waited_ms: 0` — it was not queued, it was *running*, inside a thread
already owned by something else.

**How to tell.** Send `return 1` with a short timeout. That is the only honest test.

**What not to trust.** `GET /sessions` reports `pending` and `queued`, and **neither is a busy
signal**: `pending` drops to 0 the moment a request is dispatched, while the thread is still
chewing. A session showing `pending: 0, queued: 0` answered a `return 1` with a timeout in
this run.

**What to do.** Wait it out — it usually returns. If it does not, re-run the plugin in Figma
(**Plugins → Development → Figmosha · Compatibl → Run**). Better: never send a request that
can overrun. An overrun is not a cheap retry; it costs minutes, and sometimes the plugin.

---

## 2. Out of memory — the expensive call is `mainComponent`, not the search

**Symptom.** Figma shows an out-of-memory error and the tab dies. The plugin vanishes from
`GET /sessions` entirely (`sessions: []`), rather than merely going quiet.

**What actually happens.** Three costs stack, and the obvious one is the smallest:

1. `findAllWithCriteria` returns an **array of live node proxies**, not a count. One frame in
   this file holds **53 439** instances; nine subtrees held over 20 000 each.
2. Reading `instance.mainComponent` forces Figma to **resolve and load the backing component**,
   including remote library components from Controls & Charts, Big components and Design
   System. The run resolved **876 849** of them in its completed units alone. What gets loaded
   this way is not released.
3. Pages do not unload. The scan touched all 15 pages; a file this size normally survives
   because a designer opens two or three.

On top of that, an overrun (§1) keeps running in the background after the CLI gives up, so at
several points two large walks were alive at once.

**Nothing is at risk.** A read-only scan writes nothing, and the document lives on the server.
An OOM is a crash of the local tab. There is nothing to roll back.

**What to do.**

- **Do not descend into `INSTANCE` subtrees.** This is where the volume is. An instance found
  inside another instance is a copy that arrived with its parent component, not a placement
  someone chose. In this run **0 of 339** hits were nested — pruning there costs nothing and
  cuts the walk by orders of magnitude.
- Resolve `mainComponent` only for candidates that survived a cheaper filter.
- Scan a **subtree per request**, not a page, and descend *before* a frame overruns rather
  than after. See §3.
- Keep one heavy file open at a time.

---

## 3. The unit of work is a subtree, and it must be chosen before the overrun

Page-per-request is the rule `corner-radius-binding\` and `migration-to-slots\` arrived at, and
it is **not small enough** for the mockup files. A single page here overran 100 s.

Splitting *after* a failure works but is expensive: each overrun costs the thread for minutes
(§1). Splitting *ahead of time* — list a node's children and scan each child — is cheap,
because `list` does no deep walk at all. In this run, top-level screens ran 28 s each while
their sections ran ~5 s each.

`custom-control-usage\scripts\scan_cc.py` does both: `--presplit N` descends N levels before
attempting anything, and an overrun still falls back to listing and descending. Reuse it rather
than writing a third copy.

---

## 4. `importComponentByKeyAsync` hangs on a remote library component

**Symptom.** No answer at all. Not an error, not a slow return — nothing.

Measured: importing one variant key of `custom-control` from a consuming file ran to the CLI's
**180 s** limit with no result.

**What to do.** Do not reach for it to find a library component's instances. Resolve from the
consuming side instead: walk the instances you can see and compare `mainComponent.key` against
the keys you are looking for. `ComponentNode.instances` is unreachable this way too, since you
cannot get the node.

---

## 5. `--set` injects JSON as a value, not as a string

`--set NAME=value` parses the value as JSON when it can, and passes it through as a string when
it cannot. So:

```bash
--set KEYS_JSON='{"a":"b"}'     # ->  const KEYS_JSON = {"a":"b"};   an OBJECT
--set ID=185:21880              # ->  const ID = "185:21880";        a STRING
```

A script that then calls `JSON.parse(KEYS_JSON)` throws on the first form. Write the tolerant
version and stop thinking about it:

```js
const KEYS = typeof KEYS_JSON === "string" ? JSON.parse(KEYS_JSON) : KEYS_JSON;
```

Scripts in `migration-to-slots\scripts\` still carry the fragile form.

---

## 6. The plugin is bound to one file, and switching files kills it

Opening a second file and running the plugin there gives **two sessions**, and any command that
could mean either returns the candidate list instead of guessing — name one with `--session` or
`FIGMOSHA_SESSION`. That part is by design and documented.

What is easy to lose: **switching files in Figma closes the plugin window**, and both sessions
go at once. In this run both files disconnected together, and the first sign was
`plugin not connected` on a file that had been answering a minute earlier.

**What to do.** Start the plugin in the file you are about to scan, and leave that file in the
foreground for the whole run. Long runs must be resumable, because this will happen.

---

## Rules that follow

- **Never walk a whole file, or a whole page, in one request.** A subtree per request.
- **Descend before you overrun**, not after.
- **Prune at `INSTANCE`** unless nested copies are genuinely the thing being counted.
- **`return 1` is the liveness test.** `pending` and `queued` are not.
- **Write state after every unit.** Every long run here has been interrupted at least once —
  by an overrun, a disconnect, or an OOM — and resumability is what turned those into delays
  rather than restarts.
- **An overrun is not free.** Budget it as minutes of blocked thread and a possible lost plugin.
