"""The board as JSON, and back: what a snapshot stores and the page reads.

`report_to_json` writes a `CoverageReport` down completely — every device
with its per-tool verdict, every unpaired machine, the tools' reports, the
organisations — so the Coverage page can be served from the latest snapshot
without a vendor call. `report_from_json` rebuilds the report and lays the
client's *live* exclusions over it: the board is computed and stored without
exclusions, and a device excluded (or un-excluded) since the snapshot is
shown as it is now, not as it was, because an exclusion is a decision in our
database, not a fact at the vendor.

Everything here is plain data in and out; nothing reads a vendor or the
database.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable
from typing import Any

from app.coverage.compute import (
    CoverageReport,
    OrganisationCount,
    ToolReport,
    summarise_sites,
)
from app.coverage.core import (
    Assessment,
    DeviceCoverage,
    Exclusion,
    ManagedDevice,
    ToolState,
    Unmanaged,
)

VERSION = 1


def _iso(when: dt.datetime | None) -> str | None:
    return when.isoformat() if when else None


def _when(raw: Any) -> dt.datetime | None:
    if not raw:
        return None
    return dt.datetime.fromisoformat(str(raw))


def _site(raw: Any) -> uuid.UUID | str:
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return str(raw)


def _unmanaged_json(entry: Unmanaged) -> dict[str, Any]:
    return {
        "site_id": str(entry.site_id),
        "tool": entry.tool,
        "name": entry.name,
        "detail": entry.detail,
        "last_seen": _iso(entry.last_seen),
        "external_ids": list(entry.external_ids),
        "note": entry.note,
    }


def _unmanaged(raw: dict[str, Any]) -> Unmanaged:
    return Unmanaged(
        site_id=_site(raw["site_id"]),
        tool=raw["tool"],
        name=raw["name"],
        detail=raw["detail"],
        last_seen=_when(raw.get("last_seen")),
        external_ids=tuple(raw.get("external_ids", ())),
        note=raw.get("note"),
    )


def report_to_json(report: CoverageReport) -> dict[str, Any]:
    """Every part of the board a page or a CSV reads, as JSON-safe data."""
    return {
        "version": VERSION,
        "client_id": str(report.client_id),
        "computed_at": report.computed_at.isoformat(),
        "duration_ms": report.duration_ms,
        "not_computers": report.not_computers,
        "population_truncated": report.population_truncated,
        "rmm_stale_days": report.rmm_stale_days,
        "reads": dict(report.reads),
        # Which sites the backup scope was guessed at rather than decided.
        # The board is read back from this row, so a flag the computation
        # raised and the snapshot dropped would be a flag nobody ever sees.
        "backup_scope_mixed": [str(s.site_id) for s in report.sites if s.backup_scope_mixed],
        "tools": [
            {
                "key": t.key,
                "label": t.label,
                "assessed": t.assessed,
                "reason": t.reason,
                "truncated": t.truncated,
                "endpoints": t.endpoints,
            }
            for t in report.tools
        ],
        # Every site the board knows: the names the computation kept, or failing
        # those the summaries' (a report built by hand carries only the latter).
        "site_names": {
            str(site_id): [slug, name]
            for site_id, (slug, name) in (
                report.site_names or {s.site_id: (s.slug, s.name) for s in report.sites}
            ).items()
        },
        "devices": [
            {
                "site_id": str(d.device.site_id),
                "site_slug": d.device.site_slug,
                "site_name": d.device.site_name,
                "device_id": d.device.device_id,
                "name": d.device.name,
                "node_class": d.device.node_class,
                "last_contact": _iso(d.device.last_contact),
                "macs": sorted(d.device.macs),
                "serials": sorted(d.device.serials),
                "ninjaone_state": d.ninjaone_state,
                "ninjaone_detail": d.ninjaone_detail,
                "tools": {
                    key: {
                        "state": s.state,
                        "detail": s.detail,
                        "last_seen": _iso(s.last_seen),
                        "matched_by": s.matched_by,
                        "candidates": list(s.candidates),
                        "endpoint_ids": list(s.endpoint_ids),
                        "found_by": s.found_by,
                    }
                    for key, s in d.tools.items()
                },
            }
            for d in report.devices
        ],
        "unmanaged": [_unmanaged_json(u) for u in report.unmanaged],
        "discovered": [_unmanaged_json(u) for u in report.discovered],
        "not_assessable": [_unmanaged_json(u) for u in report.not_assessable],
        "external": [_unmanaged_json(u) for u in report.external],
        "organisations": [
            {
                "tool": o.tool,
                "site_id": str(o.site_id),
                "external_id": o.external_id,
                "endpoints": o.endpoints,
                "with_agent": o.with_agent,
            }
            for o in report.organisations
        ],
    }


def report_from_json(
    payload: dict[str, Any], *, exclusions: Iterable[Exclusion] = ()
) -> CoverageReport:
    """The board back from `report_to_json`, with `exclusions` laid over it.

    A device excluded from a tool now is `excluded` with the reason whatever
    the stored verdict was; one whose exclusion was withdrawn shows the stored
    verdict. Per-site and per-tool counts are recomputed from the result, so
    the page's numbers follow the decisions without a vendor call.
    """
    excluded = {(e.device_id, e.tool): e for e in exclusions}
    tools = [
        ToolReport(
            t["key"],
            t["label"],
            assessed=bool(t["assessed"]),
            reason=t.get("reason", ""),
            truncated=bool(t.get("truncated", False)),
            endpoints=int(t.get("endpoints", 0)),
        )
        for t in payload["tools"]
    ]
    devices: list[DeviceCoverage] = []
    for raw in payload["devices"]:
        device = ManagedDevice(
            site_id=_site(raw["site_id"]),
            site_slug=raw["site_slug"],
            site_name=raw["site_name"],
            device_id=raw["device_id"],
            name=raw["name"],
            node_class=raw.get("node_class"),
            last_contact=_when(raw.get("last_contact")),
            macs=frozenset(raw.get("macs", ())),
            serials=frozenset(raw.get("serials", ())),
        )
        states: dict[str, ToolState] = {}
        for key, s in raw["tools"].items():
            exclusion = excluded.get((device.device_id, key))
            if exclusion is not None:
                states[key] = ToolState("excluded", exclusion.reason)
                continue
            states[key] = ToolState(
                s["state"],
                s["detail"],
                _when(s.get("last_seen")),
                matched_by=s.get("matched_by"),
                candidates=tuple(s.get("candidates", ())),
                endpoint_ids=tuple(s.get("endpoint_ids", ())),
                found_by=s.get("found_by"),
            )
        devices.append(
            DeviceCoverage(
                device=device,
                tools=states,
                ninjaone_state=raw["ninjaone_state"],
                ninjaone_detail=raw.get("ninjaone_detail", ""),
            )
        )
    assessment = Assessment(
        devices=devices,
        unmanaged=[_unmanaged(u) for u in payload.get("unmanaged", [])],
        discovered=[_unmanaged(u) for u in payload.get("discovered", [])],
        not_assessable=[_unmanaged(u) for u in payload.get("not_assessable", [])],
        external=[_unmanaged(u) for u in payload.get("external", [])],
    )
    site_names = {
        uuid.UUID(site_id): (slug, name)
        for site_id, (slug, name) in payload.get("site_names", {}).items()
    }
    return CoverageReport(
        client_id=uuid.UUID(payload["client_id"]),
        computed_at=dt.datetime.fromisoformat(payload["computed_at"]),
        duration_ms=int(payload.get("duration_ms", 0)),
        tools=tools,
        devices=assessment.devices,
        unmanaged=assessment.unmanaged,
        sites=summarise_sites(
            assessment,
            site_names=site_names,
            tools=tools,
            mixed_scopes={
                uuid.UUID(site_id): True for site_id in payload.get("backup_scope_mixed", [])
            },
        ),
        exclusions=list(exclusions),
        not_computers=int(payload.get("not_computers", 0)),
        population_truncated=bool(payload.get("population_truncated", False)),
        rmm_stale_days=int(payload.get("rmm_stale_days", 14)),
        discovered=assessment.discovered,
        not_assessable=assessment.not_assessable,
        external=assessment.external,
        organisations=[
            OrganisationCount(
                o["tool"],
                uuid.UUID(o["site_id"]),
                o["external_id"],
                int(o["endpoints"]),
                int(o["with_agent"]),
            )
            for o in payload.get("organisations", [])
        ],
        site_names=site_names,
        reads=dict(payload.get("reads", {})),
    )
