"""Transactional, idempotent FilaScan inventory import."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import flag_modified

from app.core.event_bus import event_bus
from app.core.security import Principal
from app.models import (
    Color,
    Device,
    Filament,
    FilamentColor,
    Location,
    Manufacturer,
    Spool,
    SpoolStatus,
    SystemExtraField,
)
from app.services.spool_service import SpoolService

from .schemas import (
    ArchiveSpoolResponse,
    BambuSpoolImport,
    ImportResponse,
    MoveLocationResponse,
)


logger = logging.getLogger(__name__)
PLUGIN_KEY = "filascan_import"
BAMBU_EXTERNAL_ID_PREFIX = "bambulab:"
_BAMBU_PRODUCT_CODE_RE = re.compile(r"(?<!\d)(\d{5})(?!\d)")
_FILAMENT_FAMILY_NAMES = {
    "hf": "Hf",
    "silk+": "Silk Plus",
    "silk plus": "Silk Plus",
}


class ImportConflict(Exception):
    """The Tray UUID already belongs to incompatible inventory data."""


class ImportUnavailable(Exception):
    """FilaMan is missing a prerequisite required for the import."""


class InvalidStorageLocation(Exception):
    """The selected location cannot hold a user-managed spool."""


class SpoolNotFound(Exception):
    """The requested spool does not exist."""


class StorageLocationNotFound(Exception):
    """The requested storage location does not exist."""


class ArchiveUnavailable(Exception):
    """FilaMan is missing the system status required for archiving."""


@dataclass
class ImportResult:
    response: ImportResponse
    http_status: int


def canonical_external_id(tray_uuid: str) -> str:
    return f"{BAMBU_EXTERNAL_ID_PREFIX}{tray_uuid.upper()}"


def _legacy_filament_designation(data: BambuSpoolImport) -> str:
    return (
        f"{data.official_material_name} · {data.color_name} "
        f"[{data.material_id}-{data.variant_id}]"
    )


def _is_legacy_generated_designation(
    filament: Filament, data: BambuSpoolImport
) -> bool:
    """Recognize the old technical fallback even if its material label changed."""
    suffix = re.escape(f"[{data.material_id}-{data.variant_id}]")
    match = re.fullmatch(
        rf".+ · (?P<color>.+) {suffix}",
        filament.designation,
        flags=re.IGNORECASE,
    )
    return bool(
        match
        and match.group("color").strip().casefold()
        == data.color_name.strip().casefold()
    )


def _filament_family(data: BambuSpoolImport) -> str:
    """Return the FilamentDB-style family name for generated Bambu entries."""
    value = data.detailed_filament_type or data.official_material_name
    value = re.sub(r"^Bambu(?:\s+Lab)?\s+", "", value, flags=re.IGNORECASE)
    value = re.sub(
        rf"^{re.escape(data.filament_type)}(?:\s+|-)?",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip(" -")
    if value.casefold() == "basic":
        return ""
    return _FILAMENT_FAMILY_NAMES.get(value.casefold(), value)


def _color_label(data: BambuSpoolImport) -> str:
    product_code = _product_code(data.bambu_color_code)
    if product_code and _product_code(data.color_name) != product_code:
        return f"{data.color_name} ({product_code})"
    return data.color_name


def _material_subgroup(data: BambuSpoolImport) -> str | None:
    family = _filament_family(data)
    if not family:
        return None
    return re.sub(r"[^a-z0-9]+", "-", family.casefold()).strip("-")


def _filament_designation(data: BambuSpoolImport) -> str:
    family = _filament_family(data)
    color_label = _color_label(data)
    return f"{family} - {color_label}" if family else color_label


def _rgb(rgba: str) -> str:
    return rgba[:7]


def _product_code(value: object) -> str | None:
    match = _BAMBU_PRODUCT_CODE_RE.search(str(value or ""))
    return match.group(1) if match else None


def _filament_product_codes(filament: Filament) -> set[str]:
    values = (
        filament.designation,
        filament.manufacturer_color_name,
        (filament.custom_fields or {}).get("bambu_color_code"),
        (filament.custom_fields or {}).get("bambu_product_code"),
    )
    return {code for value in values if (code := _product_code(value))}


def _filamentdb_id(filament: Filament) -> int | None:
    try:
        return int((filament.custom_fields or {}).get("filamentdb_id"))
    except (TypeError, ValueError):
        return None


def _canonical_catalog_candidate(candidates: list[Filament]) -> Filament:
    """Prefer the canonical FilamentDB row among duplicate product records."""
    return min(
        candidates,
        key=lambda item: (
            _filamentdb_id(item) is None,
            _filamentdb_id(item) or item.id,
            item.id,
        ),
    )


def _filament_custom_fields(data: BambuSpoolImport) -> dict:
    return {
        "bambu_material_id": data.material_id,
        "bambu_variant_id": data.variant_id,
        "bambu_color_code": data.bambu_color_code,
        "bambu_product_code": _product_code(data.bambu_color_code),
        "bambu_detailed_filament_type": data.detailed_filament_type,
        "bambu_primary_rgba": data.primary_rgba,
        "bambu_secondary_rgba": data.secondary_rgba,
        "bambu_nozzle_temperature_min_c": data.nozzle_temperature_min_c,
        "bambu_nozzle_temperature_max_c": data.nozzle_temperature_max_c,
        "bambu_bed_temperature_c": data.bed_temperature_c,
        "bambu_drying_temperature_c": data.drying_temperature_c,
        "bambu_drying_time_h": data.drying_time_h,
    }


def _spool_custom_fields(data: BambuSpoolImport) -> dict:
    return {
        "bambu_tray_uuid": data.external_id,
        "bambu_material_id": data.material_id,
        "bambu_variant_id": data.variant_id,
        "bambu_color_code": data.bambu_color_code,
        "bambu_product_code": _product_code(data.bambu_color_code),
        "bambu_color_name": data.color_name,
        "bambu_primary_rgba": data.primary_rgba,
        "bambu_secondary_rgba": data.secondary_rgba,
        "bambu_filament_length_m": data.filament_length_m,
        "bambu_production_date": data.production_date,
        "bambu_nozzle_temperature_min_c": data.nozzle_temperature_min_c,
        "bambu_nozzle_temperature_max_c": data.nozzle_temperature_max_c,
        "bambu_bed_temperature_c": data.bed_temperature_c,
        "bambu_drying_temperature_c": data.drying_temperature_c,
        "bambu_drying_time_h": data.drying_time_h,
    }


EXTRA_FIELD_DEFINITIONS = (
    ("spool", "bambu_tray_uuid", "Bambu Tray UUID", "text"),
    ("spool", "bambu_material_id", "Bambu Material ID", "text"),
    ("spool", "bambu_variant_id", "Bambu Variant ID", "text"),
    ("spool", "bambu_color_code", "Bambu Color Code", "text"),
    ("spool", "bambu_color_name", "Bambu Color Name", "text"),
    ("spool", "bambu_primary_rgba", "Bambu Primary RGBA", "text"),
    ("spool", "bambu_secondary_rgba", "Bambu Secondary RGBA", "text"),
    ("spool", "bambu_filament_length_m", "Filament Length", "number"),
    ("spool", "bambu_production_date", "Bambu Production Date", "text"),
    ("filament", "bambu_material_id", "Bambu Material ID", "text"),
    ("filament", "bambu_variant_id", "Bambu Variant ID", "text"),
    ("filament", "bambu_color_code", "Bambu Color Code", "text"),
    ("filament", "bambu_detailed_filament_type", "Bambu Filament Type", "text"),
    ("filament", "bambu_primary_rgba", "Bambu Primary RGBA", "text"),
    ("filament", "bambu_secondary_rgba", "Bambu Secondary RGBA", "text"),
)


async def _ensure_extra_fields(db: AsyncSession) -> None:
    result = await db.execute(select(SystemExtraField))
    existing = {(field.target_type, field.key) for field in result.scalars().all()}
    for target_type, key, label, field_type in EXTRA_FIELD_DEFINITIONS:
        if (target_type, key) not in existing:
            db.add(
                SystemExtraField(
                    target_type=target_type,
                    key=key,
                    label=label,
                    field_type=field_type,
                    source=PLUGIN_KEY,
                )
            )


async def _load_spool(db: AsyncSession, external_id: str) -> Spool | None:
    result = await db.execute(
        select(Spool)
        .where(Spool.external_id == external_id)
        .options(
            selectinload(Spool.filament).selectinload(Filament.manufacturer),
            selectinload(Spool.filament)
            .selectinload(Filament.filament_colors)
            .selectinload(FilamentColor.color),
        )
    )
    return result.scalar_one_or_none()


async def _find_catalog_filament(
    db: AsyncSession,
    manufacturer_id: int,
    data: BambuSpoolImport,
) -> Filament | None:
    """Find a Bambu product by its five-digit catalog code.

    The maintained Bambu RFID and shop mappings use each five-digit code for
    one product. FilamentDB can still contain duplicate rows for that product,
    so equivalent rows are collapsed deterministically.
    """
    incoming_code = _product_code(data.bambu_color_code)
    if incoming_code is None:
        return None

    result = await db.execute(
        select(Filament)
        .where(
            Filament.manufacturer_id == manufacturer_id,
            func.lower(Filament.material_type) == data.filament_type.casefold(),
        )
        .options(
            selectinload(Filament.manufacturer),
            selectinload(Filament.filament_colors).selectinload(
                FilamentColor.color
            ),
        )
    )
    candidates: list[Filament] = []
    for candidate in result.scalars().unique().all():
        if incoming_code not in _filament_product_codes(candidate):
            continue
        fields = candidate.custom_fields or {}
        material_id = str(fields.get("bambu_material_id") or "").upper()
        variant_id = str(fields.get("bambu_variant_id") or "").upper()
        if material_id and material_id != data.material_id:
            continue
        if variant_id and variant_id != data.variant_id:
            continue
        candidates.append(candidate)

    return _canonical_catalog_candidate(candidates) if candidates else None


def _ordered_color_ids(filament: Filament) -> list[int]:
    return [
        item.color_id
        for item in sorted(filament.filament_colors, key=lambda item: item.position)
    ]


def _assert_compatible(spool: Spool, data: BambuSpoolImport) -> None:
    filament = spool.filament
    manufacturer = filament.manufacturer
    if manufacturer.name.casefold() != data.manufacturer.casefold():
        raise ImportConflict(
            "Existing spool belongs to an incompatible manufacturer"
        )
    if filament.material_type.casefold() != data.filament_type.casefold():
        raise ImportConflict("Existing spool belongs to an incompatible material type")

    existing_rgbs = [
        item.color.hex_code.upper()
        for item in sorted(filament.filament_colors, key=lambda item: item.position)
        if item.color is not None
    ]
    expected_rgbs = [_rgb(data.primary_rgba)]
    if data.secondary_rgba:
        expected_rgbs.append(_rgb(data.secondary_rgba))
    if existing_rgbs and existing_rgbs != expected_rgbs:
        raise ImportConflict("Existing spool has incompatible ordered colors")

    fields = {**(spool.filament.custom_fields or {}), **(spool.custom_fields or {})}
    comparisons = {
        "bambu_material_id": data.material_id,
        "bambu_variant_id": data.variant_id,
        "bambu_primary_rgba": data.primary_rgba,
        "bambu_secondary_rgba": data.secondary_rgba,
    }
    for key, expected in comparisons.items():
        actual = fields.get(key)
        if actual not in (None, "") and str(actual).upper() != str(expected or "").upper():
            raise ImportConflict(
                f"Existing spool has incompatible {key} for this Tray UUID"
            )


def _merge_missing(target: dict | None, incoming: dict) -> tuple[dict, bool]:
    merged = dict(target or {})
    changed = False
    for key, value in incoming.items():
        if value is not None and merged.get(key) in (None, ""):
            merged[key] = value
            changed = True
    return merged, changed


def _is_filascan_generated_filament(
    filament: Filament, data: BambuSpoolImport
) -> bool:
    fields = filament.custom_fields or {}
    return (
        (
            filament.designation.casefold() == _filament_designation(data).casefold()
            or _is_legacy_generated_designation(filament, data)
        )
        and str(fields.get("bambu_material_id") or "").upper() == data.material_id
        and str(fields.get("bambu_variant_id") or "").upper() == data.variant_id
    )


def _migrate_legacy_generated_filament(
    filament: Filament, data: BambuSpoolImport
) -> bool:
    """Rename only a structurally verified legacy fallback created by this plugin."""
    if (
        not _is_legacy_generated_designation(filament, data)
        or not _is_filascan_generated_filament(filament, data)
    ):
        return False
    filament.designation = _filament_designation(data)
    filament.manufacturer_color_name = _color_label(data)
    filament.material_subgroup = _material_subgroup(data)
    return True


def _merge_filament_metadata(filament: Filament, data: BambuSpoolImport) -> bool:
    fields, changed = _merge_missing(
        filament.custom_fields, _filament_custom_fields(data)
    )
    if changed:
        filament.custom_fields = fields
        flag_modified(filament, "custom_fields")
    return changed


def _existing_response(spool: Spool, data: BambuSpoolImport, status: str) -> ImportResponse:
    return ImportResponse(
        status=status,
        spool_id=spool.id,
        filament_id=spool.filament_id,
        manufacturer_id=spool.filament.manufacturer_id,
        color_ids=_ordered_color_ids(spool.filament),
        external_id=spool.external_id or canonical_external_id(data.external_id),
        tray_uuid=data.external_id,
    )


async def _resolve_existing(
    db: AsyncSession,
    spool: Spool,
    data: BambuSpoolImport,
    device: Device,
) -> tuple[ImportResponse, bool]:
    _assert_compatible(spool, data)
    changed = False

    catalog_filament = await _find_catalog_filament(
        db, spool.filament.manufacturer_id, data
    )
    if (
        catalog_filament is not None
        and catalog_filament.id != spool.filament_id
        and _is_filascan_generated_filament(spool.filament, data)
    ):
        spool.filament_id = catalog_filament.id
        spool.filament = catalog_filament
        changed = True

    if _migrate_legacy_generated_filament(spool.filament, data):
        changed = True

    spool_fields, spool_changed = _merge_missing(
        spool.custom_fields, _spool_custom_fields(data)
    )
    if spool_changed:
        spool.custom_fields = spool_fields
        flag_modified(spool, "custom_fields")
        changed = True

    if _merge_filament_metadata(spool.filament, data):
        changed = True

    archived_status = await _get_status_by_key(db, "archived")
    if archived_status is not None and spool.status_id == archived_status.id:
        active_status = await _resolve_status(db)
        await SpoolService(db).change_status(
            spool=spool,
            status_key=active_status.key,
            event_at=datetime.now(timezone.utc),
            principal=_device_principal(device),
            source="filascan",
            note=f"Restored by FilaScan device {device.name}",
        )
        changed = True

    return _existing_response(spool, data, "updated" if changed else "existing"), changed


async def _get_status_by_key(
    db: AsyncSession, status_key: str
) -> SpoolStatus | None:
    result = await db.execute(
        select(SpoolStatus).where(SpoolStatus.key == status_key)
    )
    return result.scalar_one_or_none()


async def _resolve_status(db: AsyncSession) -> SpoolStatus:
    result = await db.execute(
        select(SpoolStatus)
        .where(SpoolStatus.key != "archived")
        .order_by(
            case(
                (SpoolStatus.key == "new", 0),
                (SpoolStatus.key == "opened", 1),
                else_=2,
            ),
            SpoolStatus.id,
        )
    )
    status = result.scalars().first()
    if status is None:
        raise ImportUnavailable("FilaMan has no spool statuses")
    return status


def _is_bambu_driver_managed(location: Location) -> bool:
    managed_by = str((location.custom_fields or {}).get("managed_by") or "")
    return managed_by.strip().casefold() in {
        "bambulab",
        "bambulab_plugin",
        "bambu_lab",
        "bambu_lab_plugin",
    }


def _device_principal(device: Device) -> Principal:
    return Principal(
        auth_type="device",
        device_id=device.id,
        scopes=device.scopes,
    )


def _assert_storage_location(location: Location) -> None:
    identifier = str(location.identifier or "").strip()
    if identifier.casefold().startswith(BAMBU_EXTERNAL_ID_PREFIX.rstrip(":")):
        raise InvalidStorageLocation(
            "The selected location is managed by a printer driver"
        )
    if location.is_driver_managed or _is_bambu_driver_managed(location):
        raise InvalidStorageLocation(
            "The selected location is managed by a printer driver"
        )


async def _validate_storage_location(
    db: AsyncSession, location_id: int
) -> Location:
    location = await db.get(Location, location_id)
    if location is None:
        raise InvalidStorageLocation("The selected storage location does not exist")

    _assert_storage_location(location)
    return location


async def move_spool_location(
    db: AsyncSession,
    spool_id: int,
    location_id: int,
    device: Device,
) -> MoveLocationResponse:
    spool = await db.get(Spool, spool_id)
    if spool is None:
        raise SpoolNotFound("The requested spool does not exist")

    location = await db.get(Location, location_id)
    if location is None:
        raise StorageLocationNotFound("The selected storage location does not exist")

    _assert_storage_location(location)

    response = MoveLocationResponse(
        spool_id=spool.id,
        location_id=location.id,
        location_name=location.name,
    )
    if spool.location_id == location.id:
        return response

    await SpoolService(db).move_location(
        spool=spool,
        to_location_id=location.id,
        event_at=datetime.now(timezone.utc),
        principal=_device_principal(device),
        source="filascan",
        note=f"Located by FilaScan device {device.name}",
    )
    try:
        await event_bus.publish({"event": "spools_changed"})
    except Exception:
        logger.warning(
            "FilaScan location move committed but spools_changed publication failed"
        )
    return response


async def archive_spool(
    db: AsyncSession,
    spool_id: int,
    device: Device,
) -> ArchiveSpoolResponse:
    spool = await db.get(Spool, spool_id)
    if spool is None:
        raise SpoolNotFound("The requested spool does not exist")

    archived_status = await _get_status_by_key(db, "archived")
    if archived_status is None:
        raise ArchiveUnavailable("FilaMan has no archived spool status")

    response = ArchiveSpoolResponse(spool_id=spool.id)
    if spool.status_id == archived_status.id:
        return response

    await SpoolService(db).change_status(
        spool=spool,
        status_key="archived",
        event_at=datetime.now(timezone.utc),
        principal=_device_principal(device),
        source="filascan",
        note=f"Archived by FilaScan device {device.name}",
    )
    try:
        await event_bus.publish({"event": "spools_changed"})
    except Exception:
        logger.warning(
            "FilaScan spool archive committed but spools_changed publication failed"
        )
    return response


async def _resolve_manufacturer(
    db: AsyncSession, data: BambuSpoolImport
) -> tuple[Manufacturer, bool]:
    result = await db.execute(
        select(Manufacturer).where(
            func.lower(Manufacturer.name) == data.manufacturer.casefold()
        )
    )
    manufacturer = result.scalar_one_or_none()
    if manufacturer is not None:
        return manufacturer, False
    manufacturer = Manufacturer(
        name=data.manufacturer,
        url="https://bambulab.com" if data.manufacturer == "Bambu Lab" else None,
    )
    db.add(manufacturer)
    await db.flush()
    return manufacturer, True


async def _resolve_color(
    db: AsyncSession, name: str, rgba: str
) -> tuple[Color, bool]:
    rgb = _rgb(rgba)
    result = await db.execute(
        select(Color).where(
            func.lower(Color.name) == name.casefold(),
            func.lower(Color.hex_code) == rgb.casefold(),
        )
    )
    color = result.scalar_one_or_none()
    if color is not None:
        return color, False
    color = Color(name=name, hex_code=rgb, custom_fields={"bambu_rgba": rgba})
    db.add(color)
    await db.flush()
    return color, True


async def _resolve_filament(
    db: AsyncSession,
    manufacturer: Manufacturer,
    colors: list[Color],
    data: BambuSpoolImport,
) -> tuple[Filament, bool]:
    designation = _filament_designation(data)
    result = await db.execute(
        select(Filament)
        .where(
            Filament.manufacturer_id == manufacturer.id,
            func.lower(Filament.designation) == designation.casefold(),
        )
        .options(selectinload(Filament.filament_colors))
    )
    matches = list(result.scalars().unique().all())
    if len(matches) > 1:
        raise ImportConflict("Multiple filaments match the Bambu product identity")
    if matches:
        filament = matches[0]
        existing_color_ids = _ordered_color_ids(filament)
        expected_color_ids = [color.id for color in colors]
        if existing_color_ids != expected_color_ids:
            raise ImportConflict("Existing filament has incompatible ordered colors")
        return filament, False

    filament = Filament(
        manufacturer_id=manufacturer.id,
        designation=designation,
        material_type=data.filament_type.upper(),
        material_subgroup=_material_subgroup(data),
        diameter_mm=data.diameter_mm,
        manufacturer_color_name=_color_label(data),
        raw_material_weight_g=data.weight_g,
        spool_width_mm=data.spool_width_mm,
        shop_url=data.product_image_url,
        color_mode="multi" if len(colors) > 1 else "single",
        multi_color_style="striped" if len(colors) > 1 else None,
        custom_fields=_filament_custom_fields(data),
    )
    db.add(filament)
    await db.flush()
    for position, color in enumerate(colors, start=1):
        db.add(
            FilamentColor(
                filament_id=filament.id,
                color_id=color.id,
                position=position,
            )
        )
    await db.flush()
    await db.refresh(filament, attribute_names=["filament_colors"])
    return filament, True


def _create_spool_record(
    db: AsyncSession,
    filament: Filament,
    status: SpoolStatus,
    external_id: str,
    data: BambuSpoolImport,
) -> Spool:
    spool = Spool(
        filament_id=filament.id,
        status_id=status.id,
        location_id=data.location_id,
        external_id=external_id,
        stocked_in_at=datetime.now(timezone.utc),
        initial_total_weight_g=data.weight_g,
        remaining_weight_g=data.weight_g,
        spool_width_mm=data.spool_width_mm,
        custom_fields=_spool_custom_fields(data),
    )
    db.add(spool)
    return spool


async def _import_once(
    db: AsyncSession,
    data: BambuSpoolImport,
    external_id: str,
    device: Device,
) -> tuple[ImportResponse, bool]:
    existing = await _load_spool(db, external_id)
    if existing is not None:
        response, changed = await _resolve_existing(db, existing, data, device)
        logger.info(
            "FilaScan inventory resolution external_id=%s manufacturer=resolved colors=resolved filament=resolved spool=%s",
            external_id,
            response.status,
        )
        return response, changed

    await _validate_storage_location(db, data.location_id)
    status = await _resolve_status(db)
    await _ensure_extra_fields(db)
    manufacturer, manufacturer_created = await _resolve_manufacturer(db, data)
    filament = await _find_catalog_filament(db, manufacturer.id, data)
    if filament is not None:
        _merge_filament_metadata(filament, data)
        colors = [
            item.color
            for item in sorted(
                filament.filament_colors, key=lambda item: item.position
            )
            if item.color is not None
        ]
        color_created = False
        filament_created = False
    else:
        primary, primary_created = await _resolve_color(
            db, data.color_name, data.primary_rgba
        )
        colors = [primary]
        color_created = primary_created
        if data.secondary_rgba:
            secondary, secondary_created = await _resolve_color(
                db, f"{data.color_name} (secondary)", data.secondary_rgba
            )
            colors.append(secondary)
            color_created = color_created or secondary_created

        filament, filament_created = await _resolve_filament(
            db, manufacturer, colors, data
        )
    spool = _create_spool_record(db, filament, status, external_id, data)
    await db.flush()
    await db.refresh(spool)
    logger.info(
        "FilaScan inventory resolution external_id=%s manufacturer=%s colors=%s filament=%s spool=created",
        external_id,
        "created" if manufacturer_created else "resolved",
        "created" if color_created else "resolved",
        "created" if filament_created else "resolved",
    )
    return (
        ImportResponse(
            status="created",
            spool_id=spool.id,
            filament_id=filament.id,
            manufacturer_id=manufacturer.id,
            color_ids=[color.id for color in colors],
            external_id=external_id,
            tray_uuid=data.external_id,
        ),
        True,
    )


async def import_bambu_spool(
    db: AsyncSession, data: BambuSpoolImport, device: Device
) -> ImportResult:
    external_id = canonical_external_id(data.external_id)
    for attempt in range(2):
        try:
            response, changed = await _import_once(db, data, external_id, device)
            await db.commit()
            if changed:
                await _publish_changes()
            return ImportResult(
                response=response,
                http_status=201 if response.status == "created" else 200,
            )
        except ImportConflict:
            await db.rollback()
            raise
        except IntegrityError:
            await db.rollback()
            existing = await _load_spool(db, external_id)
            if existing is not None:
                response, changed = await _resolve_existing(
                    db, existing, data, device
                )
                await db.commit()
                if changed:
                    await _publish_changes()
                return ImportResult(response=response, http_status=200)
            if attempt == 0:
                continue
            raise
        except Exception:
            await db.rollback()
            raise
    raise RuntimeError("FilaScan import retry exhausted")


async def _publish_changes() -> None:
    try:
        await event_bus.publish({"event": "filaments_changed"})
        await event_bus.publish({"event": "spools_changed"})
    except Exception:
        # Inventory is already committed. A best-effort UI refresh must never
        # turn a successful idempotent import into a misleading HTTP 500.
        logger.warning("FilaScan import committed but change event publication failed")
