"""Narrow, independently verified authorization for FilaScan devices."""

import logging
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select

from app.api.deps import DBSession
from app.core.security import (
    is_argon2_hash,
    parse_token,
    verify_password_async,
    verify_token,
)
from app.models import Device


logger = logging.getLogger(__name__)
from .state import load_allowed_device_ids


def _unauthenticated(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "unauthenticated", "message": message},
    )


async def require_authorized_filascan_device(
    db: DBSession,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> Device:
    if not authorization or not authorization.startswith("Device "):
        raise _unauthenticated("Device authentication required")
    parsed = parse_token(authorization[7:])
    if parsed is None or parsed[0] != "dev":
        raise _unauthenticated("Invalid device token")
    _, device_id, secret = parsed

    result = await db.execute(
        select(Device).where(
            Device.id == device_id,
            Device.is_active.is_(True),
            Device.deleted_at.is_(None),
        )
    )
    device = result.scalar_one_or_none()
    if device is None:
        raise _unauthenticated("Device is inactive or unavailable")

    valid_secret = (
        await verify_password_async(secret, device.token_hash)
        if is_argon2_hash(device.token_hash)
        else verify_token(secret, device.token_hash)
    )
    if not valid_secret:
        raise _unauthenticated("Invalid device token")

    if device.id not in load_allowed_device_ids():
        logger.warning("FilaScan import denied for device_id=%s", device.id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "filascan_device_not_allowed",
                "message": "This device is not authorized for FilaScan imports",
            },
        )
    return device


AuthorizedFilaScanDevice = Annotated[
    Device, Depends(require_authorized_filascan_device)
]
