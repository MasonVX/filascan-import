"""Administrator API for choosing devices allowed to import FilaScan spools."""

from pydantic import BaseModel, ConfigDict, Field
from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.api.deps import DBSession, RequirePermission
from app.models import Device

from . import __version__
from .state import load_allowed_device_ids, save_allowed_device_ids


admin_router = APIRouter(prefix="/filascan-import", tags=["FilaScan Import Admin"])


class AllowlistUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed_device_ids: list[int] = Field(default_factory=list, max_length=500)


def _device_payload(device: Device, allowed: set[int]) -> dict:
    return {
        "id": device.id,
        "name": device.name,
        "device_type": device.device_type,
        "is_active": device.is_active,
        "allowed": device.id in allowed,
    }


@admin_router.get("/config")
async def get_config(
    db: DBSession,
    principal=RequirePermission("admin:devices_manage"),
):
    result = await db.execute(
        select(Device)
        .where(Device.deleted_at.is_(None))
        .order_by(Device.name, Device.id)
    )
    devices = list(result.scalars().all())
    allowed = load_allowed_device_ids()
    return {
        "plugin_version": __version__,
        "allowed_device_ids": sorted(allowed),
        "devices": [_device_payload(device, allowed) for device in devices],
    }


@admin_router.put("/config")
async def update_config(
    payload: AllowlistUpdate,
    db: DBSession,
    principal=RequirePermission("admin:devices_manage"),
):
    requested = set(payload.allowed_device_ids)
    if any(device_id <= 0 for device_id in requested):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_device_id", "message": "Device IDs must be positive"},
        )
    if requested:
        result = await db.execute(
            select(Device.id).where(
                Device.id.in_(requested),
                Device.is_active.is_(True),
                Device.deleted_at.is_(None),
            )
        )
        existing = set(result.scalars().all())
        missing = sorted(requested - existing)
        if missing:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_device_allowlist",
                    "message": f"Unknown or inactive device IDs: {missing}",
                },
            )
    save_allowed_device_ids(requested)
    return {"allowed_device_ids": sorted(requested)}
