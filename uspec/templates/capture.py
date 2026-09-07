#!/usr/bin/env python3
"""Capture the uSpec templates out of Figma into a portable JSON description.

    python capture.py --session "uSpec template (Community)"

Read-only. Posts capture.js through the Figmosha bridge and writes templates.json next to it.
The description is what replay.py rebuilds from, and what git diffs when the templates change
upstream.
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


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--session", help="Figma file holding the templates, when several are connected")
    p.add_argument("--port", type=int, default=default_port(),
                   help="bridge port (default: this copy's project.json)")
    p.add_argument("--timeout", type=int, default=240)
    p.add_argument("--out", default=str(HERE / "templates.json"))
    args = p.parse_args()

    payload = {
        "code": (HERE / "capture.js").read_text(encoding="utf-8"),
        "timeout": args.timeout,
        "want_value": True,
    }
    if args.session:
        payload["session"] = args.session

    req = urllib.request.Request(
        "http://localhost:%d/exec" % args.port,
        data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=args.timeout + 15) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        body = e.read()
    except urllib.error.URLError as e:
        sys.exit("bridge unreachable on port %d (%s)" % (args.port, e))

    resp = json.loads(body)
    if not resp.get("ok"):
        sys.stderr.write("capture failed: %s\n%s\n" % (resp.get("error"), (resp.get("stack") or "")[:1500]))
        sys.exit(1)

    v = resp["value"]

    if v["violations"]:
        sys.stderr.write(
            "\nREFUSING TO CAPTURE — the templates now contain things that do not survive a move\n"
            "to another file. Replay would silently produce a different frame:\n"
        )
        for x in v["violations"]:
            sys.stderr.write("  - %s\n" % x)
        sys.stderr.write(
            "\nThis check is the whole reason capture-and-replay is safe. Do not remove it;\n"
            "re-read ../uspec-templates.md and decide what the new content needs.\n"
        )
        sys.exit(1)

    Path(args.out).write_text(json.dumps(v, ensure_ascii=True, indent=2), encoding="utf-8")

    print("captured from %s in %.1f s" % (v["capturedFrom"], time.time() - t0))
    for t in v["perTemplate"]:
        print("  %-18s %3d nodes, %3d anchors" % (t["name"], t["nodes"], t["anchors"]))
    print("  %-18s %3d nodes, %3d anchors" % ("TOTAL", v["totalNodes"], v["totalAnchors"]))
    print("  fonts required: %s" % ", ".join("%s %s" % (f["family"], f["style"]) for f in v["fontsRequired"]))
    print("  -> %s (%.0f KB)" % (args.out, Path(args.out).stat().st_size / 1024))

    gaps = v.get("fidelityGaps") or []
    if gaps:
        print("\n  %d fidelity gap(s) — properties that read `mixed` and replay cannot reproduce" % len(gaps))
        for g in gaps:
            print("    %-10s %-16s %s" % (g["type"], g["property"], g["node"]))
        print("  These are recorded, not silent. Check them against the replayed frame.")


if __name__ == "__main__":
    main()
