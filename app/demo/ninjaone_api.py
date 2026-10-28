"""NinjaOne, faked: the endpoints `NinjaOneClient` sends, answered from the world.

Shapes follow docs/api-notes/ninjaone.md and the recorded fixtures: bare arrays
for the list endpoints with `after` paging, the `{cursor, results}` envelope
for `/v2/queries/*` with the same cursor name re-sent on every page and the
offset checked by the client, epoch-second timestamps, `df=org=<id>` scoping.
The vendor's silent filter behaviours are kept: an unknown `status` on the
waiting list returns no rows, and an unknown one on the install history
returns everything.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import Any

import httpx

from app.demo.api import Call, Router, json_response, paged, parse_date
from app.demo.dataset import DAYS, Customer, Device, World, epoch, iso
from app.demo.vocabulary import SOFTWARE

DEMO_BASE_URL = "https://ninjaone.demo.invalid"
OS_PATCH_STATUSES = {"APPROVED", "MANUAL", "REJECTED"}
INSTALL_STATUSES = {"INSTALLED", "FAILED"}


def _error(status: int, message: str) -> Any:
    if status == 401:
        return {"error": "invalid_token", "error_description": message}
    return {"resultCode": "ERROR", "errorMessage": message}


def _customer_of(world: World, device: Device) -> Customer:
    return world.customer(device.customer_key)


def _offline(device: Device) -> bool:
    """NinjaOne reports an agent that is not connected right now. A machine seen in
    the last few hours is on; one lost for days is not. Everything read as offline
    until 2026-09-29, because the threshold was 72 minutes."""
    seen = device.last_contact_days
    if seen is None:
        return True
    return seen > (0.5 if device.is_server else 0.35)


def _device_row(world: World, device: Device, now: dt.datetime) -> dict[str, Any]:
    customer = _customer_of(world, device)
    return {
        "id": device.id,
        "uid": f"demo-{device.id}",
        "organizationId": customer.ninja_org_id,
        "locationId": device.location_id,
        "nodeClass": device.node_class,
        "approvalStatus": "APPROVED",
        "offline": _offline(device),
        "systemName": device.system_name,
        "dnsName": device.dns_name,
        "displayName": device.system_name,
        "created": epoch(device.created_days, now),
        "lastContact": epoch(device.last_contact_days, now),
        "lastUpdate": epoch(device.last_contact_days, now),
        "tags": [],
    }


def _os_block(device: Device, now: dt.datetime) -> dict[str, Any]:
    windows = "Windows" in device.os_name
    return {
        "manufacturer": "Microsoft Corporation" if windows else "Apple Inc.",
        "name": device.os_name,
        "architecture": "64-bit",
        "lastBootTime": epoch((device.last_contact_days or 0) + 3.0, now),
        "buildNumber": "26100" if "11" in device.os_name else "19045" if windows else "23G93",
        "releaseId": "24H2" if "11" in device.os_name else "22H2" if windows else "",
        "needsReboot": device.pending_reboot,
    }


def _system_block(device: Device) -> dict[str, Any]:
    return {
        "name": device.system_name,
        "manufacturer": device.manufacturer,
        "model": device.model,
        "biosSerialNumber": device.serial,
        "serialNumber": device.serial,
        "domain": f"{device.customer_key}.local",
        "domainRole": "PRIMARY_DOMAIN_CONTROLLER"
        if device.system_name.endswith("DC01")
        else "MEMBER_WORKSTATION",
        "numberOfProcessors": 1,
        "totalPhysicalMemory": device.memory_gb * 2**30 - 400 * 2**20,
        "virtualMachine": device.virtual,
        "chassisType": "Notebook" if "-LT-" in device.system_name else "Desktop",
    }


def _volume_rows(device: Device, now: dt.datetime, *, bitlocker: bool) -> list[dict[str, Any]]:
    rows = []
    for volume in device.volumes:
        row: dict[str, Any] = {
            "name": volume.letter,
            "driveLetter": volume.letter,
            "label": "OS" if volume.letter == "C:" else "Data",
            "deviceType": "Local Disk",
            "fileSystem": "NTFS" if "Windows" in device.os_name else "APFS",
            "capacity": volume.capacity,
            "freeSpace": volume.free,
            "serialNumber": f"{device.id:08X}",
            "deviceId": device.id,
            "timestamp": epoch(device.last_contact_days, now),
        }
        if bitlocker and volume.bitlocker is not None:
            encrypted = volume.bitlocker == "Encrypted"
            row["bitLockerStatus"] = {
                "conversionStatus": "FULLY_ENCRYPTED" if encrypted else "FULLY_DECRYPTED",
                "encryptionMethod": "XTS_AES_128" if encrypted else "NONE",
                "protectionStatus": "PROTECTED" if encrypted else "UNPROTECTED",
                "lockStatus": "UNLOCKED",
                "initializedForProtection": encrypted,
            }
        rows.append(row)
    return rows


class NinjaOneApi:
    """One instance per world; holds the query cursors between pages."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.router = Router("ninjaone", _error)
        self._cursors: dict[str, int] = {}
        self._results: dict[str, list[dict[str, Any]]] = {}
        r = self.router
        r.add("POST", "/ws/oauth/token", self.token)
        r.add("GET", "/v2/organizations", self.organizations)
        r.add("GET", "/v2/devices", self.devices)
        r.add("GET", "/v2/organization/{organization_id}/devices", self.organization_devices)
        r.add("GET", "/v2/organization/{organization_id}/locations", self.locations)
        r.add("GET", "/v2/device/{device_id}", self.device)
        r.add("GET", "/v2/device/{device_id}/activities", self.device_activities)
        r.add("GET", "/v2/activities", self.activities)
        r.add("GET", "/v2/alerts", self.alerts)
        r.add("GET", "/v2/device-custom-fields", self.custom_field_definitions)
        r.add("GET", "/v2/queries/{report}", self.query)

    def handle(self, request: httpx.Request) -> httpx.Response:
        return self.router.handle(request, self.world, dt.datetime.now(dt.UTC))

    # --- auth and organisations -----------------------------------------------------------

    def token(self, call: Call) -> httpx.Response:
        form = dict(httpx.QueryParams(call.body if isinstance(call.body, str) else ""))
        if form.get("grant_type") != "client_credentials" or not form.get("client_id"):
            return json_response(_error(401, "invalid client"), 401)
        return json_response(
            {
                "access_token": "demo-token",
                "expires_in": 3600,
                "token_type": "bearer",
                "scope": "monitoring",
            }
        )

    def organizations(self, call: Call) -> httpx.Response:
        rows = [
            {"id": c.ninja_org_id, "name": c.name, "description": c.industry}
            for c in call.world.customers
        ]
        rows.append(
            {"id": 199, "name": call.world.internal_organisation, "description": "Internal"}
        )
        after = call.int_param("after", -1)
        return json_response(
            paged(rows, after, call.int_param("pageSize", 100), lambda r: int(r["id"]))
        )

    # --- devices -----------------------------------------------------------------------------

    def _org_filter(self, call: Call) -> int | None:
        df = call.param("df")
        if df and df.startswith("org="):
            return int(df.removeprefix("org="))
        return None

    def _devices_in(self, organization_id: int | None) -> list[Device]:
        if organization_id is None:
            return list(self.world.devices)
        return [
            d
            for d in self.world.devices
            if _customer_of(self.world, d).ninja_org_id == organization_id
        ]

    def devices(self, call: Call) -> httpx.Response:
        rows = [_device_row(call.world, d, call.now) for d in self.world.devices]
        page = paged(
            rows,
            call.int_param("after", -1),
            call.int_param("pageSize", 1000),
            lambda r: int(r["id"]),
        )
        return json_response(page)

    def organization_devices(self, call: Call) -> httpx.Response:
        organization_id = int(call.path_params["organization_id"])
        rows = [_device_row(call.world, d, call.now) for d in self._devices_in(organization_id)]
        page = paged(
            rows,
            call.int_param("after", -1),
            call.int_param("pageSize", 100),
            lambda r: int(r["id"]),
        )
        return json_response(page)

    def locations(self, call: Call) -> httpx.Response:
        organization_id = int(call.path_params["organization_id"])
        customer = next(
            (c for c in call.world.customers if c.ninja_org_id == organization_id), None
        )
        if customer is None:
            return json_response(_error(404, "organization not found"), 404)
        return json_response(
            [
                {"id": loc.id, "name": loc.name, "address": loc.address, "description": ""}
                for loc in customer.locations
            ]
        )

    def device(self, call: Call) -> httpx.Response:
        device_id = int(call.path_params["device_id"])
        device = next((d for d in self.world.devices if d.id == device_id), None)
        if device is None:
            return json_response(_error(404, "device not found"), 404)
        row = _device_row(call.world, device, call.now)
        row.update(
            {
                "ipAddresses": [f"10.{device.id % 200}.{device.id % 250}.{device.id % 254 + 1}"],
                "macAddresses": list(device.macs),
                "publicIP": "203.0.113.5",
                "deviceType": "SERVER" if device.is_server else "WORKSTATION",
                "lastLoggedInUser": _login(device),
                "os": _os_block(device, call.now),
                "system": _system_block(device),
                "volumes": _volume_rows(device, call.now, bitlocker=False),
            }
        )
        return json_response(row)

    # --- alerts and activities ------------------------------------------------------------

    def _alert_row(self, alert: Any, now: dt.datetime) -> dict[str, Any]:
        device = self.world.device(alert.device_id)
        customer = _customer_of(self.world, device)
        return {
            "uid": f"00000000-0000-4000-8000-{alert.id:012d}",
            "deviceId": device.id,
            "message": alert.message,
            "subject": alert.message,
            "createTime": epoch(alert.created_days, now),
            "updateTime": epoch(alert.created_days, now),
            "sourceType": "CONDITION_CUSTOM",
            "sourceName": alert.message,
            "severity": alert.severity,
            "priority": {"MAJOR": "HIGH", "MODERATE": "MEDIUM", "MINOR": "LOW"}.get(
                alert.severity, "MEDIUM"
            ),
            "conditionName": alert.message,
            "device": {
                "id": device.id,
                "organizationId": customer.ninja_org_id,
                "systemName": device.system_name,
                "displayName": device.system_name,
            },
        }

    def alerts(self, call: Call) -> httpx.Response:
        organization_id = self._org_filter(call)
        device_ids = {d.id for d in self._devices_in(organization_id)}
        rows = [
            self._alert_row(a, call.now)
            for a in self.world.alerts
            if a.resolved_days is None and a.device_id in device_ids
        ]
        return json_response(rows)

    def _activity_rows(self, organization_id: int | None, now: dt.datetime) -> list[dict[str, Any]]:
        device_ids = {d.id for d in self._devices_in(organization_id)}
        rows = []
        for alert in self.world.alerts:
            if alert.device_id not in device_ids:
                continue
            for status, days, offset in (
                ("TRIGGERED", alert.created_days, 0),
                ("RESET", alert.resolved_days, 1),
            ):
                if days is None:
                    continue
                rows.append(
                    {
                        "id": alert.id * 10 + offset,
                        "activityTime": epoch(days, now),
                        "deviceId": alert.device_id,
                        "severity": alert.severity,
                        "priority": "MEDIUM",
                        "activityType": "CONDITION",
                        "statusCode": status,
                        "status": status.title(),
                        "activityResult": "SUCCESS",
                        "sourceName": alert.message,
                        "subject": alert.message,
                        "message": alert.message
                        if status == "TRIGGERED"
                        else f"{alert.message} is no longer met",
                        "type": "Condition",
                    }
                )
        return sorted(rows, key=lambda r: -r["id"])

    def activities(self, call: Call) -> httpx.Response:
        if call.param("status") and call.param("status") not in {"RESET", "TRIGGERED"}:
            return json_response(_error(500, "unknown status"), 500)
        rows = self._activity_rows(self._org_filter(call), call.now)
        if call.param("type") == "CONDITION":
            rows = [r for r in rows if r["activityType"] == "CONDITION"]
        if call.param("status"):
            rows = [r for r in rows if r["statusCode"] == call.param("status")]
        after, before = parse_date(call.param("after")), parse_date(call.param("before"))
        if after:
            rows = [r for r in rows if r["activityTime"] >= after.timestamp()]
        if before:
            rows = [r for r in rows if r["activityTime"] < before.timestamp()]
        older_than = call.param("olderThan")
        if older_than:
            rows = [r for r in rows if r["id"] < int(older_than)]
        page = rows[: max(call.int_param("pageSize", 200), 10)]
        return json_response({"lastActivityId": page[-1]["id"] if page else 0, "activities": page})

    def device_activities(self, call: Call) -> httpx.Response:
        device_id = int(call.path_params["device_id"])
        rows = [r for r in self._activity_rows(None, call.now) if r["deviceId"] == device_id]
        start = parse_date(call.param("startTime"))
        if start:
            rows = [r for r in rows if r["activityTime"] >= start.timestamp()]
        page = rows[: call.int_param("pageSize", 100)]
        return json_response(
            {
                "lastActivityId": page[-1]["id"] if page else 0,
                "lastNodeActivityId": 0,
                "activities": page,
            }
        )

    def custom_field_definitions(self, call: Call) -> httpx.Response:
        return json_response(
            [
                {
                    "name": "assetTag",
                    "label": "Asset tag",
                    "description": "",
                    "type": "TEXT",
                    "scope": "NODE",
                    "active": True,
                    "system": False,
                }
            ]
        )

    # --- the report queries ---------------------------------------------------------------

    def query(self, call: Call) -> httpx.Response:
        report = call.path_params["report"]
        page_size = call.int_param("pageSize", 5000)
        organization_id = self._org_filter(call)
        cursor = call.param("cursor")
        filters = {k: v for k, v in call.params.items() if k not in ("pageSize", "cursor")}
        key = hashlib.sha1(  # noqa: S324 - a page token, not a secret
            f"{report}|{sorted(filters.items())}".encode()
        ).hexdigest()[:16]
        name = f"cursor{key}"
        if cursor is None:
            rows = self._report_rows(report, organization_id, filters, call.now)
            if rows is None:
                return json_response(_error(404, f"unknown report {report}"), 404)
            self._results[name] = rows
            offset = 0
        elif cursor != name or cursor not in self._results:
            return json_response(_error(400, "unknown cursor"), 400)
        else:
            offset = self._cursors[cursor]
        rows = self._results[name]
        batch = rows[offset : offset + page_size]
        self._cursors[name] = offset + len(batch)
        count = None if organization_id is not None else len(rows)  # null under df, as live
        # Past the last row the vendor answers an empty page with no cursor name.
        cursor_out: dict[str, Any] = {
            "name": name if batch else None,
            "offset": offset,
            "count": count,
            "expires": call.now.timestamp() + 600,
        }
        return json_response({"cursor": cursor_out, "results": batch})

    def _report_rows(
        self, report: str, organization_id: int | None, filters: dict[str, str], now: dt.datetime
    ) -> list[dict[str, Any]] | None:
        devices = [d for d in self._devices_in(organization_id) if d.is_agent_class]
        by_id = {d.id: d for d in devices}
        stamp = {
            d.id: epoch(d.last_contact_days if d.last_contact_days is not None else 400.0, now)
            for d in devices
        }
        if report == "operating-systems":
            return [
                {**_os_block(d, now), "deviceId": d.id, "timestamp": stamp[d.id]} for d in devices
            ]
        if report == "computer-systems":
            return [
                {**_system_block(d), "deviceId": d.id, "timestamp": stamp[d.id]} for d in devices
            ]
        if report == "processors":
            return [
                {
                    "name": d.processor,
                    "architecture": "64-bit",
                    "numCores": 8 if d.is_server else 4,
                    "deviceId": d.id,
                    "timestamp": stamp[d.id],
                }
                for d in devices
            ]
        if report == "volumes":
            return [
                row
                for d in devices
                for row in _volume_rows(d, now, bitlocker=filters.get("include") == "bl")
            ]
        if report == "logged-on-users":
            return [
                {
                    "userName": _login(d),
                    "logonTime": epoch((d.last_contact_days or 0) + 0.2, now),
                    "deviceId": d.id,
                }
                for d in devices
                if d.last_user is not None
            ]
        if report == "antivirus-status":
            rows = []
            for d in devices:
                if d.node_class == "MAC":
                    continue
                if d.in_bitdefender:
                    rows.append(
                        {
                            "productName": "Bitdefender Endpoint Security Tools Antimalware",
                            "productState": "ON",
                            "definitionStatus": "Out-of-Date"
                            if d.scenario == "rmm_stale"
                            else "Up-to-Date",
                            "version": "7.9.8.1",
                            "deviceId": d.id,
                            "timestamp": stamp[d.id],
                        }
                    )
                rows.append(
                    {
                        "productName": "Windows Defender",
                        "productState": "OFF" if d.in_bitdefender else "ON",
                        "definitionStatus": "Up-to-Date",
                        "version": "4.18.24090.11",
                        "deviceId": d.id,
                        "timestamp": stamp[d.id],
                    }
                )
            return rows
        if report == "software":
            rows = []
            findings_by_device: dict[int, set[str]] = {}
            for finding in self.world.findings:
                findings_by_device.setdefault(finding.device_id, set()).add(finding.product)
            for d in devices:
                if d.node_class == "MAC":
                    continue
                vulnerable = findings_by_device.get(d.id, set())
                for n, (title, publisher, bad, good) in enumerate(SOFTWARE):
                    if (d.id + n) % 3 == 0 and title not in vulnerable:
                        continue
                    rows.append(
                        {
                            "name": title,
                            "publisher": publisher,
                            "version": bad if title in vulnerable else good,
                            "installDate": (
                                now - dt.timedelta(days=(d.id + n) % 300 + 30)
                            ).strftime("%Y-%m-%d"),
                            "deviceId": d.id,
                            "timestamp": stamp[d.id],
                        }
                    )
            return rows
        if report == "windows-services":
            rows = []
            for d in devices:
                if "Windows" not in d.os_name:
                    continue
                for name, display, start, state in (
                    (
                        "Spooler",
                        "Print Spooler",
                        "AUTO_START",
                        "STOPPED" if d.id % 17 == 0 else "RUNNING",
                    ),
                    ("wuauserv", "Windows Update", "DEMAND_START", "STOPPED"),
                    ("WinDefend", "Microsoft Defender Antivirus Service", "AUTO_START", "RUNNING"),
                ):
                    if filters.get("state") and filters["state"] != state:
                        continue
                    if (
                        filters.get("name")
                        and filters["name"].lower() not in (name + display).lower()
                    ):
                        continue
                    rows.append(
                        {
                            "name": name,
                            "displayName": display,
                            "description": "",
                            "startType": start,
                            "state": state,
                            "userName": "LocalSystem",
                            "deviceId": d.id,
                            "timestamp": stamp[d.id],
                        }
                    )
            return rows
        if report == "custom-fields":
            return [
                {"deviceId": d.id, "fields": {filters.get("fields", "assetTag"): f"TAG-{d.id}"}}
                for d in devices
            ]
        if report == "network-interfaces":
            return [
                {
                    "deviceId": d.id,
                    "adapterName": "Ethernet",
                    "interfaceName": "Ethernet",
                    "interfaceType": "ETHERNET",
                    "interfaceIndex": "1",
                    "macAddress": list(d.macs),
                    "ipAddress": [f"10.{d.id % 200}.{d.id % 250}.{d.id % 254 + 1}"],
                    "dnsHostName": d.hostname,
                    "dnsServers": "10.0.0.2",
                    "defaultGateway": "10.0.0.1",
                    "subnetMask": "255.255.255.0",
                    "linkSpeed": "1 Gbps",
                    "mtu": "1500",
                    "status": "UP",
                }
                for d in devices
            ]
        if report in ("os-patches", "software-patches"):
            kind = "os" if report == "os-patches" else "software"
            status = filters.get("status")
            if status is not None and status not in OS_PATCH_STATUSES:
                return []  # the vendor's silent failure on the waiting list: no rows
            rows = []
            for p in self.world.pending:
                if p.kind != kind or p.device_id not in by_id or (status and p.status != status):
                    continue
                if kind == "os":
                    if filters.get("severity") and p.severity != filters["severity"]:
                        continue
                    rows.append(
                        {
                            "id": f"pend-{p.id}",
                            "name": p.title,
                            "kbNumber": p.identifier,
                            "severity": p.severity,
                            "status": p.status,
                            "type": "SECURITY_UPDATES"
                            if p.severity in ("CRITICAL", "IMPORTANT")
                            else "REGULAR_UPDATES",
                            "deviceId": p.device_id,
                            "timestamp": stamp[p.device_id],
                        }
                    )
                else:
                    rows.append(
                        {
                            "id": f"pend-{p.id}",
                            "productIdentifier": p.identifier,
                            "title": f"{p.title} {p.version}",
                            "impact": p.severity,
                            "status": p.status,
                            "type": "PATCH",
                            "deviceId": p.device_id,
                            "timestamp": stamp[p.device_id],
                        }
                    )
            return rows
        if report in ("os-patch-installs", "software-patch-installs"):
            kind = "os" if report == "os-patch-installs" else "software"
            after, before = (
                parse_date(filters.get("installedAfter")),
                parse_date(filters.get("installedBefore")),
            )
            status = filters.get("status")
            wanted = (
                status if status in INSTALL_STATUSES else None
            )  # unknown: ignored, everything returned
            rows = []
            for i in self.world.installs:
                if i.kind != kind or i.device_id not in by_id or (wanted and i.status != wanted):
                    continue
                when = epoch(i.installed_days, now) or 0.0
                if after and when < after.timestamp():
                    continue
                if before and when >= before.timestamp():
                    continue
                if kind == "os":
                    rows.append(
                        {
                            "id": f"inst-{i.id}",
                            "name": i.title,
                            "kbNumber": i.identifier,
                            "severity": i.severity,
                            "status": i.status,
                            "type": "SECURITY_UPDATES"
                            if i.severity in ("CRITICAL", "IMPORTANT")
                            else "DEFINITION_UPDATES"
                            if i.identifier == "KB2267602"
                            else "REGULAR_UPDATES",
                            "installedAt": when,
                            "deviceId": i.device_id,
                            "timestamp": when,
                        }
                    )
                else:
                    rows.append(
                        {
                            "id": f"inst-{i.id}",
                            "productIdentifier": i.identifier,
                            "title": i.title,
                            "impact": "recommended",
                            "status": i.status,
                            "type": "PATCH",
                            "installedAt": when,
                            "deviceId": i.device_id,
                            "timestamp": when,
                        }
                    )
            return rows
        return None


def _login(device: Device) -> str:
    if device.last_user is None:
        return ""
    return f"{device.customer_key.upper()}\\{device.last_user.login}"


__all__ = ["DAYS", "DEMO_BASE_URL", "NinjaOneApi", "iso"]
