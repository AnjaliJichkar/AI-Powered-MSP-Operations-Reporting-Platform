"""Which machine is this ticket about? Hostnames in the text, then the contact.

Two methods, each with a confidence the page shows, and neither guesses:

- **hostname**: every hostname-looking token in the title and description is
  looked up through the identity layer (`find_devices`: the canonical name or
  the NetBIOS key, within the ticket's site; then the names the other vendors
  use, through the links). An exact canonical name is `high`; a NetBIOS or
  vendor-name match is `medium`.
- **contact**: the ticket's contact against NinjaOne's last logged-on user
  per device at the site (`list_logged_on_users`), on the usual login shapes
  (`first.last`, `flast`, `firstlast`, the full name). One machine is
  `medium`; several are each `low`.

The device is chosen for the engineer only when one candidate stands above
the rest; otherwise the candidates are listed and the engineer chooses, which
is recorded as method `engineer` at `high` and audited. Nothing here calls a
vendor except the one logged-on-users read, and only when there is a contact.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.matching import normalise_hostname
from app.devices.identity import find_devices
from app.models.device import Device, DeviceVendorLink

CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}

#: A hostname as people type it: letters, digits and hyphens, optionally
#: dotted, at least four characters, holding a digit or a hyphen so a plain
#: word does not qualify. Starts with a letter, so error codes (0x8007…) and
#: numbers do not. False positives cost nothing: a token is only ever looked
#: up against the site's devices.
_HOSTNAME = re.compile(r"(?<![\w.-])([A-Za-z][A-Za-z0-9-]{2,}(?:\.[A-Za-z0-9-]+)*)(?![\w-])")
_NOISE = frozenset(
    {
        "wi-fi",
        "e-mail",
        "covid-19",
        "windows-10",
        "windows-11",
        "office-365",
        "microsoft-365",
        "log-in",
        "sign-in",
        "set-up",
        "back-up",
        "follow-up",
        "on-site",
        "off-site",
        "re-image",
        "x-ray",
        "co-worker",
        "one-drive",
    }
)


def hostnames_in(*texts: str | None) -> list[str]:
    """Hostname-looking tokens, in order of first appearance, without repeats."""
    found: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for match in _HOSTNAME.finditer(text or ""):
            token = match.group(1).rstrip(".-")
            key = token.lower()
            if key in seen or key in _NOISE or len(token) < 4:
                continue
            if not any(ch.isdigit() for ch in token) and "-" not in token:
                continue
            seen.add(key)
            found.append(token)
    return found


@dataclass(slots=True)
class Candidate:
    device_id: str
    name: str
    site_id: str
    method: str
    confidence: str
    why: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Resolution:
    device_id: uuid.UUID | None = None
    method: str = "none"
    confidence: str | None = None
    candidates: list[Candidate] = field(default_factory=list)
    hostnames: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "device_id": str(self.device_id) if self.device_id else None,
            "method": self.method,
            "confidence": self.confidence,
            "candidates": [c.as_dict() for c in self.candidates],
            "hostnames": self.hostnames,
        }


def _better(existing: Candidate | None, new: Candidate) -> bool:
    return (
        existing is None or CONFIDENCE_RANK[new.confidence] > CONFIDENCE_RANK[existing.confidence]
    )


async def by_hostname(
    session: AsyncSession,
    client_id: uuid.UUID,
    *,
    site_id: uuid.UUID | None,
    texts: list[str | None],
) -> tuple[list[Candidate], list[str]]:
    """Candidates from the hostnames in the ticket's text, best confidence per device."""
    tokens = hostnames_in(*texts)
    best: dict[str, Candidate] = {}
    for token in tokens:
        key = normalise_hostname(token)
        for device in await find_devices(session, client_id, token, site_id=site_id):
            exact = normalise_hostname(device.canonical_name) == key
            candidate = Candidate(
                device_id=str(device.id),
                name=device.canonical_name,
                site_id=str(device.site_id),
                method="hostname",
                confidence="high" if exact else "medium",
                why=(
                    f"'{token}' in the ticket is this machine's name"
                    if exact
                    else f"'{token}' in the ticket shares this machine's NetBIOS name"
                ),
            )
            if _better(best.get(candidate.device_id), candidate):
                best[candidate.device_id] = candidate
        # The name another vendor uses for the machine, through its link.
        if key:
            query = (
                select(DeviceVendorLink, Device)
                .join(Device, Device.id == DeviceVendorLink.device_id)
                .where(
                    DeviceVendorLink.client_id == client_id,
                    DeviceVendorLink.active.is_(True),
                    DeviceVendorLink.vendor_name.is_not(None),
                    Device.status != "retired",
                )
            )
            if site_id is not None:
                query = query.where(Device.site_id == site_id)
            for link, device in (await session.execute(query)).all():
                if normalise_hostname(link.vendor_name) != key:
                    continue
                candidate = Candidate(
                    device_id=str(device.id),
                    name=device.canonical_name,
                    site_id=str(device.site_id),
                    method="hostname",
                    confidence="medium",
                    why=f"'{token}' is what {link.connector_name.value} calls this machine",
                )
                if _better(best.get(candidate.device_id), candidate):
                    best[candidate.device_id] = candidate
    return list(best.values()), tokens


def login_shapes(contact: str) -> set[str]:
    """The logins a person's name is usually turned into, lower case."""
    parts = [p for p in re.split(r"[\s,]+", contact.strip().lower()) if p]
    parts = [re.sub(r"[^a-z0-9]", "", p) for p in parts]
    parts = [p for p in parts if p]
    if not parts:
        return set()
    if len(parts) == 1:
        return {parts[0]} if len(parts[0]) >= 4 else set()
    first, last = parts[0], parts[-1]
    shapes = {
        f"{first} {last}",
        f"{first}.{last}",
        f"{first[0]}{last}",
        f"{first}{last}",
        f"{first}_{last}",
        f"{last}{first[0]}",
        f"{last}.{first}",
        f"{first}-{last}",
    }
    return {shape for shape in shapes if len(shape) >= 4}


def login_key(user_name: str | None) -> str:
    """A NinjaOne last-logged-on value without its domain or mail suffix, lower case."""
    if not user_name:
        return ""
    value = user_name.strip().lower()
    if "\\" in value:
        value = value.rsplit("\\", 1)[1]
    if "@" in value:
        value = value.split("@", 1)[0]
    return value


def by_contact(
    contact: str | None,
    logged_on: list[tuple[str, str | None]],
    devices_by_name: dict[str, Device],
) -> list[Candidate]:
    """Candidates from the contact's name against the last logged-on user per device.

    `logged_on` is (device name, user) rows from NinjaOne for the site;
    `devices_by_name` maps normalised device names to identity devices.
    """
    if not contact:
        return []
    shapes = login_shapes(contact)
    if not shapes:
        return []
    matched: list[tuple[str, str]] = []
    for device_name, user in logged_on:
        key = login_key(user)
        if key and (key in shapes or key.replace(".", " ") in shapes):
            matched.append((device_name, user or ""))
    confidence = "medium" if len(matched) == 1 else "low"
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for device_name, user in matched:
        device = devices_by_name.get(normalise_hostname(device_name))
        if device is None or str(device.id) in seen:
            continue
        seen.add(str(device.id))
        candidates.append(
            Candidate(
                device_id=str(device.id),
                name=device.canonical_name,
                site_id=str(device.site_id),
                method="contact",
                confidence=confidence,
                why=f"the contact was last logged on as {user}"
                + ("" if confidence == "medium" else f" (one of {len(matched)} machines)"),
            )
        )
    return candidates


def decide(candidates: list[Candidate], hostnames: list[str]) -> Resolution:
    """One device when one candidate stands above the rest; otherwise nobody's guess."""
    ordered = sorted(candidates, key=lambda c: (-CONFIDENCE_RANK[c.confidence], c.name.lower()))
    resolution = Resolution(candidates=ordered, hostnames=hostnames)
    if not ordered:
        return resolution
    top = ordered[0]
    rivals = [c for c in ordered[1:] if c.confidence == top.confidence]
    if rivals or top.confidence == "low":
        return resolution
    resolution.device_id = uuid.UUID(top.device_id)
    resolution.method = top.method
    resolution.confidence = top.confidence
    return resolution
