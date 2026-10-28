"""The shadow package cannot reach a vendor, and this is what keeps it so.

Level 0's promise is that measuring the agent cannot do anything to a
franchisee's tools. A promise in a docstring is worth little; what makes it
true is that `app/autopilot/shadow/` imports no connector factory and no vendor
client, so there is no code path from a replay to an HTTP request at all.

Checked two ways, because each catches what the other misses:

* every `import` in the package's own source, which names the file and line
  when somebody adds one; and
* the real import graph in a **subprocess**, which catches an indirect reach —
  importing `app.resolved.ledger`, say, which imports the factory three hops
  down. A subprocess because this suite has already imported half the
  application by the time it gets here, so `sys.modules` in this process
  proves nothing.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

FEATURE = pathlib.Path(__file__).resolve().parents[2] / "app" / "autopilot"
PACKAGE = FEATURE / "shadow"

#: The one module that may reach a connector, and the reason it exists: the
#: forward shadow has to read a vendor, so the seam is named here rather than
#: left to whoever adds the next import. Everything else under `app/autopilot/`
#: — the service, the gate, the suggestions and both routers — is held to the
#: same rule the replay is, because a page that can write to a vendor is a page
#: somebody writes to a vendor from.
SEAM = "wiring.py"

#: What a shadow run may not reach. The factory is the only thing that builds a
#: connector (CLAUDE.md, "Connector pattern"), so forbidding it forbids every
#: vendor call; the client modules are named as well so a direct construction
#: is caught with a clearer message than "no factory".
FORBIDDEN = (
    "app.connectors.factory",
    "app.connectors.ninjaone.client",
    "app.connectors.autotask.client",
    "app.connectors.bitdefender.client",
    "app.connectors.connectsecure.client",
    "app.resolved.ledger",
    "app.resolved.baselines",
    "app.tickets.enrich",
)


def _imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _feature_modules() -> list[pathlib.Path]:
    """Every module of the feature but the seam, both routers included."""
    return sorted(
        path
        for path in FEATURE.rglob("*.py")
        if path.name != SEAM and "__pycache__" not in path.parts
    )


def test_the_package_exists_and_is_being_checked() -> None:
    """A guard over an empty directory passes and proves nothing."""
    modules = sorted(path.name for path in PACKAGE.glob("*.py"))
    assert "replay.py" in modules
    assert len(modules) >= 5
    # And the wider guard is looking at the pages' own code, which is where
    # "no route under this feature calls a vendor" has to hold.
    names = {path.name for path in _feature_modules()}
    assert {"suggestions.py", "gate.py", "labelling.py", "results.py"} <= names


def test_no_module_of_the_feature_imports_a_way_to_reach_a_vendor() -> None:
    """Every module under `app/autopilot/` but the seam, routers included.

    The suggestion pages write "correct" into our own database and put a note
    on a clipboard; neither is a vendor call, and what keeps that true is that
    the code behind them cannot make one.
    """
    for path in _feature_modules():
        for name in _imports(path):
            for forbidden in FORBIDDEN:
                assert not (name == forbidden or name.startswith(f"{forbidden}.")), (
                    f"{path.name} imports {name}, which can reach a vendor. The forward "
                    f"shadow's live reads are injected from app/autopilot/wiring.py, which "
                    f"is the one seam; nothing else under app/autopilot/ may import a "
                    f"connector itself."
                )


def test_importing_the_package_pulls_in_no_vendor_client() -> None:
    """The transitive check, in a process of its own."""
    probe = (
        "import sys\n"
        "import app.autopilot.shadow  # noqa: F401\n"
        "import app.autopilot.suggestions  # noqa: F401\n"
        "import app.autopilot.api  # noqa: F401\n"
        f"forbidden = {FORBIDDEN!r}\n"
        "reached = sorted(m for m in sys.modules if m in forbidden)\n"
        "print(','.join(reached))\n"
    )
    found = subprocess.run(  # noqa: S603  our own interpreter, a fixed script
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert found.returncode == 0, found.stderr[-2000:]
    assert found.stdout.strip() == "", (
        f"importing the feature reached {found.stdout.strip()} — neither a replay nor a "
        f"page behind a suggestion may be able to call a vendor, even indirectly"
    )
