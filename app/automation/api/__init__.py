"""The Automations pages' JSON: one router, Inputiv staff only.

Every route here is a short transaction through `app/automation/service.py`
or `controls.py`. None runs a step: the worker does that, in the background,
and the pages read the result.

`INPUTIV_ANY` on every route but four: the Desk note on a ticket, Post and
Not useful, and the notes a ticket holds go to the roles that work a ticket
(`CAN_CHAT`), because an internal note on their own PSA about their own
ticket is theirs to post or dismiss. Everything else — the pages, the
switches, the modes, the reports — a franchisee gets 403 and no nav entry:
the engine is ours, the runs are things we do at a client with their
authoriser's yes, and there is no friendlier page to send a franchisee to.

Under `app/automation/` rather than `app/api/` for the reason the Autopilot
routers are: the feature is one directory, read together, imported by nothing
else — and the no-AI guard covers this router with the rest of it.
"""

from fastapi import APIRouter

from app.automation.api.notes import router as notes_router
from app.automation.api.routes import router as routes_router

router = APIRouter()
router.include_router(routes_router)
router.include_router(notes_router)
