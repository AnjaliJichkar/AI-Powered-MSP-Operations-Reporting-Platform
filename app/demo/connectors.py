"""The demo tenant's connectors: the real clients over a transport that cannot leave the process.

`DemoNinjaOneClient` *is* a `NinjaOneClient`: the same code, the same models,
the same guards, pagination and error mapping. What changes is the wire: its
`httpx.AsyncClient` carries an `httpx.MockTransport` that hands every request
to the fake vendor built from the world, and its base URL is a host under
`.demo.invalid` that no resolver answers. A demo class cannot be built without
a `World`, so the factory's live path — which has none — can never produce
one; the factory builds a demo class only for a client whose `kind` is `demo`
(`app/connectors/factory.py`), so a demo client can never reach a real class
or hold a credential.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping
from functools import lru_cache
from types import TracebackType
from typing import Any, ClassVar, Self

import httpx

from app.connectors.autotask.client import AutotaskClient
from app.connectors.autotask.notes import AutotaskNoteWriter
from app.connectors.base import BaseConnector, ConnectorLimits
from app.connectors.bitdefender.client import BitdefenderClient
from app.connectors.connectsecure.client import ConnectSecureClient
from app.connectors.cove.client import CoveClient
from app.connectors.microsoft365.client import Microsoft365Client
from app.connectors.ninjaone.client import NinjaOneClient
from app.connectors.scope import ClientScope
from app.demo import (
    autotask_api,
    bitdefender_api,
    connectsecure_api,
    cove_api,
    microsoft365_api,
    ninjaone_api,
)
from app.demo.dataset import World, build_world
from app.models.enums import ConnectorName

DEMO_SEED = 20260929
DEMO_HOST_SUFFIX = ".demo.invalid"


@lru_cache(maxsize=4)
def demo_world(slug: str = "demo", seed: int = DEMO_SEED) -> World:
    """The world for a demo client, built once per process. The same seed is the same
    world on every machine; `slug` keeps two demo clients apart if there are ever two."""
    return build_world(seed)


@lru_cache(maxsize=16)
def _api(name: ConnectorName, slug: str, seed: int) -> Any:
    world = demo_world(slug, seed)
    return {
        ConnectorName.NINJAONE: ninjaone_api.NinjaOneApi,
        ConnectorName.AUTOTASK: autotask_api.AutotaskApi,
        # The note writer talks to the same fake Autotask over the same world,
        # so a note it posts is on the world the read connector reads.
        ConnectorName.AUTOTASK_NOTES: autotask_api.AutotaskApi,
        ConnectorName.BITDEFENDER: bitdefender_api.BitdefenderApi,
        ConnectorName.CONNECTSECURE: connectsecure_api.ConnectSecureApi,
        ConnectorName.COVE: cove_api.CoveApi,
        ConnectorName.MICROSOFT365: microsoft365_api.Microsoft365Api,
    }[name](world)


class DemoTransportError(RuntimeError):
    """A demo connector was asked to reach a host that is not the fake vendor."""


def _transport(
    handle: Callable[[httpx.Request], httpx.Response], base_url: str
) -> httpx.MockTransport:
    host = httpx.URL(base_url).host

    def guarded(request: httpx.Request) -> httpx.Response:
        # Autotask's zone lookup is the one absolute URL a client builds itself; it is
        # answered too. Anything else off the fake host is a bug, not a request.
        if request.url.host != host and not request.url.host.endswith(DEMO_HOST_SUFFIX):
            if request.url.host == "webservices.autotask.net":
                return handle(request)
            raise DemoTransportError(f"demo connector tried to reach {request.url.host}")
        return handle(request)

    return httpx.MockTransport(guarded)


class DemoConnector(BaseConnector):
    """What every demo class adds: a world, a fake vendor, a transport that stays in-process.

    Listed before the real client in each demo class's bases, so its `__init__`
    supplies the demo secrets and base URL and the real client's `__init__` runs
    after it, unchanged.
    """

    demo_base_url: ClassVar[str] = ""
    demo_secrets: ClassVar[Mapping[str, str]] = {}
    demo_config: ClassVar[Mapping[str, str]] = {}

    def __init__(
        self,
        *,
        scope: ClientScope,
        world: World,
        limits: ConnectorLimits | None = None,
        **kwargs: Any,
    ) -> None:
        if not isinstance(world, World):
            raise TypeError("a demo connector is built from a World; the live factory has none")
        self.world = world
        self.api = _api(self.name, scope.client_slug, world.seed)
        for key in ("http_client", "secrets", "config", "credential_version"):
            kwargs.pop(key, None)
        super().__init__(
            scope=scope,
            secrets=dict(self.demo_secrets),
            config={"base_url": self.demo_base_url, **self.demo_config},
            limits=limits,
            credential_version=f"demo:{world.seed}",
            **kwargs,
        )

    async def __aenter__(self) -> Self:
        self._client = httpx.AsyncClient(
            transport=_transport(self.api.handle, self.demo_base_url),
            timeout=httpx.Timeout(5.0),
        )
        self._owns_client = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class DemoNinjaOneClient(DemoConnector, NinjaOneClient):
    demo_base_url = ninjaone_api.DEMO_BASE_URL
    demo_secrets: ClassVar[Mapping[str, str]] = {
        "client_id": "demo-client",
        "client_secret": "demo-secret",
    }


class DemoAutotaskClient(DemoConnector, AutotaskClient):
    demo_base_url = autotask_api.DEMO_BASE_URL
    demo_secrets: ClassVar[Mapping[str, str]] = {
        "api_integration_code": "demo",
        "username": "api@tailwind-it.example",
        "secret": "demo",
    }


class DemoAutotaskNoteWriter(DemoConnector, AutotaskNoteWriter):
    """The note writer over the fake Autotask: the same allowlist, the same
    picklist resolution, and the note lands on the world rather than a tenant.
    Built by `build_note_writer` for a demo client and by nothing else."""

    demo_base_url = autotask_api.DEMO_BASE_URL
    demo_secrets: ClassVar[Mapping[str, str]] = {
        "api_integration_code": "demo",
        "username": "notes@tailwind-it.example",
        "secret": "demo",
    }


class DemoBitdefenderClient(DemoConnector, BitdefenderClient):
    demo_base_url = bitdefender_api.DEMO_BASE_URL
    demo_secrets: ClassVar[Mapping[str, str]] = {"api_key": "demo-api-key"}


class DemoConnectSecureClient(DemoConnector, ConnectSecureClient):
    demo_base_url = connectsecure_api.DEMO_BASE_URL
    demo_secrets: ClassVar[Mapping[str, str]] = {
        "client_id": "demo-client",
        "client_secret": "demo-secret",
    }
    demo_config: ClassVar[Mapping[str, str]] = {"tenant": str(connectsecure_api.TENANT)}


class DemoCoveClient(DemoConnector, CoveClient):
    """Cove over the fake. One endpoint, one envelope and a visa chain, so the
    client's sign-in, its re-authentication on a refused visa and its column
    folding are what run here."""

    demo_base_url = cove_api.DEMO_BASE_URL
    demo_secrets: ClassVar[Mapping[str, str]] = {"password": "demo-password"}
    demo_config: ClassVar[Mapping[str, str]] = {
        "partner": cove_api.ROOT_PARTNER_NAME,
        "username": "api@tailwind-it.example",
    }


class DemoMicrosoft365Client(DemoConnector, Microsoft365Client):
    """Graph over the fake tenants. Two hosts, as Graph has two: the login
    host issues a token for one customer's tenant and the Graph host answers
    that tenant's reads, so the real client's per-tenant token handling is
    what runs here."""

    demo_base_url = microsoft365_api.DEMO_BASE_URL
    demo_secrets: ClassVar[Mapping[str, str]] = {
        "client_id": "demo-application",
        "client_secret": "demo-secret",
    }
    demo_config: ClassVar[Mapping[str, str]] = {"login_url": microsoft365_api.DEMO_LOGIN_URL}


#: What the factory builds for a demo client, and nothing else builds.
DEMO_CONNECTOR_CLASSES: dict[ConnectorName, type[DemoConnector]] = {
    ConnectorName.NINJAONE: DemoNinjaOneClient,
    ConnectorName.AUTOTASK: DemoAutotaskClient,
    ConnectorName.BITDEFENDER: DemoBitdefenderClient,
    ConnectorName.CONNECTSECURE: DemoConnectSecureClient,
    ConnectorName.COVE: DemoCoveClient,
    ConnectorName.MICROSOFT365: DemoMicrosoft365Client,
}


def is_demo_connector(connector: BaseConnector) -> bool:
    return isinstance(connector, DemoConnector)


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
