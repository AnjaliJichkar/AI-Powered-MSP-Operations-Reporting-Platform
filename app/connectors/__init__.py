"""Connectors: one layer, shared by every product (hard rule 5).

Importing this package imports every connector, which registers every tool.
Nothing here constructs a connector — that is `factory.connector_for`.
"""

# Imported last, and from here rather than from each vendor package: the tools
# modules import `connector_for`, so they must load after the factory is fully
# initialised. This is what registers every tool with `app.agent.registry`.
from app.connectors.autotask import tools as _autotask_tools  # noqa: F401
from app.connectors.base import BaseConnector, ConnectorLimits
from app.connectors.bitdefender import tools as _bitdefender_tools  # noqa: F401
from app.connectors.connectsecure import tools as _connectsecure_tools  # noqa: F401
from app.connectors.cove import tools as _cove_tools  # noqa: F401
from app.connectors.errors import (
    AuthError,
    ConnectorError,
    ConnectorNotConfigured,
    NotFound,
    RateLimited,
    UnknownClient,
    UpstreamError,
)
from app.connectors.factory import connector_for
from app.connectors.microsoft365 import tools as _microsoft365_tools  # noqa: F401
from app.connectors.ninjaone import tools as _ninjaone_tools  # noqa: F401
from app.coverage import (
    tools as _coverage_tools,  # noqa: F401  cross-tool, on NinjaOne's population
)

__all__ = [
    "AuthError",
    "BaseConnector",
    "ConnectorError",
    "ConnectorLimits",
    "ConnectorNotConfigured",
    "NotFound",
    "RateLimited",
    "UnknownClient",
    "UpstreamError",
    "connector_for",
]
