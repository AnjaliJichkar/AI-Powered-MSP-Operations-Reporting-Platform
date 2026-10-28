"""Autotask PSA connector.

Tool registration lives in `app.connectors`, not here: the factory imports this
package's `client` module, so importing `tools` from here would close an import
cycle through `factory.connector_for`.
"""

from app.connectors.autotask.client import AutotaskClient

__all__ = ["AutotaskClient"]
