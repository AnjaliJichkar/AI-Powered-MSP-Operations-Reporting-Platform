"""The pages: sign in, Ask, Reports, Sites, Coverage, Devices, Tickets, Settings.

Server-rendered, with no build step. A page is a Jinja template that draws the
shell — the left nav, the header, the franchisee's logo — and a block of markup
for the page itself; the numbers in it arrive from the JSON endpoints, which
resolve the session themselves. So a template never decides what anybody may
see, and adding a page is a template and a script rather than another copy of a
header and another set of colours.

**The shell is rendered from the session, so these routes are authenticated.**
That is a change: the pages used to be public and hand an empty shell to a
browser with no session, which then got a 401 from its first fetch and had to
redirect itself. Now the header is right on the first paint, and a browser with
no session is sent to the sign-in page by the server (`redirect_unauthenticated`
in `app/main.py`) rather than rendering a page it cannot fill.

The sign-in page is the one public page, and it has no shell: there is no user
to name in a header and no nav to offer.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.auth.dependencies import INPUTIV_ANY, PUBLIC, SIGNED_IN
from app.auth.scope import CAN_RUN_MODEL, Scope
from app.auth.service import InviteState, describe_invite
from app.db.session import get_session
from app.interfaces.consoles import console_urls
from app.models.client import Client, ClientKind
from app.models.enums import ConnectorName, Role, vendor_label
from app.models.organisation import Invite, User
from app.models.tool_mapping import ToolMapping
from app.reports.tbr.config import load_config
from app.settings import get_settings

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"
#: The stylesheet and the page scripts. Mounted by the app factory; nothing in
#: here is tenant data, which is why it is a mount rather than a scoped route.
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def asset(path: str) -> str:
    """A static file's URL, stamped so a browser refetches it when it changes.

    Stamped with the file's own modification time rather than the application
    version. The version is the same string all through a development session,
    so a stylesheet edited between two page loads was served from cache and the
    change appeared not to have happened — which is a slow and confusing way to
    discover a caching policy. An mtime changes exactly when the file does,
    which is also what a deploy wants.
    """
    file = STATIC_DIR / path
    try:
        stamp = int(file.stat().st_mtime)
    except OSError:
        # Missing is not this function's problem to report: the request for it
        # will 404 and say so plainly.
        stamp = 0
    return f"/static/{path}?v={stamp}"


templates.env.globals["asset"] = asset

router = APIRouter(tags=["web"])

#: What each role is called on screen. The enum value is for the database and
#: the audit trail; nobody should have to read `franchisee_viewer` in a header.
ROLE_LABELS: dict[Role, str] = {
    Role.INPUTIV_ADMIN: "Inputiv administrator",
    Role.INPUTIV_ENGINEER: "Inputiv engineer",
    Role.FRANCHISEE_ADMIN: "Administrator",
    Role.FRANCHISEE_TECHNICIAN: "Technician",
    Role.FRANCHISEE_VIEWER: "Viewer",
}


class NavItem:
    """One entry in the left nav, and who sees it.

    An entry may hold `children`, and then it is a **group label rather than a
    link**: the pages beneath it are the links. A parent that was also a link
    would be a second address for whichever child it pointed at, and the two
    would drift apart the day a child is renamed.
    """

    __slots__ = ("children", "href", "icon", "key", "label", "roles")

    def __init__(
        self,
        key: str,
        label: str,
        href: str,
        icon: str,
        roles: frozenset[Role],
        children: tuple[NavItem, ...] = (),
    ) -> None:
        self.key, self.label, self.href = key, label, href
        # The name of a drawing in `_icons.html`, not a character to print: the
        # shaded squares this replaced (one per page) were four variations on a
        # grid, and in a collapsed nav the icon is the whole of the row.
        self.icon, self.roles = icon, roles
        self.children = children

    def for_role(self, role: Role) -> NavItem:
        """This entry with only the children that role may use.

        A group whose every child is hidden is not drawn at all (`visible`), so
        a role never sees a heading with nothing under it.
        """
        if not self.children:
            return self
        allowed = tuple(child for child in self.children if role in child.roles)
        return NavItem(self.key, self.label, self.href, self.icon, self.roles, allowed)

    @property
    def visible(self) -> bool:
        return bool(self.href) or bool(self.children)


EVERYONE = frozenset(Role)
INPUTIV = frozenset({Role.INPUTIV_ADMIN, Role.INPUTIV_ENGINEER})
#: The nav for anything that runs a model, which is the same set the routes
#: behind those pages allow (`auth.scope.CAN_RUN_MODEL`). Read from there rather
#: than repeated, so a nav entry cannot outlive the permission behind it.
CAN_ASK = CAN_RUN_MODEL

#: The nav, in the order it is read. An entry a role may not use is not shown
#: rather than shown and refused: the endpoints behind each are what actually
#: enforce this (`app/auth/dependencies.py`), and hiding a link the caller
#: would be refused is a courtesy, not the control.
NAV: tuple[NavItem, ...] = (
    NavItem("ask", "Ask", "/", "spark", CAN_ASK),
    NavItem("reports", "Reports", "/reports", "chart", EVERYONE),
    NavItem("sites", "Sites", "/sites", "sites", EVERYONE),
    NavItem("coverage", "Coverage", "/coverage", "shield", EVERYONE),
    NavItem("devices", "Devices", "/devices", "device", EVERYONE),
    NavItem("tickets", "Tickets", "/tickets", "ticket", CAN_ASK),
    # Which tools this franchisee is on and when each last answered. Every
    # role: a franchisee administrator asking why a number is missing is
    # asking this page's question.
    NavItem("integrations", "Integrations", "/integrations", "plug", EVERYONE),
    # Inputiv staff only: a measurement of our own agent, not anything about a
    # franchisee's estate. One entry, pointing at the results — the page that
    # answers "may it act yet". **Labelling past tickets is deliberately not
    # here**: the review that matters now happens on a ticket an engineer is
    # working anyway, and the past-ticket sample is a job you go to from the
    # results page when a row says it is waiting on one, not a standing
    # invitation in the nav.
    NavItem("autopilot", "Autopilot", "/autopilot/results", "flag", INPUTIV),
    # Inputiv staff only, like Autopilot, and for a stronger reason: a run is
    # something we do at a client with their authoriser's yes, and the page
    # holds the switches that let it happen at all.
    NavItem("automations", "Automations", "/automations", "bolt", INPUTIV),
    NavItem("settings", "Settings", "/settings", "settings", EVERYONE),
)


#: The drawing `_icons.html` falls back to: a crossed box, which is not a
#: drawing anybody would choose and so is not mistakable for one. A nav entry
#: may not name it (`assert_icons_named`); it exists to be seen when something
#: has gone wrong, which an empty square at 18px emphatically is not.
MISSING_ICON = "missing"


def icon_names() -> set[str]:
    """Every drawing `_icons.html` holds, read from the file itself.

    The icons are markup in a template and the nav is a list in Python, and
    the two name the same things from two files. Nothing joined them until a
    rename moved one and not the other: seven nav rows then drew the same
    sparkle, because the macro fell back to one, and there was no error
    anywhere to say so. The macro no longer falls back; this is the other
    half, so the mismatch is caught at build rather than on a page.
    """
    text = (TEMPLATE_DIR / "_icons.html").read_text(encoding="utf-8")
    return set(re.findall(r'^\s*"([a-z0-9_-]+)":', text, re.MULTILINE))


def assert_icons_named() -> None:
    """Refuse to build an application whose nav names a drawing we do not have."""
    known = icon_names() - {MISSING_ICON}
    missing = sorted(f"{item.key} wants {item.icon!r}" for item in NAV if item.icon not in known)
    if missing:
        raise RuntimeError(
            "every nav entry must name a drawing in app/templates/_icons.html, which draws "
            f"{MISSING_ICON!r} — a crossed box — for a name it does not hold:\n  "
            + "\n  ".join(missing)
        )
    drawn = [item.icon for item in NAV]
    repeated = sorted({name for name in drawn if drawn.count(name) > 1})
    if repeated:
        raise RuntimeError(
            "two nav entries may not share a drawing: a narrowed nav is its icons, "
            "and two rows that look alike are two rows nobody can tell apart:\n  "
            + "\n  ".join(repeated)
        )


def nav_for(role: Role) -> list[NavItem]:
    """The nav this role is shown, children filtered the same way."""
    items = [item.for_role(role) for item in NAV if role in item.roles]
    return [item for item in items if item.visible]


def nav_pages(items: list[NavItem]) -> list[dict[str, str]]:
    """The same nav, flattened, for the command palette to search.

    A group's label is not a page and is left out; its children are the pages
    and each carries the group's name, so "Runs" reads as "Automations · Runs"
    in a list where the heading above it is gone.
    """
    pages: list[dict[str, str]] = []
    for item in items:
        if item.children:
            for child in item.children:
                pages.append({"label": child.label, "href": child.href, "group": item.label})
        else:
            pages.append({"label": item.label, "href": item.href, "group": ""})
    return pages


#: How each vendor is shown where a tool is named: its own mark, or a tile
#: with its initial on it.
#:
#: **One map, read by the template and by the page scripts.** It is handed to
#: the browser as `DESK.vendorMarks`, so a mark drawn by a script and a mark
#: drawn by Jinja cannot come from two lists that disagree — which is how the
#: Coverage board ended up with a Cove column that had no mark while the
#: header row beside it did.
#:
#: A `file` is a vendor's own artwork, kept in `app/static/logos` with its
#: source in `CREDITS.md`. Where we hold none, the mark is a coloured tile
#: carrying the vendor's initial: a monogram is plainly ours and claims
#: nothing, which is the right answer for a vendor whose artwork we have no
#: clear licence to redistribute. Three are: Autotask, whose file was under a
#: share-alike licence; Microsoft 365, whose brand rules are strict about
#: what may be shown and how; and Cove, which has never had one here.
VENDOR_MARKS: dict[str, dict[str, str]] = {
    "ninjaone": {"file": "ninjaone-icon.png"},
    "autotask": {"letter": "A", "tint": "#2f6fd0"},
    "bitdefender": {"file": "bitdefender-icon.png"},
    "connectsecure": {"file": "connectsecure-icon.png"},
    "cove": {"letter": "C", "tint": "#1f8f86"},
    "microsoft365": {"letter": "M", "tint": "#b05520"},
}


def vendor_marks() -> dict[str, dict[str, str]]:
    """`VENDOR_MARKS` with each file resolved to the URL that serves it."""
    marks: dict[str, dict[str, str]] = {}
    for key, mark in VENDOR_MARKS.items():
        # The name comes from `VENDOR_LABELS`, which is where every vendor's
        # spelling lives; this map says only how each one is drawn.
        resolved = {"name": vendor_label(key), **mark}
        if "file" in resolved:
            resolved["src"] = asset("logos/" + resolved.pop("file"))
        marks[key] = resolved
    return marks


#: The pages that can write to a client's tools, which is what the note at the
#: bottom of the nav reports. Read from the nav key rather than from the URL so
#: a page moved to another address keeps telling the truth.
WRITES_HERE = frozenset({"automations"})

#: The tools the header's row of marks reports on, in the order it shows them.
#: Cove has no mark of its own, so it is not in the row; it is on the Coverage
#: board's own columns like every other tool.
HEADER_TOOLS: tuple[ConnectorName, ...] = (
    ConnectorName.NINJAONE,
    ConnectorName.AUTOTASK,
    ConnectorName.BITDEFENDER,
    ConnectorName.CONNECTSECURE,
    ConnectorName.MICROSOFT365,
)
TOOL_NAMES: dict[ConnectorName, str] = {
    ConnectorName.NINJAONE: "NinjaOne",
    ConnectorName.AUTOTASK: "Autotask",
    ConnectorName.BITDEFENDER: "Bitdefender",
    ConnectorName.CONNECTSECURE: "ConnectSecure",
    ConnectorName.MICROSOFT365: "Microsoft 365",
}


async def client_is_demo(db: AsyncSession, client_id: uuid.UUID | None) -> bool:
    """Whether this client is the fictional one, for the strip at the top.

    Read from `Client.kind` and nowhere else. A setting that said "this is a
    demo" would be a setting somebody could leave on over real data, and the
    strip is the one thing on the page claiming every figure under it is made
    up.
    """
    if client_id is None:
        return False
    kind = await db.scalar(select(Client.kind).where(Client.id == client_id))
    return kind == ClientKind.DEMO


async def tool_status(db: AsyncSession, client_id: uuid.UUID | None) -> list[dict[str, str]]:
    """Which tools this client is set up on, for the row of marks in the header.

    **Set up, not healthy.** A dot here says whether Desk can read that tool
    for this client at all: whether any of their customers has an organisation
    mapped on it. Whether the vendor answered this morning is a live check,
    and a green dot that quietly meant "configured" while a reader took it for
    "working" is the kind of figure this product does not print. The live check
    belongs to the Integrations page, which reads each connector and says when
    it last answered.

    One query for the whole client, on a page render that already has a
    connection.
    """
    if client_id is None:
        return []
    rows = (
        await db.execute(
            select(ToolMapping.connector_name, func.count())
            .where(ToolMapping.client_id == client_id, ToolMapping.site_id.is_not(None))
            .group_by(ToolMapping.connector_name)
        )
    ).all()
    counts = {connector: count for connector, count in rows}
    status: list[dict[str, str]] = []
    for connector in HEADER_TOOLS:
        name = TOOL_NAMES[connector]
        mapped = counts.get(connector, 0)
        status.append(
            {
                "key": connector.value,
                "tone": "ok" if mapped else "neutral",
                "title": (
                    f"{name}: set up for {mapped} customer(s)"
                    if mapped
                    else f"{name}: no customer of this client is mapped to it"
                ),
            }
        )
    return status


async def _logo_path(db: AsyncSession, scope: Scope) -> Path | None:
    """The client's stored logo file, if their report settings name one that
    is there to be read.

    Takes the request's own session rather than opening one: a page render
    is already inside a request that has a connection, and a second one per
    page is a connection per page for a single boolean.
    """
    if scope.client_id is None:
        return None
    config = await load_config(db, scope.client_id, client_name=scope.client_name or "")
    return config.logo()


async def page(
    request: Request,
    db: AsyncSession,
    template: str,
    scope: Scope,
    nav: str,
    **extra: Any,
) -> HTMLResponse:
    """Render one page inside the shell, with the session's own header."""
    client = (
        {"slug": scope.client_slug, "name": scope.client_name or scope.client_slug}
        if scope.client_id is not None
        else None
    )
    nav_items = nav_for(scope.role)
    context: dict[str, Any] = {
        "version": __version__,
        "nav": nav,
        "nav_items": nav_items,
        # Handed to the palette as JSON: the pages this role may open, which
        # is exactly the nav beside it. The palette invents no destination.
        "nav_json": json.dumps(nav_pages(nav_items)),
        # Whether this page is one of the few that can change anything in a
        # client's tools. The nav says which it is, every page, at the bottom
        # of the column where it stays in view.
        "writes_here": nav in WRITES_HERE,
        # The row of vendor marks in the header: which tools this client is
        # set up on. Never a health check (see `tool_status`).
        "tool_status": await tool_status(db, scope.client_id),
        # Every figure under this strip is invented. Said once, in the frame.
        "client_is_demo": await client_is_demo(db, scope.client_id),
        # How each vendor is drawn, for the template and for the scripts.
        "vendor_marks": vendor_marks(),
        "vendor_marks_json": json.dumps(vendor_marks()),
        # The name as well as the address: the ticket queue's "Mine" filter
        # compares the signed-in person against Autotask's assignee, which is a
        # person's name and never an email address. A user with no display name
        # set has no "Mine" to press, and the page says so rather than showing
        # an empty queue.
        "user": {
            "email": scope.user.email,
            "role": scope.role.value,
            "name": scope.user.display_name,
        },
        "role_label": ROLE_LABELS[scope.role],
        "organisation": {
            "slug": scope.organisation.slug,
            "name": scope.organisation.name,
        },
        "client": client,
        # Handed to the page scripts as JSON, so `window.DESK.client` is either
        # an object or null rather than a string that has to be parsed twice.
        "client_json": json.dumps(client),
        "has_logo": await _logo_path(db, scope) is not None,
        "may_switch_client": scope.may_switch_client,
        "csrf_token": scope.session.csrf_token,
        # Where each vendor's console lives for *this* client, so a page can
        # offer "open it in NinjaOne" with the id it already holds. Derived
        # from the client's own connector settings and never guessed; empty
        # where we hold no address we recognise, and then no link is drawn.
        "consoles_json": json.dumps(await console_urls(db, scope.client_id)),
        "login_path": get_settings().login_path,
        # Staff who have not enrolled a second factor, where the deployment
        # requires one: every data call they make is refused, so the shell
        # says why on first paint rather than leaving the page to guess.
        "second_factor_required": (
            get_settings().totp_required_for_inputiv
            and scope.role.is_inputiv
            and not scope.user.totp_enabled
        ),
        **extra,
    }
    response = templates.TemplateResponse(request, template, context)
    # No caching: the shell carries the signed-in user, and a page held by a
    # browser after a sign-out or a client switch would name the wrong one.
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def ask_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(SIGNED_IN)],
) -> HTMLResponse:
    """Ask: the conversation. A viewer has no nav entry for it and no session
    that `/chat` would accept, so they are shown Reports instead of a dead end."""
    if scope.role not in CAN_ASK:
        return await page(request, db, "reports.html", scope, "reports")
    return await page(request, db, "ask.html", scope, "ask")


@router.get("/reports", response_class=HTMLResponse, include_in_schema=False)
async def reports_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(SIGNED_IN)],
) -> HTMLResponse:
    """Business reviews: generate and review them, or read the released ones."""
    return await page(request, db, "reports.html", scope, "reports")


@router.get("/tbr", response_class=HTMLResponse, include_in_schema=False)
async def tbr_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(SIGNED_IN)],
) -> HTMLResponse:
    """The review screen's old address. Kept so a bookmark still works."""
    return await page(request, db, "reports.html", scope, "reports")


@router.get("/integrations", response_class=HTMLResponse, include_in_schema=False)
async def integrations_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(SIGNED_IN)],
) -> HTMLResponse:
    """Which tools this client is on, and when each last answered."""
    return await page(request, db, "integrations.html", scope, "integrations")


@router.get("/sites", response_class=HTMLResponse, include_in_schema=False)
async def sites_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(SIGNED_IN)],
) -> HTMLResponse:
    """The client's end customers, and which vendors know each of them."""
    return await page(request, db, "sites.html", scope, "sites")


@router.get("/coverage", response_class=HTMLResponse, include_in_schema=False)
async def coverage_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(SIGNED_IN)],
) -> HTMLResponse:
    """Which managed computers each tool covers, site by site, and the exclusions."""
    return await page(request, db, "coverage.html", scope, "coverage")


@router.get("/devices", response_class=HTMLResponse, include_in_schema=False)
async def devices_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(SIGNED_IN)],
) -> HTMLResponse:
    """Every machine across every vendor, from the identity layer; for Inputiv
    staff, the unresolved queue and the day's agreement as well. The cards a
    franchisee may not use are not drawn; the endpoints behind them refuse anyway."""
    return await page(request, db, "devices.html", scope, "devices", is_staff=scope.is_inputiv)


@router.get("/tickets", response_class=HTMLResponse, include_in_schema=False)
async def tickets_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(SIGNED_IN)],
) -> HTMLResponse:
    """The open tickets and a workspace for working one. A viewer has no nav entry
    for it and the endpoints behind it refuse them; they are shown Reports."""
    if scope.role not in CAN_ASK:
        return await page(request, db, "reports.html", scope, "reports")
    return await page(request, db, "tickets.html", scope, "tickets")


@router.get("/tickets/{ticket_ref}", response_class=HTMLResponse, include_in_schema=False)
async def ticket_page(
    ticket_ref: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(SIGNED_IN)],
) -> HTMLResponse:
    """One ticket's workspace, as its own page.

    `ticket_ref` is the **ticket number** an engineer reads and Autotask prints,
    not an id of ours: it exists before a workspace does, it is what the queue
    shows, and it is what somebody pastes into a chat message. The shell is all
    this route renders; the workspace itself arrives from `/tickets/workspaces`,
    which resolves the session and refuses another client's, so the number in
    the address bar decides nothing about what anybody may see.

    The JSON routes under `/tickets/` are registered before this one, so
    `/tickets/list` and `/tickets/statistics` stay themselves rather than being
    read as ticket numbers.
    """
    if scope.role not in CAN_ASK:
        return await page(request, db, "reports.html", scope, "reports")
    return await page(
        request,
        db,
        "ticket.html",
        scope,
        "tickets",
        ticket_ref=ticket_ref,
        is_staff=scope.is_inputiv,
    )


@router.get("/autopilot/labelling", response_class=HTMLResponse, include_in_schema=False)
async def autopilot_labelling_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> HTMLResponse:
    """Judging a sample blind: where a ticket came from, and whether a group is
    one thing.

    `INPUTIV_ANY` rather than `SIGNED_IN`, and a **403** rather than a
    redirection to another page: this is a measurement of our own agent, not
    something a franchisee is shown a polite alternative to. The endpoints
    behind it refuse them for the same reason.
    """
    return await page(request, db, "autopilot_labelling.html", scope, "autopilot")


@router.get("/autopilot/results", response_class=HTMLResponse, include_in_schema=False)
async def autopilot_results_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> HTMLResponse:
    """What the shadow runs say, against the gate. Read only, all the way down:
    the page offers nothing that starts, stops or changes a run, and the
    router behind it holds no state-changing route to offer."""
    return await page(request, db, "autopilot_results.html", scope, "autopilot")


@router.get("/automations", response_class=HTMLResponse, include_in_schema=False)
async def automations_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> HTMLResponse:
    """The runs for this client, the switch per workflow and the kill switch.
    `INPUTIV_ANY` and a 403 for a franchisee, as the Autopilot pages are: the
    engine is ours, and the endpoints behind this page refuse them for the same
    reason."""
    return await page(request, db, "automations.html", scope, "automations")


@router.get(
    "/automations/workflows/{workflow_name}", response_class=HTMLResponse, include_in_schema=False
)
async def automation_workflow_page(
    workflow_name: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> HTMLResponse:
    """One workflow as this client has it, read only: what starts it, what it
    is told and where that comes from, its steps in words, the approval rule
    and the windows. Declared before the run page so `workflows` is never read
    as a run id; the JSON behind it is `/automation/workflows/<name>`."""
    return await page(
        request, db, "automation_workflow.html", scope, "automations", workflow_name=workflow_name
    )


@router.get(
    "/automations/reports/{workflow_name}", response_class=HTMLResponse, include_in_schema=False
)
async def automation_report_page(
    workflow_name: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> HTMLResponse:
    """The latest report a scheduled workflow built — the chronic devices
    report — read only; the JSON is `/automation/reports/<name>`."""
    return await page(
        request, db, "automation_report.html", scope, "automations", workflow_name=workflow_name
    )


@router.get("/automations/{run_id}", response_class=HTMLResponse, include_in_schema=False)
async def automation_run_page(
    run_id: uuid.UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> HTMLResponse:
    """One run: its inputs, the dry run as a checklist, approval, live progress
    per step, and Undo where allowed. A page per thing somebody works on, with
    its own address; the JSON behind it is `/automation/runs/<id>`, under the
    singular prefix so the two never collide."""
    return await page(request, db, "automation_run.html", scope, "automations", run_id=str(run_id))


@router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(SIGNED_IN)],
) -> HTMLResponse:
    """The caller's own account, and — for an administrator — other people's."""
    return await page(
        request,
        db,
        "settings.html",
        scope,
        "settings",
        may_administer=scope.role in (Role.INPUTIV_ADMIN, Role.FRANCHISEE_ADMIN),
    )


@router.get("/invite/{token}", response_class=HTMLResponse, include_in_schema=False)
async def invite_page(
    token: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(PUBLIC)],
) -> HTMLResponse:
    """Accept an invitation: who invited you, into what, and a password.

    Public, because the person holding the link has no account yet — the token
    is the only thing they have and it is what this checks.

    Every way a link can fail is an ordinary end rather than an error: a page
    that says what happened and what to do, with the status that describes it
    (410 for one that has been used or has expired, 404 for one we do not
    recognise). Not a stack trace, and not a bare 404 either: somebody who was
    sent a link a week ago deserves a sentence.
    """
    settings = get_settings()
    lookup = await describe_invite(db, token)
    invite = lookup.invite

    context: dict[str, Any] = {
        "lookup": lookup,
        "token": token,
        "login_path": settings.login_path,
        "email": invite.email if invite else "",
        "organisation_name": invite.organisation.name if invite else "",
        "role_label": ROLE_LABELS[invite.role] if invite else "",
        "expires": f"{invite.expires_at:%d %b %Y at %H:%M UTC}" if invite else "",
        "invited_by": await _inviter(db, invite),
    }
    response = templates.TemplateResponse(request, "invite.html", context)
    response.status_code = _INVITE_STATUS[lookup.state]
    response.headers["Cache-Control"] = "no-store"
    return response


#: The status that describes each ending. The page is the same either way; the
#: code is for whatever is watching, and for a link checker in a mail client.
_INVITE_STATUS: dict[InviteState, int] = {
    InviteState.OPEN: status.HTTP_200_OK,
    InviteState.UNKNOWN: status.HTTP_404_NOT_FOUND,
    InviteState.EXPIRED: status.HTTP_410_GONE,
    InviteState.ACCEPTED: status.HTTP_410_GONE,
    InviteState.REVOKED: status.HTTP_410_GONE,
    InviteState.HAS_ACCOUNT: status.HTTP_409_CONFLICT,
}


async def _inviter(db: AsyncSession, invite: Invite | None) -> str | None:
    """Who sent this, as a person's name. None for one the CLI created.

    Saying "Nobody invited you" would be worse than saying nothing, and an
    invite made by `manage_auth.py` genuinely has no account behind it.
    """
    if invite is None or invite.created_by_user_id is None:
        return None
    user = await db.get(User, invite.created_by_user_id)
    if user is None:
        return None
    return user.display_name or user.email


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, _: Annotated[None, Depends(PUBLIC)]) -> HTMLResponse:
    """The sign-in form. The one page that is drawn without a session."""
    response = templates.TemplateResponse(request, "login.html", {"version": __version__})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/branding/logo", include_in_schema=False)
async def client_logo(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(SIGNED_IN)],
) -> FileResponse:
    """The scoped client's own logo, as stored in their report settings.

    Scoped like everything else: a caller gets the logo of the client their
    session is on, and there is no parameter that could name another one.
    """
    path = await _logo_path(db, scope)
    if path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no logo is stored for the client this session is on",
        )
    return FileResponse(path, headers={"Cache-Control": "private, max-age=300"})
