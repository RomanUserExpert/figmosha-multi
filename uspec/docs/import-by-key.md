# uSpec templates — what is in the file, and why the keys do not work yet

Read 2026-09-07 from **uSpec template (Community)**, page `Templates`, a duplicate of the
Community file in a personal drafts folder. Read-only; nothing was written to either file.
The seven templates themselves are captured, node for node, in
`uspec/templates/templates.json` — see `uspec/templates/README.md`.

`firstrun` Step 6 wants these seven keys in `uspecs.config.json` → `templateKeys`. They are
listed here so nobody has to re-open the file to find them — but **see the blocker below
before writing them into the config.**

| `templateKeys` field | Template | Node id | Key | `#anchor` layers |
|---|---|---|---|---|
| `screenReader` | Screen reader | `8:301` | `7f14b0bf8c9f5a9f2fb68a81499a9ae62a5e57f9` | 17 |
| `colorAnnotation` | Color Annotation | `55:39` | `68ecb4a6d224b49bf352d544d20d5f8959cd2a1c` | 20 |
| `anatomyOverview` | Anatomy | `9:60` | `2eda939e2dc18221abebf31feb365b16a7728f83` | 23 |
| `apiOverview` | API | `8:296` | `de99462687058408e13da0989c77f3f2e8b61e3a` | 31 |
| `propertyOverview` | Property | `9:59` | `b268f37f8554132234d49096a1a570fd955a9944` | 7 |
| `structureSpec` | Structure | `8:299` | `e0765deec357cb1bcf2e26adee0831f4a4e3e2a9` | 17 |
| `motionSpec` | Motion | `22:80` | `b1b93cc1bd8c95e4ea3686e0092f36e800bef0e6` | 32 |

All seven are plain `COMPONENT`s (not sets) on a single page. **147 `#anchor` layers** in
total — the named layers each render skill clones and fills. `fontFamily` is **`Inter`**
(113 of 113 TEXT nodes).

## The blocker: these keys hang, they do not reject

Tried from the working file (**<your Figma file>**), each raced against a 15 s
timer: **all seven timed out** — 15 948, 16 006, 16 001, 15 997, 16 000, 15 999 and 24 037 ms.
Not resolved, not rejected. Never settled.

It has a clean explanation. Three key kinds behave three different ways:

| Key | Behaviour | Measured |
|---|---|---|
| Published in a reachable team library | **resolves fast** | `logs-tab-bar` 907 ms, `arrow-down` 4 ms |
| Unpublished, belonging to the **open** file | **rejects fast** | the open file's own key, 264 ms, message `undefined` |
| Unpublished, belonging to **another** file | **hangs, never settles** | these seven, 15–24 s |

So the discriminator is **publication status**, not connection warmth. Figma's typings promise
a rejection when there is no published component with that key; in practice that promise only
holds when the key belongs to the open document. For a key from a file the client cannot
resolve, the request simply never comes back.

The tempting inference — that the first import in a session pays a cold-fetch cost — is
wrong. It does not. A longer timeout will not help.

## The two ways forward

Both are the designer's call; neither has been done.

1. **Publish** *uSpec template (Community)* as a team library. Then the seven keys resolve, and
   `firstrun` works exactly as written, with keys stable across every documented file. Needs a
   team project and a library slot.
2. **Paste** the seven templates into each documented file, on one "uSpec templates" page, and
   address them by **node id**. The Figmosha adapter already supports this: a `templateKeys`
   value containing `:` is treated as a node id and resolved with `h.resolve(id)` instead of
   `importComponentByKeyAsync`. Costs one page per file, and the ids are per-file — so
   `templateKeys` becomes a map keyed by `figma.root.name`. Works today, needs no publishing.

The node ids in the table above are **valid only inside the Community duplicate**. Pasting the
templates elsewhere creates new ids, which have to be re-read in that file.

## What not to do

Do not rebuild the templates by hand in JS. There are 147 named anchor layers whose fonts, auto-layout
and row templates every render script assumes, and `npx uspec-skills update` rewrites those
scripts against the real template layers on each upgrade — a hand-maintained divergence with no
end.

**Capture and replay is the third way out, and it is the one this integration took.**
`uspec/templates/capture.js` reads the seven templates out of the Community duplicate once and
writes every node, property and `#anchor` name into `templates.json`; `frame.js` replays one as
a detached FRAME beside the component being documented. No import, no publishing, no per-file
page — and because the capture is a recording rather than a reimplementation, re-running
`capture.js` after a `uspec-skills` upgrade is how it stays current.

## Not the same as the built-in branch

`firstrun` Step 4 asks whether you are a Uber employee. "Yes" writes seven **hardcoded** keys
for Uber's internal library and skips every Figma call; "No" asks for a link to your own
template library. The hardcoded keys are Uber-internal and were not tested here. The file above
is the "No" branch's answer.
