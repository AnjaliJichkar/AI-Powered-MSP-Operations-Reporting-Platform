"""Autotask, faked: the REST query API `AutotaskClient` sends, answered from the world.

`POST /V1.0/{entity}/query` takes the client's own filter grammar (`eq`, `gt`,
`gte`, `lt`, `in`, `contains`, `notExist`, `or` groups) and answers
`{items, pageDetails}` with 500 rows a page and an absolute `nextPageUrl`;
`GET /V1.0/{entity}/entityInformation/fields` answers the picklists the client
resolves labels from (status, priority, queue, issue type, company type,
market segment). Every enum is a per-tenant integer here, as it is live.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import Any

import httpx

from app.demo.api import Call, Router, json_response
from app.demo.dataset import Ticket, World, iso

DEMO_BASE_URL = "https://autotask.demo.invalid/ATServicesRest"
PAGE = 500

# Four of these stop nothing and two stop the SLA clock, which is a franchisee
# setting rather than a property of the number: `demo_tenant.py` names "Waiting
# Customer" and "Waiting Vendor" as pausing, by label, like a real tenant would.
STATUSES = {
    1: "New",
    5: "Complete",
    8: "In Progress",
    7: "Waiting Customer",
    9: "Waiting Vendor",
    11: "Escalated",
}
PRIORITIES = {4: "Low", 3: "Medium", 2: "High", 1: "Critical"}
QUEUES = {29682833: "Help Desk", 29682834: "Monitoring Alerts", 29682835: "Projects"}
SOURCES = {2: "Phone", 4: "Email", 6: "Client Portal", 8: "Monitoring Alert", 13: "Internal"}
COMPANY_TYPES = {1: "Customer", 2: "Lead", 3: "Prospect", 4: "Dead", 6: "Vendor", 7: "Partner"}
CONTRACT_STATUSES = {0: "Inactive", 1: "Active"}
PROJECT_STATUSES = {1: "Inactive", 2: "New", 4: "In Progress", 5: "Complete", 7: "On Hold"}
PROJECT_TYPES = {4: "Client", 5: "Internal", 2: "Baseline"}
ISSUE_TYPES = [
    "Email", "User Access and Management", "Printer/Scanner/Copier", "Workstation (Laptop/Desktop)",
    "Shared Drive", "Application/Software", "Network/Internet", "Cybersecurity", "Monitoring Alert",
]  # fmt: skip
SUB_ISSUE_TYPES = [
    "Outlook", "Password Reset", "Printer", "Performance", "Permissions", "Microsoft Teams",
    "Onboarding", "VPN", "Phishing", "Line of Business", "Hardware", "Wireless", "Offboarding",
    "Crash", "Scan to Email", "Install Request", "Automated",
]  # fmt: skip
MARKET_SEGMENTS = [
    "Legal Services", "Manufacturing", "Wholesale Distribution", "Retail", "Financial Services",
    "Software", "Hospitality", "Healthcare",
]  # fmt: skip
# Three of the franchisee's own engineers and two of ours, so the ledger has a
# real split to record. Which two are Inputiv's is a per-franchisee setting
# (`demo_tenant.py`), never a property of the id.
RESOURCES = [
    (501, "Priya", "Raman"),
    (502, "Marcus", "Bell"),
    (503, "Elena", "Kowalski"),
    (504, "Sam", "Okafor"),
    (505, "Dana", "Whitfield"),
]
INPUTIV_RESOURCE_IDS = (504, 505)


def _error(status: int, message: str) -> Any:
    return {"errors": [message]}


def _picklist(labels: dict[int, str]) -> list[dict[str, Any]]:
    return [
        {"value": str(value), "label": label, "isActive": True} for value, label in labels.items()
    ]


def _index(labels: list[str]) -> dict[int, str]:
    return {n + 1: label for n, label in enumerate(labels)}


FIELDS: dict[str, dict[str, dict[int, str]]] = {
    "Companies": {"companyType": COMPANY_TYPES, "marketSegmentID": _index(MARKET_SEGMENTS)},
    "Tickets": {
        "status": STATUSES,
        "priority": PRIORITIES,
        "queueID": QUEUES,
        "source": SOURCES,
        "issueType": _index(ISSUE_TYPES),
        "subIssueType": _index(SUB_ISSUE_TYPES),
    },
    "TicketNotes": {
        # As the pilot tenant reports them (live, 2026-10-13): the internal-only
        # value is the one labelled "All Autotask Users".
        "publish": {
            1: "All Autotask Users",
            2: "Internal Project Team",
            4: "Internal & Co-Managed",
        },
        "noteType": {
            1: "Task Summary",
            2: "Task Detail",
            3: "Task Notes",
            18: "Client Portal Note",
            99: "RMM Note",
        },
    },
    "Contracts": {
        "status": CONTRACT_STATUSES,
        "contractType": {7: "Recurring Service", 1: "Time and Materials"},
    },
    "Projects": {"status": PROJECT_STATUSES, "projectType": PROJECT_TYPES},
    "ServiceCalls": {"status": {1: "New", 2: "Complete"}},
    "ConfigurationItems": {"configurationItemType": {1: "Workstation", 2: "Server", 3: "Laptop"}},
}


def _matches(row: dict[str, Any], item: dict[str, Any]) -> bool:
    op = str(item.get("op", "")).lower()
    if op == "or":
        return any(_matches(row, sub) for sub in item.get("items", []))
    if op == "and":
        return all(_matches(row, sub) for sub in item.get("items", []))
    field = str(item.get("field", ""))
    have = row.get(field)
    want = item.get("value")
    if op == "notexist":
        return have is None
    if op == "exist":
        return have is not None
    if have is None:
        return False
    if op == "eq":
        return _same(have, want)
    if op == "in":
        return any(_same(have, w) for w in (want if isinstance(want, list) else [want]))
    if op == "contains":
        return str(want).casefold() in str(have).casefold()
    if op in ("gt", "gte", "lt", "lte"):
        left, right = _comparable(have), _comparable(want)
        if left is None or right is None:
            return False
        outcome = {
            "gt": left > right,
            "gte": left >= right,
            "lt": left < right,
            "lte": left <= right,
        }
        return bool(outcome[op])
    return False


def _same(have: Any, want: Any) -> bool:
    if isinstance(have, bool) or isinstance(want, bool):
        return (
            bool(have) == bool(want)
            if not isinstance(want, str)
            else str(have).lower() == want.lower()
        )
    try:
        return float(have) == float(want)
    except (TypeError, ValueError):
        return str(have) == str(want)


def _comparable(value: Any) -> Any:
    if isinstance(value, int | float):
        return float(value)
    text = str(value)
    if len(text) >= 10 and text[4] == "-":
        return text[:19].replace("T", " ")
    try:
        return float(text)
    except ValueError:
        return text


class AutotaskApi:
    def __init__(self, world: World) -> None:
        self.world = world
        self.router = Router("autotask", _error)
        self._pages: dict[str, list[list[dict[str, Any]]]] = {}
        self.router.add("GET", "/atservicesrest/v1.0/zoneInformation", self.zone)
        self.router.add("POST", "/ATServicesRest/V1.0/{entity}/query", self.query)
        self.router.add("GET", "/ATServicesRest/V1.0/{entity}/query", self.next_page)
        self.router.add(
            "GET", "/ATServicesRest/V1.0/{entity}/entityInformation/fields", self.fields
        )
        # The one write the fake accepts: a ticket note, kept on the world. Any
        # other write is a 404 from the router, which is what the real tenant's
        # read-only API user would turn into a 401 or 405.
        self.router.add("POST", "/ATServicesRest/V1.0/Tickets/{ticket_id}/Notes", self.add_note)

    def handle(self, request: httpx.Request) -> httpx.Response:
        return self.router.handle(request, self.world, dt.datetime.now(dt.UTC))

    def zone(self, call: Call) -> httpx.Response:
        if not call.param("user"):
            return json_response(_error(500, "user is required"), 500)
        return json_response(
            {
                "zoneName": "Demo",
                "url": DEMO_BASE_URL + "/",
                "webUrl": "https://autotask.demo.invalid/",
                "ci": 1,
            }
        )

    def fields(self, call: Call) -> httpx.Response:
        entity = call.path_params["entity"]
        picklists = FIELDS.get(entity, {})
        rows = [
            {
                "name": name,
                "dataType": "integer",
                "isPickList": True,
                "picklistValues": _picklist(values),
            }
            for name, values in picklists.items()
        ]
        rows.append({"name": "id", "dataType": "long", "isPickList": False, "picklistValues": None})
        return json_response({"fields": rows})

    def add_note(self, call: Call) -> httpx.Response:
        ticket_id = int(call.path_params["ticket_id"])
        if not any(t.id == ticket_id for t in self.world.tickets):
            return json_response(_error(404, f"no ticket {ticket_id}"), 404)
        body = call.body if isinstance(call.body, dict) else {}
        required = ("title", "description", "noteType", "publish")
        missing = [name for name in required if body.get(name) in (None, "")]
        if missing:
            return json_response(_error(500, f"missing {', '.join(missing)}"), 500)
        if body["publish"] not in FIELDS["TicketNotes"]["publish"]:
            return json_response(_error(500, "publish is not a valid picklist value"), 500)
        # The fake stands in for the portal here: a note published to anybody
        # but the internal project team is refused, so a writer that ever sent
        # a wider audience would fail the demo, not reach a client.
        if FIELDS["TicketNotes"]["publish"][body["publish"]] != "Internal Project Team":
            return json_response(
                _error(500, "demo refuses a ticket note not published to Internal Project Team"),
                500,
            )
        if "Client" in FIELDS["TicketNotes"]["noteType"].get(body["noteType"], ""):
            return json_response(_error(500, "demo refuses a client-facing note type"), 500)
        if set(body) - {"ticketID", "title", "description", "noteType", "publish"}:
            return json_response(_error(500, "unexpected fields on a ticket note"), 500)
        note_id = 7_000_000 + len(self.world.notes) + 1
        self.world.notes.append(
            {
                "id": note_id,
                "ticketID": ticket_id,
                "title": body["title"],
                "description": body["description"],
                "noteType": body["noteType"],
                "publish": body["publish"],
                "createDateTime": call.now.isoformat(),
            }
        )
        return json_response({"itemId": note_id})

    def query(self, call: Call) -> httpx.Response:
        entity = call.path_params["entity"]
        rows = self._rows(entity, call.now)
        if rows is None:
            return json_response(_error(404, f"unknown entity {entity}"), 404)
        body = call.body if isinstance(call.body, dict) else {}
        filters = body.get("filter") or []
        matched = [row for row in rows if all(_matches(row, item) for item in filters)]
        per_page = min(int(body.get("maxRecords") or PAGE), PAGE)
        pages = [matched[i : i + per_page] for i in range(0, len(matched), per_page)] or [[]]
        token = hashlib.sha1(  # noqa: S324 - a page token, not a secret
            repr((entity, filters, call.now.timestamp())).encode()
        ).hexdigest()[:12]
        self._pages[token] = pages
        return self._page(entity, token, 0, len(matched))

    def next_page(self, call: Call) -> httpx.Response:
        entity = call.path_params["entity"]
        token, page = call.param("token", ""), call.int_param("page", 0)
        if token not in self._pages:
            return json_response(_error(404, "unknown page"), 404)
        total = sum(len(p) for p in self._pages[token])
        return self._page(entity, token, page, total)

    def _page(self, entity: str, token: str, page: int, total: int) -> httpx.Response:
        pages = self._pages[token]
        items = pages[page] if page < len(pages) else []
        next_url = (
            f"{DEMO_BASE_URL}/V1.0/{entity}/query?token={token}&page={page + 1}"
            if page + 1 < len(pages)
            else None
        )
        prev_url = (
            f"{DEMO_BASE_URL}/V1.0/{entity}/query?token={token}&page={page - 1}"
            if page > 0
            else None
        )
        return json_response(
            {
                "items": items,
                "pageDetails": {
                    "count": total,
                    "requestCount": len(items),
                    "prevPageUrl": prev_url,
                    "nextPageUrl": next_url,
                },
            }
        )

    # --- the entities ----------------------------------------------------------------------------

    def _rows(self, entity: str, now: dt.datetime) -> list[dict[str, Any]] | None:
        world = self.world
        if entity == "Companies":
            rows: list[dict[str, Any]] = [
                {
                    "id": c.autotask_company_id,
                    "companyName": c.name,
                    "companyNumber": str(c.autotask_company_id),
                    "companyType": 1,
                    "classification": 1,
                    "marketSegmentID": MARKET_SEGMENTS.index(c.industry) + 1,
                    "isActive": True,
                    "phone": "512-555-0100",
                    "address1": c.locations[0].address,
                    "city": "Austin",
                    "state": "TX",
                    "postalCode": "78701",
                    "webAddress": f"https://www.{c.key}.example",
                    "ownerResourceID": 501,
                    "createDate": iso(900, now),
                    "lastActivityDate": iso(1, now),
                }
                for c in world.customers
            ]
            rows.append(
                {
                    "id": 2099,
                    "companyName": world.internal_organisation,
                    "companyNumber": "2099",
                    "companyType": 6,
                    "isActive": True,
                    "createDate": iso(1500, now),
                }
            )
            rows.append(
                {
                    "id": 2098,
                    "companyName": "Alpine Ski House",
                    "companyNumber": "2098",
                    "companyType": 3,
                    "isActive": True,
                    "createDate": iso(30, now),
                }
            )
            return rows
        if entity == "CompanyLocations":
            return [
                {
                    "id": loc.id,
                    "companyID": c.autotask_company_id,
                    "name": loc.name.split(" - ")[-1],
                    "city": loc.address.split(", ")[1],
                    "state": "TX",
                    "isActive": True,
                    "isPrimary": n == 0,
                }
                for c in world.customers
                for n, loc in enumerate(c.locations)
            ]
        if entity == "Contacts":
            rows = [
                {
                    "id": p.id,
                    "companyID": c.autotask_company_id,
                    "firstName": p.first,
                    "lastName": p.last,
                    "emailAddress": p.email,
                    "phone": "512-555-0199",
                    "isActive": 1,
                    "primaryContact": n == 0,
                    "lastActivityDate": iso(3, now),
                }
                for c in world.customers
                for n, p in enumerate(c.people)
            ]
            return rows
        if entity == "Tickets":
            return [self._ticket_row(t, now) for t in world.tickets]
        if entity == "TicketNotes":
            return [
                {
                    "id": t.id * 10 + 1,
                    "ticketID": t.id,
                    "title": "Resolution",
                    "description": t.resolution,
                    "noteType": 1,
                    "publish": 1,
                    "createDateTime": iso(t.completed_days, now),
                    "creatorResourceID": RESOURCES[t.id % 3][0],
                }
                for t in world.tickets
                if t.completed_days is not None
            ]
        if entity == "TimeEntries":
            # One to three entries a ticket, longer for the harder ones, some of
            # them ours: a cost per ticket computed from one flat entry each
            # would be a number that told nobody anything.
            entries: list[dict[str, Any]] = []
            for t in world.tickets:
                if t.completed_days is None:
                    continue
                count = 1 + (t.id % 3)
                base = {"Critical": 1.5, "High": 0.9, "Medium": 0.5, "Low": 0.25}[t.priority]
                for n in range(count):
                    resource = RESOURCES[(t.id + n) % len(RESOURCES)][0]
                    entries.append(
                        {
                            "id": t.id * 10 + 2 + n,
                            "ticketID": t.id,
                            "resourceID": resource,
                            "dateWorked": iso(t.completed_days, now),
                            "startDateTime": iso(t.completed_days, now),
                            "hoursWorked": round(base * (1 + n * 0.4), 2),
                            "summaryNotes": t.resolution[:80],
                        }
                    )
            return entries
        if entity == "Resources":
            return [
                {
                    "id": rid,
                    "firstName": first,
                    "lastName": last,
                    "email": f"{first}.{last}@tailwind-it.example".lower(),
                    "userName": f"{first[0]}{last}".lower(),
                    "title": "Support Engineer",
                    "isActive": True,
                }
                for rid, first, last in RESOURCES
            ]
        if entity == "Contracts":
            return [
                {
                    "id": 8000 + n,
                    "companyID": c.autotask_company_id,
                    "contractName": f"{c.name} Managed Services",
                    "status": 1,
                    "contractType": 7,
                    "startDate": iso(400, now),
                    "endDate": iso(-330, now),
                }
                for n, c in enumerate(world.customers)
            ]
        if entity == "Projects":
            rows = []
            for n, c in enumerate(world.customers):
                rows.append(
                    {
                        "id": 9100 + n,
                        "companyID": c.autotask_company_id,
                        "projectName": f"{c.name} Windows 11 migration",
                        "projectNumber": f"P{9100 + n}",
                        "status": 4,
                        "projectType": 4,
                        "description": "Upgrade the remaining Windows 10 devices.",
                        "completedPercentage": 25 + (n * 15) % 60,
                        "startDateTime": iso(60 - n, now),
                        "endDateTime": iso(-40 + n * 3, now),
                        "completedDateTime": None,
                        "estimatedTime": 40.0,
                        "actualHours": 16.0,
                        "projectLeadResourceID": 501,
                    }
                )
                if n % 2 == 0:
                    rows.append(
                        {
                            "id": 9200 + n,
                            "companyID": c.autotask_company_id,
                            "projectName": f"{c.name} firewall replacement",
                            "projectNumber": f"P{9200 + n}",
                            "status": 5,
                            "projectType": 4,
                            "description": "Replace the edge firewall and migrate the rules.",
                            "completedPercentage": 100,
                            "startDateTime": iso(70, now),
                            "endDateTime": iso(20, now),
                            "completedDateTime": iso(12 + n, now),
                            "estimatedTime": 24.0,
                            "actualHours": 22.5,
                            "projectLeadResourceID": 502,
                        }
                    )
                if n % 3 == 1:
                    rows.append(
                        {
                            "id": 9300 + n,
                            "companyID": c.autotask_company_id,
                            "projectName": f"{c.name} Wi-Fi refresh",
                            "projectNumber": f"P{9300 + n}",
                            "status": 2,
                            "projectType": 4,
                            "description": "Replace the access points across the site.",
                            "completedPercentage": 0,
                            "startDateTime": iso(-25, now),
                            "endDateTime": iso(-60, now),
                            "completedDateTime": None,
                            "estimatedTime": 30.0,
                            "actualHours": 0.0,
                            "projectLeadResourceID": 503,
                        }
                    )
            return rows
        if entity == "Phases":
            return [
                {
                    "id": 9500 + n * 2 + k,
                    "projectID": 9100 + n,
                    "title": ["Pilot group", "Remaining devices"][k],
                    "phaseNumber": str(k + 1),
                    "startDate": iso(60 - k * 30, now),
                    "dueDate": iso(10 - k * 40, now),
                }
                for n, _ in enumerate(world.customers)
                for k in range(2)
            ]
        if entity == "ConfigurationItems":
            return [
                {
                    "id": 40_000 + d.id,
                    "companyID": world.customer(d.customer_key).autotask_company_id,
                    "referenceTitle": d.system_name,
                    "referenceNumber": f"CI-{d.id}",
                    "serialNumber": d.serial,
                    "rmmDeviceAuditHostname": d.hostname,
                    "rmmDeviceAuditLastUser": (d.last_user.login if d.last_user else None),
                    "rmmDeviceAuditOperatingSystem": d.os_name,
                    "configurationItemType": 2
                    if d.is_server
                    else 3
                    if "-LT-" in d.system_name
                    else 1,
                    "installDate": iso(d.created_days, now),
                    "warrantyExpirationDate": iso(d.created_days - 1095, now),
                    "isActive": True,
                }
                for d in world.devices
            ]
        if entity in ("ServiceCalls", "ServiceCallTickets", "ServiceCallTicketResources"):
            return []
        return None

    #: The demo franchisee's working day, in its own offset (`demo_tenant.py`
    #: sets `UTC-06:00`). Tickets are moved into it so the SLA clocks measure
    #: something: a world whose tickets arrive evenly around the clock puts the
    #: median time to pickup at zero, because no business time passed.
    OFFICE = (dt.timezone(-dt.timedelta(hours=6)), 8, 17)

    def _working_shift(self, moment: dt.datetime) -> dt.timedelta:
        """How far to move this instant to land inside the working day.

        One offset per ticket, applied to every timestamp on it, so the gaps
        between created, first response and completed are exactly as generated.
        """
        zone, opens, closes = self.OFFICE
        local = moment.astimezone(zone)
        if opens <= local.hour < closes:
            return dt.timedelta(0)
        wanted = opens + (local.hour % (closes - opens))
        return dt.timedelta(hours=wanted - local.hour)

    def _ticket_row(self, t: Ticket, now: dt.datetime) -> dict[str, Any]:
        customer = self.world.customer(t.customer_key)
        status = {v: k for k, v in STATUSES.items()}[t.status]
        priority = {v: k for k, v in PRIORITIES.items()}[t.priority]
        issue = ISSUE_TYPES.index(t.issue_type) + 1 if t.issue_type in ISSUE_TYPES else None
        sub = (
            SUB_ISSUE_TYPES.index(t.sub_issue_type) + 1
            if t.sub_issue_type in SUB_ISSUE_TYPES
            else None
        )
        automated = t.contact is None
        # One shift for the whole ticket, from its own creation time.
        shift = self._working_shift(now - dt.timedelta(days=t.created_days))

        def when(days: float | None) -> str | None:
            """A timestamp on this ticket, moved into the working day with it."""
            if days is None:
                return None
            return (now - dt.timedelta(days=days) + shift).strftime("%Y-%m-%dT%H:%M:%SZ")

        return {
            "id": t.id,
            "ticketNumber": t.number,
            "title": t.title,
            "description": t.description,
            "status": status,
            "priority": priority,
            "companyID": customer.autotask_company_id,
            "configurationItemID": 40_000 + t.device.id if t.device else None,
            "contactID": t.contact.id if t.contact else None,
            "assignedResourceID": RESOURCES[t.id % len(RESOURCES)][0],
            "queueID": 29682834 if automated else 29682833,
            "issueType": issue,
            "subIssueType": sub,
            "source": 8 if automated else (4 if t.id % 2 else 2),
            "createDate": when(t.created_days),
            "lastActivityDate": when(
                t.completed_days if t.completed_days is not None else max(t.created_days - 0.5, 0)
            ),
            "dueDateTime": when(t.resolution_due_days),
            "completedDate": when(t.completed_days),
            "resolution": t.resolution or None,
            "firstResponseDueDateTime": when(t.first_response_due_days),
            "firstResponseDateTime": when(t.first_response_days),
            "resolvedDueDateTime": when(t.resolution_due_days),
            "resolvedDateTime": when(t.completed_days),
            "serviceLevelAgreementID": 3,
        }
