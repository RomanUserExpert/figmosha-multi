#!/usr/bin/env python3
"""figmosha-extract — build uSpec's `_base.json` through the Figmosha bridge.

The uSpec Extract plugin is a Figma plugin driven by its own UI. This orchestrator runs
the same phase code — bundled from the pinned uSpec commit by build-bundle.mjs — as exec
scripts sent through `POST /exec`, and assembles the result on disk exactly the way
`figma-plugin/src/code.ts` assembles it in the plugin.

Read-only by default. uSpec's phases F and G measure temporary instances; the bundle's
patched build measures the variant nodes instead unless `--reveal` is given, and `--reveal`
additionally requires `--yes-write` because it changes the Figma file.

    python extract.py --set-id <component-set-id> --out <slug>-_base.json \
        --expect-file "<your Figma file>"

Phase E can be split across calls with `--batch N` for large sets; every other phase runs
once. The plugin is stateless between calls, so all state lives here.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

def default_port() -> int:
    """This copy's port, from project.json — the same number the bridge binds."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        import project
        cfg = project.load()
        if cfg:
            return int(cfg["port"])
    except Exception:
        pass
    return 8787


HERE = Path(__file__).resolve().parent
BUNDLE = HERE / "uspec-extract.bundle.js"

# uSpec's phase code, pinned. Keep in step with build-bundle.mjs.
USPEC_COMMIT = "1e25e9b"
USPEC_PLUGIN_VERSION = "2.7.0"

# `figma.fileKey` is undefined under a development plugin, so the key that goes into `_meta`
# has to come from somewhere else. It is per project, not per tool, so it lives in
# `uspec/file-keys.json` next to this folder — the same shape as `project.json`: written once
# for your copy, gitignored, never shipped. Copy `file-keys.example.json` to start one.
#
#     { "My Design System": "AbC123…", "My Icons": "XyZ789…" }
#
# The key is what makes `_meta.figmaUrl` resolve. Without an entry the file is recorded as
# "unknown-file" with a null url and a warning — the validator accepts that, and only the
# provenance links stop working. `--file-key` overrides for a one-off run.
FILE_KEYS_PATH = HERE.parent / "file-keys.json"


def load_file_keys() -> dict:
    if not FILE_KEYS_PATH.exists():
        return {}
    try:
        keys = json.loads(FILE_KEYS_PATH.read_text(encoding="utf-8"))
    except ValueError as e:
        die("%s is not valid JSON (%s)" % (FILE_KEYS_PATH, e))
    if not isinstance(keys, dict):
        die("%s must be an object of \"<file name>\": \"<file key>\"" % FILE_KEYS_PATH)
    return keys


# --------------------------------------------------------------------------- transport


class Bridge:
    def __init__(self, port: int, session: str | None, timeout: int) -> None:
        self.port = port
        self.session = session
        self.timeout = timeout
        self.calls = 0
        self.total_bytes = 0
        self.total_wall = 0.0

    def exec(self, code: str, consts: dict[str, Any] | None = None, label: str = "") -> Any:
        prelude = ""
        for name, value in (consts or {}).items():
            prelude += "const %s = %s;\n" % (name, json.dumps(value, ensure_ascii=True))
        payload: dict[str, Any] = {
            "code": prelude + code,
            "timeout": self.timeout,
            "want_value": True,
        }
        if self.session:
            payload["session"] = self.session

        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        req = urllib.request.Request(
            "http://localhost:%d/exec" % self.port,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout + 15) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
        except urllib.error.URLError as e:
            die("bridge unreachable on port %d (%s). Is start-bridge running?" % (self.port, e))
        wall = time.time() - t0

        resp = json.loads(raw)
        self.calls += 1
        self.total_bytes += len(raw)
        self.total_wall += wall
        note(
            "  %-22s %6.1f s  %8.0f KB  plugin %s ms"
            % (label, wall, len(raw) / 1024, resp.get("elapsed_ms"))
        )
        if not resp.get("ok"):
            sys.stderr.write("\nEXEC FAILED (%s)\n  %s\n" % (label, resp.get("error")))
            sys.stderr.write((resp.get("stack") or "")[:2000] + "\n")
            for line in (resp.get("logs") or [])[:20]:
                sys.stderr.write("  log: %s\n" % line)
            sys.exit(1)
        return resp.get("value")


def note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def die(msg: str) -> None:
    sys.stderr.write("ERROR: %s\n" % msg)
    sys.exit(1)


# --------------------------------------------------------------------------- the scripts
#
# Each is appended to the bundle, which exposes the phases as USPEC.*. Constants are
# emitted ahead of the code by Bridge.exec.

RESOLVE = """
let __n = await figma.getNodeByIdAsync(SET_ID);
if (!__n) { await figma.loadAllPagesAsync(); __n = await figma.getNodeByIdAsync(SET_ID); }
if (!__n) throw new Error("node " + SET_ID + " does not exist in " + figma.root.name);
"""

SCRIPT_CENSUS = RESOLVE + """
const set = __n;
if (set.type !== "COMPONENT_SET" && set.type !== "COMPONENT")
  throw new Error(SET_ID + " is a " + set.type + ", not a component or component set");
const isCS = set.type === "COMPONENT_SET";
const vs = isCS ? set.children : [set];
const A = await USPEC.runPhaseA(SET_ID);
const B = await USPEC.runPhaseB();
const H = await USPEC.runPhaseH(SET_ID);
return {
  rootName: figma.root.name,
  pageName: figma.currentPage.name,
  fileKeyLive: typeof figma.fileKey === "string" ? figma.fileKey : null,
  nodeType: set.type,
  nodeName: set.name,
  variantIds: vs.map((v) => v.id),
  variantNames: vs.map((v) => v.name),
  nodeCount: set.findAll(() => true).length + 1,
  A: A,
  B: B,
  H: H
};
"""

SCRIPT_PHASE_E = RESOLVE + """
const set = __n;
const vs = set.type === "COMPONENT_SET" ? set.children : [set];
const win = vs.slice(START, START + COUNT);
const variants = [];
const styleIds = new Set();
const inline = {};
const varIds = new Set();
const warnings = [];
const perVariantMs = [];
const perVariantBytes = [];
const collectStyleIds = (n) => {
  if (!n || typeof n !== "object") return;
  if (n.typography && typeof n.typography.styleId === "string" && n.typography.styleId)
    styleIds.add(n.typography.styleId);
  if (Array.isArray(n.children)) for (const c of n.children) collectStyleIds(c);
};
for (const v of win) {
  const t0 = Date.now();
  const r = await USPEC.runPhaseE(v.id);
  perVariantMs.push(Date.now() - t0);
  const entry = {
    id: r.id,
    name: r.name,
    variantProperties: r.variantProperties,
    dimensions: r.dimensions,
    treeHierarchical: r.treeHierarchical,
    treeFlat: r.treeFlat,
    colorWalk: r.colorWalk,
    layoutTree: r.layoutTree,
    strokeSemantics: r.strokeSemantics
  };
  perVariantBytes.push(USPEC.safeStringify(entry).length);
  variants.push(entry);
  if (r._selfCheck.missingChildren.length > 0)
    warnings.push(
      'HIERWALK_MISSING_CHILDREN on variant "' + r.name + '": ' +
      r._selfCheck.missingChildren.map((m) => m.name).join(", ")
    );
  collectStyleIds(r.treeHierarchical);
  for (const e of r.colorWalk)
    if (typeof e.styleId === "string" && e.styleId) styleIds.add(e.styleId);
  for (const k of Object.keys(r.styleIdInlineSamples))
    if (!inline[k]) inline[k] = r.styleIdInlineSamples[k];
  for (const id of r.referencedVariableIds) varIds.add(id);
}
return {
  variants: variants,
  styleIds: Array.from(styleIds),
  inline: inline,
  varIds: Array.from(varIds),
  warnings: warnings,
  perVariantMs: perVariantMs,
  perVariantBytes: perVariantBytes
};
"""

# Phases C, D, F, G, F' and I in one call. F and G honour globalThis.USPEC_READONLY, which
# the patched bundle reads — see build-bundle.mjs.
SCRIPT_COMPOSE = """
globalThis.USPEC_READONLY = READONLY;
const B = await USPEC.runPhaseB();
const C = await USPEC.runPhaseC(STYLE_IDS, INLINE);
const D = await USPEC.runPhaseD(B, VAR_IDS);
const F = await USPEC.runPhaseF(SET_ID, BOOLEAN_KEYS);
const G = await USPEC.runPhaseG(SET_ID, BOOLEAN_KEYS, SLOT_PREFS);
const fg = USPEC.buildFirstGuess(PARENT_NAME, VARIANTS_LITE, DEFAULT_VARIANT_ID, PROP_DEFS);
const all = fg.children.concat(fg.ambiguousChildren);
const checklist = all.map((c) => ({
  name: c.name,
  nodeType: c.nodeType,
  mainComponentName: c.mainComponentName,
  parentSetName: c.parentSetName,
  subCompSetId: c.subCompSetId,
  topLevelInstanceId: c.topLevelInstanceId,
  placementCount: c.placementCount,
  presentIn: (c.presentInVariants || []).length,
  guess: c.classification,
  reason: c.classificationReason
}));
// A designer's answer keys on the child's name (what the checklist shows) or, when two
// children share a name, on topLevelInstanceId.
const answered = [];
const merged = all.map((c) => {
  const chosen =
    (c.topLevelInstanceId && CLASSIFY[c.topLevelInstanceId]) || CLASSIFY[c.name] || null;
  if (chosen) {
    answered.push(c.name);
    return Object.assign({}, c, {
      classification: chosen,
      classificationReason: "Set by designer via figmosha-extract.",
      classificationEvidence: ["user-selected"]
    });
  }
  if (c.classification === null) {
    return Object.assign({}, c, {
      classification: "referenced",
      classificationReason:
        "Headless default — ambiguous child, no designer answer supplied to figmosha-extract.",
      classificationEvidence: ["headless-default"]
    });
  }
  return c;
});
const composition = {
  children: merged.filter((c) => c.classification !== null),
  ambiguousChildren: [],
  guessConfidence: "high"
};
const I = await USPEC.runPhaseI(composition);
return {
  C: C,
  D: D,
  F: F,
  G: G,
  composition: composition,
  checklist: checklist,
  answered: answered,
  Iwalks: I.walks,
  Iwarnings: I.warnings,
  guessConfidence: fg.guessConfidence
};
"""


# Where each composed child's original actually lives. A node id alone is not navigable, and a
# spec that names a component without saying where to find it sends the reader hunting. Read-only.
#
# The distinction this makes is the point: a LOCAL child has a page in this file and gets a link;
# a REMOTE one belongs to another file entirely, and a link into this file would be wrong.
SCRIPT_ORIGINS = """
await figma.loadAllPagesAsync();
const out = {};
for (const id of SUB_COMP_IDS) {
  const n = await figma.getNodeByIdAsync(id);
  if (!n) { out[id] = { found: false }; continue; }
  let p = n;
  while (p.parent && p.type !== "PAGE") p = p.parent;
  const page = p && p.type === "PAGE" ? p : null;
  out[id] = {
    found: true, name: n.name, type: n.type,
    remote: n.remote === true, key: n.key || null,
    pageName: page ? page.name : null, pageId: page ? page.id : null
  };
}
return out;
"""


def origin_sentence(origin: dict, file_name: str, file_key: str) -> tuple[str, dict]:
    """One readable sentence saying where the original is, plus the structured fields.

    The sentence goes into `classificationReason` because that is the only free-text channel the
    renderer carries through to the `.md`'s Composition list; the fields are there for anything
    that wants to build its own link.
    """
    if not origin.get("found"):
        return ("Original not resolvable in this file.",
                {"originResolved": False, "originRemote": None, "originPageName": None,
                 "originUrl": None, "originFile": None})

    if origin.get("remote"):
        # No page, no parent chain — the plugin API cannot name the file a remote component
        # comes from, so say that rather than pointing at the wrong file.
        return ("Original lives in another library file (remote component, key %s) — not on any "
                "page of %s, so no page link can be given from here." % (origin.get("key"), file_name),
                {"originResolved": True, "originRemote": True, "originPageName": None,
                 "originUrl": None, "originFile": None, "originKey": origin.get("key")})

    url = None
    if file_key and file_key != "unknown-file" and origin.get("pageId"):
        url = build_figma_url(file_key, file_name, origin["pageId"])
    return ("Original is on page %s of %s%s." % (origin.get("pageName"), file_name,
                                                 (" — %s" % url) if url else ""),
            {"originResolved": True, "originRemote": False, "originPageName": origin.get("pageName"),
             "originUrl": url, "originFile": file_name, "originKey": origin.get("key")})


# Counts the reveal check compares before and after. Temporary instances are appended to the
# current page, which is not necessarily the component's page, so both are measured.
SCRIPT_COUNT = RESOLVE + """
const set = __n;
let page = set;
while (page.parent && page.parent.type !== "DOCUMENT") page = page.parent;
return {
  currentPage: figma.currentPage.name,
  currentPageNodes: figma.currentPage.findAll(() => true).length,
  componentPage: page.name,
  componentPageNodes: page.findAll(() => true).length,
  setNodes: set.findAll(() => true).length + 1,
  setChildren: set.type === "COMPONENT_SET" ? set.children.length : 0
};
"""


def verify_reveal(before: dict, after: dict, mutations: list) -> list[str]:
    """The designer's rule, made checkable.

    Phases F and G may do anything they like to an instance they created, and nothing at all to
    the component. So: every createInstance must be matched by a remove, and every count that
    describes the file rather than the instance must come back identical. A failure here means
    either an instance was left behind or something outside one was touched — both are the thing
    the rule forbids, and neither is visible in the extracted data.
    """
    problems = []

    created = sum(1 for m in mutations if m.get("action") == "createInstance")
    removed = sum(1 for m in mutations if m.get("action") == "remove")
    if created != removed:
        problems.append("mutations do not balance: %d createInstance, %d remove" % (created, removed))

    for key, label in (
        ("currentPageNodes", "nodes on the current page"),
        ("componentPageNodes", "nodes on the component's page"),
        ("setNodes", "nodes in the component set"),
        ("setChildren", "variants in the component set"),
    ):
        if before.get(key) != after.get(key):
            problems.append("%s changed: %s -> %s" % (label, before.get(key), after.get(key)))

    return problems


# --------------------------------------------------------------------------- assembly
#
# Ports of the post-phase passes in figma-plugin/src/code.ts. Kept as separate functions so
# each can be checked against the source it mirrors.


def slugify(name: str) -> str:
    """safe.ts slugify()."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower())
    return re.sub(r"-+", "-", s.strip("-"))


def build_figma_url(file_key: str, file_name: str, node_id: str) -> str:
    """figmaUrl.ts buildFigmaUrl()."""
    slug = re.sub(r"[^A-Za-z0-9._-]", "", re.sub(r"\s+", "-", (file_name or "").strip())) or "file"
    return "https://www.figma.com/design/%s/%s?node-id=%s" % (
        file_key,
        slug,
        node_id.replace(":", "-"),
    )


def drop_raw_hex_inside_instances(variants: list[dict], warnings: list[str]) -> None:
    """code.ts: raw-hex colorWalk entries inside a crossed INSTANCE boundary describe the
    child component's own artwork and never inform the parent's spec. Tokened entries stay."""

    def is_raw_hex_inside(e: Any) -> bool:
        if not isinstance(e, dict) or not e.get("subComponentName"):
            return False
        return not (e.get("styleId") or e.get("boundVariableId"))

    dropped = 0
    by_sub: dict[str, int] = {}
    for variant in variants:
        for arr_name in ("colorWalk", "revealedColorWalk"):
            arr = variant.get(arr_name)
            if not isinstance(arr, list):
                continue
            kept = []
            for e in arr:
                if is_raw_hex_inside(e):
                    dropped += 1
                    key = e.get("subComponentName") or "(unknown)"
                    by_sub[key] = by_sub.get(key, 0) + 1
                else:
                    kept.append(e)
            variant[arr_name] = kept
    if dropped:
        detail = ", ".join(
            "%s: %d" % (k, n) for k, n in sorted(by_sub.items(), key=lambda kv: -kv[1])
        )
        warnings.append(
            "Dropped %d raw-hex colorWalk entries that lived inside crossed INSTANCE "
            "boundaries (%s). Tokened entries were preserved." % (dropped, detail)
        )


def check_constitutive_children(
    variants: list[dict], composition: dict, warnings: list[str]
) -> None:
    """code.ts: every constitutive top-level INSTANCE placement must have a walked subtree
    in the variant where it appears."""
    indexes: dict[str, dict[str, Any]] = {}
    for variant in variants:
        index: dict[str, Any] = {}

        def walk(node: Any) -> None:
            if not isinstance(node, dict):
                return
            if isinstance(node.get("id"), str):
                index[node["id"]] = node
            for child in node.get("children") or []:
                walk(child)

        walk(variant.get("treeHierarchical"))
        indexes[variant["id"]] = index

    missing = []
    for child in composition.get("children", []):
        if child.get("classification") != "constitutive" or child.get("nodeType") != "INSTANCE":
            continue
        if child.get("origin") not in (None, "top-level"):
            continue
        for placement in (child.get("placementsByVariant") or {}).values():
            index = indexes.get(placement.get("variantId"), {})
            for node_id in placement.get("nodeIds", []):
                entry = index.get(node_id)
                if not entry or not isinstance(entry.get("children"), list) or not entry["children"]:
                    missing.append(child["name"])
    if missing:
        warnings.append(
            "Walked tree is missing children for constitutive instance(s): %s"
            % ", ".join(missing)
        )


# --------------------------------------------------------------------------- main


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--set-id", required=True, help="component or component-set node id")
    p.add_argument("--out", help="path for the assembled _base.json")
    p.add_argument("--expect-file", help="abort unless figma.root.name equals this")
    p.add_argument("--file-key", help="override the _meta.fileKey lookup")
    p.add_argument("--batch", type=int, default=0, help="variants per phase-E call (0 = all)")
    p.add_argument("--port", type=int, default=default_port(),
                   help="bridge port (default: this copy's project.json)")
    p.add_argument("--session", help="FIGMOSHA_SESSION equivalent when several files are open")
    p.add_argument("--timeout", type=int, default=300, help="per-call plugin timeout, seconds")
    p.add_argument("--context", help="_meta.optionalContext")
    p.add_argument(
        "--classify",
        action="append",
        default=[],
        metavar="NAME=constitutive|referenced|decorative",
        help="answer one checklist row; repeatable",
    )
    p.add_argument("--checklist", action="store_true", help="print the checklist and stop")
    p.add_argument(
        "--reveal",
        action="store_true",
        help="run phases F and G the plugin's way, on temporary instances. WRITES TO FIGMA.",
    )
    p.add_argument(
        "--yes-write",
        action="store_true",
        help="accepted and ignored; kept so older invocations still run (see --reveal)",
    )
    p.add_argument("--raw-dir", help="also dump each call's raw value here")
    args = p.parse_args()

    # `--reveal` does not ask for a per-run go-ahead. The rule it sits inside: the component
    # itself must never change; on a temporary instance anything goes. Phases F and G
    # only ever touch instances they created, so they sit inside that rule.
    #
    # The rule is enforced rather than remembered — see verify_reveal() below, which runs after
    # every reveal and fails the run if a single node was left behind or if the component set
    # moved. Do not remove that check to make a run pass.
    if not args.checklist and not args.out:
        die("--out is required unless --checklist is given")
    if not BUNDLE.exists():
        die("bundle missing: %s\n       Build it: node build-bundle.mjs --src=<uSpec clone>" % BUNDLE)

    classify: dict[str, str] = {}
    for item in args.classify:
        if "=" not in item:
            die("--classify wants NAME=classification, got %r" % item)
        key, value = item.split("=", 1)
        if value not in ("constitutive", "referenced", "decorative"):
            die("unknown classification %r for %r" % (value, key))
        classify[key] = value

    bundle = BUNDLE.read_text(encoding="utf-8")
    bridge = Bridge(args.port, args.session or os.environ.get("FIGMOSHA_SESSION"), args.timeout)
    raw_dir = Path(args.raw_dir) if args.raw_dir else None
    if raw_dir:
        raw_dir.mkdir(parents=True, exist_ok=True)

    def call(script: str, consts: dict[str, Any], label: str) -> Any:
        value = bridge.exec(bundle + script, consts, label)
        if raw_dir:
            (raw_dir / (label.replace(" ", "-").replace("/", "-") + ".json")).write_text(
                json.dumps(value, ensure_ascii=True), encoding="utf-8"
            )
        return value

    started = time.time()
    note("figmosha-extract — uSpec %s phases, %s" % (USPEC_COMMIT, "REVEAL" if args.reveal else "read-only"))

    # ---- call 1: guard, A, B, H -------------------------------------------------
    census = call(SCRIPT_CENSUS, {"SET_ID": args.set_id}, "census A+B+H")

    root_name = census["rootName"]
    note("  file: %s / %s" % (root_name, census["pageName"]))
    if args.expect_file and root_name != args.expect_file:
        die(
            "wrong file open.\n       expected: %s\n       open:     %s\n"
            "       Node ids repeat across a fork and its original, so this is the only guard."
            % (args.expect_file, root_name)
        )
    if not args.expect_file:
        note("  WARNING: --expect-file not given; the open file was not checked against anything.")

    phase_a = census["A"]
    variant_ids = census["variantIds"]
    component_name = phase_a["component"]["componentName"]
    prop_defs = phase_a["propertyDefinitions"]
    boolean_keys = [b["rawKey"] for b in prop_defs.get("booleans", [])]
    slot_prefs = [
        {"slotName": slot["name"], "componentId": pref["componentKey"]}
        for slot in prop_defs.get("slots", [])
        for pref in slot.get("preferredInstances", [])
    ]
    note(
        "  %s — %s, %d variants, %d nodes, %d booleans, %d slots"
        % (
            component_name,
            census["nodeType"],
            len(variant_ids),
            census["nodeCount"],
            len(boolean_keys),
            len(prop_defs.get("slots", [])),
        )
    )

    # ---- calls 2..n: phase E ----------------------------------------------------
    batch = args.batch if args.batch > 0 else len(variant_ids)
    variants: list[dict] = []
    style_ids: list[str] = []
    inline: dict[str, Any] = {}
    var_ids: list[str] = []
    warnings: list[str] = []
    per_variant_ms: list[int] = []

    for start in range(0, len(variant_ids), batch):
        count = min(batch, len(variant_ids) - start)
        label = "phase E %d-%d" % (start + 1, start + count)
        result = call(
            SCRIPT_PHASE_E, {"SET_ID": args.set_id, "START": start, "COUNT": count}, label
        )
        variants.extend(result["variants"])
        for sid in result["styleIds"]:
            if sid not in style_ids:
                style_ids.append(sid)
        for k, v in result["inline"].items():
            inline.setdefault(k, v)
        for vid in result["varIds"]:
            if vid not in var_ids:
                var_ids.append(vid)
        warnings.extend(result["warnings"])
        per_variant_ms.extend(result["perVariantMs"])

    if len(variants) != len(variant_ids):
        die("phase E returned %d variants, expected %d" % (len(variants), len(variant_ids)))

    # ---- call n+1: C, D, F, G, F', I --------------------------------------------
    # buildFirstGuess reads id, name and the walked tree of every variant. The colour and
    # layout walks are the bulk of phase E's output and it never touches them, so only the
    # tree goes back up.
    variants_lite = [
        {
            "id": v["id"],
            "name": v["name"],
            "variantProperties": v["variantProperties"],
            "treeHierarchical": v["treeHierarchical"],
        }
        for v in variants
    ]
    before_counts = call(SCRIPT_COUNT, {"SET_ID": args.set_id}, "count before") if args.reveal else None

    composed = call(
        SCRIPT_COMPOSE,
        {
            "SET_ID": args.set_id,
            "READONLY": not args.reveal,
            "STYLE_IDS": style_ids,
            "INLINE": inline,
            "VAR_IDS": var_ids,
            "BOOLEAN_KEYS": boolean_keys,
            "SLOT_PREFS": slot_prefs,
            "PARENT_NAME": component_name,
            "DEFAULT_VARIANT_ID": phase_a["defaultVariant"]["id"],
            "PROP_DEFS": prop_defs,
            "VARIANTS_LITE": variants_lite,
            "CLASSIFY": classify,
        },
        "compose C+D+F+G+I",
    )

    if args.reveal:
        after_counts = call(SCRIPT_COUNT, {"SET_ID": args.set_id}, "count after")
        muts = (composed["F"]["mutationsPerformed"] if composed["F"] else []) + (
            composed["G"]["mutationsPerformed"] if composed["G"] else []
        )
        problems = verify_reveal(before_counts, after_counts, muts)
        note("")
        if problems:
            sys.stderr.write(
                "REVEAL LEFT THE FILE CHANGED — this is the one thing reveal must never do.\n"
            )
            for x in problems:
                sys.stderr.write("  - %s\n" % x)
            sys.stderr.write(
                "  Look in Figma for a leftover instance of %s and delete it.\n"
                "  No _base.json was written.\n" % args.set_id
            )
            sys.exit(1)
        note(
            "  reveal verified: %d temp instances created and removed, "
            "page and component-set node counts unchanged"
            % sum(1 for m in muts if m.get("action") == "createInstance")
        )

    # ---- the checklist ----------------------------------------------------------
    print("\nChild composition checklist — %s" % component_name)
    print("%-28s %-10s %-7s %-14s %s" % ("child", "type", "×", "classification", "why"))
    for row in composed["checklist"]:
        answered = row["name"] in composed["answered"]
        mark = "*" if answered else (" " if row["guess"] else "?")
        shown = row["guess"] or "referenced"
        if answered:
            shown = classify.get(row["topLevelInstanceId"] or "", classify.get(row["name"], shown))
        print(
            "%s%-27s %-10s %-7s %-14s %s"
            % (mark, row["name"][:27], row["nodeType"], row["placementCount"], shown, (row["reason"] or "")[:60])
        )
    unanswered = [r["name"] for r in composed["checklist"] if r["guess"] is None and r["name"] not in composed["answered"]]
    if unanswered:
        print(
            "\n  ? = ambiguous, defaulted to `referenced` and stamped headless-default:\n"
            "      %s\n"
            "    Answer with --classify \"<name>=constitutive|referenced|decorative\" to stamp\n"
            "    user-selected instead. create-component-md Step 4.5 resolves them either way."
            % ", ".join(unanswered)
        )
    if args.checklist:
        note("\n--checklist: stopping before assembly.")
        return

    # ---- assemble ---------------------------------------------------------------
    phase_g = composed["G"]
    if phase_g:
        by_name = phase_g["revealedByVariantName"]
        cw_by_name = phase_g["revealedColorWalkByVariantName"]
        rep = phase_g["structuralRepresentativeByVariantName"]
        for variant in variants:
            if variant["name"] in by_name:
                variant["revealedTree"] = by_name[variant["name"]]
            if variant["name"] in cw_by_name:
                variant["revealedColorWalk"] = cw_by_name[variant["name"]]
            variant["revealedTreeRepresentative"] = rep.get(variant["name"], variant["name"])

    warnings.extend(composed["Iwarnings"])
    drop_raw_hex_inside_instances(variants, warnings)
    check_constitutive_children(variants, composed["composition"], warnings)

    if not args.reveal:
        detail = "%d booleans not revealed; %d slots not swapped" % (
            len(boolean_keys),
            len(prop_defs.get("slots", [])),
        )
        if boolean_keys or slot_prefs:
            warnings.append(
                "Read-only extraction: axisDiffs and revealedTree were measured on the variant "
                "nodes rather than on temporary instances (%s). Re-run with --reveal for the "
                "revealed geometry." % detail
            )
        else:
            warnings.append(
                "Read-only extraction: axisDiffs and revealedTree were measured on the variant "
                "nodes rather than on temporary instances. The component has no BOOLEAN "
                "properties and no SLOTs, so the two measurements are identical."
            )

    file_key = args.file_key or load_file_keys().get(root_name, "unknown-file")
    if file_key == "unknown-file":
        warnings.append(
            'No file key known for "%s"; _meta.fileKey is "unknown-file" and figmaUrl is null. '
            "Add a row to uspec/file-keys.json, or pass --file-key, to make the provenance "
            "links resolve." % root_name
        )

    # ---- where each composed child's original lives ----------------------------
    # A node id is not navigable. Every child that names a component set gets the page its
    # original sits on, and a link to it; a remote child gets told it is remote instead of
    # being pointed at a page in the wrong file.
    children = composed["composition"].get("children", [])
    sub_ids = sorted({c["subCompSetId"] for c in children if c.get("subCompSetId")})
    if sub_ids:
        origins = call(SCRIPT_ORIGINS, {"SUB_COMP_IDS": sub_ids}, "child origins")
        for child in children:
            sid = child.get("subCompSetId")
            if not sid or sid not in origins:
                continue
            sentence, fields = origin_sentence(origins[sid], root_name, file_key)
            child.update(fields)
            reason = (child.get("classificationReason") or "").rstrip()
            if sentence not in reason:
                child["classificationReason"] = (reason + " " + sentence).strip()
        note("  resolved the origin of %d composed child component(s)" % len(sub_ids))

    base = {
        "_meta": {
            "schemaVersion": "1",
            "extractedAt": _dt.datetime.now(_dt.timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "fileKey": file_key,
            "nodeId": args.set_id,
            "componentSlug": slugify(component_name),
            "optionalContext": args.context or None,
            "figmaUrl": build_figma_url(file_key, root_name, args.set_id)
            if file_key != "unknown-file"
            else None,
            # The validator's enum is plugin | mcp. JS executed through a bridge is what
            # "mcp" names here; the pluginVersion string says what actually ran.
            "extractionSource": "mcp",
            "pluginVersion": "figmosha-extract (uspec-extract %s phases, %s)"
            % (USPEC_PLUGIN_VERSION, USPEC_COMMIT),
        },
        "component": phase_a["component"],
        "variantAxes": phase_a["variantAxes"],
        "defaultVariant": phase_a["defaultVariant"],
        "propertyDefinitions": prop_defs,
        "variables": {
            "localCollections": census["B"]["localCollections"],
            "remoteCollections": composed["D"]["remoteCollections"],
            "resolvedVariables": {
                **census["B"]["resolvedVariables"],
                **composed["D"]["resolvedVariables"],
            },
        },
        "styles": composed["C"],
        "variants": variants,
        "crossVariant": composed["F"],
        "slotHostGeometry": phase_g["slotHostGeometry"] if phase_g else None,
        "ownershipHints": census["H"]["ownershipHints"] if census["H"] else [],
        "subComponentVariantWalks": composed["Iwalks"],
        "_childComposition": composed["composition"],
        "_extractionNotes": {
            "warnings": warnings,
            "mutationsPerformed": (composed["F"]["mutationsPerformed"] if composed["F"] else [])
            + (phase_g["mutationsPerformed"] if phase_g else []),
        },
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # ensure_ascii escapes U+2028/U+2029, which uSpec's validator rejects when literal.
    out.write_text(json.dumps(base, ensure_ascii=True, indent=2), encoding="utf-8")

    mutations = base["_extractionNotes"]["mutationsPerformed"]
    note("")
    note("  wrote %s — %.2f MB" % (out, out.stat().st_size / 1024 / 1024))
    note(
        "  %d calls, %.1f s total, %d variants, %d warnings, %d Figma mutations"
        % (bridge.calls, time.time() - started, len(variants), len(warnings), len(mutations))
    )
    if mutations:
        note("  MUTATIONS PERFORMED — this run changed the Figma file.")


if __name__ == "__main__":
    main()
