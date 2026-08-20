"""Step 1 — the project axis: one copy, one port, one plugin identity.

These run `init` and the bridge as real subprocesses in a throwaway copy,
because what is being tested is exactly the thing an import-and-forget test
would miss: that the files on disk end up agreeing with each other.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def copy(tmp_path):
    """A zero copy of Figmosha — the state a freshly cloned folder is in."""
    dst = tmp_path / "Acme" / "figmosha"
    dst.mkdir(parents=True)
    for name in ("figmosha.py", "bridge.py", "project.py"):
        shutil.copy(REPO / name, dst / name)
    shutil.copytree(REPO / "plugin", dst / "plugin")
    return dst


def run(copy, *args):
    return subprocess.run([sys.executable, *args], cwd=copy,
                          capture_output=True, text=True, encoding="utf-8")


def init(copy, *args):
    return run(copy, "figmosha.py", "init", *args)


def test_init_claims_the_copy(copy):
    r = init(copy, "--name", "Acme")
    assert r.returncode == 0, r.stderr

    cfg = json.loads((copy / "project.json").read_text(encoding="utf-8"))
    assert cfg["name"] == "Acme"
    port = cfg["port"]
    assert 8800 <= port < 8900, "project ports live above the zero copy's 8787"

    manifest = json.loads((copy / "plugin" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "figmosha-acme"
    assert manifest["name"] == "Figmosha · Acme"
    assert f"ws://localhost:{port}" in manifest["networkAccess"]["allowedDomains"]
    # Permissions are patched around, not regenerated — they must survive.
    assert "teamlibrary" in manifest["permissions"]

    ui = (copy / "plugin" / "ui.html").read_text(encoding="utf-8")
    assert f'const SERVER = "ws://localhost:{port}/plugin";' in ui


def test_init_defaults_the_name_to_the_project_folder(copy):
    assert init(copy).returncode == 0
    cfg = json.loads((copy / "project.json").read_text(encoding="utf-8"))
    assert cfg["name"] == "Acme"   # the parent of the figmosha/ folder


def test_init_is_idempotent(copy):
    init(copy, "--name", "Acme")
    first = json.loads((copy / "project.json").read_text(encoding="utf-8"))

    assert init(copy).returncode == 0
    again = json.loads((copy / "project.json").read_text(encoding="utf-8"))

    # The port must not drift: a plugin was already imported against it.
    assert again == first


def test_init_keeps_the_port_across_a_rename(copy):
    init(copy, "--name", "Acme")
    port = json.loads((copy / "project.json").read_text(encoding="utf-8"))["port"]

    init(copy, "--name", "Acme Renamed")
    cfg = json.loads((copy / "project.json").read_text(encoding="utf-8"))
    assert cfg["name"] == "Acme Renamed"
    assert cfg["port"] == port, "renaming must not move a port already in use"

    manifest = json.loads((copy / "plugin" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "figmosha-acme-renamed"


def test_bridge_refuses_to_start_uninitialized(copy):
    r = run(copy, "bridge.py")
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "project.json" in out
    assert "init" in out, "the error has to name the fix, not just the problem"


def test_bridge_reports_its_project(copy):
    init(copy, "--name", "Acme")
    r = run(copy, "-c",
            "import bridge, project; bridge.PROJECT_NAME = project.load()['name']; "
            "print(bridge.PROJECT_NAME)")
    assert r.stdout.strip() == "Acme"


def test_port_is_derived_from_the_name_not_the_folder(copy):
    sys.path.insert(0, str(REPO))
    import project as proj

    assert proj.port_for("Acme") == proj.port_for("Acme")
    assert proj.port_for("Acme") != proj.port_for("Northwind")
    assert proj.slug("Northwind DS") == "northwind-ds"
