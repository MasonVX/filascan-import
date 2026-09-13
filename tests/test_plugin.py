import asyncio
import hashlib
import json
import logging
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import deps as deps_module
from app.core import middleware as middleware_module
from app.core.middleware import AuthMiddleware, invalidate_auth_caches
from app.core.security import hash_token
from app.models import (
    Base,
    Color,
    Device,
    Filament,
    FilamentColor,
    Location,
    Manufacturer,
    Spool,
    SpoolEvent,
    SpoolStatus,
    SystemExtraField,
)
from app.services.plugin_service import PluginInstallService

from filascan_import import service as service_module
from filascan_import import admin as admin_module
from filascan_import import state as state_module
from filascan_import.router import router


TRAY_UUID = "5332FC68F1A34865838C48D43615E74B"
OPENPRINTTAG_UUID = "8bc3b75d-4702-4c46-a9c1-2fc81ec03ebf"
DEVICE_SECRET = "scanner-secret"


def spool_payload(**overrides):
    payload = {
        "location_id": 1,
        "external_id": TRAY_UUID,
        "manufacturer": "Bambu Lab",
        "material_id": "GFA06",
        "variant_id": "A06-D1",
        "filament_type": "PLA",
        "detailed_filament_type": "PLA Silk+",
        "official_material_name": "Bambu PLA Silk+",
        "color_name": "Silver",
        "bambu_color_code": "13109",
        "primary_rgba": "#C8C8C8FF",
        "secondary_rgba": None,
        "product_image_url": "https://store.bblcdn.eu/example.png",
        "weight_g": 1000,
        "diameter_mm": 1.75,
        "drying_temperature_c": 55,
        "drying_time_h": 8,
        "bed_temperature_c": 55,
        "nozzle_temperature_min_c": 190,
        "nozzle_temperature_max_c": 240,
        "spool_width_mm": 66.25,
        "filament_length_m": 330,
        "production_date": "2025_03_15_09_34",
    }
    payload.update(overrides)
    return payload


def openprinttag_payload(**overrides):
    payload = {
        "location_id": 1,
        "external_id": OPENPRINTTAG_UUID,
        "manufacturer": "Prusament",
        "material_type": "PETG",
        "material_name": "PETG Prusa Galaxy Black",
        "color_name": "Prusa Galaxy Black",
        "primary_rgba": "#3D3E3DFF",
        "secondary_rgba": None,
        "product_url": "https://openprinttag.org/t/8bc3b75d-4702-4c46-a9c1-2fc81ec03ebf",
        "weight_g": 1000,
        "remaining_weight_g": 875,
        "diameter_mm": 1.75,
    }
    payload.update(overrides)
    return payload


class FilaScanPluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary.name) / "filaman.db"
        state_dir = Path(self.temporary.name) / "plugin_state"
        self.state_patchers = [
            patch.object(state_module, "STATE_DIR", state_dir),
            patch.object(state_module, "STATE_FILE", state_dir / "filascan_import.json"),
        ]
        for patcher in self.state_patchers:
            patcher.start()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{database_path}",
            connect_args={"timeout": 30},
        )

        @event.listens_for(self.engine.sync_engine, "connect")
        def configure_sqlite(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.sessions() as db:
            db.add_all(
                [
                    Device(
                        id=1,
                        name="FilaScan",
                        device_type="filascan",
                        token_hash=hash_token(DEVICE_SECRET),
                        scopes=None,
                        is_active=True,
                    ),
                    Device(
                        id=2,
                        name="Unrelated Scale",
                        device_type="scale",
                        token_hash=hash_token("scale-secret"),
                        scopes=["spools:create"],
                        is_active=True,
                    ),
                    Location(
                        id=1,
                        name="Shelf A",
                        identifier="shelf-a",
                    ),
                    SpoolStatus(
                        key="new", label="New", sort_order=1, is_system=True
                    ),
                    SpoolStatus(
                        key="opened", label="Opened", sort_order=2, is_system=True
                    ),
                    SpoolStatus(
                        key="archived",
                        label="Archived",
                        sort_order=6,
                        is_system=True,
                    ),
                ]
            )
            await db.commit()
        state_module.save_allowed_device_ids({1})

        async def override_get_db():
            async with self.sessions() as db:
                yield db

        self.app = FastAPI()
        self.app.include_router(router, prefix="/api/v1")
        self.app.dependency_overrides[deps_module.get_db] = override_get_db
        self.app.add_middleware(AuthMiddleware)
        self.middleware_patch = patch.object(
            middleware_module, "async_session_maker", self.sessions
        )
        self.middleware_patch.start()
        invalidate_auth_caches()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        invalidate_auth_caches()
        self.middleware_patch.stop()
        await self.engine.dispose()
        for patcher in reversed(self.state_patchers):
            patcher.stop()
        self.temporary.cleanup()

    @staticmethod
    def device_header(device_id=1, secret=DEVICE_SECRET):
        return {"Authorization": f"Device dev.{device_id}.{secret}"}

    async def import_request(self, payload=None, spool_type="bambu", headers=None):
        suffix = "" if spool_type is None else f"?type={spool_type}"
        return await self.client.post(
            f"/api/v1/devices/filascan/import-spool{suffix}",
            headers=headers or self.device_header(),
            json=payload or spool_payload(),
        )

    async def move_request(self, spool_id, location_id, headers=None):
        return await self.client.post(
            f"/api/v1/devices/filascan/spools/{spool_id}/location",
            headers=headers or self.device_header(),
            json={"location_id": location_id},
        )

    async def archive_request(self, spool_id, payload=None, headers=None):
        return await self.client.post(
            f"/api/v1/devices/filascan/spools/{spool_id}/archive",
            headers=headers or self.device_header(),
            json={} if payload is None else payload,
        )

    async def test_authorized_device_status(self):
        response = await self.client.get(
            "/api/v1/devices/filascan/status", headers=self.device_header()
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "status": "ready",
                "plugin": "filascan_import",
                "version": "0.2.0",
                "device_id": 1,
                "bambu_import": True,
                "openprinttag_import": True,
                "location_selection": True,
                "location_management": True,
                "spool_archiving": True,
            },
        )

    async def test_missing_malformed_wrong_and_revoked_tokens_return_401(self):
        requests = [
            await self.client.get("/api/v1/devices/filascan/status"),
            await self.client.get(
                "/api/v1/devices/filascan/status",
                headers={"Authorization": "Device malformed"},
            ),
            await self.client.get(
                "/api/v1/devices/filascan/status",
                headers=self.device_header(secret="wrong-secret"),
            ),
        ]
        async with self.sessions() as db:
            device = await db.get(Device, 1)
            device.is_active = False
            await db.commit()
        invalidate_auth_caches()
        requests.append(
            await self.client.get(
                "/api/v1/devices/filascan/status", headers=self.device_header()
            )
        )
        self.assertEqual([response.status_code for response in requests], [401] * 4)

    async def test_rotated_token_is_rejected_even_if_middleware_cache_is_warm(self):
        first = await self.client.get(
            "/api/v1/devices/filascan/status", headers=self.device_header()
        )
        self.assertEqual(first.status_code, 200)
        async with self.sessions() as db:
            device = await db.get(Device, 1)
            device.token_hash = hash_token("new-secret")
            await db.commit()

        old_token = await self.client.get(
            "/api/v1/devices/filascan/status", headers=self.device_header()
        )
        new_token = await self.client.get(
            "/api/v1/devices/filascan/status",
            headers=self.device_header(secret="new-secret"),
        )
        self.assertEqual(old_token.status_code, 401)
        self.assertEqual(new_token.status_code, 200)

    async def test_valid_unrelated_device_is_forbidden_despite_crud_scope(self):
        response = await self.client.get(
            "/api/v1/devices/filascan/status",
            headers=self.device_header(2, "scale-secret"),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "filascan_device_not_allowed")

    async def test_saved_allowlist_can_authorize_selected_device_id(self):
        state_module.save_allowed_device_ids({2})
        response = await self.client.get(
            "/api/v1/devices/filascan/status",
            headers=self.device_header(2, "scale-secret"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["device_id"], 2)

    async def test_empty_allowlist_denies_even_a_valid_registered_device(self):
        state_module.save_allowed_device_ids(set())
        response = await self.client.get(
            "/api/v1/devices/filascan/status", headers=self.device_header()
        )
        self.assertEqual(response.status_code, 403)

    async def test_admin_config_lists_and_persists_selected_devices(self):
        async with self.sessions() as db:
            before = await admin_module.get_config(db, principal=object())
            result = await admin_module.update_config(
                admin_module.AllowlistUpdate(allowed_device_ids=[2]),
                db,
                principal=object(),
            )
            after = await admin_module.get_config(db, principal=object())

        self.assertEqual(before["allowed_device_ids"], [1])
        self.assertEqual(result, {"allowed_device_ids": [2]})
        self.assertEqual(after["allowed_device_ids"], [2])
        selected = {item["id"]: item["allowed"] for item in after["devices"]}
        self.assertEqual(selected, {1: False, 2: True})

    async def test_admin_config_rejects_unknown_or_inactive_device(self):
        async with self.sessions() as db:
            with self.assertRaises(Exception) as caught:
                await admin_module.update_config(
                    admin_module.AllowlistUpdate(allowed_device_ids=[999]),
                    db,
                    principal=object(),
                )
        self.assertEqual(caught.exception.status_code, 422)

    async def test_missing_or_unsupported_type_returns_structured_400(self):
        missing = await self.import_request(spool_type=None)
        unsupported = await self.import_request(spool_type="example")
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(unsupported.status_code, 400)
        self.assertEqual(
            unsupported.json()["detail"]["code"], "unsupported_spool_type"
        )

    async def test_malformed_or_zero_tray_uuid_is_rejected(self):
        malformed = await self.import_request(
            spool_payload(external_id="NOT-A-VALID-TRAY-UUID")
        )
        zero = await self.import_request(
            spool_payload(external_id="0" * 32)
        )
        self.assertEqual(malformed.status_code, 422)
        self.assertEqual(zero.status_code, 422)

    async def test_bambu_format_cannot_create_an_unrelated_manufacturer(self):
        response = await self.import_request(
            spool_payload(manufacturer="Unrelated Vendor")
        )
        self.assertEqual(response.status_code, 422)
        async with self.sessions() as db:
            self.assertEqual(
                await db.scalar(select(func.count()).select_from(Manufacturer)), 0
            )

    async def test_openprinttag_import_is_idempotent(self):
        first = await self.import_request(
            openprinttag_payload(), spool_type="openprinttag"
        )
        second = await self.import_request(
            openprinttag_payload(), spool_type="openprinttag"
        )

        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["status"], "existing")
        self.assertEqual(second.json()["spool_id"], first.json()["spool_id"])
        self.assertEqual(
            first.json()["external_id"],
            f"openprinttag:{OPENPRINTTAG_UUID}",
        )

        async with self.sessions() as db:
            spool = await db.get(Spool, first.json()["spool_id"])
            filament = await db.get(Filament, first.json()["filament_id"])
            manufacturer = await db.get(Manufacturer, first.json()["manufacturer_id"])

        self.assertEqual(manufacturer.name, "Prusament")
        self.assertEqual(filament.material_type, "PETG")
        self.assertEqual(filament.manufacturer_color_name, "Prusa Galaxy Black")
        self.assertEqual(spool.remaining_weight_g, 875)
        self.assertIsNone(spool.custom_fields)
        self.assertIsNone(filament.custom_fields)

    async def test_openprinttag_reuses_single_filamentdb_color_match(self):
        async with self.sessions() as db:
            manufacturer = Manufacturer(
                name="Prusament", custom_fields={"filamentdb_id": 689}
            )
            color = Color(name="Galaxy Black", hex_code="#3D3E3D")
            db.add_all([manufacturer, color])
            await db.flush()
            filament = Filament(
                manufacturer_id=manufacturer.id,
                designation="Galaxy - Galaxy Black",
                material_type="PETG",
                diameter_mm=1.75,
                manufacturer_color_name="Galaxy Black",
                custom_fields={"filamentdb_id": 2541},
            )
            db.add(filament)
            await db.flush()
            db.add(
                FilamentColor(
                    filament_id=filament.id, color_id=color.id, position=1
                )
            )
            await db.commit()
            filament_id = filament.id

        response = await self.import_request(
            openprinttag_payload(), spool_type="openprinttag"
        )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["filament_id"], filament_id)
        async with self.sessions() as db:
            self.assertEqual(
                await db.scalar(select(func.count()).select_from(Filament)), 1
            )
            self.assertEqual(
                await db.scalar(select(func.count()).select_from(Color)), 1
            )

    async def test_openprinttag_uses_fuzzy_name_for_multiple_color_matches(self):
        async with self.sessions() as db:
            manufacturer = Manufacturer(name="Prusament")
            color = Color(name="Galaxy Black", hex_code="#3D3E3D")
            db.add_all([manufacturer, color])
            await db.flush()
            galaxy = Filament(
                manufacturer_id=manufacturer.id,
                designation="Galaxy - Galaxy Black",
                material_type="PETG",
                diameter_mm=1.75,
                manufacturer_color_name="Galaxy Black",
            )
            other = Filament(
                manufacturer_id=manufacturer.id,
                designation="Special - Anthracite",
                material_type="PETG",
                diameter_mm=1.75,
                manufacturer_color_name="Anthracite",
            )
            db.add_all([galaxy, other])
            await db.flush()
            db.add_all(
                [
                    FilamentColor(
                        filament_id=galaxy.id, color_id=color.id, position=1
                    ),
                    FilamentColor(
                        filament_id=other.id, color_id=color.id, position=1
                    ),
                ]
            )
            await db.commit()
            galaxy_id = galaxy.id

        response = await self.import_request(
            openprinttag_payload(), spool_type="openprinttag"
        )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["filament_id"], galaxy_id)

    async def test_spool_type_must_match_payload(self):
        bambu_as_openprinttag = await self.import_request(
            spool_payload(), spool_type="openprinttag"
        )
        openprinttag_as_bambu = await self.import_request(
            openprinttag_payload(), spool_type="bambu"
        )
        self.assertEqual(bambu_as_openprinttag.status_code, 422)
        self.assertEqual(openprinttag_as_bambu.status_code, 422)

    async def test_location_id_is_required_and_must_be_positive(self):
        missing_payload = spool_payload()
        del missing_payload["location_id"]

        missing = await self.import_request(missing_payload)
        zero = await self.import_request(spool_payload(location_id=0))

        self.assertEqual(missing.status_code, 422)
        self.assertEqual(zero.status_code, 422)

    async def test_new_spool_is_stored_at_selected_regular_location(self):
        response = await self.import_request()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["status"], "created")
        async with self.sessions() as db:
            spool = await db.get(Spool, response.json()["spool_id"])
        self.assertEqual(spool.location_id, 1)

    async def test_nonexistent_location_is_rejected_without_inventory_changes(self):
        response = await self.import_request(spool_payload(location_id=999))

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"]["code"], "invalid_storage_location"
        )
        async with self.sessions() as db:
            for model in (
                Manufacturer,
                Color,
                Filament,
                FilamentColor,
                Spool,
                SystemExtraField,
            ):
                self.assertEqual(
                    await db.scalar(select(func.count()).select_from(model)), 0
                )

    async def test_bambulab_identifier_is_rejected_case_insensitively(self):
        async with self.sessions() as db:
            db.add_all(
                [
                    Location(
                        id=2,
                        name="AMS Slot",
                        identifier="bambulab_1_0_0",
                    ),
                    Location(
                        id=3,
                        name="Uppercase AMS Slot",
                        identifier="BAMBuLab_1_0_1",
                    ),
                ]
            )
            await db.commit()

        lower = await self.import_request(spool_payload(location_id=2))
        upper = await self.import_request(spool_payload(location_id=3))

        self.assertEqual(lower.status_code, 422)
        self.assertEqual(upper.status_code, 422)
        self.assertEqual(
            lower.json()["detail"]["code"], "invalid_storage_location"
        )

    async def test_driver_managed_location_is_rejected(self):
        async with self.sessions() as db:
            db.add(
                Location(
                    id=2,
                    name="Driver Slot",
                    identifier="printer-slot",
                    custom_fields={"managed_by": "moonraker_plugin"},
                )
            )
            await db.commit()

        response = await self.import_request(spool_payload(location_id=2))

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"],
            {
                "code": "invalid_storage_location",
                "message": "The selected location is managed by a printer driver",
            },
        )

    async def test_bambu_managed_by_marker_is_rejected(self):
        async with self.sessions() as db:
            db.add(
                Location(
                    id=2,
                    name="Legacy Bambu Slot",
                    identifier="printer-slot",
                    custom_fields={"managed_by": "BambuLab_Plugin"},
                )
            )
            await db.commit()

        response = await self.import_request(spool_payload(location_id=2))

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"]["code"], "invalid_storage_location"
        )

    async def test_repeat_import_does_not_move_existing_spool(self):
        first = await self.import_request()
        async with self.sessions() as db:
            db.add(
                Location(
                    id=2,
                    name="Shelf B",
                    identifier="shelf-b",
                )
            )
            await db.commit()

        second = await self.import_request(spool_payload(location_id=2))

        self.assertEqual(second.status_code, 200)
        self.assertIn(second.json()["status"], {"existing", "updated"})
        self.assertEqual(second.json()["spool_id"], first.json()["spool_id"])
        async with self.sessions() as db:
            spool = await db.get(Spool, first.json()["spool_id"])
        self.assertEqual(spool.location_id, 1)

    async def test_device_endpoint_moves_spool_and_records_attributed_event(self):
        imported = await self.import_request()
        async with self.sessions() as db:
            db.add(Location(id=2, name="Shelf B", identifier="shelf-b"))
            await db.commit()

        with patch.object(
            service_module.event_bus, "publish", new_callable=AsyncMock
        ) as publish:
            response = await self.move_request(imported.json()["spool_id"], 2)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "status": "moved",
                "spool_id": imported.json()["spool_id"],
                "location_id": 2,
                "location_name": "Shelf B",
            },
        )
        publish.assert_awaited_once_with({"event": "spools_changed"})

        async with self.sessions() as db:
            spool = await db.get(Spool, imported.json()["spool_id"])
            events = list(
                (
                    await db.execute(
                        select(SpoolEvent).where(
                            SpoolEvent.spool_id == imported.json()["spool_id"]
                        )
                    )
                ).scalars()
            )
        self.assertEqual(spool.location_id, 2)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "move_location")
        self.assertEqual(events[0].from_location_id, 1)
        self.assertEqual(events[0].to_location_id, 2)
        self.assertEqual(events[0].device_id, 1)
        self.assertIsNone(events[0].user_id)
        self.assertEqual(events[0].source, "filascan")

    async def test_move_rejects_ams_and_other_driver_managed_locations(self):
        imported = await self.import_request()
        async with self.sessions() as db:
            db.add_all(
                [
                    Location(
                        id=2,
                        name="AMS Slot",
                        identifier="BAMBuLab_1_0_0",
                    ),
                    Location(
                        id=3,
                        name="Moonraker Toolhead",
                        identifier="toolhead",
                        custom_fields={"managed_by": "moonraker_plugin"},
                    ),
                ]
            )
            await db.commit()

        ams = await self.move_request(imported.json()["spool_id"], 2)
        driver = await self.move_request(imported.json()["spool_id"], 3)

        self.assertEqual(ams.status_code, 422)
        self.assertEqual(driver.status_code, 422)
        self.assertEqual(
            ams.json()["detail"]["code"], "invalid_storage_location"
        )
        self.assertEqual(
            driver.json()["detail"]["code"], "invalid_storage_location"
        )
        async with self.sessions() as db:
            spool = await db.get(Spool, imported.json()["spool_id"])
            event_count = await db.scalar(
                select(func.count()).select_from(SpoolEvent)
            )
        self.assertEqual(spool.location_id, 1)
        self.assertEqual(event_count, 0)

    async def test_move_to_current_location_is_success_without_duplicate_event(self):
        imported = await self.import_request()

        with patch.object(
            service_module.event_bus, "publish", new_callable=AsyncMock
        ) as publish:
            response = await self.move_request(imported.json()["spool_id"], 1)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "moved")
        self.assertEqual(response.json()["location_name"], "Shelf A")
        publish.assert_not_awaited()
        async with self.sessions() as db:
            event_count = await db.scalar(
                select(func.count()).select_from(SpoolEvent)
            )
        self.assertEqual(event_count, 0)

    async def test_move_returns_404_for_unknown_spool_or_location(self):
        imported = await self.import_request()

        unknown_spool = await self.move_request(999, 1)
        unknown_location = await self.move_request(
            imported.json()["spool_id"], 999
        )

        self.assertEqual(unknown_spool.status_code, 404)
        self.assertEqual(unknown_location.status_code, 404)
        self.assertEqual(
            unknown_spool.json()["detail"]["code"], "spool_not_found"
        )
        self.assertEqual(
            unknown_location.json()["detail"]["code"],
            "storage_location_not_found",
        )

    async def test_archive_changes_status_and_records_attributed_event(self):
        imported = await self.import_request()

        with patch.object(
            service_module.event_bus, "publish", new_callable=AsyncMock
        ) as publish:
            response = await self.archive_request(imported.json()["spool_id"])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "status": "archived",
                "spool_id": imported.json()["spool_id"],
            },
        )
        publish.assert_awaited_once_with({"event": "spools_changed"})

        async with self.sessions() as db:
            spool = await db.get(Spool, imported.json()["spool_id"])
            archived_status = (
                await db.execute(
                    select(SpoolStatus).where(SpoolStatus.key == "archived")
                )
            ).scalar_one()
            events = list(
                (
                    await db.execute(
                        select(SpoolEvent).where(
                            SpoolEvent.spool_id == imported.json()["spool_id"]
                        )
                    )
                ).scalars()
            )
        self.assertEqual(spool.status_id, archived_status.id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "archived")
        self.assertEqual(events[0].device_id, 1)
        self.assertIsNone(events[0].user_id)
        self.assertEqual(events[0].source, "filascan")
        self.assertEqual(events[0].to_status_id, archived_status.id)

    async def test_archive_is_idempotent_without_duplicate_event_or_publication(self):
        imported = await self.import_request()
        first = await self.archive_request(imported.json()["spool_id"])

        with patch.object(
            service_module.event_bus, "publish", new_callable=AsyncMock
        ) as publish:
            second = await self.archive_request(imported.json()["spool_id"])

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], "archived")
        publish.assert_not_awaited()
        async with self.sessions() as db:
            event_count = await db.scalar(
                select(func.count()).select_from(SpoolEvent)
            )
        self.assertEqual(event_count, 1)

    async def test_archive_rejects_unknown_spool_and_nonempty_body(self):
        unknown = await self.archive_request(999)
        unexpected_body = await self.archive_request(999, {"force": True})

        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(
            unknown.json()["detail"]["code"], "spool_not_found"
        )
        self.assertEqual(unexpected_body.status_code, 422)

    async def test_reimport_restores_archived_spool_without_duplicate_or_move(self):
        imported = await self.import_request()
        spool_id = imported.json()["spool_id"]
        archived = await self.archive_request(spool_id)
        self.assertEqual(archived.status_code, 200)

        async with self.sessions() as db:
            db.add(Location(id=2, name="Shelf B", identifier="shelf-b"))
            await db.commit()

        restored = await self.import_request(spool_payload(location_id=2))

        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()["status"], "updated")
        self.assertEqual(restored.json()["spool_id"], spool_id)
        async with self.sessions() as db:
            spool = await db.get(Spool, spool_id)
            status_row = await db.get(SpoolStatus, spool.status_id)
            spool_count = await db.scalar(select(func.count()).select_from(Spool))
            events = list(
                (
                    await db.execute(
                        select(SpoolEvent)
                        .where(SpoolEvent.spool_id == spool_id)
                        .order_by(SpoolEvent.id)
                    )
                ).scalars()
            )
        self.assertEqual(spool_count, 1)
        self.assertEqual(spool.location_id, 1)
        self.assertEqual(status_row.key, "new")
        self.assertEqual([item.event_type for item in events], ["archived", "new"])
        self.assertEqual(events[-1].device_id, 1)
        self.assertEqual(events[-1].source, "filascan")

    async def test_lowercase_tray_uuid_is_normalized_and_optional_zeroes_are_unknown(self):
        response = await self.import_request(
            spool_payload(
                external_id=TRAY_UUID.lower(),
                drying_temperature_c=0,
                drying_time_h=0,
                bed_temperature_c=0,
                nozzle_temperature_min_c=0,
                nozzle_temperature_max_c=0,
                spool_width_mm=0,
                filament_length_m=0,
            )
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["external_id"], f"bambulab:{TRAY_UUID}")
        async with self.sessions() as db:
            spool = await db.get(Spool, response.json()["spool_id"])
            filament = await db.get(Filament, response.json()["filament_id"])
        self.assertIsNone(spool.spool_width_mm)
        self.assertIsNone(spool.custom_fields["bambu_filament_length_m"])
        self.assertIsNone(
            filament.custom_fields["bambu_nozzle_temperature_min_c"]
        )

    async def test_first_import_creates_inventory_and_repeat_is_idempotent(self):
        first = await self.import_request()
        second = await self.import_request()

        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json()["status"], "created")
        self.assertEqual(first.json()["external_id"], f"bambulab:{TRAY_UUID}")
        self.assertEqual(first.json()["tray_uuid"], TRAY_UUID)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], "existing")
        self.assertEqual(second.json()["spool_id"], first.json()["spool_id"])

        async with self.sessions() as db:
            counts = {
                "manufacturers": await db.scalar(
                    select(func.count()).select_from(Manufacturer)
                ),
                "colors": await db.scalar(select(func.count()).select_from(Color)),
                "filaments": await db.scalar(
                    select(func.count()).select_from(Filament)
                ),
                "spools": await db.scalar(select(func.count()).select_from(Spool)),
            }
            spool = (await db.execute(select(Spool))).scalar_one()
            filament = (await db.execute(select(Filament))).scalar_one()
            color = (await db.execute(select(Color))).scalar_one()
            extra_fields = list(
                (
                    await db.execute(
                        select(SystemExtraField).order_by(
                            SystemExtraField.target_type,
                            SystemExtraField.key,
                        )
                    )
                ).scalars()
            )
        self.assertEqual(
            counts,
            {"manufacturers": 1, "colors": 1, "filaments": 1, "spools": 1},
        )
        self.assertEqual(spool.external_id, f"bambulab:{TRAY_UUID}")
        self.assertIsNone(spool.rfid_uid)
        self.assertEqual(
            spool.custom_fields,
            {
                "bambu_filament_length_m": 330.0,
                "bambu_production_date": "2025_03_15_09_34",
            },
        )
        self.assertEqual(filament.custom_fields["article_number"], "13109")
        self.assertEqual(filament.custom_fields["bambu_material_id"], "GFA06")
        self.assertEqual(filament.custom_fields["bambu_variant_id"], "A06-D1")
        self.assertEqual(
            filament.custom_fields["filament_image_url"],
            "https://store.bblcdn.eu/example.png",
        )
        self.assertEqual(
            filament.custom_fields["filament_image_provider"], "bambulab"
        )
        self.assertIsNone(filament.shop_url)
        for redundant_key in (
            "bambu_color_code",
            "bambu_product_code",
            "bambu_primary_rgba",
            "bambu_secondary_rgba",
        ):
            self.assertNotIn(redundant_key, filament.custom_fields)
        self.assertEqual(color.name, "Silver")
        self.assertIsNone(color.custom_fields)
        definitions = {
            (field.target_type, field.key): field for field in extra_fields
        }
        self.assertEqual(
            definitions[("filament", "article_number")].label,
            "Bambu Color Code (Article Number)",
        )
        self.assertEqual(
            set(definitions),
            {
                (target_type, key)
                for target_type, key, _label, _field_type, _config
                in service_module.EXTRA_FIELD_DEFINITIONS
            },
        )

    async def test_extra_field_migration_removes_only_obsolete_owned_definitions(self):
        first = await self.import_request()
        self.assertEqual(first.status_code, 201)

        async with self.sessions() as db:
            current = list((await db.execute(select(SystemExtraField))).scalars())
            for field in current:
                await db.delete(field)
            await db.flush()
            db.add_all(
                [
                    SystemExtraField(
                        target_type="spool",
                        key="bambu_tray_uuid",
                        label="Bambu Tray UUID",
                        field_type="text",
                        source="filascan_import",
                    ),
                    SystemExtraField(
                        target_type="filament",
                        key="article_number",
                        label="Manufacturer Article Number",
                        field_type="text",
                        source="bambulab",
                    ),
                ]
            )
            await db.commit()

        response = await self.import_request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "existing")

        async with self.sessions() as db:
            fields = list(
                (
                    await db.execute(
                        select(SystemExtraField).order_by(
                            SystemExtraField.target_type,
                            SystemExtraField.key,
                        )
                    )
                ).scalars()
            )
        self.assertEqual(
            {(field.target_type, field.key, field.source) for field in fields},
            {
                ("filament", "article_number", "bambulab"),
                *{
                    (target_type, key, "filascan_import")
                    for target_type, key, _label, _field_type, _config
                    in service_module.EXTRA_FIELD_DEFINITIONS
                    if (target_type, key) != ("filament", "article_number")
                },
            },
        )
        self.assertEqual(fields[0].label, "Manufacturer Article Number")

    async def test_reuses_existing_filamentdb_product_by_bambu_color_code(self):
        async with self.sessions() as db:
            manufacturer = Manufacturer(name="Bambu Lab")
            color = Color(name="Grass Green", hex_code="#61C680")
            db.add_all([manufacturer, color])
            await db.flush()
            catalog = Filament(
                manufacturer_id=manufacturer.id,
                designation="Matte - Grass Green",
                manufacturer_color_name="Grass Green",
                material_type="PLA",
                material_subgroup="matte",
                diameter_mm=1.75,
                custom_fields={"filamentdb_id": 149, "article_number": "11500"},
            )
            db.add(catalog)
            await db.flush()
            db.add(
                FilamentColor(
                    filament_id=catalog.id, color_id=color.id, position=1
                )
            )
            await db.commit()
            catalog_id = catalog.id
            color_id = color.id

        response = await self.import_request(
            spool_payload(
                material_id="GFA01",
                variant_id="A01-G1",
                detailed_filament_type="PLA Matte",
                official_material_name="Bambu PLA Matte",
                color_name="Grass Green",
                bambu_color_code="11500",
                primary_rgba="#61C680FF",
            )
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["filament_id"], catalog_id)
        self.assertEqual(response.json()["color_ids"], [color_id])
        async with self.sessions() as db:
            self.assertEqual(
                await db.scalar(select(func.count()).select_from(Filament)), 1
            )
            self.assertEqual(await db.scalar(select(func.count()).select_from(Color)), 1)
            enriched = await db.get(Filament, catalog_id)
        self.assertEqual(enriched.custom_fields["filamentdb_id"], 149)
        self.assertEqual(enriched.custom_fields["article_number"], "11500")
        self.assertNotIn("bambu_color_code", enriched.custom_fields)
        self.assertEqual(enriched.custom_fields["bambu_material_id"], "GFA01")

    async def test_legacy_image_in_shop_url_moves_to_shared_image_metadata(self):
        payload = spool_payload(product_image_url=None)
        first = await self.import_request(payload)
        image_url = "https://store.bblcdn.eu/example.png"
        async with self.sessions() as db:
            filament = await db.get(Filament, first.json()["filament_id"])
            filament.shop_url = image_url
            fields = dict(filament.custom_fields or {})
            fields.pop("filament_image_url", None)
            fields.pop("filament_image_provider", None)
            filament.custom_fields = fields
            await db.commit()

        second = await self.import_request(
            spool_payload(product_image_url=image_url)
        )

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], "updated")
        async with self.sessions() as db:
            filament = await db.get(Filament, first.json()["filament_id"])
        self.assertIsNone(filament.shop_url)
        self.assertEqual(filament.custom_fields["filament_image_url"], image_url)
        self.assertEqual(
            filament.custom_fields["filament_image_provider"], "bambulab"
        )

    async def test_unknown_pla_pure_uses_filamentdb_style_fallback(self):
        response = await self.import_request(
            spool_payload(
                material_id="GFA19",
                variant_id="A19-W00",
                detailed_filament_type="PLA Pure",
                official_material_name="Bambu PLA Pure",
                color_name="Pure White",
                bambu_color_code="17100",
                primary_rgba="#FFFFFFFF",
                product_image_url=None,
            )
        )

        self.assertEqual(response.status_code, 201)
        async with self.sessions() as db:
            filament = await db.get(Filament, response.json()["filament_id"])
        self.assertEqual(filament.designation, "Pure - Pure White (17100)")
        self.assertEqual(filament.manufacturer_color_name, "Pure White (17100)")
        self.assertEqual(filament.material_subgroup, "pure")
        self.assertIsNone(filament.shop_url)
        self.assertEqual(filament.custom_fields["article_number"], "17100")
        self.assertNotIn("bambu_product_code", filament.custom_fields)

        async with self.sessions() as db:
            spool = await db.get(Spool, response.json()["spool_id"])
        self.assertNotIn("bambu_product_code", spool.custom_fields)

    async def test_repeat_scan_migrates_exact_legacy_fallback_name(self):
        payload = spool_payload(
            material_id="GFA19",
            variant_id="A19-W00",
            detailed_filament_type="PLA Pure",
            official_material_name="Bambu PLA Pure",
            color_name="Pure White",
            bambu_color_code="17100",
            primary_rgba="#FFFFFFFF",
            product_image_url=None,
        )
        first = await self.import_request(payload)
        async with self.sessions() as db:
            filament = await db.get(Filament, first.json()["filament_id"])
            filament.designation = (
                "Bambu PLA Pure · Pure White [GFA19-A19-W00]"
            )
            filament.manufacturer_color_name = "Pure White"
            filament.material_subgroup = "Bambu PLA Pure"
            await db.commit()

        second = await self.import_request(payload)

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], "updated")
        async with self.sessions() as db:
            filament = await db.get(Filament, first.json()["filament_id"])
        self.assertEqual(filament.designation, "Pure - Pure White (17100)")
        self.assertEqual(filament.manufacturer_color_name, "Pure White (17100)")
        self.assertEqual(filament.material_subgroup, "pure")

    async def test_repeat_scan_migrates_legacy_name_after_material_label_changes(self):
        original = spool_payload(
            material_id="GFA19",
            variant_id="A19-W00",
            detailed_filament_type="PLA Pure",
            official_material_name="PLA Pure",
            color_name="Pure White",
            bambu_color_code="17100",
            primary_rgba="#FFFFFFFF",
            product_image_url=None,
        )
        first = await self.import_request(original)
        async with self.sessions() as db:
            filament = await db.get(Filament, first.json()["filament_id"])
            filament.designation = "PLA Pure · Pure White [GFA19-A19-W00]"
            filament.manufacturer_color_name = "Pure White"
            filament.material_subgroup = "PLA Pure"
            await db.commit()

        changed_label = dict(original, official_material_name="Bambu PLA Pure")
        second = await self.import_request(changed_label)

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], "updated")
        async with self.sessions() as db:
            filament = await db.get(Filament, first.json()["filament_id"])
        self.assertEqual(filament.designation, "Pure - Pure White (17100)")
        self.assertEqual(filament.manufacturer_color_name, "Pure White (17100)")
        self.assertEqual(filament.material_subgroup, "pure")

    async def test_repeat_scan_does_not_rename_a_custom_filament_name(self):
        payload = spool_payload(
            material_id="GFA19",
            variant_id="A19-W00",
            detailed_filament_type="PLA Pure",
            official_material_name="Bambu PLA Pure",
            color_name="Pure White",
            bambu_color_code="17100",
            primary_rgba="#FFFFFFFF",
            product_image_url=None,
        )
        first = await self.import_request(payload)
        async with self.sessions() as db:
            filament = await db.get(Filament, first.json()["filament_id"])
            filament.designation = "My AMS White"
            await db.commit()

        second = await self.import_request(payload)

        self.assertEqual(second.status_code, 200)
        async with self.sessions() as db:
            filament = await db.get(Filament, first.json()["filament_id"])
        self.assertEqual(filament.designation, "My AMS White")

    async def test_duplicate_filamentdb_rows_with_same_code_choose_canonical(self):
        async with self.sessions() as db:
            manufacturer = Manufacturer(name="Bambu Lab")
            color = Color(name="Silver (13109)", hex_code="#C8C8C8")
            db.add_all([manufacturer, color])
            await db.flush()
            duplicate = Filament(
                manufacturer_id=manufacturer.id,
                designation="Silk Plus - Silver (13109)",
                manufacturer_color_name="Silver (13109)",
                material_type="PLA",
                diameter_mm=1.75,
                custom_fields={"filamentdb_id": 22174},
            )
            canonical = Filament(
                manufacturer_id=manufacturer.id,
                designation="Silk Plus - Silver (13109)",
                manufacturer_color_name="Silver (13109)",
                material_type="PLA",
                diameter_mm=1.75,
                custom_fields={"filamentdb_id": 2850},
            )
            db.add_all([duplicate, canonical])
            await db.flush()
            db.add_all(
                [
                    FilamentColor(
                        filament_id=filament.id,
                        color_id=color.id,
                        position=1,
                    )
                    for filament in (duplicate, canonical)
                ]
            )
            await db.commit()
            canonical_id = canonical.id

        response = await self.import_request()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["filament_id"], canonical_id)

    async def test_same_code_in_different_material_type_is_not_reused(self):
        async with self.sessions() as db:
            manufacturer = Manufacturer(name="Bambu Lab")
            color = Color(name="Silver (13109)", hex_code="#C8C8C8")
            db.add_all([manufacturer, color])
            await db.flush()
            incompatible = Filament(
                manufacturer_id=manufacturer.id,
                designation="ABS Silver (13109)",
                manufacturer_color_name="Silver (13109)",
                material_type="ABS",
                diameter_mm=1.75,
                custom_fields={"filamentdb_id": 999},
            )
            db.add(incompatible)
            await db.flush()
            db.add(
                FilamentColor(
                    filament_id=incompatible.id, color_id=color.id, position=1
                )
            )
            await db.commit()
            incompatible_id = incompatible.id

        response = await self.import_request()

        self.assertEqual(response.status_code, 201)
        self.assertNotEqual(response.json()["filament_id"], incompatible_id)
        async with self.sessions() as db:
            self.assertEqual(
                await db.scalar(select(func.count()).select_from(Filament)), 2
            )

    async def test_repeat_scan_relinks_plugin_generated_filament_to_catalog(self):
        first = await self.import_request()
        generated_id = first.json()["filament_id"]
        async with self.sessions() as db:
            generated = await db.get(Filament, generated_id)
            manufacturer = await db.get(Manufacturer, generated.manufacturer_id)
            color = Color(name="Silver (13109)", hex_code="#C8C8C8")
            db.add(color)
            await db.flush()
            catalog = Filament(
                manufacturer_id=manufacturer.id,
                designation="Silk Plus - Silver (13109)",
                manufacturer_color_name="Silver (13109)",
                material_type="PLA",
                diameter_mm=1.75,
                custom_fields={"filamentdb_id": 2850},
            )
            db.add(catalog)
            await db.flush()
            db.add(
                FilamentColor(
                    filament_id=catalog.id, color_id=color.id, position=1
                )
            )
            await db.commit()
            catalog_id = catalog.id

        second = await self.import_request()

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], "updated")
        self.assertEqual(second.json()["filament_id"], catalog_id)
        async with self.sessions() as db:
            spool = await db.get(Spool, first.json()["spool_id"])
        self.assertEqual(spool.filament_id, catalog_id)

    async def test_existing_spool_is_enriched_without_overwriting_weight(self):
        first = await self.import_request()
        async with self.sessions() as db:
            spool = await db.get(Spool, first.json()["spool_id"])
            spool.remaining_weight_g = 412
            spool.custom_fields = {
                "user_note": "keep",
                "article_number": "13109",
            }
            filament = await db.get(Filament, spool.filament_id)
            filament.custom_fields = None
            await db.commit()

        response = await self.import_request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "updated")
        async with self.sessions() as db:
            spool = await db.get(Spool, first.json()["spool_id"])
            filament = await db.get(Filament, spool.filament_id)
        self.assertEqual(spool.remaining_weight_g, 412)
        self.assertEqual(spool.custom_fields["user_note"], "keep")
        self.assertEqual(spool.custom_fields["article_number"], "13109")
        self.assertNotIn("bambu_material_id", spool.custom_fields)
        self.assertEqual(filament.custom_fields["bambu_material_id"], "GFA06")
        self.assertEqual(filament.custom_fields["article_number"], "13109")

    async def test_incompatible_existing_identity_returns_409(self):
        await self.import_request()
        response = await self.import_request(
            spool_payload(variant_id="A06-DIFFERENT")
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "spool_identity_conflict")

    async def test_external_id_cannot_claim_unrelated_existing_spool(self):
        first = await self.import_request()
        async with self.sessions() as db:
            spool = await db.get(Spool, first.json()["spool_id"])
            filament = await db.get(Filament, spool.filament_id)
            filament.material_type = "ABS"
            filament.custom_fields = None
            spool.custom_fields = None
            await db.commit()

        response = await self.import_request()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "spool_identity_conflict")

    async def test_broad_pla_type_does_not_collapse_variants_or_named_colors(self):
        first = await self.import_request()
        second = await self.import_request(
            spool_payload(
                external_id="85F131573411401CA37A26A27631E21B",
                material_id="GFA01",
                variant_id="A01-K1",
                official_material_name="Bambu PLA Matte",
                detailed_filament_type="PLA Matte",
                color_name="Charcoal",
                primary_rgba="#C8C8C8FF",
                bambu_color_code="11101",
            )
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(first.json()["filament_id"], second.json()["filament_id"])
        async with self.sessions() as db:
            self.assertEqual(
                await db.scalar(select(func.count()).select_from(Filament)), 2
            )
            self.assertEqual(await db.scalar(select(func.count()).select_from(Color)), 2)

    async def test_multicolor_preserves_primary_secondary_order(self):
        response = await self.import_request(
            spool_payload(secondary_rgba="#FF0000FF")
        )
        self.assertEqual(response.status_code, 201)
        color_ids = response.json()["color_ids"]
        self.assertEqual(len(color_ids), 2)
        async with self.sessions() as db:
            filament_id = response.json()["filament_id"]
            associations = list(
                (
                    await db.execute(
                        select(FilamentColor)
                        .where(FilamentColor.filament_id == filament_id)
                        .order_by(FilamentColor.position)
                    )
                ).scalars()
            )
        self.assertEqual([item.color_id for item in associations], color_ids)
        self.assertEqual([item.position for item in associations], [1, 2])

    async def test_concurrent_identical_requests_create_at_most_one_spool(self):
        first, second = await asyncio.gather(
            self.import_request(), self.import_request()
        )
        self.assertEqual({first.status_code, second.status_code}, {200, 201})
        self.assertEqual(first.json()["spool_id"], second.json()["spool_id"])
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(Spool)), 1)
            self.assertEqual(
                await db.scalar(select(func.count()).select_from(Filament)), 1
            )

    async def test_failure_rolls_back_every_inventory_record(self):
        with patch.object(
            service_module,
            "_create_spool_record",
            side_effect=RuntimeError("simulated database failure"),
        ):
            response = await self.import_request()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"]["code"], "import_failed")

        async with self.sessions() as db:
            for model in (
                Manufacturer,
                Color,
                Filament,
                FilamentColor,
                Spool,
                SystemExtraField,
            ):
                self.assertEqual(
                    await db.scalar(select(func.count()).select_from(model)), 0
                )

    async def test_physical_tag_uid_is_rejected_not_logged_or_stored(self):
        physical_uid = "6BEBCDFE"
        with self.assertLogs(level=logging.INFO) as captured:
            response = await self.import_request(
                spool_payload(tag_uid=physical_uid)
            )
            logging.getLogger("filascan_import").info("validation completed")
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(physical_uid, "\n".join(captured.output))

        async with self.sessions() as db:
            stored = []
            for model in (Manufacturer, Color, Filament, Spool):
                items = list((await db.execute(select(model))).scalars())
                stored.extend(getattr(item, "custom_fields", None) for item in items)
        self.assertNotIn(physical_uid, json.dumps(stored))


class PluginPackageTests(unittest.TestCase):
    def test_manifest_registers_visible_configuration_page(self):
        plugin_dir = Path(__file__).resolve().parents[1] / "filascan_import"
        manifest = json.loads((plugin_dir / "plugin.json").read_text())
        self.assertEqual(manifest["plugin_key"], "filascan_import")
        self.assertEqual(manifest["version"], "0.2.0")
        self.assertEqual(
            manifest["homepage"], "https://github.com/MasonVX/filascan-import"
        )
        self.assertEqual(manifest["license"], "MIT")
        self.assertEqual(manifest["page_url"], "/plugin-page/filascan-import")
        self.assertTrue(manifest["show_in_nav"])
        self.assertTrue(manifest["capabilities"]["location_selection"])
        self.assertTrue(manifest["capabilities"]["location_management"])
        self.assertTrue(manifest["capabilities"]["spool_archiving"])
        self.assertTrue((plugin_dir / "page.html").is_file())

    def test_configuration_page_uses_standalone_layout_and_user_language(self):
        plugin_dir = Path(__file__).resolve().parents[1] / "filascan_import"
        page = (plugin_dir / "page.html").read_text()

        self.assertIn('class="page-shell"', page)
        self.assertIn('class="topbar"', page)
        self.assertIn('class="language-switch"', page)
        self.assertNotIn('class="fm-sidebar"', page)
        self.assertNotIn('class="fm-content"', page)
        self.assertNotIn("/api/v1/plugin-nav", page)
        self.assertIn("/api/v1/me", page)
        self.assertIn("localStorage.getItem('lang')", page)
        self.assertIn("en: {", page)
        self.assertIn("de: {", page)
        self.assertIn('data-i18n="pageTitle"', page)
        self.assertIn('data-i18n-aria="languageLabel"', page)
        self.assertIn('data-i18n="authorizedDevices"', page)
        self.assertIn("/api/v1/filascan-import/config", page)
        self.assertIn('[data-theme="brand"] { color-scheme:dark;', page)
        self.assertIn("localStorage.getItem('theme') || 'brand'", page)

    def test_public_and_admin_routes_are_exported(self):
        from filascan_import import router as router_module

        public_paths = {route.path for route in router_module.router.routes}
        admin_paths = {route.path for route in router_module.admin_router.routes}
        self.assertIn("/devices/filascan/import-spool", public_paths)
        self.assertIn("/devices/filascan/status", public_paths)
        self.assertIn("/devices/filascan/spools/{spool_id}/location", public_paths)
        self.assertIn("/devices/filascan/spools/{spool_id}/archive", public_paths)
        self.assertIn("/filascan-import/config", admin_paths)

    def test_release_builder_creates_installable_archive_and_checksum(self):
        project_dir = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            subprocess.run(
                [
                    sys.executable,
                    str(project_dir / "scripts" / "build_release.py"),
                    "--output-dir",
                    str(output_dir),
                ],
                cwd=project_dir,
                check=True,
                capture_output=True,
                text=True,
            )

            archive_path = output_dir / "filascan-import-0.2.0.zip"
            checksum_path = archive_path.with_suffix(".zip.sha256")
            self.assertTrue(archive_path.is_file())
            self.assertTrue(checksum_path.is_file())

            digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            self.assertEqual(
                checksum_path.read_text(encoding="utf-8"),
                f"{digest}  {archive_path.name}\n",
            )
            with zipfile.ZipFile(archive_path) as archive:
                names = archive.namelist()
                self.assertIsNone(archive.testzip())
            self.assertTrue(names)
            self.assertTrue(
                all(name.startswith("filascan_import/") for name in names)
            )
            self.assertIn("filascan_import/plugin.json", names)
            self.assertIn("filascan_import/LICENSE", names)
            self.assertFalse(any("__pycache__" in name for name in names))

            installer = PluginInstallService(db=None)
            archive_data = archive_path.read_bytes()
            installer._validate_zip(archive_data)
            with tempfile.TemporaryDirectory() as install_temp:
                extracted = installer._extract_zip(archive_data, install_temp)
                manifest = installer._validate_manifest(extracted)
                installer._validate_structure(extracted, manifest["plugin_type"])
                installer._validate_security(extracted)


if __name__ == "__main__":
    unittest.main()
