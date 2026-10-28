"""GravityZone, faked: the JSON-RPC services `BitdefenderClient` calls, answered from the world.

Every request is `POST /v{version}/jsonrpc/{service}` with `{jsonrpc, id,
method, params}`; the answer echoes `id` and carries `result` or `error`.
Errors follow the vendor: -32001 authorisation, -32602 with `data.details`
naming the accepted values for a bad parameter, and "does not exist" for an
unknown id. Its silent behaviours are kept: a company read without
`depth.allItemsRecursively` returns nothing, and `getIncidentsList` for a
company whose licence has no EDR returns an empty page.
"""

from __future__ import annotations

import base64
import datetime as dt
from typing import Any

import httpx

from app.demo.api import Call, Router, json_response
from app.demo.dataset import Customer, Device, World, iso

DEMO_BASE_URL = "https://gravityzone.demo.invalid/api"
PARTNER_ID = "6a0b1c2d3e4f5a6b7c8d9e0f"
MODULES = (
    "antimalware", "firewall", "contentControl", "powerUser", "deviceControl",
    "advancedThreatControl", "applicationControl", "encryption", "networkAttackDefense",
    "antiTampering", "advancedAntiExploit", "userControl", "antiphishing", "trafficScan",
    "remoteEnginesScanning", "riskManagement",
)  # fmt: skip


def _error(status: int, message: str) -> Any:
    return {
        "id": None,
        "jsonrpc": "2.0",
        "error": {"code": -32600, "message": message, "data": {"details": message}},
    }


def _rpc_error(request_id: Any, code: int, message: str, details: str) -> httpx.Response:
    return json_response(
        {
            "id": request_id,
            "jsonrpc": "2.0",
            "error": {"code": code, "message": message, "data": {"details": details}},
        }
    )


def _iso_offset(days: float | None, now: dt.datetime) -> str | None:
    moment = iso(days, now)
    return moment.replace("Z", "+00:00") if moment else None


class BitdefenderApi:
    def __init__(self, world: World) -> None:
        self.world = world
        self.router = Router("bitdefender", _error)
        self.router.add("POST", "/api/v{version}/jsonrpc/{service}", self.rpc)
        self._endpoint_ids: dict[int, str] = {}
        for device in world.devices:
            self._endpoint_ids[device.id] = f"64e{device.id:021d}"
        self.methods = {
            ("companies", "getCompanyDetails"): self.company_details,
            ("network", "getCompaniesList"): self.companies_list,
            ("licensing", "getLicenseInfo"): self.license_info,
            ("network", "getNetworkInventoryItems"): self.inventory,
            ("network", "getManagedEndpointDetails"): self.endpoint_details,
            ("incidents", "getIncidentsList"): self.incidents,
            ("general", "getApiKeyDetails"): self.api_key_details,
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        return self.router.handle(request, self.world, dt.datetime.now(dt.UTC))

    def rpc(self, call: Call) -> httpx.Response:
        auth = call.request.headers.get("authorization", "")
        if not auth.startswith("Basic ") or not base64.b64decode(auth[6:]).startswith(
            b"demo-api-key:"
        ):
            return _rpc_error(None, -32001, "Authorization failed", "Invalid API key")
        body = call.body if isinstance(call.body, dict) else {}
        method = str(body.get("method", ""))
        handler = self.methods.get((call.path_params["service"], method))
        if handler is None:
            return _rpc_error(
                body.get("id"),
                -32601,
                "Method not found",
                f"{method} is not a method of this service",
            )
        result = handler(call, body.get("params") or {}, body.get("id"))
        if isinstance(result, httpx.Response):
            return result
        return json_response({"id": body.get("id"), "jsonrpc": "2.0", "result": result})

    # --- companies and licences ---------------------------------------------------------------

    def _company(self, company_id: str | None) -> tuple[Customer, int] | None:
        for customer in self.world.customers:
            for index, held in enumerate(customer.bitdefender_company_ids):
                if held == company_id:
                    return customer, index
        return None

    def _company_name(self, customer: Customer, index: int) -> str:
        return (
            customer.name if index == 0 else f"{customer.name} - a7f3k2"
        )  # the integration's suffix

    def api_key_details(self, call: Call, params: dict[str, Any], request_id: Any) -> Any:
        return {
            "id": PARTNER_ID,
            "name": "Tailwind demo key",
            "apis": ["network", "companies", "licensing", "incidents", "general"],
        }

    def company_details(self, call: Call, params: dict[str, Any], request_id: Any) -> Any:
        wanted = params.get("companyId")
        if wanted is None:
            return {"id": PARTNER_ID, "name": self.world.franchisee, "type": 0, "country": "US"}
        found = self._company(wanted)
        if found is None:
            return _rpc_error(request_id, -32602, "Invalid params", "Company does not exist")
        customer, index = found
        return {
            "id": wanted,
            "name": self._company_name(customer, index),
            "type": 1,
            "country": "US",
        }

    def companies_list(self, call: Call, params: dict[str, Any], request_id: Any) -> Any:
        return [
            {"id": held, "name": self._company_name(customer, index)}
            for customer in self.world.customers
            for index, held in enumerate(customer.bitdefender_company_ids)
        ]

    def license_info(self, call: Call, params: dict[str, Any], request_id: Any) -> Any:
        found = self._company(params.get("companyId"))
        if found is None:
            return _rpc_error(request_id, -32602, "Invalid params", "Company does not exist")
        customer, _ = found
        return {
            "subscriptionType": 3,
            "assignedProtectionModel": "Business Security"
            if customer.av_only
            else "Business Security Premium",
            "expiryDate": _iso_offset(-500, call.now),
            "usedSlots": len(
                [
                    d
                    for d in self.world.devices
                    if d.customer_key == customer.key and d.in_bitdefender
                ]
            ),
            "manageEncryption": False,
            "manageEventCorrelator": customer.edr_licensed,
            "managePatchManagement": False,
            "manageExchange": False,
            "manageContainerProtection": False,
            "manageHyperDetect": customer.edr_licensed,
            "manageSandboxAnalyzer": False,
            "manageIntegrityMonitoring": False,
            "manageEmailSecurity": False,
        }

    # --- the inventory --------------------------------------------------------------------------

    def _endpoint_item(self, device: Device, company_id: str, now: dt.datetime) -> dict[str, Any]:
        customer = self.world.customer(device.customer_key)
        last_seen = (
            device.bitdefender_last_seen_days
            if device.bitdefender_last_seen_days is not None
            else device.last_contact_days
        )
        modules = {
            name: name
            in (
                "antimalware",
                "advancedThreatControl",
                "antiphishing",
                "trafficScan",
                "riskManagement",
            )
            for name in MODULES
        }
        if not customer.av_only:
            modules.update(
                {
                    "firewall": True,
                    "contentControl": True,
                    "deviceControl": True,
                    "networkAttackDefense": True,
                    "antiTampering": True,
                    "advancedAntiExploit": True,
                }
            )
        if customer.edr_licensed:
            modules["edrSensor"] = True
        return {
            "id": self._endpoint_ids[device.id],
            "name": device.system_name,
            "type": 6 if device.virtual else 5,
            "parentId": company_id,
            "companyId": company_id,
            "details": {
                "label": "",
                "fqdn": device.dns_name,
                "groupId": f"64c{company_id[3:]}",
                "isManaged": True,
                "machineType": 2 if device.virtual else 1,
                "operatingSystemVersion": device.os_name.replace("Microsoft ", "").replace(
                    " Edition", ""
                ),
                "ip": f"10.{device.id % 200}.{device.id % 250}.{device.id % 254 + 1}",
                "macs": (
                    [m.lower().replace(":", "") for m in device.macs]
                    if device.vendor_identifiers
                    else []
                ),
                "ssid": "",
                "isIsolated": False,
                "managedWithBest": True,
                "firstSeen": _iso_offset(device.created_days, now),
                "policy": {
                    "id": "64d000000000000000000001",
                    "name": "Default policy",
                    "applied": True,
                },
                "modules": modules,
                "productOutdated": device.scenario == "rmm_stale",
                "lastSuccessfulScan": {
                    "name": "Quick Scan",
                    "date": _iso_offset(
                        (last_seen or 0) + 0.3 if last_seen is not None else 60, now
                    ),
                },
                "riskScore": {
                    "value": f"{30 + device.id % 50}%",
                    "impact": "Medium",
                    "misconfigurations": "40%",
                    "appVulnerabilities": "30%",
                    "humanRisks": "20%",
                    "industryModifier": "10%",
                },
            },
        }

    def _vendor_only_items(
        self, customer: Customer, company_id: str, now: dt.datetime
    ) -> list[dict[str, Any]]:
        items = []
        for n, endpoint in enumerate(
            e
            for e in self.world.vendor_only
            if e.tool == "bitdefender" and e.customer_key == customer.key
        ):
            items.append(
                {
                    "id": f"64f{customer.ninja_org_id:03d}{n:018d}",
                    "name": endpoint.name,
                    "type": 5,
                    "parentId": company_id,
                    "companyId": company_id,
                    "details": {
                        "label": "",
                        "fqdn": endpoint.name.lower(),
                        "isManaged": endpoint.kind != "discovered",
                        "machineType": 1,
                        "operatingSystemVersion": "Windows 11 Pro",
                        "ip": "10.99.0.5",
                        "macs": [endpoint.mac.lower().replace(":", "")] if endpoint.mac else [],
                        "managedWithBest": endpoint.kind != "discovered",
                        "firstSeen": _iso_offset(20, now),
                        "modules": {"antimalware": endpoint.kind != "discovered"},
                        "productOutdated": False,
                        "lastSuccessfulScan": {
                            "name": "Quick Scan",
                            "date": _iso_offset(endpoint.last_seen_days, now),
                        },
                    },
                }
            )
        return items

    def inventory(self, call: Call, params: dict[str, Any], request_id: Any) -> Any:
        filters = params.get("filters") or {}
        kinds = filters.get("type") or {}
        depth = (filters.get("depth") or {}).get("allItemsRecursively", False)
        page = int(params.get("page") or 1)
        per_page = int(params.get("perPage") or 1000)
        if not 1 <= per_page <= 1000:
            return _rpc_error(
                request_id, -32602, "Invalid params", "perPage must be between 1 and 1000"
            )
        parent = params.get("parentId")
        if parent is not None and self._company(parent) is None and parent != PARTNER_ID:
            return _rpc_error(
                request_id,
                -32602,
                "Invalid params",
                "Invalid value for 'parentId': the item does not exist",
            )
        items: list[dict[str, Any]] = []
        if kinds.get("companies") and depth:
            items = [
                {
                    "id": held,
                    "name": self._company_name(c, index),
                    "type": 1,
                    "parentId": PARTNER_ID,
                    "companyId": held,
                    "details": {
                        "type": 1,
                        "isSuspended": False,
                        "canBeManagedByAbove": True,
                        "createdAt": _iso_offset(800, call.now),
                        "riskScore": {"value": f"{40 + n}%", "impact": "Medium"},
                    },
                }
                for n, c in enumerate(self.world.customers)
                for index, held in enumerate(c.bitdefender_company_ids)
                if parent is None or parent == PARTNER_ID or parent == held
            ]
        elif kinds.get("computers") or kinds.get("virtualMachines"):
            if not depth:
                items = []  # the vendor's silent case: no depth, no rows
            else:
                for customer in self.world.customers:
                    for index, held in enumerate(customer.bitdefender_company_ids):
                        if parent is not None and parent != held:
                            continue
                        for device in self.world.devices:
                            if (
                                device.customer_key == customer.key
                                and device.in_bitdefender
                                and device.bitdefender_company_index == index
                            ):
                                items.append(self._endpoint_item(device, held, call.now))
                        if index == 0:
                            items.extend(self._vendor_only_items(customer, held, call.now))
        start = (page - 1) * per_page
        batch = items[start : start + per_page]
        pages = max(1, -(-len(items) // per_page))
        return {
            "page": page,
            "perPage": per_page,
            "pagesCount": pages,
            "total": len(items),
            "hasMoreRecords": page < pages,
            "items": batch,
        }

    def endpoint_details(self, call: Call, params: dict[str, Any], request_id: Any) -> Any:
        wanted = params.get("endpointId")
        device = next(
            (
                d
                for d in self.world.devices
                if self._endpoint_ids.get(d.id) == wanted and d.in_bitdefender
            ),
            None,
        )
        if device is None:
            return _rpc_error(request_id, -32602, "Invalid params", "Endpoint does not exist")
        customer = self.world.customer(device.customer_key)
        company_id = customer.bitdefender_company_ids[device.bitdefender_company_index]
        item = self._endpoint_item(device, company_id, call.now)
        last_seen = (
            device.bitdefender_last_seen_days
            if device.bitdefender_last_seen_days is not None
            else device.last_contact_days
        )
        return {
            "id": item["id"],
            "name": device.system_name,
            "companyId": company_id,
            "operatingSystem": item["details"]["operatingSystemVersion"],
            "state": 1 if (last_seen or 99) < 0.2 else 2,
            "ip": item["details"]["ip"],
            "firstSeen": item["details"]["firstSeen"],
            "lastSeen": _iso_offset(last_seen, call.now),
            "isIsolated": False,
            "machineType": item["details"]["machineType"],
            "agent": {
                "engineVersion": "7.101551",
                "primaryEngine": 3,
                "lastUpdate": _iso_offset(last_seen, call.now),
                "licensed": 1,
                "productOutdated": item["details"]["productOutdated"],
                "productUpdateDisabled": False,
                "productVersion": "8.26.9.668",
                "signatureOutdated": False,
                "signatureUpdateDisabled": False,
                "type": 3,
            },
            "malwareStatus": {"detection": False, "infected": False},
            "policy": item["details"]["policy"],
            "modules": item["details"]["modules"],
            "label": "",
            "managedWithBest": True,
            "riskScore": item["details"]["riskScore"],
            "lastSuccessfulScan": item["details"]["lastSuccessfulScan"],
        }

    def incidents(self, call: Call, params: dict[str, Any], request_id: Any) -> Any:
        filters = params.get("filters") or {}
        found = self._company(filters.get("companyId"))
        page = int(params.get("page") or 1)
        per_page = int(params.get("perPage") or 100)
        if not 10 <= per_page <= 10000:
            return _rpc_error(
                request_id, -32602, "Invalid params", "perPage must be between 10 and 10000"
            )
        items: list[dict[str, Any]] = []
        if found is not None and found[0].edr_licensed:
            customer = found[0]
            for n, incident in enumerate(
                i for i in self.world.detections if i.customer_key == customer.key
            ):
                device = self.world.device(incident.device_id)
                items.append(
                    {
                        "incidentId": f"6a1b{n:020d}",
                        "incidentNumber": 100 + n,
                        "incidentType": "incident",
                        "company": {"id": filters.get("companyId"), "name": customer.name},
                        "status": "closed" if incident.action != "Blocked" else "open",
                        "mainAction": "blocked"
                        if incident.action in ("Blocked", "Deleted", "Quarantined")
                        else "reported",
                        "created": _iso_offset(incident.detected_days, call.now),
                        "lastUpdated": _iso_offset(incident.detected_days - 0.1, call.now),
                        "severityScore": 30 + (n * 7) % 60,
                        "priority": ["low", "medium", "high"][n % 3],
                        "attackTypes": ["Malware"],
                        "incidentLink": f"https://gravityzone.demo.invalid/#!/incidents/view/6a1b{n:020d}",
                        "details": {
                            "detectionName": incident.threat,
                            "computerId": self._endpoint_ids[device.id],
                            "computerName": device.system_name,
                            "computerFqdn": device.dns_name,
                            "computerIp": "10.0.0.9",
                        },
                    }
                )
        start = (page - 1) * per_page
        batch = items[start : start + per_page]
        pages = max(1, -(-len(items) // per_page))
        return {
            "total": len(items),
            "page": page,
            "perPage": per_page,
            "pagesCount": pages,
            "items": batch,
        }
