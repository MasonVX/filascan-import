"""Validated wire format for FilaScan spool imports."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


TRAY_UUID_LENGTH = 32


def _normalize_rgba(value: str) -> str:
    normalized = value.strip().upper()
    if (
        len(normalized) != 9
        or not normalized.startswith("#")
        or not all(character in "0123456789ABCDEF" for character in normalized[1:])
    ):
        raise ValueError("must use #RRGGBBAA")
    return normalized


class BambuSpoolImport(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    location_id: int = Field(gt=0)
    external_id: str = Field(min_length=TRAY_UUID_LENGTH, max_length=TRAY_UUID_LENGTH)
    manufacturer: str = Field(min_length=1, max_length=255)
    material_id: str = Field(min_length=1, max_length=50)
    variant_id: str = Field(min_length=1, max_length=50)
    filament_type: str = Field(min_length=1, max_length=50)
    detailed_filament_type: str = Field(min_length=1, max_length=100)
    official_material_name: str = Field(min_length=1, max_length=100)
    color_name: str = Field(min_length=1, max_length=100)
    bambu_color_code: str | None = Field(default=None, max_length=50)
    primary_rgba: str
    secondary_rgba: str | None = None
    product_image_url: str | None = Field(default=None, max_length=500)
    weight_g: float = Field(gt=0, le=100_000)
    diameter_mm: float = Field(gt=0, le=5)
    drying_temperature_c: float | None = Field(default=None, gt=0, le=500)
    drying_time_h: float | None = Field(default=None, gt=0, le=1_000)
    bed_temperature_c: float | None = Field(default=None, gt=0, le=500)
    nozzle_temperature_min_c: float | None = Field(default=None, gt=0, le=500)
    nozzle_temperature_max_c: float | None = Field(default=None, gt=0, le=500)
    spool_width_mm: float | None = Field(default=None, gt=0, le=1_000)
    filament_length_m: float | None = Field(default=None, gt=0, le=1_000_000)
    production_date: str | None = Field(default=None, max_length=64)

    @field_validator("external_id")
    @classmethod
    def validate_external_id(cls, value: str) -> str:
        normalized = value.upper()
        if not all(character in "0123456789ABCDEF" for character in normalized):
            raise ValueError("must contain exactly 32 hexadecimal characters")
        if set(normalized) == {"0"}:
            raise ValueError("all-zero Tray UUID is not a spool identity")
        return normalized

    @field_validator("manufacturer")
    @classmethod
    def normalize_manufacturer(cls, value: str) -> str:
        if value.casefold() not in {"bambu", "bambu lab"}:
            raise ValueError("type=bambu only accepts manufacturer Bambu Lab")
        return "Bambu Lab"

    @field_validator("material_id", "variant_id")
    @classmethod
    def normalize_bambu_identifiers(cls, value: str) -> str:
        return value.upper()

    @field_validator("primary_rgba", "secondary_rgba")
    @classmethod
    def validate_rgba(cls, value: str | None) -> str | None:
        return None if value is None else _normalize_rgba(value)

    @field_validator(
        "drying_temperature_c",
        "drying_time_h",
        "bed_temperature_c",
        "nozzle_temperature_min_c",
        "nozzle_temperature_max_c",
        "spool_width_mm",
        "filament_length_m",
        mode="before",
    )
    @classmethod
    def zero_means_unknown(cls, value):
        if value in (0, 0.0, "0", "0.0", ""):
            return None
        return value

    @field_validator("product_image_url")
    @classmethod
    def validate_product_image_url(cls, value: str | None) -> str | None:
        if value is None or not value:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("must be an absolute HTTP(S) URL")
        return value

    @field_validator("nozzle_temperature_max_c")
    @classmethod
    def validate_nozzle_range(cls, value: float | None, info):
        minimum = info.data.get("nozzle_temperature_min_c")
        if value is not None and minimum is not None and value < minimum:
            raise ValueError("must be greater than or equal to nozzle_temperature_min_c")
        return value


class ImportResponse(BaseModel):
    status: Literal["created", "existing", "updated"]
    spool_id: int
    filament_id: int
    manufacturer_id: int
    color_ids: list[int]
    external_id: str
    tray_uuid: str


class StatusResponse(BaseModel):
    status: Literal["ready"] = "ready"
    plugin: Literal["filascan_import"] = "filascan_import"
    version: str
    device_id: int
    bambu_import: Literal[True] = True
    location_selection: Literal[True] = True
    location_management: Literal[True] = True
    spool_archiving: Literal[True] = True


class MoveLocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location_id: int = Field(gt=0)


class MoveLocationResponse(BaseModel):
    status: Literal["moved"] = "moved"
    spool_id: int
    location_id: int
    location_name: str


class ArchiveSpoolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArchiveSpoolResponse(BaseModel):
    status: Literal["archived"] = "archived"
    spool_id: int
