"""ConnectSecure v4, faked: the `/r/...` reads `ConnectSecureClient` sends, answered from the world.

`POST /w/authorize` with the `Client-Auth-Token` header gives a token and a
user id, both sent back on every read. Listings answer `{status, total, data}`
and take `condition`, `skip` and `limit`; the condition grammar is the
client's own (`company_id=30001 AND unconfirmed=false`). The vendor's silent
failures are kept: an unknown field or value on `/r/company/*` and
`/r/asset/*` returns no rows, and on `/r/report_queries/*` returns everything.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import httpx

from app.demo.api import Call, Router, json_response
from app.demo.dataset import Customer, Device, World, at

DEMO_BASE_URL = "https://connectsecure.demo.invalid"
TENANT = 77
PROBLEM_GROUPS = {"CRITICAL": "1", "HIGH": "2", "MEDIUM": "3", "LOW": "4"}
KEV_GROUP = "34"
_CONDITION = re.compile(r"^\s*(\w+)\s*=\s*('(?:[^']|'')*'|[^\s]+)\s*$")


def _error(status: int, message: str) -> Any:
    return {"status": False, "message": message}


def _stamp(days: float | None, now: dt.datetime, *, zulu: bool = False) -> str | None:
    moment = at(days, now)
    if moment is None:
        return None
    text = moment.strftime("%Y-%m-%dT%H:%M:%S.%f")
    return text + "Z" if zulu else text


def parse_condition(text: str | None) -> dict[str, str] | None:
    """`field=value AND field='value'` -> {field: value}; None when malformed."""
    if not text:
        return {}
    parsed: dict[str, str] = {}
    for part in text.split(" AND "):
        match = _CONDITION.match(part)
        if match is None:
            return None
        value = match.group(2)
        if value.startswith("'"):
            value = value[1:-1].replace("''", "'")
        parsed[match.group(1)] = value
    return parsed


class ConnectSecureApi:
    def __init__(self, world: World) -> None:
        self.world = world
        self.router = Router("connectsecure", _error)
        r = self.router
        r.add("POST", "/w/authorize", self.authorize)
        r.add("GET", "/r/company/companies", self.companies)
        r.add("GET", "/r/company/company_stats", self.company_stats)
        r.add("GET", "/r/company/agents", self.agents)
        r.add("GET", "/r/asset/asset_view", self.assets)
        r.add("GET", "/r/asset/asset_stats", self.asset_stats)
        r.add("GET", "/r/report_queries/asset_wise_vulnerabilities", self.vulnerabilities)
        r.add("GET", "/r/report_queries/vulnerabilities_details", self.vulnerabilities)
        r.add("GET", "/r/report_queries/get_remediation", self.remediations)
        r.add("GET", "/r/user/get_users", self.users)
        self._asset_ids = {d.id: 50_000 + n for n, d in enumerate(world.devices)}
        self._agent_ids = {d.id: 60_000 + n for n, d in enumerate(world.devices)}

    def handle(self, request: httpx.Request) -> httpx.Response:
        return self.router.handle(request, self.world, dt.datetime.now(dt.UTC))

    def authorize(self, call: Call) -> httpx.Response:
        if not call.request.headers.get("client-auth-token"):
            return json_response(_error(401, "missing Client-Auth-Token"), 401)
        return json_response(
            {"status": True, "access_token": "demo-cs-token", "user_id": "u-demo-1"}
        )

    def _check(self, call: Call) -> httpx.Response | None:
        if call.request.headers.get(
            "authorization"
        ) != "Bearer demo-cs-token" or not call.request.headers.get("x-user-id"):
            return json_response(_error(401, "unauthorized"), 401)
        return None

    def _listing(
        self, call: Call, rows: list[dict[str, Any]], *, report: bool, allowed: set[str]
    ) -> httpx.Response:
        refused = self._check(call)
        if refused is not None:
            return refused
        condition = parse_condition(call.param("condition"))
        if condition is None or any(field not in allowed for field in condition):
            # The vendor's silent failures, by endpoint family.
            rows = list(rows) if report else []
        else:
            rows = [
                row
                for row in rows
                if all(self._row_has(row, field, value) for field, value in condition.items())
            ]
        skip, limit = call.int_param("skip", 0), call.int_param("limit", 10_000)
        return json_response(
            {"status": True, "total": len(rows), "data": rows[skip : skip + limit]}
        )

    @staticmethod
    def _row_has(row: dict[str, Any], field: str, value: str) -> bool:
        if field in ("unconfirmed", "problem_group_id", "severity"):
            return True  # applied by the handler before the listing
        have = row.get(field)
        if have is None and field == "company_id":
            return str(value) in [str(v) for v in row.get("company_ids", [])]
        return str(have).casefold() == str(value).casefold()

    # --- companies and agents --------------------------------------------------------------

    def _scanned(self) -> list[Customer]:
        return [c for c in self.world.customers if c.connectsecure_company_id is not None]

    def companies(self, call: Call) -> httpx.Response:
        rows = [
            {
                "id": c.connectsecure_company_id,
                "name": c.name,
                "is_deleted": False,
                "is_assessment": False,
                "created": _stamp(700, call.now),
                "updated": _stamp(1, call.now),
                "tenantid": TENANT,
                "internal_scan": True,
                "external_scan": True,
            }
            for c in self._scanned()
        ]
        rows.append(
            {
                "id": 30_900,
                "name": "Proseware Assessment",
                "is_deleted": False,
                "is_assessment": True,
                "created": _stamp(20, call.now),
                "updated": _stamp(1, call.now),
                "tenantid": TENANT,
            }
        )
        rows.append(
            {
                "id": 30_901,
                "name": "Lucerne Publishing",
                "is_deleted": True,
                "is_assessment": False,
                "created": _stamp(600, call.now),
                "updated": _stamp(200, call.now),
                "tenantid": TENANT,
            }
        )
        return self._listing(call, rows, report=False, allowed={"id"})

    def _devices_of(self, customer: Customer) -> list[Device]:
        return [
            d
            for d in self.world.devices
            if d.customer_key == customer.key and d.in_connectsecure and d.is_agent_class
        ]

    def _scan_days(self, device: Device) -> float:
        if device.connectsecure_scan_days is not None:
            return device.connectsecure_scan_days
        return (device.last_contact_days if device.last_contact_days is not None else 60.0) + 0.4

    def agents(self, call: Call) -> httpx.Response:
        rows = []
        for customer in self._scanned():
            for device in self._devices_of(customer):
                scan = self._scan_days(device)
                rows.append(
                    {
                        "id": self._agent_ids[device.id],
                        "host_name": device.hostname,
                        "ip": f"10.{device.id % 200}.{device.id % 250}.{device.id % 254 + 1}",
                        "os_type": "2",
                        "os_platform": device.os_name.replace(" Edition", ""),
                        "os_name": "Windows",
                        "os_version": "10.0.26100",
                        "agent_type": "LIGHTWEIGHT",
                        "agent_version": "4.4.0",
                        "last_scanned_time": _stamp(scan, call.now),
                        "last_reported": _stamp(scan - 0.1, call.now),
                        "last_ping_time": _stamp(scan - 0.1, call.now),
                        "is_deprecated": False,
                        "is_retired": False,
                        "agent_deleted": False,
                        "reported": True,
                        "company_id": customer.connectsecure_company_id,
                        "mac": device.macs[0].lower() if device.vendor_identifiers else "",
                        "tenantid": TENANT,
                    }
                )
            for n, endpoint in enumerate(
                e
                for e in self.world.vendor_only
                if e.tool == "connectsecure" and e.customer_key == customer.key
            ):
                rows.append(
                    {
                        "id": 65_000 + customer.ninja_org_id * 10 + n,
                        "host_name": endpoint.name,
                        "ip": "10.99.0.7",
                        "os_type": "2",
                        "os_platform": "Microsoft Windows 11 Pro",
                        "os_name": "Windows",
                        "agent_type": "PROBE" if endpoint.kind == "discovered" else "LIGHTWEIGHT",
                        "agent_version": "4.4.0",
                        "last_scanned_time": _stamp(endpoint.last_seen_days, call.now),
                        "last_reported": _stamp(endpoint.last_seen_days, call.now),
                        "last_ping_time": _stamp(endpoint.last_seen_days, call.now),
                        "is_deprecated": endpoint.kind == "discovered",
                        "is_retired": False,
                        "agent_deleted": False,
                        "reported": True,
                        "company_id": customer.connectsecure_company_id,
                        "mac": endpoint.mac.lower(),
                        "tenantid": TENANT,
                    }
                )
        return self._listing(call, rows, report=False, allowed={"company_id"})

    # --- assets --------------------------------------------------------------------------------

    def _findings_of(self, device: Device) -> list[Any]:
        return [f for f in self.world.findings if f.device_id == device.id]

    def _asset_row(self, customer: Customer, device: Device, now: dt.datetime) -> dict[str, Any]:
        findings = self._findings_of(device)
        confirmed = [f for f in findings if f.confirmed]
        unconfirmed = [f for f in findings if not f.confirmed]

        def count(rows: list[Any], severity: str) -> int:
            return sum(1 for f in rows if f.severity == severity)

        risk = min(
            100,
            20
            + 15 * count(confirmed, "CRITICAL")
            + 5 * count(confirmed, "HIGH")
            + 25 * sum(1 for f in confirmed if f.kev),
        )
        grade = (
            "A"
            if risk <= 40
            else "B"
            if risk <= 45
            else "C"
            if risk <= 60
            else "D"
            if risk <= 75
            else "E"
            if risk <= 90
            else "F"
        )
        scan = self._scan_days(device)
        return {
            "id": self._asset_ids[device.id],
            "host_name": device.hostname,
            "name": device.hostname,
            "ip": f"10.{device.id % 200}.{device.id % 250}.{device.id % 254 + 1}",
            "mac": device.macs[0].lower() if device.vendor_identifiers else "",
            "serial_number": device.serial if device.vendor_identifiers else "",
            "domain": f"{device.customer_key}.local",
            "os_name": device.os_name.replace(" Edition", ""),
            "os_version": "10.0.26100",
            "platform": "mac" if device.node_class == "MAC" else "windows",
            "asset_type": "agent",
            "importance": 25,
            "agent_id": self._agent_ids[device.id],
            "agent_type": "LIGHTWEIGHT",
            "agent_version": "4.4.0",
            "company_id": customer.connectsecure_company_id,
            "company_name": customer.name,
            "is_deprecated": False,
            "online_status": scan < 1,
            "security_grade": grade,
            "max_risk_score": float(min(100, risk + 15)),
            "avg_risk_score": float(risk),
            "critical": count(confirmed, "CRITICAL"),
            "high": count(confirmed, "HIGH"),
            "medium": count(confirmed, "MEDIUM"),
            "low": count(confirmed, "LOW"),
            "total_vul": len(confirmed),
            "cisa_vul": sum(1 for f in confirmed if f.kev),
            "epss_vul": 0,
            "critical_unconfirmed": count(unconfirmed, "CRITICAL"),
            "high_unconfirmed": count(unconfirmed, "HIGH"),
            "medium_unconfirmed": count(unconfirmed, "MEDIUM"),
            "low_unconfirmed": count(unconfirmed, "LOW"),
            "total_vul_unconfirmed": len(unconfirmed),
            "cisa_vul_unconfirmed": sum(1 for f in unconfirmed if f.kev),
            "discovered": _stamp(device.created_days, now),
            "last_discovered_time": _stamp(scan, now),
            "last_ping_time": _stamp(scan - 0.1, now),
            "manufacturer": device.manufacturer,
            "hardware_model": device.model,
            "tenantid": TENANT,
        }

    def _external_rows(self, customer: Customer, now: dt.datetime) -> list[dict[str, Any]]:
        rows = []
        for n, endpoint in enumerate(
            e
            for e in self.world.vendor_only
            if e.tool == "connectsecure" and e.customer_key == customer.key
        ):
            rows.append(
                {
                    "id": 55_000 + customer.ninja_org_id * 10 + n,
                    "host_name": endpoint.name,
                    "name": endpoint.name,
                    "ip": "203.0.113.10" if endpoint.kind == "external" else "10.99.0.7",
                    "mac": endpoint.mac.lower(),
                    "serial_number": "",
                    "os_name": "Linux"
                    if endpoint.kind == "external"
                    else "Microsoft Windows 11 Pro",
                    "platform": "linux" if endpoint.kind == "external" else "windows",
                    "asset_type": "external"
                    if endpoint.kind == "external"
                    else "discovered"
                    if endpoint.kind == "discovered"
                    else "agent",
                    "importance": 25,
                    "agent_id": 65_000 + customer.ninja_org_id * 10 + n,
                    "agent_type": "PROBE" if endpoint.kind == "discovered" else "LIGHTWEIGHT",
                    "company_id": customer.connectsecure_company_id,
                    "is_deprecated": endpoint.kind == "discovered",
                    "online_status": True,
                    "security_grade": "B",
                    "max_risk_score": 45.0,
                    "avg_risk_score": 35.0,
                    "critical": 0,
                    "high": 1,
                    "medium": 2,
                    "low": 1,
                    "total_vul": 4,
                    "cisa_vul": 0,
                    "epss_vul": 0,
                    "discovered": _stamp(30, now),
                    "last_discovered_time": _stamp(endpoint.last_seen_days, now),
                    "last_ping_time": _stamp(endpoint.last_seen_days, now),
                    "tenantid": TENANT,
                }
            )
        return rows

    def assets(self, call: Call) -> httpx.Response:
        rows: list[dict[str, Any]] = []
        for customer in self._scanned():
            rows.extend(self._asset_row(customer, d, call.now) for d in self._devices_of(customer))
            rows.extend(self._external_rows(customer, call.now))
        return self._listing(
            call, rows, report=False, allowed={"company_id", "platform", "cisa_vul"}
        )

    def asset_stats(self, call: Call) -> httpx.Response:
        return self._listing(call, [], report=False, allowed={"company_id"})

    def company_stats(self, call: Call) -> httpx.Response:
        rows = []
        for customer in self._scanned():
            devices = self._devices_of(customer)
            confirmed = [f for d in devices for f in self._findings_of(d) if f.confirmed]
            problem = {"1": 0, "2": 0, "3": 0, "4": 0, KEV_GROUP: 0}
            for f in confirmed:
                problem[PROBLEM_GROUPS[f.severity]] += 1
                if f.kev:
                    problem[KEV_GROUP] += 1
            assets = [self._asset_row(customer, d, call.now) for d in devices]
            avg = (
                round(sum(a["avg_risk_score"] for a in assets) / len(assets), 1) if assets else 0.0
            )
            rows.append(
                {
                    "company_id": customer.connectsecure_company_id,
                    "total_assets": len(assets),
                    "date": call.now.strftime("%Y-%m-%d"),
                    "asset_problem_stats": problem,
                    "asset_vul_stats": {
                        "avg_risk_score": avg,
                        "max_risk_score": max((a["max_risk_score"] for a in assets), default=0.0),
                    },
                    "tenantid": TENANT,
                }
            )
        return self._listing(call, rows, report=False, allowed={"company_id"})

    # --- vulnerabilities and remediations -----------------------------------------------------

    def vulnerabilities(self, call: Call) -> httpx.Response:
        condition = parse_condition(call.param("condition")) or {}
        unconfirmed_too = condition.get("unconfirmed", "true").lower() != "false"
        group = condition.get("problem_group_id")
        severity = condition.get("severity")
        rows = []
        for customer in self._scanned():
            for device in self._devices_of(customer):
                for n, f in enumerate(self._findings_of(device)):
                    if not unconfirmed_too and not f.confirmed:
                        continue
                    if group == KEV_GROUP and not f.kev:
                        continue
                    if group in PROBLEM_GROUPS.values() and PROBLEM_GROUPS[f.severity] != group:
                        continue
                    if severity and severity.casefold() != f.severity.casefold():
                        continue
                    score = {"CRITICAL": 9.8, "HIGH": 7.8, "MEDIUM": 5.5, "LOW": 3.1}[f.severity]
                    rows.append(
                        {
                            "problem_id": 700_000 + n + device.id * 10,
                            "problem_name": f.cve,
                            "description": f"{f.product}: {f.cve}",
                            "severity": f.severity.title(),
                            "base_score": score,
                            "epss_score": 0.5 if f.kev else 0.02,
                            "exploitability_score": 3.9,
                            "impact_score": 5.9,
                            "software_name": [f.product],
                            "asset_ids": [self._asset_ids[device.id]],
                            "company_ids": [customer.connectsecure_company_id],
                            "host_name": device.hostname,
                            "ip": f"10.{device.id % 200}.{device.id % 250}.{device.id % 254 + 1}",
                            "port": None,
                            "discovered": _stamp(35 + (device.id % 40), call.now, zulu=True),
                            "last_discovered_time": _stamp(
                                self._scan_days(device), call.now, zulu=True
                            ),
                            "critical_problems": 1 if f.severity == "CRITICAL" else 0,
                            "high_problems": 1 if f.severity == "HIGH" else 0,
                            "medium_problems": 0,
                            "low_problems": 0,
                            "total_problems": 1,
                        }
                    )
        return self._listing(
            call,
            rows,
            report=True,
            allowed={"company_id", "severity", "problem_group_id", "unconfirmed"},
        )

    def remediations(self, call: Call) -> httpx.Response:
        condition = parse_condition(call.param("condition")) or {}
        unconfirmed_too = condition.get("unconfirmed", "true").lower() != "false"
        rows: list[dict[str, Any]] = []
        solution = 7000
        for customer in self._scanned():
            for device in self._devices_of(customer):
                by_fix: dict[str, list[Any]] = {}
                for f in self._findings_of(device):
                    if not unconfirmed_too and not f.confirmed:
                        continue
                    by_fix.setdefault(f.fix, []).append(f)
                for fix, group in by_fix.items():
                    solution += 1
                    first = group[0]
                    target = (
                        fix.split(" to ")[-1].split(" or later")[0]
                        if first.remediation_type == "Update" and " to " in fix
                        else ""
                    )
                    rows.append(
                        {
                            "solution_id": solution,
                            "asset_id": self._asset_ids[device.id],
                            "agent_id": self._agent_ids[device.id],
                            "company_id": customer.connectsecure_company_id,
                            "company_name": customer.name,
                            "host_name": device.hostname,
                            "product": first.product,
                            "fix": target or fix,
                            "fix_script": None,
                            "remediation_action": "Uninstall"
                            if first.remediation_type == "Uninstall"
                            else ("Software Patch" if target else "OS Update"),
                            "os_type": "windows",
                            "url": "",
                            "severity": max(
                                group,
                                key=lambda g: ["LOW", "MEDIUM", "HIGH", "CRITICAL"].index(
                                    g.severity
                                ),
                            ).severity.title(),
                            "total_vuls_count": len(group),
                            "critical_vuls_count": sum(
                                1 for g in group if g.severity == "CRITICAL"
                            ),
                            "high_vuls_count": sum(1 for g in group if g.severity == "HIGH"),
                            "medium_vuls_count": sum(1 for g in group if g.severity == "MEDIUM"),
                            "low_vuls_count": sum(1 for g in group if g.severity == "LOW"),
                            "epss_vuls": 0,
                            "first_vul_discovered": _stamp(40, call.now),
                            "last_vul_discovered": _stamp(self._scan_days(device), call.now),
                            "created": _stamp(40, call.now),
                        }
                    )
        return self._listing(call, rows, report=True, allowed={"company_id", "unconfirmed"})

    def users(self, call: Call) -> httpx.Response:
        rows = [
            {
                "id": "u-demo-1",
                "email": "api@tailwind-it.example",
                "roles": ["ASSETVIEWER", "VULNERABILITYVIEWER"],
                "state": "active",
                "first_name": "Demo",
                "last_name": "Reader",
            }
        ]
        return self._listing(call, rows, report=False, allowed=set())
