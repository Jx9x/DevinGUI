from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

from fastapi import APIRouter

from .db_reader import DBReader
from .diagnostics import collect_diagnostics


def create_diagnostics_router(*, get_db_reader: Callable[[], DBReader],
                              get_transcripts_dir: Callable[[], str], frontend_dist: str | Path) -> APIRouter:
    router = APIRouter()

    @router.get("/api/diagnostics")
    async def diagnostics(refresh: bool = False):
        return await asyncio.to_thread(
            collect_diagnostics, get_db_reader(), get_transcripts_dir(), frontend_dist, refresh=refresh,
        )

    return router
