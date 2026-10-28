"""Facts to findings to sentences: the rules that keep a review honest."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.reports.tbr import review
from app.reports.tbr.audit import NullAudit
from app.reports.tbr.config import QUOTE_PENDING, FranchiseeConfig, PriceBookEntry
from app.reports.tbr.findings import derive_findings, overall_opinion, quarter_for
from app.reports.tbr.model import FactSet, ReviewPeriod
from app.reports.tbr.narrative import (
    Brief,
    Slot,
    problems_with,
    research_trends,
    substitute,
    write_section,
)
from app.settings import Settings
from tests.reports.conftest import (
    PERIOD,
    ScriptedModel,
    make_config,
    make_document,
    make_facts,
    tool_use,
)

# --- the period ---------------------------------------------------------------------------------


def test_a_period_is_whole_months_ending_where_asked() -> None:
    august = ReviewPeriod.ending(2026, 8)
    assert (august.start.isoformat(), august.end.isoformat()) == (
        "2026-08-01T00:00:00+00:00",
        "2026-09-01T00:00:00+00:00",
    )
    assert (august.label, august.key) == ("August 2026", "2026-08/1")
    assert august.previous().label == "July 2026"

    half = ReviewPeriod.ending(2026, 2, months=6)
    assert half.start.isoformat().startswith("2025-09-01")
    assert half.label == "September 2025 to February 2026"
    assert ReviewPeriod.ending(2026, 12).end.isoformat().startswith("2027-01-01")
    with pytest.raises(ValueError, match="month must be"):
        ReviewPeriod.ending(2026, 13)


def test_the_quarter_follows_severity() -> None:
    assert quarter_for("critical", PERIOD) == "Q4 2026"  # the quarter after August ends
    assert quarter_for("high", PERIOD) == "Q4 2026"
    assert quarter_for("medium", PERIOD) == "Q1 2027"
    assert quarter_for("low", PERIOD) == "Q2 2027"


# --- prices come from the price book or not at all ------------------------------------------------


def test_a_price_is_the_price_books_or_quote_pending() -> None:
    config = FranchiseeConfig(
        display_name="Example IT",
        price_book={
            "device_replacement": PriceBookEntry(hours=Decimal("2.5"), cost=1450, per="device"),
            "assessment": PriceBookEntry(cost=Decimal("2500.50")),
            "hours_only": PriceBookEntry(hours=1),
        },
    )
    assert config.quote("device_replacement", quantity=4) == ("10 hours", "$5,800")
    assert config.quote("assessment", quantity=9) == (QUOTE_PENDING, "$2,500.50")  # flat fee
    assert config.quote("hours_only") == ("1 hour", QUOTE_PENDING)
    assert config.quote("never_priced") == (QUOTE_PENDING, QUOTE_PENDING)
    assert config.quote(None) == (QUOTE_PENDING, QUOTE_PENDING)
    assert FranchiseeConfig.default_for("Example IT").price_book == {}


# --- findings -------------------------------------------------------------------------------------


def findings_for(facts: FactSet, prior: FactSet | None = None) -> dict[str, Any]:
    found = derive_findings(
        facts,
        prior=prior,
        prior_label="July 2026" if prior else None,
        config=make_config(),
        period=PERIOD,
    )
    return {finding.id: finding for finding in found}


def test_a_measurement_is_asserted_and_a_single_tool_gap_is_held() -> None:
    found = findings_for(make_facts())

    assert found["patch.critical_pending"].assertable  # one tool counted it: a measurement
    gap = found["antivirus.unprotected"]
    assert gap.kind == "gap" and gap.sources == ["ninjaone"]
    assert gap.needs_confirmation and not gap.assertable


def test_no_earlier_review_means_no_improvements_and_no_regressions() -> None:
    assert not [f for f in findings_for(make_facts()) if f.startswith("change.")]


def test_changes_are_measured_against_the_stored_snapshot() -> None:
    prior = make_facts(ninjaone__patches__failed=40, autotask__sla__resolution_met_percent=95.0)
    found = findings_for(make_facts(), prior)

    better = found["change.ninjaone.patches.failed"]
    assert better.kind == "improvement" and better.section == "accomplishments"
    worse = found["change.autotask.sla.resolution_met_percent"]
    assert worse.kind == "regression" and worse.section == "dashboard"
    # A change under ten per cent is noise and is not reported either way.
    steady = make_facts(autotask__tickets__open=9)
    assert "change.autotask.tickets.open" not in findings_for(steady, make_facts())


def test_the_delta_is_a_fact_of_its_own_so_the_sentence_cites_one() -> None:
    facts = make_facts()
    findings_for(facts, make_facts(ninjaone__patches__failed=40))
    delta = facts.facts["delta.ninjaone.patches.failed"]
    assert delta.display == "from 40 to 14" and delta.source == "derived"


def test_a_finding_backs_the_service_that_addresses_it_or_none() -> None:
    """Every service a finding names is in the catalogue and is for that finding.

    A full disk was pointed at Hardware Lifecycle Planning because it was the
    nearest name; it is a job with a price-book line, and the catalogue sells
    no storage or maintenance service, so it backs nothing rather than the
    nearest thing. Lifecycle is for age or end of support, which today only
    the Windows 10 devices that cannot run Windows 11 show.
    """
    found = findings_for(make_facts(ninjaone__hardware__low_memory=2))
    catalogue = {item.key for item in make_config().service_catalog}
    for finding in found.values():
        assert finding.service_key is None or finding.service_key in catalogue, finding.id

    assert found["hardware.windows10_replace"].service_key == "hardware_lifecycle"
    for job in ("hardware.low_disk", "hardware.low_memory", "hardware.windows10_upgrade"):
        assert found[job].service_key is None, job
        assert found[job].recommendation_type, job  # still priced on slides 13 and 22
    assert found["patch.critical_pending"].service_key == "patch_remediation"
    assert found["tickets.passwords"].service_key == "password_manager"


def test_the_opinion_is_chosen_from_severity_not_written_by_the_model() -> None:
    found = list(findings_for(make_facts()).values())
    assert "small number of items" in overall_opinion(found)  # worst asserted is high
    assert overall_opinion([]) == "effective and stable"


# --- the narrative rules ------------------------------------------------------------------


def check(text: str, cites: list[str], facts: FactSet, **kw: Any) -> list[str]:
    allowed = {"ninjaone.patches.failed", "patch.failed_installs"}
    return problems_with(text, cites, allowed=allowed, facts=facts, **kw)


def test_a_sentence_may_not_type_a_number() -> None:
    facts = make_facts()
    good = "{{ninjaone.patches.failed}} installations failed on Windows 11 machines."
    assert check(good, ["patch.failed_installs"], facts) == []
    assert substitute(good, facts) == "14 installations failed on Windows 11 machines."

    assert "digit outside a fact token" in " ".join(
        check("14 installations failed.", ["patch.failed_installs"], facts)
    )
    assert "digit outside" in " ".join(
        check("Roughly 5% of installs failed.", ["patch.failed_installs"], facts)
    )


def test_a_token_must_be_a_fact_this_section_was_given() -> None:
    facts = make_facts()
    outside = check("{{autotask.tickets.open}} tickets are open.", ["patch.failed_installs"], facts)
    assert "was not given" in " ".join(outside)
    invented = check("{{ninjaone.made.up}} things.", ["patch.failed_installs"], facts)
    assert "is not a fact" in " ".join(invented)


def test_a_sentence_must_cite_and_may_not_carry_placeholder_text() -> None:
    facts = make_facts()
    assert "cites nothing" in " ".join(check("Patching is fine.", [], facts))
    assert "not in the brief" in " ".join(check("Patching is fine.", ["something.else"], facts))
    assert "placeholder text 'Insert'" in " ".join(
        check("Insert findings here.", ["patch.failed_installs"], facts)
    )
    assert "brace" in " ".join(check("Patching {is} fine.", ["patch.failed_installs"], facts))


def brief() -> Brief:
    finding = next(
        f
        for f in derive_findings(
            make_facts(), prior=None, prior_label=None, config=make_config(), period=PERIOD
        )
        if f.id == "patch.failed_installs"
    )
    return Brief(
        key="cybersecurity",
        title="Cybersecurity",
        purpose="test",
        fact_ids=list(finding.fact_ids),
        findings=[finding],
        slots=[
            Slot(
                id="patch.failed_installs/observation",
                guidance="observe",
                must_cite=("patch.failed_installs",),
                fallback=finding.detail,
            )
        ],
    )


async def test_an_acceptable_draft_is_used_and_its_tokens_count_as_citations(
    audit: NullAudit,
) -> None:
    model = ScriptedModel(
        tool_use(
            [
                {
                    "id": "patch.failed_installs/observation",
                    "text": "{{ninjaone.patches.failed}} updates did not install.",
                    "cites": ["patch.failed_installs"],
                }
            ]
        )
    )
    draft = await write_section(
        brief(),
        make_facts(),
        model_client=model,
        settings=Settings(),
        audit=audit,  # type: ignore[arg-type]
    )

    assert draft.narrative == "model"
    (item,) = draft.items
    assert item.text == "14 updates did not install."
    assert item.template == "{{ninjaone.patches.failed}} updates did not install."
    assert item.cites == ["patch.failed_installs", "ninjaone.patches.failed"]
    # The model was shown the facts it may use, and forced to answer with the tool.
    sent = model.requests[0]
    assert sent["tool_choice"] == {"type": "tool", "name": "write_section"}
    assert "ninjaone.patches.failed" in sent["messages"][0]["content"]


async def test_a_rejected_draft_is_retried_once_then_the_code_wording_is_used(
    audit: NullAudit,
) -> None:
    bad = tool_use(
        [{"id": "patch.failed_installs/observation", "text": "14 updates failed.", "cites": ["x"]}]
    )
    model = ScriptedModel(bad, bad)

    draft = await write_section(
        brief(),
        make_facts(),
        model_client=model,
        settings=Settings(),
        audit=audit,  # type: ignore[arg-type]
    )

    assert len(model.requests) == 2
    assert "rejected" in model.requests[1]["messages"][-1]["content"]  # it was told why
    assert draft.narrative == "fallback"
    assert draft.items[0].author == "code"
    assert draft.items[0].text.startswith("14 Windows patch installations failed on 6 devices")
    assert any("digit outside" in problem for problem in draft.problems)


async def test_without_a_model_the_code_wording_is_used(audit: NullAudit) -> None:
    draft = await write_section(
        brief(), make_facts(), model_client=None, settings=Settings(), audit=audit
    )
    assert draft.narrative == "fallback" and draft.items[0].author == "code"


# --- industry trends: the web, the industry only, an approval ---------------------------


def searched(query: str, urls: list[str], slots: list[dict[str, Any]]) -> SimpleNamespace:
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="server_tool_use", name="web_search", input={"query": query}),
            SimpleNamespace(
                type="web_search_tool_result",
                content=[SimpleNamespace(url=url) for url in urls],
            ),
            SimpleNamespace(type="tool_use", name="write_section", input={"slots": slots}),
        ],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        stop_reason="tool_use",
    )


async def trends(response: Any, audit: NullAudit) -> Any:
    return await research_trends(
        "Legal Services",
        never_in_a_query=["Acme Legal", "Example IT Partners"],
        model_client=ScriptedModel(response),  # type: ignore[arg-type]
        settings=Settings(web_search_enabled=True),
        audit=audit,
    )


async def test_the_model_is_told_the_industry_and_nothing_else(audit: NullAudit) -> None:
    model = ScriptedModel(
        searched(
            "law firm cyber insurance requirements",
            ["https://www.cisa.gov/a"],
            [
                {
                    "id": "1",
                    "text": "Insurers now ask for proof.",
                    "cites": [],
                    "url": "https://www.cisa.gov/a",
                }
            ],
        )
    )
    draft = await research_trends(
        "Legal Services",
        never_in_a_query=["Acme Legal"],
        model_client=model,  # type: ignore[arg-type]
        settings=Settings(web_search_enabled=True),
        audit=audit,
    )

    assert model.requests[0]["messages"][0]["content"] == '{"industry": "Legal Services"}'
    assert [item.url for item in draft.items] == ["https://www.cisa.gov/a"]
    assert [kind.value for kind, _, _ in audit.rows] == ["prompt", "web_search", "model_response"]


async def test_a_trend_whose_link_the_search_never_returned_is_dropped(audit: NullAudit) -> None:
    draft = await trends(
        searched(
            "law firm ransomware",
            ["https://www.cisa.gov/real"],
            [
                {"id": "1", "text": "Real trend.", "cites": [], "url": "https://www.cisa.gov/real"},
                {
                    "id": "2",
                    "text": "Invented.",
                    "cites": [],
                    "url": "https://www.cisa.gov/made-up",
                },
                {
                    "id": "3",
                    "text": "Fees rose 40% in 2025.",
                    "cites": [],
                    "url": "https://www.cisa.gov/real",
                },
            ],
        ),
        audit,
    )
    assert [item.text for item in draft.items] == ["Real trend."]
    assert any("not one the searches returned" in p for p in draft.problems)
    assert any("digit" in p for p in draft.problems)  # no statistics, no dates


async def test_a_query_that_names_the_client_voids_the_research(audit: NullAudit) -> None:
    draft = await trends(
        searched(
            "Acme Legal data breach",
            ["https://www.cisa.gov/a"],
            [{"id": "1", "text": "A trend.", "cites": [], "url": "https://www.cisa.gov/a"}],
        ),
        audit,
    )
    assert draft.items == []
    assert "named the client" in " ".join(draft.problems)


# --- the review step --------------------------------------------------------------------


async def test_review_actions_and_what_they_refuse() -> None:
    document = await make_document()

    with pytest.raises(review.ReviewError, match="nothing to approve"):
        review.approve(document, "trends", by="sam")
    with pytest.raises(review.ReviewError, match="does not need approval"):
        review.approve(document, "cybersecurity", by="sam")
    with pytest.raises(review.ReviewError, match="placeholder text 'XX'"):
        review.add_item(document, "accomplishments", "Closed XX tickets", by="sam")
    with pytest.raises(review.ReviewError, match="static slide"):
        review.add_item(document, "ai_essentials", "anything", by="sam")
    with pytest.raises(review.ReviewError, match="waits on N-able Cove"):
        review.add_item(document, "backup", "We back up nightly", by="sam")
    with pytest.raises(review.ReviewError, match="must be a number"):
        review.set_scores(document, nps="high", csat=None, pqual=None, comments=[], by="sam")

    scheduled = document.section("scheduled")
    scheduled.items, scheduled.status = [], "awaiting_engineer"
    review.add_item(
        document, "scheduled", "Phone system replacement, starting in January", by="sam"
    )
    assert scheduled.status == "filled" and scheduled.engineer_items[0].cites == ["engineer:sam"]


async def test_a_held_gap_reaches_the_deck_only_once_confirmed() -> None:
    document = await make_document()
    cyber = document.section("cybersecurity")
    assert "antivirus.unprotected" in cyber.finding_ids
    assert not any("antivirus" in " ".join(item.cites) for item in cyber.items)  # held, not written
    assert "Confirm or dismiss" in (cyber.waiting_for or "")

    review.confirm_finding(document, "antivirus.unprotected", by="sam")
    assert document.finding("antivirus.unprotected").assertable
    with pytest.raises(review.ReviewError, match="already asserted"):
        review.confirm_finding(document, "patch.critical_pending", by="sam")


async def test_changing_an_approved_section_withdraws_the_approval() -> None:
    document = await make_document()
    trends_section = document.section("trends")
    trends_section.items = [item.model_copy() for item in document.section("priorities").items[:1]]
    review.approve(document, "trends", by="sam")
    assert trends_section.renderable

    review.add_item(document, "trends", "One more trend", by="alex")

    assert trends_section.approved_by is None and not trends_section.renderable


async def test_every_slide_reports_one_of_the_four_states_and_why() -> None:
    document = await make_document()
    rows = {row["slide"]: row for row in review.status_report(document)}

    assert len(rows) == 24  # 23 slides, and the Monthly Report PDF's approval step at 0
    assert rows[0]["status"] == "awaiting_engineer" and rows[0]["on_deck"] == "no"
    assert rows[15]["status"] == "deferred" and "Cove" in str(rows[15]["later_connector"])
    assert rows[9]["status"] == "awaiting_engineer" and rows[9]["on_deck"] == "no"
    assert rows[3]["status"] == "awaiting_engineer"  # researched or not, it needs a person
    assert rows[6]["status"] == "filled" and rows[6]["on_deck"] == "yes"
    assert all(row["why"] for row in rows.values())
