#!/usr/bin/env python3
"""project.json — identity of one Figmosha copy.

One copy of Figmosha serves one project: one port, one plugin entry in Figma's
menu. Everything that has to agree on those two facts — the bridge, the CLI,
the launchers, the generated plugin manifest — reads them from here, so there
is exactly one place to change and nothing to keep in sync by hand.

    {"name": "Northwind", "port": 8834, "created": "2026-08-19"}

A copy without project.json is a *zero copy*: freshly cloned, not yet claimed
by any project. `figmosha init` turns it into a project copy.
"""

import json
import re
import socket
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "project.json"

# Reported by the bridge in GET / and printed by --version. One number for the
# whole copy, so a client can tell an old bridge from a new one — the handshake
# is the only place where that is visible at all.
VERSION = "3.2.0"

# 8787 stays with the zero copy, so project ports start above it. 100 slots is
# far more projects than anyone runs, and keeps the numbers short to read.
PORT_BASE = 8800
PORT_SPAN = 100


def load(root: Path | None = None) -> dict | None:
    """The project config, or None if this copy has not been initialized."""
    path = (Path(root) / "project.json") if root else CONFIG
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        raise SystemExit(f"figmosha: {path} is not valid JSON — {e}")

    if not isinstance(cfg.get("name"), str) or not cfg["name"].strip():
        raise SystemExit(f"figmosha: {path} has no 'name'")
    if not isinstance(cfg.get("port"), int):
        raise SystemExit(f"figmosha: {path} has no numeric 'port'")
    return cfg


def save(cfg: dict, root: Path | None = None) -> Path:
    path = (Path(root) / "project.json") if root else CONFIG
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


def slug(name: str) -> str:
    """Project name -> a token safe for a plugin id: 'Northwind DS' -> 'northwind-ds'."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "project"


def port_for(name: str) -> int:
    """Deterministic port from the project name.

    Deterministic so two people initializing the same project independently
    land on the same number. The result is still written to project.json as a
    literal, so renaming the project later does not move the port under a
    plugin that was already imported.
    """
    return PORT_BASE + zlib.crc32(name.encode("utf-8")) % PORT_SPAN


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    """Can we bind it right now? False also covers 'someone else is listening'."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def occupant(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> str | None:
    """Name of the project whose bridge holds this port, if it is a bridge at all.

    Returns the project name, "" for a bridge that has no project (a zero copy
    started with an explicit --port), or None when the port is held by
    something that is not a Figmosha bridge.

    Addressed by IP rather than by name: `init` probes several ports in a row,
    and on Windows each "localhost" probe waits out an IPv6 attempt first.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=timeout) as r:
            info = json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None
    if info.get("service") != "figmosha-bridge":
        return None
    return info.get("project") or ""
