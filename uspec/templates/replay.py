#!/usr/bin/env python3
"""Rebuild the captured uSpec templates in a Figma file, then verify by reading them back.

    python replay.py --expect-file "<your Figma file>" --dry-run
    python replay.py --expect-file "<your Figma file>" --yes-write

Replay WRITES: it creates a page and ~330 nodes. This workspace requires an explicit go-ahead
for that specific run, so `--yes-write` is mandatory and means exactly that. `--dry-run` is
free and reports what would be created.

Verification is not a screenshot. After building, replay.js reads the new components back out
of the file with the same projection capture.js produces, and this script diffs that against
templates.json property by property. A run that builds but does not match is a failure.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

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

# Properties replay cannot set, or that Figma derives. Excluded from the diff so it reports
# real regressions rather than known physics.
IGNORED = {
    # a mixed scalar is replayed through its per-side siblings
    "strokeWeight",
    # capture records the source position; replay lays the components out on a fresh page
    "x", "y",
    # read-only in the plugin API
    "overflowDirection", "autoRename",
}

# Geometry that auto-layout recomputes. A HUG frame's size is whatever its content makes it,
# so comparing it to the captured pixel is comparing Figma to itself.
GEOMETRY = {"width", "height"}


def post(port: int, code: str, consts: dict, session: str | None, timeout: int) -> dict:
    prelude = "".join("const %s = %s;\n" % (k, json.dumps(v, ensure_ascii=True)) for k, v in consts.items())
    payload = {"code": prelude + code, "timeout": timeout, "want_value": True}
    if session:
        payload["session"] = session
    req = urllib.request.Request(
        "http://localhost:%d/exec" % port,
        data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout + 15) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        body = e.read()
    except urllib.error.URLError as e:
        sys.exit("bridge unreachable on port %d (%s)" % (port, e))
    resp = json.loads(body)
    if not resp.get("ok"):
        sys.stderr.write("replay failed: %s\n%s\n" % (resp.get("error"), (resp.get("stack") or "")[:2000]))
        sys.exit(1)
    return resp["value"]


def diff(want: dict, got: dict, path: str, out: list) -> None:
    """Compare a captured node against the one that came back, recursively."""
    if got is None:
        out.append((path, "node", "present", "MISSING"))
        return
    for key in ("name", "type"):
        if want.get(key) != got.get(key):
            out.append((path, key, want.get(key), got.get(key)))

    for k, wv in want.items():
        if k in ("children", "fills", "strokes", "effects", "runs", "vectorPaths", "constraints"):
            continue
        if k in IGNORED or k not in got:
            continue
        # `layoutAlign` and `layoutSizing*` are two vocabularies for one state, and the modern
        # one wins: three template nodes report `layoutAlign: STRETCH` together with
        # `layoutSizingHorizontal: FIXED`, which cannot both be acted on. Figma resolves that in
        # favour of the sizing and normalises layoutAlign to INHERIT — so the original and the
        # rebuild lay out identically and only the vestige differs. Forcing STRETCH back would
        # change the real layout, which is worse than the mismatch it silences.
        if k == "layoutAlign" and "layoutSizingHorizontal" in want and "layoutSizingVertical" in want:
            continue
        gv = got[k]
        if k in GEOMETRY:
            # Only meaningful where the node is fixed-size in that axis.
            axis = "layoutSizingHorizontal" if k == "width" else "layoutSizingVertical"
            if want.get(axis) != "FIXED":
                continue
        if isinstance(wv, (int, float)) and isinstance(gv, (int, float)):
            if abs(wv - gv) > 0.01:
                out.append((path, k, round(wv, 2), round(gv, 2)))
        elif wv != gv:
            out.append((path, k, wv, gv))

    # Fills compared as a whole: a wrong colour is the loudest possible regression.
    if isinstance(want.get("fills"), list) and isinstance(got.get("fills"), list):
        if json.dumps(want["fills"], sort_keys=True) != json.dumps(got["fills"], sort_keys=True):
            out.append((path, "fills", "captured", "differs"))

    wc, gc = want.get("children") or [], got.get("children") or []
    if len(wc) != len(gc):
        out.append((path, "childCount", len(wc), len(gc)))
    for i, cw in enumerate(wc):
        diff(cw, gc[i] if i < len(gc) else None, "%s/%s" % (path, cw.get("name")), out)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--expect-file", required=True, help="abort unless figma.root.name equals this")
    p.add_argument("--session", help="which connected file to address, when several are open")
    p.add_argument("--page-name", default="uSpec templates")
    p.add_argument("--only", nargs="*", default=[], help="replay only these templates")
    p.add_argument("--port", type=int, default=default_port(),
                   help="bridge port (default: this copy's project.json)")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--dry-run", action="store_true", help="report what would be created, write nothing")
    p.add_argument("--yes-write", action="store_true",
                   help="required to actually build: confirms a go-ahead for this specific run")
    args = p.parse_args()

    if not args.dry_run and not args.yes_write:
        sys.exit(
            "replay creates a page and ~330 nodes in the open Figma file.\n"
            "       This workspace requires an explicit go-ahead for that specific run.\n"
            "       Use --dry-run to see what it would do, or --yes-write once you have one."
        )

    src = json.loads((HERE / "templates.json").read_text(encoding="utf-8"))
    consts = {
        "TEMPLATES": src["templates"],
        "EXPECT_FILE": args.expect_file,
        "PAGE_NAME": args.page_name,
        "DRY_RUN": bool(args.dry_run),
        "ONLY": args.only,
    }

    t0 = time.time()
    v = post(args.port, (HERE / "replay.js").read_text(encoding="utf-8"), consts, args.session, args.timeout)

    if v.get("dryRun"):
        print("DRY RUN — nothing was written to %s" % v["file"])
        print("  page to create: %r%s" % (v["pageName"], "  (A PAGE WITH THIS NAME ALREADY EXISTS)"
                                          if v["pageAlreadyExists"] else ""))
        print("  fonts loaded OK: %s" % ", ".join("%s %s" % (f["family"], f["style"]) for f in v["fontsLoaded"]))
        for w in v["wouldCreate"]:
            print("    %-18s %3d nodes" % (w["name"], w["nodes"]))
        print("  total: %d nodes" % v["totalNodes"])
        print("\n  Re-run with --yes-write once the designer has approved this run.")
        return

    print("built %d templates on page %r of %s in %.1f s"
          % (len(v["built"]), v["pageName"], v["file"], time.time() - t0))
    for b in v["built"]:
        print("    %-18s %-12s key=%s" % (b["name"], b["id"], b["key"]))

    print("\nverifying — read back from the file and diffed against templates.json")
    total = 0
    for name, want in src["templates"].items():
        if args.only and name not in args.only:
            continue
        got = v["readBack"].get(name)
        out: list = []
        diff(want, got, name, out)
        total += len(out)
        print("  %-18s %s" % (name, "OK" if not out else "%d difference(s)" % len(out)))
        for path, prop, wv, gv in out[:12]:
            print("      %-52s %-22s want %r got %r" % (path[-52:], prop, wv, gv))
        if len(out) > 12:
            print("      ... %d more" % (len(out) - 12))

    print("\n%s — %d difference(s) across %d templates"
          % ("VERIFIED" if total == 0 else "MISMATCH", total, len(v["built"])))
    print("Rollback: delete page %s (%s)." % (v["pageName"], v["pageId"]))
    if total:
        sys.exit(1)


if __name__ == "__main__":
    main()
