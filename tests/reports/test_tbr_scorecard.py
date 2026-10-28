"""The Key Takeaways scorecard: statuses computed by the rules in scorecard.toml."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pptx import Presentation
from pydantic import ValidationError

from app.reports.tbr import collect, gate, security, vulnerabilities
from app.reports.tbr.render import render
from app.reports.tbr.scorecard import RULES_FILE, MetricRule, ScorecardRules, evaluate, load_rules
from tests.reports.conftest import make_document, make_facts, tool_use


def statuses(rows: list[Any]) -> dict[str, str]:
    return {row.key: row.status for row in rows}


def takeaways_slide(result: Any) -> Any:
    deck = Presentation(str(result.path))
    return list(deck.slides)[result.kept.index(7)]


def chips(slide: Any) -> dict[str, str]:
    return {
        shape.name.removeprefix("Scorecard: "): shape.text_frame.text.replace("\n", " | ")
        for shape in slide.shapes
        if shape.name.startswith("Scorecard: ") and shape.name != "Scorecard: how it is computed"
    }


# --- the rules file -----------------------------------------------------------------------------


def test_the_rules_file_loads_documents_itself_and_names_real_facts() -> None:
    rules = load_rules()

    assert [area.key for area in rules.area] == [
        "service",
        "devices",
        "patching",
        "security",
        "backup",
    ]
    assert [area.key for area in rules.area if not area.assessed] == ["backup"]
    text = RULES_FILE.read_text(encoding="utf-8")
    for heading in ("HOW AN AREA IS SCORED", "HOW A METRIC IS SCORED", "THE TREND ARROW"):
        assert heading in text
    # Every fact a rule reads is one the collectors can produce (an id, or an f-string family).
    source = "".join(
        Path(module.__file__).read_text(encoding="utf-8")  # type: ignore[arg-type]
        for module in (collect, security, vulnerabilities)
    )
    for area in rules.area:
        for rule in area.metric:
            for fact_id in filter(None, (rule.fact, rule.per)):
                family = fact_id.rsplit(".", 1)[0] + ".{"
                assert f'"{fact_id}"' in source or family in source, fact_id


def test_thresholds_the_wrong_way_round_are_refused() -> None:
    with pytest.raises(ValidationError, match="must be above"):
        MetricRule(fact="x", label="x", better="higher", good=70, action=90)
    with pytest.raises(ValidationError, match="must be below"):
        MetricRule(fact="x", label="x", better="lower", good=10, action=2)
    with pytest.raises(ValidationError, match="no metric"):
        ScorecardRules.model_validate({"area": [{"key": "a", "label": "A"}]})
    with pytest.raises(ValidationError, match="what it waits for"):
        ScorecardRules.model_validate({"area": [{"key": "a", "label": "A", "assessed": False}]})


@pytest.mark.parametrize(
    ("better", "good", "action", "value", "expected"),
    [
        ("higher", 90, 75, 90.0, "good"),  # at the threshold is good
        ("higher", 90, 75, 89.9, "attention"),
        ("higher", 90, 75, 75.0, "attention"),  # at the action threshold is not yet action
        ("higher", 90, 75, 74.9, "action"),
        ("lower", 2, 10, 2.0, "good"),
        ("lower", 2, 10, 2.1, "attention"),
        ("lower", 2, 10, 10.0, "attention"),
        ("lower", 2, 10, 10.1, "action"),
    ],
)
def test_a_metric_is_scored_at_its_boundaries(
    better: Any, good: float, action: float, value: float, expected: str
) -> None:
    rule = MetricRule(fact="x", label="x", better=better, good=good, action=action)
    assert rule.status(value) == expected


# --- evaluation ---------------------------------------------------------------------------------


def test_each_area_takes_its_worst_metric_and_two_are_not_assessed() -> None:
    rows = evaluate(make_facts())

    assert statuses(rows) == {
        "service": "attention",  # resolution SLA 82%: under 90, not under 75
        "devices": "attention",  # 2 of 44 workstations cannot run Windows 11: 4.5%
        "patching": "attention",  # 95.7% installs good, but 4 of 48 devices wait on a critical
        "security": "no_data",  # Bitdefender did not answer for this site: never guessed
        "backup": "not_assessed",
    }
    assert all(row.trend is None for row in rows)  # no earlier review: no arrow
    patching = next(row for row in rows if row.key == "patching")
    assert [(m.fact_id, m.status) for m in patching.metrics] == [
        ("ninjaone.patches.success_percent", "good"),
        ("ninjaone.patches.critical_pending_devices", "attention"),
    ]
    assert "Devices waiting on a critical patch: 8.3% of devices" in patching.reason
    assert "good at 5 or less, action above 20" in patching.reason  # the rule, in words
    assert patching.cites() == [
        "ninjaone.patches.success_percent",
        "ninjaone.patches.critical_pending_devices",
        "ninjaone.devices.total",
    ]
    backup = next(row for row in rows if row.key == "backup")
    assert (backup.status_label, backup.metrics) == ("Not yet assessed", [])
    assert "Cove" in backup.reason


def test_action_beats_attention_and_a_healthy_site_is_good() -> None:
    bad = evaluate(make_facts(ninjaone__patches__success_percent=60.0))
    fine = evaluate(
        make_facts(
            autotask__sla__resolution_met_percent=97.0,
            ninjaone__windows11__ineligible=0,
            ninjaone__disks__low=0,
            ninjaone__patches__critical_pending_devices=1,
        )
    )

    assert statuses(bad)["patching"] == "action"
    assert [statuses(fine)[key] for key in ("service", "devices", "patching")] == ["good"] * 3


def test_an_area_with_no_facts_is_no_data_never_a_guess() -> None:
    facts = make_facts(ninjaone__devices__workstations=0)  # a zero denominator scores nothing
    for fact_id in ("autotask.sla.resolution_met_percent", "autotask.sla.resolution_measured"):
        del facts.facts[fact_id]

    rows = evaluate(facts)

    service = next(row for row in rows if row.key == "service")
    assert (service.status, service.status_label, service.metrics) == ("no_data", "No data", [])
    devices = next(row for row in rows if row.key == "devices")
    assert [m.fact_id for m in devices.metrics] == ["ninjaone.disks.low"]  # per workstation: out


def test_the_arrow_needs_a_prior_review_and_up_always_means_better() -> None:
    prior = make_facts(
        autotask__sla__resolution_met_percent=70.0,  # was worse: service improved
        ninjaone__patches__success_percent=99.0,  # was better: patching declined
        ninjaone__patches__critical_pending_devices=4,
        ninjaone__disks__low=1,  # unchanged: steady
    )

    rows = {row.key: row for row in evaluate(make_facts(), prior)}

    assert (rows["service"].trend, rows["patching"].trend) == ("up", "down")
    assert rows["devices"].trend == "steady"
    assert rows["security"].trend is None  # nothing scored, nothing to compare
    assert rows["service"].metrics[0].prior_value == 70.0
    # Fewer faults is better: a falling count of low disks is an UP arrow.
    fewer = evaluate(make_facts(ninjaone__disks__low=0), make_facts(ninjaone__disks__low=6))
    assert next(row for row in fewer if row.key == "devices").trend == "up"
    # A movement inside the tolerance is noise.
    nudged = evaluate(make_facts(), make_facts(autotask__sla__resolution_met_percent=81.0))
    assert next(row for row in nudged if row.key == "service").trend == "steady"


# --- on the review and the slide ----------------------------------------------------------------


async def test_the_model_never_sees_or_writes_the_scorecard() -> None:
    class Recording:
        """Answers every call with an empty draft, so each section falls back to code."""

        def __init__(self) -> None:
            self.requests: list[dict[str, Any]] = []
            self.messages = self

        async def create(self, **kwargs: Any) -> Any:
            self.requests.append(kwargs)
            return tool_use([])

    recording = Recording()
    document = await make_document(model_client=recording)
    assert recording.requests  # the model really was asked for the takeaways
    sent = " ".join(str(request) for request in recording.requests).casefold()

    assert "scorecard" not in sent and "not yet assessed" not in sent
    section = document.section("takeaways")
    assert [row.key for row in section.scorecard][:3] == ["service", "devices", "patching"]
    assert "scorecard.toml" in section.reason and "no trend arrows" in section.reason
    assert "ninjaone.patches.success_percent" in section.fact_ids
    assert not any("scorecard" in (item.slot or "") for item in section.all_items())


async def test_the_slide_shows_a_chip_per_area_in_words_and_passes_the_gate(
    tmp_path: Path,
) -> None:
    document = await make_document()

    result = render(document, tmp_path / "deck.pptx")

    slide = takeaways_slide(result)
    assert chips(slide) == {
        "Service": "Service | Attention",
        "Devices": "Devices | Attention",
        "Patching": "Patching | Attention",
        "Security": "Security | No data",
        "Backup": "Backup | Not yet assessed",
    }
    caption = next(s for s in slide.shapes if s.name == "Scorecard: how it is computed")
    assert "fixed rules" in caption.text_frame.text and "Arrows" not in caption.text_frame.text
    assert result.gate.passed and not gate.red_text(slide)  # "action" red is not placeholder red
    table = next(s for s in slide.shapes if getattr(s, "has_table", False) and s.has_table)
    assert table.top > caption.top  # the takeaways moved down to make room


async def test_arrows_appear_only_with_a_prior_review_and_say_what_they_compare(
    tmp_path: Path,
) -> None:
    prior = make_facts(autotask__sla__resolution_met_percent=70.0)
    document = await make_document(prior=prior)

    slide = takeaways_slide(render(document, tmp_path / "deck.pptx", mode="internal"))

    assert chips(slide)["Service"] == "Service | Attention ↑"
    assert chips(slide)["Backup"] == "Backup | Not yet assessed"  # never an arrow
    caption = next(s for s in slide.shapes if s.name == "Scorecard: how it is computed")
    assert "Arrows compare with July 2026; up is better." in caption.text_frame.text
    notes = gate.notes_text(slide)  # the internal deck traces the scorecard to its facts
    assert "# Scorecard, Service: Attention." in notes
    assert "[autotask.sla.resolution_met_percent]" in notes


async def test_a_scorecard_with_no_takeaways_is_a_slide_without_an_empty_table(
    tmp_path: Path,
) -> None:
    document = await make_document()
    document.section("takeaways").items = []

    slide = takeaways_slide(render(document, tmp_path / "deck.pptx"))

    assert len(chips(slide)) == 5
    assert not [s for s in slide.shapes if getattr(s, "has_table", False) and s.has_table]
