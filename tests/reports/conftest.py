"""A synthetic review: facts written by hand, then the real findings, sections and renderer.

No vendor, no model, no database. The model client is a script of canned
responses shaped like the SDK's (`content` blocks with `type`, `name`, `input`),
so the narrative layer's validation is exercised against what it really reads.
"""

from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.reports.tbr.audit import NullAudit
from app.reports.tbr.config import FranchiseeConfig, PriceBookEntry
from app.reports.tbr.findings import derive_findings
from app.reports.tbr.model import (
    ChartData,
    ChartSeries,
    Fact,
    FactSet,
    FactTable,
    ReviewDocument,
    ReviewPeriod,
)
from app.reports.tbr.sections import Context, build_sections
from app.settings import Settings, get_settings

NOW = dt.datetime(2026, 9, 21, 9, 0, tzinfo=dt.UTC)
PERIOD = ReviewPeriod.ending(2026, 8)


def fact(fact_id: str, value: Any, *, source: str = "ninjaone", display: str | None = None) -> Fact:
    return Fact(
        id=fact_id,
        label=fact_id.rsplit(".", 1)[-1].replace("_", " "),
        value=value,
        display=display if display is not None else str(value),
        definition=f"Test definition of {fact_id}.",
        source=source,  # type: ignore[arg-type]
        as_of=NOW,
        period_bound=fact_id.startswith(("autotask.tickets", "ninjaone.patches")),
    )


def table(table_id: str, columns: list[str], rows: list[list[str]]) -> FactTable:
    return FactTable(
        id=table_id,
        label=table_id,
        definition="Test table.",
        source="ninjaone",
        columns=columns,
        rows=rows,
        total=len(rows),
        as_of=NOW,
    )


def make_facts(**overrides: Any) -> FactSet:
    values: dict[str, tuple[Any, str, str | None]] = {
        "report.period.label": ("August 2026", "config", None),
        "report.period.months": (1, "config", "month"),
        "ninjaone.devices.total": (48, "ninjaone", None),
        "ninjaone.devices.workstations": (44, "ninjaone", None),
        "ninjaone.devices.servers_physical": (1, "ninjaone", None),
        "ninjaone.devices.servers_virtual": (3, "ninjaone", None),
        "ninjaone.locations": (2, "ninjaone", None),
        "autotask.locations": (2, "autotask", None),
        "site.locations": (2, "ninjaone", None),
        "autotask.company.industry": ("Legal Services", "autotask", None),
        "ninjaone.patches.installed": (310, "ninjaone", None),
        "ninjaone.patches.failed": (14, "ninjaone", None),
        "ninjaone.patches.failed_devices": (6, "ninjaone", None),
        "ninjaone.patches.success_percent": (95.7, "ninjaone", "95.7%"),
        "ninjaone.patches.critical_pending": (9, "ninjaone", None),
        "ninjaone.patches.critical_pending_devices": (4, "ninjaone", None),
        "ninjaone.antivirus.devices_reporting": (47, "ninjaone", None),
        "ninjaone.antivirus.devices_unprotected": (2, "ninjaone", None),
        "ninjaone.windows11.ineligible": (2, "ninjaone", None),
        "ninjaone.windows11.eligible": (3, "ninjaone", None),
        "ninjaone.windows11.unknown": (0, "ninjaone", None),
        "ninjaone.hardware.low_memory": (0, "ninjaone", None),
        "ninjaone.disks.low": (1, "ninjaone", None),
        "autotask.tickets.created": (120, "autotask", None),
        "autotask.tickets.completed": (115, "autotask", None),
        "autotask.tickets.open": (9, "autotask", None),
        "autotask.tickets.from_people": (100, "autotask", None),
        "autotask.tickets.automated": (20, "autotask", None),
        "autotask.tickets.password": (18, "autotask", None),
        "autotask.sla.resolution_met_percent": (82.0, "autotask", "82%"),
        "autotask.sla.resolution_measured": (115, "autotask", None),
        "autotask.projects.current": (1, "autotask", None),
        "autotask.projects.overdue": (0, "autotask", None),
        "autotask.projects.scheduled": (1, "autotask", None),
        "autotask.projects.completed": (1, "autotask", None),
    }
    for key, value in overrides.items():
        fact_id = key.replace("__", ".")
        source = values.get(fact_id, (None, "ninjaone", None))[1]
        values[fact_id] = (value, source, None)
    facts = FactSet()
    for fact_id, (value, source, display) in values.items():
        facts.add(fact(fact_id, value, source=source, display=display))
    facts.tables["ninjaone.windows11.ineligible.devices"] = table(
        "ninjaone.windows11.ineligible.devices",
        ["Device", "Model", "Processor", "Why"],
        [
            ["ACME-WS01", "Dell OptiPlex 7040", "Intel Core i5-6500", "processor is older"],
            ["ACME-WS02", "Dell OptiPlex 7040", "Intel Core i5-6500", "processor is older"],
        ],
    )
    facts.tables["ninjaone.windows11.eligible.devices"] = table(
        "ninjaone.windows11.eligible.devices",
        ["Device", "Model", "Processor", "Why"],
        [[f"ACME-WS1{n}", "Dell Latitude 5400", "Intel Core i5-8365U", "meets"] for n in range(3)],
    )
    facts.tables["ninjaone.disks.low.volumes"] = table(
        "ninjaone.disks.low.volumes", ["Device", "Volume", "Free"], [["ACME-DC01", "C:", "6% free"]]
    )
    facts.tables["autotask.projects.current.list"] = table(
        "autotask.projects.current.list",
        ["Project", "Phase", "Complete", "Due", "Description", "Status", "Late"],
        [["Office move", "Cabling", "40%", "October 2026", "", "In Progress", ""]],
    )
    facts.tables["autotask.projects.scheduled.list"] = table(
        "autotask.projects.scheduled.list",
        ["Project", "Starts"],
        [["Wi-Fi refresh", "November 2026"]],
    )
    facts.tables["autotask.projects.completed.list"] = table(
        "autotask.projects.completed.list", ["Project", "Completed"], [["UPS swap", "18 Aug 2026"]]
    )
    for chart_id, kind, categories in (
        ("autotask.chart.daily", "line", [f"{d} Aug" for d in range(1, 32)]),
        ("autotask.chart.open_priority", "column", ["High", "Medium", "Low"]),
        ("autotask.chart.issue_type", "bar", ["Email", "Printing", "Access"]),
        ("autotask.chart.sla", "column", ["Resolution SLA met"]),
        ("autotask.chart.top_contacts", "bar", ["A. Person", "Automation and system accounts"]),
    ):
        series = [
            ChartSeries(name="Tickets", values=[float(i % 7) for i in range(len(categories))])
        ]
        if kind == "line":
            series.append(ChartSeries(name="Completed", values=[1.0] * len(categories)))
        facts.charts[chart_id] = ChartData(
            id=chart_id,
            title=chart_id.rsplit(".", 1)[-1].replace("_", " ").title(),
            kind=kind,  # type: ignore[arg-type]
            categories=categories,
            series=series,
            definition="Test chart.",
            source="autotask",
        )
    # Every name the collectors saw, on a slide or not: what a demo deck must replace.
    facts.names = {
        "devices": ["ACME-WS01", "ACME-WS02", "ACME-DC01", "RECEPTION"]
        + [f"ACME-WS1{n}" for n in range(3)],
        "contacts": ["A. Person"],
    }
    return facts


def make_config(**fields: Any) -> FranchiseeConfig:
    return FranchiseeConfig(
        display_name="Example IT Partners",
        price_book={
            "device_replacement": PriceBookEntry(hours=3, cost=1450, per="device"),
            "patch_remediation": PriceBookEntry(label="Patch remediation", cost=900),
        },
        **fields,
    )


async def make_document(
    *,
    facts: FactSet | None = None,
    prior: FactSet | None = None,
    config: FranchiseeConfig | None = None,
    model_client: Any = None,
) -> ReviewDocument:
    facts = facts or make_facts()
    config = config or make_config()
    findings = derive_findings(
        facts, prior=prior, prior_label="July 2026" if prior else None, config=config, period=PERIOD
    )
    sections = await build_sections(
        Context(
            facts=facts,
            findings=findings,
            config=config,
            period=PERIOD,
            site_name="Acme Legal",
            client_name="Example IT Partners",
            has_prior=prior is not None,
            prior=prior,
            prior_label="July 2026" if prior else None,
            model_client=model_client,
            settings=Settings(),
            audit=NullAudit(),
        )
    )
    return ReviewDocument(
        client_id=str(uuid.uuid4()),
        client_slug="example-it",
        client_name="Example IT Partners",
        site_id=str(uuid.uuid4()),
        site_slug="acme-legal",
        site_name="Acme Legal",
        period=PERIOD,
        generated_at=NOW,
        generated_by="tester",
        franchisee=config.model_dump(mode="json"),
        facts=facts,
        prior_facts=prior,
        prior_period_label="July 2026" if prior else None,
        findings=findings,
        sections=sections,
    )


# --- a scripted model -----------------------------------------------------------------------


def tool_use(slots: list[dict[str, Any]]) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", name="write_section", input={"slots": slots})],
        usage=SimpleNamespace(input_tokens=10, output_tokens=10),
        stop_reason="tool_use",
    )


class ScriptedModel:
    """`messages.create` returns the next canned response and records what it was sent."""

    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.messages = self

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if not self._responses:
            raise AssertionError("the narrative layer made more model calls than were scripted")
        response = self._responses.pop(0)
        return response(kwargs) if callable(response) else response


@pytest.fixture(autouse=True)
def _wide_rmm_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """The recorded NinjaOne devices last checked in weeks before the tests' fixed
    clock. These tests are about the join, not the threshold, so the RMM-stale rule
    is set wide; a device with no contact at all (TL-AUS-MAC03) is still rmm_stale,
    whatever the threshold, and the join tests say so."""
    monkeypatch.setenv("COVERAGE_RMM_STALE_DAYS", "365")
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _last_seen_links_in_memory(last_seen_in_memory: dict[str, Any]) -> None:
    """The review's last-seen read keeps its dates on the device links, which need
    the database; here the store is a dict (tests/conftest.py)."""


@pytest.fixture
def audit() -> NullAudit:
    return NullAudit()
