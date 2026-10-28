"""N-able Cove, faked: the JSON-RPC calls `CoveClient` sends, answered from the world.

The real client runs against this unchanged, so its visa chain, its paging,
its column folding and its error mapping are all exercised exactly as they are
against a tenant.

Cove is one endpoint and one envelope: every method is a `method` field in the
body rather than a path, and authentication is a **visa** that each answer
re-issues. The fake keeps that shape, because the chain is the part of the
client worth proving — a visa that is never refreshed and a visa that is
refused both look like a working connector until the quiet quarter of an hour
that breaks them.

What is deliberately faithful rather than convenient:

* a call with no visa, or a visa this fake did not issue, is refused with the
  vendor's own code so `_authenticate` is driven by a refusal rather than by a
  clock;
* every answer carries a **fresh** visa, so the client's "last answer wins"
  handling is exercised;
* `EnumerateAccountStatistics` returns the columns that were asked for and
  silently omits a code it does not recognise, which is the vendor's quietest
  behaviour and the reason `columns.py` exists;
* values are strings, including the numbers and the Unix times;
* a customer's Microsoft 365 tenant is a device row with its OS type and
  physicality undefined, told apart only by `I59`;
* paging ends on a short page, because the vendor never says how many rows
  match.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx

from app.connectors.cove import columns as col
from app.demo.api import Call, Router, json_response
from app.demo.dataset import Customer, Device, World, at

DEMO_BASE_URL = "https://api.cove.demo.invalid"

#: The franchisee's own partner, which every customer hangs beneath.
ROOT_PARTNER = 69_000
ROOT_PARTNER_NAME = "Tailwind IT Services"

#: What the vendor calls the levels. Only `EndCustomer` becomes a site.
DISTRIBUTOR = "Distributor"
END_CUSTOMER = "EndCustomer"

#: The error a refused visa comes back as. Unverified against the vendor, as
#: the client's own comment says; the fake uses what the client matches on.
VISA_REFUSED = 2001


def _error(status: int, message: str) -> Any:
    """The envelope a transport-level miss comes back in.

    Cove answers 200 with an `error` member for almost everything, so this is
    only reached for a path the router does not know at all.
    """
    return {"jsonrpc": "2.0", "id": "jsonrpc", "error": {"code": status, "message": message}}


def _seconds(days: float | None, now: dt.datetime) -> str:
    """A time as the vendor sends it: Unix seconds, as a string.

    `"0"` for never, which is the vendor's own way of saying it and the reason
    `as_time` refuses to read zero as 1 January 1970.
    """
    moment = at(days, now)
    return str(int(moment.timestamp())) if moment else "0"


class CoveApi:
    """One fake Cove over the whole demo franchisee."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.router = Router("cove", _error)
        self.router.add("POST", "/jsonapi", self.rpc)
        #: Visas this fake has issued. A client holding one of these is signed
        #: in; anything else is refused, as a stale one is.
        self._visas: set[str] = set()

    def handle(self, request: httpx.Request) -> httpx.Response:
        return self.router.handle(request, self.world, dt.datetime.now(dt.UTC))

    # --- the envelope ---------------------------------------------------

    def rpc(self, call: Call) -> httpx.Response:
        """One JSON-RPC call: check the visa, run the method, re-issue a visa."""
        body: dict[str, Any] = call.body if isinstance(call.body, dict) else {}
        method = str(body.get("method") or "")
        raw = body.get("params")
        params: dict[str, Any] = raw if isinstance(raw, dict) else {}

        if method != "Login" and body.get("visa") not in self._visas:
            return self._answer(
                {"error": {"code": VISA_REFUSED, "message": "Visa is not valid"}}, visa=False
            )

        handlers = {
            "Login": self._login,
            "EnumeratePartners": self._partners,
            "EnumerateAccountStatistics": self._statistics,
        }
        handler = handlers.get(method)
        if handler is None:
            # Named, because a method the fake has not been taught is a call
            # the client has started making and nobody has looked at yet.
            return self._answer({"error": {"code": -32601, "message": f"Unknown method {method}"}})
        return self._answer(handler(call, params))

    def _answer(self, payload: dict[str, Any], *, visa: bool = True) -> httpx.Response:
        """The envelope, with a fresh visa on every answer that earns one."""
        envelope: dict[str, Any] = {"jsonrpc": "2.0", "id": "jsonrpc", **payload}
        if visa:
            issued = uuid.uuid4().hex
            self._visas.add(issued)
            envelope["visa"] = issued
        return json_response(envelope)

    # --- the methods ----------------------------------------------------

    def _login(self, call: Call, params: dict[str, Any]) -> dict[str, Any]:
        """The one call that carries no visa.

        The demo accepts any credential: a demo client holds none, and what
        this fake is for is the chain after the login rather than the login.
        """
        return {
            "result": {
                "result": {
                    "Id": ROOT_PARTNER,
                    "PartnerId": ROOT_PARTNER,
                    "Flags": [],
                }
            }
        }

    def _partners(self, call: Call, params: dict[str, Any]) -> dict[str, Any]:
        """Every partner beneath ours, flat, as the vendor returns them.

        The franchisee's own partner is in the list as a `Distributor`, which
        is what makes `list_organisations`' filter worth having: without it the
        franchisee itself would become one of its own customers' sites.
        """
        rows: list[dict[str, Any]] = [
            {
                "Id": ROOT_PARTNER,
                "Name": ROOT_PARTNER_NAME,
                "ParentId": 0,
                "Level": DISTRIBUTOR,
                "State": "InProduction",
            }
        ]
        for customer in self.world.customers:
            if customer.cove_partner_id is None:
                continue
            rows.append(
                {
                    "Id": customer.cove_partner_id,
                    "Name": customer.name,
                    "ParentId": ROOT_PARTNER,
                    "Level": END_CUSTOMER,
                    "State": "InProduction",
                }
            )
        return {"result": {"result": rows}}

    def _statistics(self, call: Call, params: dict[str, Any]) -> dict[str, Any]:
        """One partner's device rows, paged, with exactly the columns asked for."""
        raw = params.get("query")
        query: dict[str, Any] = raw if isinstance(raw, dict) else {}
        partner = query.get("PartnerId")
        wanted = [str(code) for code in (query.get("Columns") or [])]
        start = int(query.get("StartRecordNumber") or 0)
        count = int(query.get("RecordsCount") or 100)

        # **A read at a partner covers the tree beneath it.** That is the
        # vendor's own behaviour and the reason the client reads the whole
        # fleet in one call rather than one call per customer, so a fake that
        # answered only the exact partner would leave that path untested — and
        # the allowlist guard behind it, which is what makes a recursive read
        # a wider net rather than a looser one.
        if partner == ROOT_PARTNER:
            customers = [c for c in self.world.customers if c.cove_partner_id is not None]
        else:
            customers = [c for c in self.world.customers if c.cove_partner_id == partner]
        if not customers:
            # A partner with no devices is an empty list, and the vendor sends
            # an empty list as `null`.
            return {"result": {"result": None}}

        rows = [row for customer in customers for row in self._rows_for(customer, call.now)]
        page = rows[start : start + count]
        return {"result": {"result": [self._row(values, wanted) for values in page] or None}}

    # --- the rows -------------------------------------------------------

    def _rows_for(self, customer: Customer, now: dt.datetime) -> list[dict[str, str]]:
        """Every row Cove holds for one customer: its machines, then its tenant."""
        devices = [d for d in self.world.devices if d.customer_key == customer.key and d.in_cove]
        rows = [self._device_values(device, customer, now, n) for n, device in enumerate(devices)]
        rows.append(self._tenant_values(customer, now, len(devices)))
        return rows

    def _device_values(
        self, device: Device, customer: Customer, now: dt.datetime, index: int
    ) -> dict[str, str]:
        """One machine, as the columns the client asks for."""
        last_session = "8" if device.cove_errors else "5"
        if device.cove_last_success_days is not None and device.cove_last_success_days > 30:
            # Nothing has completed in a month: the sessions that run fail.
            last_session = "2"
        return {
            _PARTNER: str(customer.cove_partner_id),
            col.COLUMNS["device_id"]: str(900_000 + device.id),
            col.COLUMNS["device_name"]: device.system_name,
            col.COLUMNS["customer"]: customer.name,
            col.COLUMNS["computer_name"]: device.system_name,
            col.COLUMNS["macs"]: ", ".join(device.macs),
            col.COLUMNS["os_version"]: device.os_name,
            col.COLUMNS["os_type"]: "2" if "SERVER" in device.node_class or device.virtual else "1",
            col.COLUMNS["physicality"]: "2" if device.virtual else "1",
            col.COLUMNS["used_storage"]: str(18_000_000_000 + index * 3_100_000_000),
            col.COLUMNS["data_sources"]: "D01, D02",
            col.COLUMNS["product"]: "All-In" if "SERVER" in device.node_class else "Documents",
            col.COLUMNS["retention_units"]: "Enhanced",
            col.COLUMNS["sku"]: "Server" if "SERVER" in device.node_class else "Workstation",
            col.COLUMNS["account_type"]: "1",
            col.COLUMNS["recovery_testing"]: "1" if "SERVER" in device.node_class else "0",
            col.COLUMNS["created_at"]: _seconds(device.created_days, now),
            col.COLUMNS["profile"]: "Servers" if "SERVER" in device.node_class else "Laptops",
            col.stat(col.TOTAL, "last_session_status"): last_session,
            col.stat(col.TOTAL, "last_session_errors"): str(device.cove_errors),
            col.stat(col.TOTAL, "colour_bar_28_days"): _bar(device, last_session),
            col.stat(col.TOTAL, "last_successful_at"): _seconds(device.cove_last_success_days, now),
            col.stat(col.TOTAL, "last_session_at"): _seconds(0.3, now),
        }

    def _tenant_values(self, customer: Customer, now: dt.datetime, index: int) -> dict[str, str]:
        """The customer's Microsoft 365 tenant, which Cove holds as a device.

        Its OS type and physicality are undefined and `I59` is what says what
        it is, exactly as live — which is why every count about machines has
        to exclude it rather than assume the list is machines.
        """
        people = len(customer.m365_accounts) or 12
        return {
            _PARTNER: str(customer.cove_partner_id),
            col.COLUMNS["device_id"]: str(950_000 + index),
            col.COLUMNS["device_name"]: f"{customer.name} Microsoft 365",
            col.COLUMNS["customer"]: customer.name,
            col.COLUMNS["os_type"]: "0",
            col.COLUMNS["physicality"]: "0",
            col.COLUMNS["used_storage"]: str(240_000_000_000),
            col.COLUMNS["data_sources"]: "D19, D20, D05",
            col.COLUMNS["product"]: "Microsoft 365",
            col.COLUMNS["account_type"]: "2",
            col.COLUMNS["created_at"]: _seconds(420.0, now),
            col.stat(col.TOTAL, "last_session_status"): "5",
            col.stat(col.TOTAL, "last_session_errors"): "0",
            col.stat(col.TOTAL, "colour_bar_28_days"): "5" * 28,
            col.stat(col.TOTAL, "last_successful_at"): _seconds(0.4, now),
            col.stat(col.TOTAL, "last_session_at"): _seconds(0.4, now),
            col.stat(col.M365_EXCHANGE, "user_mailboxes"): str(people),
            col.stat(col.M365_EXCHANGE, "shared_mailboxes"): "3",
            col.stat(col.M365_ONEDRIVE, "user_mailboxes"): str(people),
        }

    @staticmethod
    def _row(values: dict[str, str], wanted: list[str]) -> dict[str, Any]:
        """One row in the vendor's own shape: a list of single-key objects.

        A column that was asked for and has no value is left out, and a code
        the vendor does not recognise is left out too — which is the same
        thing from the client's side, and the reason every code it sends is
        built rather than typed.
        """
        return {
            "AccountId": int(values[col.COLUMNS["device_id"]]),
            # The row's own partner, which is what the client checks every row
            # against before it returns one. A fake that sent 0 here would let
            # the allowlist guard pass untested, which is the one thing about
            # a merged read worth proving.
            "PartnerId": int(values[_PARTNER]),
            "Settings": [{code: values[code]} for code in wanted if code in values],
        }


#: Where a row carries its own partner id for `_row` to lift out. Not a Cove
#: column: the client never asks for it, and `_row` only sends codes that were
#: asked for, so it cannot reach a connector by accident.
_PARTNER = "_partner"


def _bar(device: Device, last_session: str) -> str:
    """The 28-day colour bar, newest first.

    Built from the device's own count of bad days so the rate a review
    computes and the state the page prints come from one number. The newest
    character is the last session, because that is what the bar's first day
    is: a device whose last session failed shows a failure today.
    """
    bad = max(0, min(device.cove_bad_days, 28))
    good = "8" if device.cove_errors else "5"
    # A day that failed and a day nothing ran on are different things, and the
    # rate counts both against: alternate them so both appear in the demo.
    bad_days = "".join("2" if n % 2 == 0 else "0" for n in range(bad))
    return (last_session + bad_days + good * 28)[:28]
