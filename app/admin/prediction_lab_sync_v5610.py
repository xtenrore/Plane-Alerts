"""Admin-only HTTP bridge used by the external Prediction Lab repository sync."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app.admin.routes import _check_auth
from app.prediction_lab_sync_bridge_v5610 import (
    BRIDGE_VERSION,
    MAX_BATCH_BYTES,
    MAX_BATCH_FILES,
    SyncBridgeError,
    acknowledge,
    build_batch_zip,
    clear_sync_guard,
    status,
)

router = APIRouter(tags=["admin-prediction-lab-sync"])


class AckItem(BaseModel):
    relative: str
    bytes: int = Field(ge=0)
    sha256: str


class AckRequest(BaseModel):
    files: list[AckItem] = Field(default_factory=list, max_length=MAX_BATCH_FILES)


def _cleanup_archive(path: Path) -> None:
    path.unlink(missing_ok=True)
    # If the caller downloaded the bundle but never acknowledges it, the guard is
    # intentionally kept until its TTL. FileResponse cleanup therefore removes
    # only the ephemeral archive, not the sync guard.


@router.get("/api/prediction-lab/sync-status")
async def prediction_lab_sync_status(_: None = Depends(_check_auth)) -> dict[str, Any]:
    result = await asyncio.to_thread(status)
    return result


@router.get("/api/prediction-lab/sync-batch", response_class=FileResponse)
async def prediction_lab_sync_batch(
    max_files: int = Query(default=MAX_BATCH_FILES, ge=1, le=MAX_BATCH_FILES),
    max_bytes: int = Query(default=MAX_BATCH_BYTES, ge=1, le=MAX_BATCH_BYTES),
    _: None = Depends(_check_auth),
) -> FileResponse:
    try:
        archive, manifest = await asyncio.to_thread(
            build_batch_zip,
            max_files=max_files,
            max_bytes=max_bytes,
        )
    except SyncBridgeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        clear_sync_guard()
        raise HTTPException(status_code=503, detail=f"Prediction Lab sync export unavailable: {type(exc).__name__}") from exc
    return FileResponse(
        archive,
        media_type="application/zip",
        filename=f"plane-alerts-prediction-lab-{BRIDGE_VERSION}.zip",
        headers={
            "Cache-Control": "no-store",
            "X-Plane-Alerts-Sync-Bridge": BRIDGE_VERSION,
            "X-Plane-Alerts-Sync-Files": str(manifest["total_files"]),
            "X-Plane-Alerts-Sync-Bytes": str(manifest["total_bytes"]),
        },
        background=BackgroundTask(_cleanup_archive, archive),
    )


@router.post("/api/prediction-lab/sync-ack")
async def prediction_lab_sync_ack(
    body: AckRequest,
    _: None = Depends(_check_auth),
) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(
            acknowledge,
            [item.model_dump() for item in body.files],
        )
    except SyncBridgeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        clear_sync_guard()
        raise HTTPException(status_code=503, detail=f"Prediction Lab acknowledgement unavailable: {type(exc).__name__}") from exc
    return result
