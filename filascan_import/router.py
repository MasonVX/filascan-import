"""Device-authenticated API routes for FilaScan imports."""

import logging

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.api.deps import DBSession

from . import __version__
from .admin import admin_router
from .auth import AuthorizedFilaScanDevice
from .schemas import (
    ArchiveSpoolRequest,
    ArchiveSpoolResponse,
    BambuSpoolImport,
    ImportResponse,
    MoveLocationRequest,
    MoveLocationResponse,
    StatusResponse,
)
from .service import (
    ArchiveUnavailable,
    ImportConflict,
    ImportUnavailable,
    InvalidStorageLocation,
    SpoolNotFound,
    StorageLocationNotFound,
    archive_spool,
    import_bambu_spool,
    move_spool_location,
)


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/devices/filascan", tags=["FilaScan Spool Import"])


def _validate_spool_type(spool_type: str | None) -> None:
    if spool_type != "bambu":
        display = spool_type if spool_type else "<missing>"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "unsupported_spool_type",
                "message": f"Unsupported spool type: {display}",
            },
        )


@router.get("/status", response_model=StatusResponse)
async def filascan_status(
    device: AuthorizedFilaScanDevice,
):
    return StatusResponse(version=__version__, device_id=device.id)


@router.post("/spools/{spool_id}/location", response_model=MoveLocationResponse)
async def move_existing_spool(
    spool_id: int,
    payload: MoveLocationRequest,
    db: DBSession,
    device: AuthorizedFilaScanDevice,
):
    logger.info(
        "FilaScan location move requested device_id=%s spool_id=%s location_id=%s",
        device.id,
        spool_id,
        payload.location_id,
    )
    try:
        result = await move_spool_location(
            db,
            spool_id=spool_id,
            location_id=payload.location_id,
            device=device,
        )
    except SpoolNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "spool_not_found", "message": str(exc)},
        ) from exc
    except StorageLocationNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "storage_location_not_found", "message": str(exc)},
        ) from exc
    except InvalidStorageLocation as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_storage_location", "message": str(exc)},
        ) from exc
    except Exception as exc:
        await db.rollback()
        logger.exception(
            "FilaScan location move failed device_id=%s spool_id=%s location_id=%s",
            device.id,
            spool_id,
            payload.location_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "location_move_failed",
                "message": "Spool location update failed",
            },
        ) from exc

    logger.info(
        "FilaScan location move completed device_id=%s spool_id=%s location_id=%s",
        device.id,
        result.spool_id,
        result.location_id,
    )
    return result


@router.post("/spools/{spool_id}/archive", response_model=ArchiveSpoolResponse)
async def archive_existing_spool(
    spool_id: int,
    payload: ArchiveSpoolRequest,
    db: DBSession,
    device: AuthorizedFilaScanDevice,
):
    logger.info(
        "FilaScan spool archive requested device_id=%s spool_id=%s",
        device.id,
        spool_id,
    )
    try:
        result = await archive_spool(db, spool_id=spool_id, device=device)
    except SpoolNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "spool_not_found", "message": str(exc)},
        ) from exc
    except ArchiveUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "archive_unavailable", "message": str(exc)},
        ) from exc
    except Exception as exc:
        await db.rollback()
        logger.exception(
            "FilaScan spool archive failed device_id=%s spool_id=%s",
            device.id,
            spool_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "spool_archive_failed",
                "message": "Spool archive failed",
            },
        ) from exc

    logger.info(
        "FilaScan spool archive completed device_id=%s spool_id=%s",
        device.id,
        result.spool_id,
    )
    return result


@router.post("/import-spool", response_model=ImportResponse)
async def import_spool(
    payload: BambuSpoolImport,
    response: Response,
    db: DBSession,
    device: AuthorizedFilaScanDevice,
    spool_type: str | None = Query(default=None, alias="type"),
):
    _validate_spool_type(spool_type)
    device_id = device.id
    logger.info(
        "FilaScan import requested device_id=%s type=%s external_id=bambulab:%s",
        device_id,
        spool_type,
        payload.external_id,
    )
    try:
        result = await import_bambu_spool(db, payload, device)
    except ImportConflict as exc:
        logger.warning(
            "FilaScan import conflict device_id=%s external_id=bambulab:%s",
            device_id,
            payload.external_id,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "spool_identity_conflict", "message": str(exc)},
        ) from exc
    except InvalidStorageLocation as exc:
        logger.warning(
            "FilaScan import rejected invalid location device_id=%s external_id=bambulab:%s location_id=%s",
            device_id,
            payload.external_id,
            payload.location_id,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_storage_location", "message": str(exc)},
        ) from exc
    except ImportUnavailable as exc:
        logger.error(
            "FilaScan import unavailable device_id=%s external_id=bambulab:%s",
            device_id,
            payload.external_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "import_unavailable", "message": str(exc)},
        ) from exc
    except Exception as exc:
        await db.rollback()
        logger.exception(
            "FilaScan import failed device_id=%s type=%s external_id=bambulab:%s",
            device_id,
            spool_type,
            payload.external_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "import_failed", "message": "Transactional spool import failed"},
        ) from exc

    response.status_code = result.http_status
    logger.info(
        "FilaScan import completed device_id=%s external_id=%s status=%s spool_id=%s",
        device_id,
        result.response.external_id,
        result.response.status,
        result.response.spool_id,
    )
    return result.response
