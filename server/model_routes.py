"""Model catalogue discovery and user metadata routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from .model_registry import ModelRegistry


class ModelUpdate(BaseModel):
    """Allowed fields for PATCH/PUT updates to model metadata.

    Only these fields are forwarded to ``model_registry.update_model``;
    any other keys in the request body are rejected by Pydantic.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None
    vision: bool | None = None
    tags: list[str] | None = None
    description: str | None = None


class ModelIdList(BaseModel):
    """Request body for bulk exclude/include operations on models."""

    model_ids: list[str]


def create_model_router(*, get_registry: Callable[[], ModelRegistry]) -> APIRouter:
    router = APIRouter()

    @router.get("/api/models")
    async def get_models():
        get_registry().refresh()
        await get_registry().refresh_from_cli()
        return {
            "models": get_registry().get_all_models(),
            "selectable": get_registry().get_selectable_models(),
            "recent": get_registry().get_recently_used(),
            "last_used": get_registry().get_last_used_model(),
            "last_used_variants": get_registry().get_last_used_variants(),
            "has_new": get_registry().has_new_models(),
            "cache_info": get_registry().get_cache_info(),
        }

    @router.post("/api/models/bulk-exclude")
    async def bulk_exclude_models(body: ModelIdList):
        get_registry().exclude_models(body.model_ids)
        return {"ok": True}

    @router.post("/api/models/bulk-include")
    async def bulk_include_models(body: ModelIdList):
        get_registry().include_models(body.model_ids)
        return {"ok": True}

    @router.post("/api/models/{model_id}/exclude")
    async def exclude_model(model_id: str):
        get_registry().exclude_model(model_id)
        return {"ok": True}

    @router.post("/api/models/{model_id}/include")
    async def include_model(model_id: str):
        get_registry().include_model(model_id)
        return {"ok": True}

    @router.post("/api/models/{model_id}/acknowledge")
    async def acknowledge_model(model_id: str):
        get_registry().acknowledge_new_model(model_id)
        return {"ok": True}

    @router.put("/api/models/{model_id}")
    async def update_model(model_id: str, body: ModelUpdate | None = None):
        updates = body.model_dump(exclude_none=True) if body else {}
        get_registry().update_model(model_id, updates)
        return {"ok": True}

    return router
