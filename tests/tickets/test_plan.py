"""A plan keeps only what cites, shows a command only from its source, and never
lets a search that named the client cite the web."""

from __future__ import annotations

from app.settings import Settings
from app.tickets.plan import Evidence, search_terms, url_allowed, validate_steps

TICKET = Evidence(
    id="ticket:T1",
    kind="ticket",
    label="T1: Outlook will not open",
    text=(
        "Outlook crashed at start. Resolution: ran `outlook.exe /safe`, then "
        "Get-AppxPackage Microsoft.Office.Desktop | Remove-AppxPackage and reinstalled."
    ),
)
FACT = Evidence(id="fact:device.needs_reboot", kind="fact", label="Pending reboot: True")
EVIDENCE = {TICKET.id: TICKET, FACT.id: FACT}
URLS = {"https://learn.microsoft.com/outlook/safe-mode": "Start Outlook in safe mode"}


def test_steps_that_cite_nothing_we_hold_are_dropped() -> None:
    steps, dropped, problems = validate_steps(
        [
            {"text": "Reboot the machine first.", "cites": ["fact:device.needs_reboot"]},
            {"text": "Reinstall Windows.", "cites": []},
            {"text": "Clear the profile.", "cites": ["ticket:T999"]},
            {
                "text": "Try safe mode.",
                "cites": ["url:https://learn.microsoft.com/outlook/safe-mode"],
            },
            {"text": "Try safe mode again.", "cites": ["url:https://evil.example/outlook"]},
        ],
        EVIDENCE,
        allowed_urls=URLS,
        web_voided=False,
    )
    assert [s["text"] for s in steps] == ["Reboot the machine first.", "Try safe mode."]
    assert dropped == 3
    assert [s["id"] for s in steps] == ["s1", "s2"]
    assert steps[1]["citations"][0]["url"] == "https://learn.microsoft.com/outlook/safe-mode"
    assert any("did not return" in p for p in problems)


def test_a_command_is_shown_only_when_a_cited_ticket_holds_it_word_for_word() -> None:
    steps, dropped, problems = validate_steps(
        [
            {
                "text": "Start Outlook in safe mode as the last ticket did.",
                "cites": ["ticket:T1"],
                "command": "outlook.exe /safe",
            },
            {
                "text": "Remove the Store package.",
                "cites": ["ticket:T1"],
                "command": "Get-AppxPackage Microsoft.Office.Desktop | Remove-AppxPackage",
            },
            {
                "text": "Reset the profile with `Remove-Item $env:LOCALAPPDATA\\Outlook`.",
                "cites": ["ticket:T1"],
                "command": "Remove-Item $env:LOCALAPPDATA\\Microsoft\\Outlook -Recurse",
            },
            {
                "text": "Reboot, since a reboot is pending.",
                "cites": ["fact:device.needs_reboot"],
                "command": "shutdown /r /t 0",
            },
        ],
        EVIDENCE,
        allowed_urls={},
        web_voided=False,
    )
    assert dropped == 0
    assert steps[0]["command"] == "outlook.exe /safe"
    assert steps[0]["command_source"] == "ticket:T1"
    assert steps[0]["execution"] == "command from ticket:T1, not executed"
    assert steps[1]["command"].startswith("Get-AppxPackage")
    # An invented command goes, and so does code the model wrote into the prose.
    assert steps[2]["command"] is None
    assert steps[2]["execution"] == "manual, not executed"
    assert "[command omitted: not in a cited source]" in steps[2]["text"]
    # A fact is not a source a command can come from.
    assert steps[3]["command"] is None and steps[3]["execution"] == "manual, not executed"
    assert sum("removed" in p for p in problems) == 2


def test_a_leaked_query_voids_every_web_citation() -> None:
    steps, dropped, problems = validate_steps(
        [
            {
                "text": "Only the web backs this.",
                "cites": ["url:https://learn.microsoft.com/outlook/safe-mode"],
            },
            {
                "text": "This has a fact too.",
                "cites": [
                    "fact:device.needs_reboot",
                    "url:https://learn.microsoft.com/outlook/safe-mode",
                ],
            },
        ],
        EVIDENCE,
        allowed_urls=URLS,
        web_voided=True,
    )
    assert [s["text"] for s in steps] == ["This has a fact too."]
    assert dropped == 1
    assert [c["kind"] for c in steps[0]["citations"]] == ["fact"]
    assert any("discarded" in p for p in problems)


def test_the_allowlist_is_checked_in_code_as_well() -> None:
    settings = Settings(ticket_web_search_allowed_domains="learn.microsoft.com, ninjaone.com")
    assert url_allowed("https://learn.microsoft.com/x", settings)
    assert url_allowed("https://www.ninjaone.com/docs", settings)
    assert not url_allowed("https://ninjaone.com.evil.example/", settings)
    assert not url_allowed("https://example.com/", settings)


def test_search_terms_are_the_title_and_its_rarest_words() -> None:
    terms = search_terms(
        "Outlook keeps crashing when opening shared calendar",
        "The user cannot open the shared calendar since the update.",
    )
    assert terms[0] == "Outlook keeps crashing when opening shared calendar"
    assert len(terms) == 3 and "calendar" in {t.lower() for t in terms[1:]}
    assert search_terms(None, None) == []
