"""The two Autopilot pages' JSON, in one router per page.

Split by page rather than kept in one module because the two have opposite
rules and that is easier to hold true when they are not neighbours in a file:
`labelling` writes — a person's judgement, audited — and `results` is read
only, with no state-changing route under it at all
(`tests/api/test_autopilot_results.py` asserts the method list). `suggestions`
is the third: what the agent said about a live ticket, where the engineer
already works, and what they made of it.

Both are **Inputiv staff only** (`INPUTIV_ANY`). A franchisee gets 403 on every
route here and no nav entry for either page, because these pages are a
measurement of our own agent rather than anything about their estate.

Routers live under `app/autopilot/` rather than in `app/api/` so the feature is
one directory: the models, the service, the gate, the scoring and the two
routers are all read together, and nothing else in the application imports
them.
"""

from fastapi import APIRouter

from app.autopilot.api.labelling import router as labelling_router
from app.autopilot.api.results import router as results_router
from app.autopilot.api.suggestions import router as suggestions_router

#: One router for `create_app` to include, as the other features expose.
router = APIRouter()
router.include_router(labelling_router)
router.include_router(results_router)
router.include_router(suggestions_router)

__all__ = ["labelling_router", "results_router", "router", "suggestions_router"]
