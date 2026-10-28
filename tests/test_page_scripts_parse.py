"""Every page script parses.

There is no build step and there is not going to be one, so nothing between
writing a page script and a browser running it would notice that it does not
parse. A script that fails to parse does not fail loudly: the page renders its
markup, the script never runs, and what comes back is a page that looks
half-finished with nothing in the console that says why. That is how `const
ask` beside `async function ask()` took the whole Ask page out — the shell
drew, the feed stayed empty, and the only clue was that no request was made.

`node --check` parses a file and runs none of it. Node is not a dependency of
this project and CI need not have it, so this skips without it rather than
failing — like the two browser checks beside it. It is a syntax check and not
a linter: it says nothing about an undefined name or an unused variable, only
that a browser could load the file at all.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "app" / "static" / "js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None,
    reason="needs node, which this project does not depend on: `node --check` is the parser",
)


def script_files() -> list[Path]:
    return sorted(SCRIPTS.glob("*.js"))


def test_there_are_scripts_to_check() -> None:
    """The check is not vacuous: a glob that found nothing would pass silently."""
    assert len(script_files()) >= 10


@pytest.mark.parametrize("script", script_files(), ids=lambda p: p.name)
def test_the_script_parses(script: Path) -> None:
    assert NODE is not None
    # The command is a path this file computed and a file in the repository:
    # nothing here comes from a request, a vendor or a model, and no shell is
    # involved.
    done = subprocess.run(  # noqa: S603
        [NODE, "--check", str(script)], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, f"{script.name} does not parse:\n{done.stderr.strip()}"


def test_a_file_that_does_not_parse_is_caught(tmp_path: Path) -> None:
    """Proved to fail, not only to pass: the exact shape that broke Ask."""
    assert NODE is not None
    broken = tmp_path / "broken.js"
    broken.write_text("const ask = 1;\nasync function ask() {}\n", encoding="utf-8")
    done = subprocess.run(  # noqa: S603
        [NODE, "--check", str(broken)], capture_output=True, text=True, timeout=60
    )
    assert done.returncode != 0
    assert "already been declared" in done.stderr
