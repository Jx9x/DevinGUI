"""Independent Devin and Codex allowance routes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .usage_provider import UsageProviderError

if TYPE_CHECKING:
    from .codex_usage_provider import CodexUsageProvider
    from .usage_provider import UsageProvider


def create_usage_router(
    *,
    get_api_key: Callable[[], str | None],
    create_usage_provider: Callable[[str], UsageProvider],
    get_codex_usage_provider: Callable[[], CodexUsageProvider],
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/usage")
    async def get_usage():
        """Return live Windsurf/Devin quota usage from the GetPlanStatus API.

        Response shape::

            {
              "daily_used_percent": 5,
              "weekly_used_percent": 10,
              "weekly_reset_unix": 1753000000,
              "plan_name": "Pro",
              "fetched_at": 1753000000,
              "error": null
            }

        On any failure (no credentials, network error, non-200 response) the
        endpoint still returns 200 with an ``error`` message so the frontend can
        render a graceful placeholder instead of throwing.
        """
        import time

        fetched_at = int(time.time())
        api_key = get_api_key()
        if not api_key:
            return {
                "daily_used_percent": None,
                "weekly_used_percent": None,
                "weekly_reset_unix": None,
                "plan_name": None,
                "fetched_at": fetched_at,
                "error": "No windsurf_api_key found in Devin Desktop credentials.toml. "
                "Sign in to Devin Desktop first.",
            }
        try:
            decoded = await create_usage_provider(api_key).fetch()
        except UsageProviderError as e:
            return {
                "daily_used_percent": None,
                "weekly_used_percent": None,
                "weekly_reset_unix": None,
                "plan_name": None,
                "fetched_at": fetched_at,
                "error": str(e),
            }
        daily_remaining = decoded.get("daily_remaining_percent")
        weekly_remaining = decoded.get("weekly_remaining_percent")
        return {
            "daily_used_percent": (100 - daily_remaining) if daily_remaining is not None else None,
            "weekly_used_percent": (100 - weekly_remaining) if weekly_remaining is not None else None,
            "weekly_reset_unix": decoded.get("weekly_reset_unix"),
            "plan_name": decoded.get("plan_name"),
            "fetched_at": fetched_at,
            "error": None,
        }

    @router.get("/api/usage/codex")
    async def get_codex_usage(refresh: bool = False):
        """ChatGPT allowance for the local Codex account, independently of Devin."""
        return JSONResponse(
            await asyncio.to_thread(get_codex_usage_provider().snapshot, force=refresh),
            headers={"Cache-Control": "no-store"},
        )

    return router
