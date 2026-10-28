"""Liveness and readiness endpoints.

Both are `Depends(PUBLIC)`: a load balancer has no session. They are the
only routes that say anything without one, and they say only that the
process is up and that the database answers.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.auth.dependencies import PUBLIC
from app.db.session import get_session
from app.settings import Settings, get_settings

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    environment: str


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded"]
    database: Literal["ok", "unavailable"]
    detail: str | None = None


@router.get("/health", response_model=HealthResponse)
async def health(
    settings: Annotated[Settings, Depends(get_settings)],
    _: Annotated[None, Depends(PUBLIC)],
) -> HealthResponse:
    """Liveness: the process is up. Touches nothing external."""
    return HealthResponse(status="ok", version=__version__, environment=settings.environment)


@router.get("/health/ready", response_model=ReadinessResponse)
async def readiness(
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(PUBLIC)],
) -> ReadinessResponse:
    """Readiness: the database answers."""
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(
            status="degraded", database="unavailable", detail=type(exc).__name__
        )
    return ReadinessResponse(status="ready", database="ok")
