"""No AI anywhere in the automation engine, and this is what keeps it so.

A workflow is fixed, versioned steps written in code. The promise is that no
model call, no generated value and no dependency on the agent, the autopilot
or the Anthropic client can be on the path from an approval to an action — a
promise in a docstring is worth little, and what makes it true is that
`app/automation/` cannot import any of them.

Checked two ways, as the shadow package is checked against vendors
(`tests/autopilot/test_imports.py`), because each catches what the other
misses: every `import` in the package's own source, which names the file and
line when somebody adds one; and the real import graph in a **subprocess**,
which catches an indirect reach three hops down and is honest because this
suite has already imported half the application by the time it gets here.

The second rule is the same one in the other direction: no write-capable
connector exists, and the engine imports no connector at all. When write
access arrives, the only write path will be this engine, through write clients
used by nothing else — and this test is where that list of permitted imports
will be widened, deliberately, in a commit that says so.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "app" / "automation"

#: What the engine may not reach. The agent and the autopilot by package, so
#: a new module under either is covered without being named; the Anthropic
#: SDK by name; and the connector factory and every vendor client, because
#: until write access is designed nothing here talks to a vendor at all.
FORBIDDEN = (
    "app.agent",
    "app.autopilot",
    "anthropic",
    "app.connectors.factory",
    "app.connectors.ninjaone.client",
    "app.connectors.autotask.client",
    "app.connectors.bitdefender.client",
    "app.connectors.connectsecure.client",
    "app.resolved.ledger",
    "app.tickets.enrich",
    "app.reports.tbr.narrative",
    "app.reports.tbr.industry",
)


def _reaches(name: str) -> str | None:
    for forbidden in FORBIDDEN:
        if name == forbidden or name.startswith(f"{forbidden}."):
            return forbidden
    return None


def _imports(path: pathlib.Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.append((node.lineno, node.module))
            found.extend((node.lineno, f"{node.module}.{alias.name}") for alias in node.names)
    return found


def _modules() -> list[pathlib.Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def test_the_package_exists_and_is_being_checked() -> None:
    """A guard over an empty directory passes and proves nothing."""
    names = {path.name for path in _modules()}
    assert {"definitions.py", "states.py", "service.py", "runner.py", "practice.py"} <= names
    # The library and the loader are under the same guard: the actions, the
    # definition loader, and the YAML files it reads are all here.
    assert {"library.py", "base.py", "reads.py", "notes.py", "write.py", "reports.py"} <= names
    assert any(path.parent.name == "actions" for path in _modules())
    assert list((PACKAGE / "definitions").glob("*.yaml")), "no definitions to load"


def test_no_module_of_the_engine_imports_the_agent_the_autopilot_or_a_vendor() -> None:
    for path in _modules():
        for line, name in _imports(path):
            hit = _reaches(name)
            assert hit is None, (
                f"{path.relative_to(PACKAGE.parent.parent)}:{line} imports {name}, which is "
                f"{hit}. No AI and no vendor anywhere in the automation engine: a workflow is "
                f"fixed steps in code, and the only write path, when there is one, will be "
                f"write clients this package alone uses."
            )


def test_importing_the_engine_pulls_in_no_ai_and_no_vendor_client() -> None:
    """The transitive check, in a process of its own."""
    probe = (
        "import sys\n"
        "import app.automation  # noqa: F401  the package, which registers the workflows\n"
        "import app.automation.service  # noqa: F401\n"
        "import app.automation.runner  # noqa: F401\n"
        "import app.automation.practice  # noqa: F401\n"
        "import app.automation.library  # noqa: F401  the definition loader\n"
        "import app.automation.actions  # noqa: F401  the action library\n"
        "assert app.automation.library.definitions(), 'the loader loaded nothing'\n"
        f"forbidden = {FORBIDDEN!r}\n"
        "reached = sorted(m for m in sys.modules "
        "if any(m == f or m.startswith(f + '.') for f in forbidden))\n"
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
        f"importing the automation engine reached {found.stdout.strip()}. Nothing on the path "
        f"from an approval to an action may be able to call a model or a vendor, even "
        f"indirectly."
    )


def test_the_guard_would_catch_an_import() -> None:
    """The three above pass over a clean package; this proves they look."""
    assert _reaches("app.autopilot.config") == "app.autopilot"
    assert _reaches("app.agent.loop") == "app.agent"
    assert _reaches("anthropic") == "anthropic"
    assert _reaches("app.connectors.factory") == "app.connectors.factory"
    assert _reaches("app.automation.service") is None
    assert _reaches("app.models.automation") is None


# --- the poll script: no AI, a vendor read allowed -------------------------------------

SCRIPT = PACKAGE.parent.parent / "scripts" / "automation.py"

#: What the poll may not reach. It reads Autotask on purpose — that is the
#: poll — and importing any connector imports `app.connectors`, whose package
#: init registers every tool with `app.agent.registry` (the `@tool`
#: decorator and the schema it keeps; it calls nothing) and reads the
#: franchisee config, which composes `app.autopilot.config` (a pydantic
#: schema of label policy; it calls nothing). So the script's rule names the
#: modules that can reach a model rather than the two packages whole: the
#: Anthropic SDK, the agent loop and everything that drives it, the shadow
#: classifier and replay and the forward shadow, the review's narrative and
#: research, the ticket plan. Widening this list is a decision; narrowing it
#: is not.
NO_AI = (
    "anthropic",
    "app.agent.loop",
    "app.agent.conversations",
    "app.agent.prompts",
    "app.agent.selection",
    "app.autopilot.shadow",
    "app.autopilot.wiring",
    "app.autopilot.suggestions",
    "app.autopilot.findings",
    "app.autopilot.gate",
    "app.autopilot.labelling",
    "app.autopilot.api",
    "app.reports.tbr.narrative",
    "app.reports.tbr.industry",
    "app.reports.tbr.pipeline",
    "app.reports.tbr.sections",
    "app.tickets.plan",
    "app.tickets.enrich",
    "app.api.tickets",
)


def _reaches_ai(name: str) -> str | None:
    for forbidden in NO_AI:
        if name == forbidden or name.startswith(f"{forbidden}."):
            return forbidden
    return None


def test_the_poll_script_imports_no_ai() -> None:
    assert SCRIPT.exists()
    for line, name in _imports(SCRIPT):
        hit = _reaches_ai(name)
        assert hit is None, (
            f"scripts/automation.py:{line} imports {name}, which is {hit}. The poll reads "
            f"tickets and evaluates fixed rules; it never calls the classifier or any model."
        )
    assert _reaches_ai("app.autopilot.shadow.classify") == "app.autopilot.shadow"
    assert _reaches_ai("app.agent.loop") == "app.agent.loop"
    assert _reaches_ai("anthropic") == "anthropic"
    assert _reaches_ai("app.tickets.poller") is None, "the shared reader is the point"


def test_running_the_poll_script_pulls_in_no_ai() -> None:
    """The transitive check: import the script as a module and look at what
    came with it. The shared reader (`app.tickets.poller`) is allowed; the
    forward shadow (`app.autopilot`) is not, and it is the one an easy import
    would reach — the reader used to live beside it."""
    probe = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('automation_poll', {str(SCRIPT)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        f"forbidden = {NO_AI!r}\n"
        "reached = sorted(m for m in sys.modules "
        "if any(m == f or m.startswith(f + '.') for f in forbidden))\n"
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
        f"scripts/automation.py reached {found.stdout.strip()}. The triggers' poll must not be "
        f"able to call a model, even indirectly."
    )


def test_the_forward_shadow_does_not_evaluate_the_triggers() -> None:
    """One owner each. The shadow's pass used to hand its tickets to the
    rules; now the poll does, and the shadow imports nothing of the engine."""
    for path in (
        PACKAGE.parent.parent / "scripts" / "autopilot.py",
        PACKAGE.parent / "autopilot" / "wiring.py",
        PACKAGE.parent / "autopilot" / "shadow" / "forward.py",
    ):
        for line, name in _imports(path):
            assert not name.startswith("app.automation"), (
                f"{path.name}:{line} imports {name}; the forward shadow classifies and the "
                f"triggers' poll evaluates, and neither owns the other"
            )
