"""The tool registry, and the import-time guard on hard rule 1."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

# Importing the connectors is what populates the registry; without this the
# tests below would depend on another module having imported them first.
import app.connectors  # noqa: F401
from app.agent.registry import (
    ToolRegistrationError,
    clear_registry,
    get_tool,
    limit_rows,
    registered_tools,
    tool,
    tools_for,
)
from app.models.enums import ConnectorName
from app.settings import get_settings


@pytest.fixture(autouse=True)
def _drop_throwaway_tools() -> Iterator[None]:
    """Remove tools defined by these tests, leaving the real ones registered."""
    before = {definition.name for definition in registered_tools()}
    yield
    after = {definition.name for definition in registered_tools()}
    clear_registry(after - before)


# --- the client scoping guard ---------------------------------------------


def test_a_tool_without_client_id_cannot_be_registered() -> None:
    """Hard rule 1, enforced at import time so it can never reach the model."""
    with pytest.raises(ToolRegistrationError, match="scoped to a client"):

        @tool(ConnectorName.NINJAONE)
        async def list_everything(days: int) -> str:
            """Would return data across every client."""
            return "no"


def test_client_id_may_not_have_a_default() -> None:
    """A default would let a missing client silently fall back instead of failing."""
    with pytest.raises(ToolRegistrationError, match="must fail, not fall back"):

        @tool(ConnectorName.NINJAONE)
        async def sloppy(client_id: str = "default-client") -> str:
            """Falls back to a client nobody asked for."""
            return "no"


def test_client_id_must_be_a_string() -> None:
    with pytest.raises(ToolRegistrationError, match="annotate 'client_id' as str"):

        @tool(ConnectorName.NINJAONE)
        async def wrongly_typed(client_id: int) -> str:
            """Takes the wrong kind of client id."""
            return "no"


# --- the rest of the contract ---------------------------------------------


def test_a_tool_must_be_async() -> None:
    with pytest.raises(ToolRegistrationError, match="must be an async function"):

        @tool(ConnectorName.NINJAONE)
        def blocking(client_id: str) -> str:
            """Blocks the event loop."""
            return "no"


def test_a_tool_must_have_a_docstring() -> None:
    with pytest.raises(ToolRegistrationError, match="no docstring"):

        @tool(ConnectorName.NINJAONE)
        async def undocumented(client_id: str) -> str:
            return "no"


def test_every_parameter_must_be_annotated() -> None:
    with pytest.raises(ToolRegistrationError, match="no type annotation"):

        @tool(ConnectorName.NINJAONE)
        async def untyped(client_id: str, days) -> str:  # type: ignore[no-untyped-def]
            """Has an unannotated parameter."""
            return "no"


def test_varargs_are_refused() -> None:
    with pytest.raises(ToolRegistrationError, match="must not take"):

        @tool(ConnectorName.NINJAONE)
        async def loose(client_id: str, *args: int) -> str:
            """Takes anything at all."""
            return "no"


def test_two_different_tools_cannot_share_a_name() -> None:
    @tool(ConnectorName.NINJAONE, name="duplicated")
    async def first(client_id: str) -> str:
        """The first one."""
        return "first"

    with pytest.raises(ToolRegistrationError, match="already registered"):

        @tool(ConnectorName.NINJAONE, name="duplicated")
        async def second(client_id: str) -> str:
            """The second one."""
            return "second"


# --- what the registry produces -------------------------------------------


# --- the row cap ----------------------------------------------------------


def test_the_row_cap_defaults_to_the_setting_and_a_tool_can_override_it() -> None:
    @tool(ConnectorName.NINJAONE)
    async def cap_default(client_id: str) -> str:
        """Uses the configured row cap."""
        return client_id

    @tool(ConnectorName.NINJAONE, max_rows=3)
    async def cap_three(client_id: str) -> str:
        """Caps itself at three rows."""
        return client_id

    assert get_tool("cap_default").max_rows is None
    assert get_tool("cap_default").row_cap == get_settings().tool_max_rows == 200
    assert get_tool("cap_three").row_cap == 3


def test_limit_rows_keeps_the_front_and_reports_the_total() -> None:
    @tool(ConnectorName.NINJAONE, max_rows=3)
    async def capped_tool(client_id: str) -> str:
        """Caps itself at three rows."""
        return client_id

    window = limit_rows("capped_tool", list(range(10)))
    assert window.rows == [0, 1, 2]
    assert window.total == 10
    assert window.truncated is True

    exact = limit_rows("capped_tool", [1, 2, 3])
    assert exact.rows == [1, 2, 3]
    assert exact.truncated is False


def test_a_row_cap_below_one_is_refused() -> None:
    with pytest.raises(ToolRegistrationError, match="max_rows"):
        tool(ConnectorName.NINJAONE, max_rows=0)


# --- registration ---------------------------------------------------------


def test_a_registered_tool_is_callable_and_described() -> None:
    @tool(ConnectorName.NINJAONE)
    async def demo_tool(client_id: str, days: int = 3, label: str | None = None) -> str:
        """Does a demonstrable thing.

        Second paragraph of the description.
        """
        return f"{client_id}/{days}/{label}"

    definition = get_tool("demo_tool")

    assert definition.connector is ConnectorName.NINJAONE
    assert definition.description.startswith("Does a demonstrable thing.")
    assert "Second paragraph" in definition.description
    assert definition.input_schema["required"] == ["client_id"]
    assert definition.input_schema["properties"]["days"]["default"] == 3


async def test_the_registry_hands_back_the_real_function() -> None:
    @tool(ConnectorName.NINJAONE)
    async def echoing(client_id: str) -> str:
        """Echoes the client id."""
        return f"called for {client_id}"

    assert await get_tool("echoing").func(client_id="abc") == "called for abc"


def test_the_anthropic_schema_has_the_three_fields_the_api_wants() -> None:
    schema = get_tool("list_devices").to_anthropic_schema()
    assert set(schema) == {"name", "description", "input_schema"}
    assert schema["input_schema"]["type"] == "object"


def test_looking_up_an_unknown_tool_lists_what_exists() -> None:
    with pytest.raises(KeyError, match="list_devices"):
        get_tool("list_all_the_things")


def test_every_tool_says_what_it_is_not_for() -> None:
    """With ~30 tools several overlap; the description is how the model tells them apart.

    Each must name what it is for and what it is NOT for, pointing at the tool
    that is — `list_patch_status` (waiting) versus `list_patch_installs`
    (failed) is the kind of pair this keeps distinguishable.
    """
    missing = [
        definition.name
        for definition in registered_tools()
        if not any(marker in definition.description for marker in ("Not for", "Not a ", "Not the "))
    ]
    assert missing == []

    # And when it points at another tool, that tool exists.
    names = {definition.name for definition in registered_tools()}
    import re

    for definition in registered_tools():
        for mentioned in re.findall(
            r"`((?:list|get|count|search)_[a-z_]+)`", definition.description
        ):
            assert mentioned in names, f"{definition.name} points at unknown tool {mentioned}"


def test_tools_are_returned_in_a_stable_order() -> None:
    names = [definition.name for definition in registered_tools()]
    assert names == sorted(names)


def test_tools_can_be_filtered_by_connector() -> None:
    assert tools_for(ConnectorName.NINJAONE)
    assert tools_for(ConnectorName.AUTOTASK)
    assert [d.name for d in tools_for(ConnectorName.BITDEFENDER)] == [
        "count_endpoints",
        "get_endpoint_protection",
        "list_outdated_agents",
        "list_recent_detections",
        "list_unprotected_endpoints",
    ]
    # Blackpoint comes after the freeze; nothing is invented for it. Cove's
    # fourth tool is the backup coverage gap, which lives with the board's own
    # join rather than with the connector: one join, whoever asks.
    assert [d.name for d in tools_for(ConnectorName.COVE)] == [
        "count_backups",
        "get_device_backup",
        "list_backup_failures",
        "list_machines_without_backup",
    ]
