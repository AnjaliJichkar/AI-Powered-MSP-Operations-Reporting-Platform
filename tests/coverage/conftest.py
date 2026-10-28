"""The join's tests have no database: the last-seen store is a dict."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _last_seen_links_in_memory(last_seen_in_memory: dict[str, Any]) -> None:
    """See tests/conftest.py."""
