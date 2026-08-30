# FilaScan Spool Import for FilaMan

[![CI](https://github.com/MasonVX/filascan-import/actions/workflows/ci.yml/badge.svg)](https://github.com/MasonVX/filascan-import/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/MasonVX/filascan-import?display_name=tag)](https://github.com/MasonVX/filascan-import/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

An independent FilaMan integration plugin that accepts decoded Bambu spool data
from an authorized FilaScan device. FilaScan only needs its existing Device token;
the plugin performs the narrowly scoped inventory transaction without granting
generic manufacturer, color, filament or spool CRUD scopes.

Current plugin version: **0.1.12**

## Features

- Idempotent import of decoded Bambu spool data using the canonical Tray UUID.
- Explicit allow-list of registered FilaMan devices.
- Required location selection for new spools.
- Controlled moves of existing spools between regular storage locations.
- Idempotent soft archival and restoration of existing spools.
- English and German plugin UI using the signed-in FilaMan user's language.
- Standalone responsive dark-brand page without a duplicated FilaMan sidebar.
- Preservation of user-maintained weight, location and inventory data during
  repeated scans.

## Identity

The request carries the raw 32-character Tray UUID from RFID block 9 in
`external_id`. The plugin normalizes it to uppercase and stores the namespaced
FilaMan identity:

```text
bambulab:<TRAY_UUID>
```

This is the same convention used by Bambu Cloud Connect and the Bambu MQTT
driver. The physical RFID chip UID is forbidden by the request schema and is
never logged or stored.

## Authorization

FilaMan's normal `Authorization: Device dev.<id>.<secret>` authentication is
used. Generic inventory permissions are not required.

After installation, open **FilaScan Spool Import** in FilaMan's navigation. The
page lists the already registered devices. Select the FilaScan device and save
the allow-list. No `device_type`, environment variable or generic inventory
scope is required. Until at least one device is selected, every import request
is denied with HTTP 403.

## Endpoints

```http
GET /api/v1/devices/filascan/status
Authorization: Device dev.<id>.<secret>
```

```http
POST /api/v1/devices/filascan/import-spool?type=bambu
Authorization: Device dev.<id>.<secret>
Content-Type: application/json
```

```http
POST /api/v1/devices/filascan/spools/<spool_id>/location
Authorization: Device dev.<id>.<secret>
Content-Type: application/json

{"location_id": 12}
```

```http
POST /api/v1/devices/filascan/spools/<spool_id>/archive
Authorization: Device dev.<id>.<secret>
Content-Type: application/json

{}
```

The Bambu payload must include a positive `location_id`. For a new spool, the
plugin verifies that the selected location exists and is a regular,
user-managed storage location; printer-driver locations such as Bambu AMS slots
are rejected. The selected location is stored as part of the same transaction
as manufacturer, ordered colors, filament and spool creation.

The import resolves the spool first. Repeated or concurrent requests for the
same Tray UUID return the existing spool and never move it to the newly
submitted location. Existing user-maintained weight, location and native
inventory fields are not overwritten; missing Bambu metadata may be added.

For an existing spool, the location endpoint moves it through FilaMan's
`SpoolService`. This records a `move_location` event attributed to the
authorized FilaScan device and publishes the normal `spools_changed` event.
Selecting the current location succeeds without creating a duplicate movement
event. The response still uses `status: "moved"` for FilaScan compatibility.

The archive endpoint changes the spool to FilaMan's system `archived` status
through the normal status-event service and attributes the event to the
authorized FilaScan device. Archiving an already archived spool succeeds
without creating another event. The spool and its external Tray UID remain in
the database. A later deliberate import of that Tray UID restores the same
record to an active status without moving it or creating a duplicate spool.

If the five-digit Bambu color code does not match a FilamentDB catalog entry,
the plugin creates a predictable FilamentDB-style fallback such as
`Pure - Pure White (17100)`. A structurally verified fallback from an older
plugin version is renamed to this form on the next scan, even if FilaScan's
material label has changed in the meantime; user-created and catalog names are
never rewritten. The normalized five-digit product identity is also written as
`article_number`, the same canonical field used by the independent Bambu plugin.
The human-readable color remains available through FilaMan's native color and
`manufacturer_color_name` fields.

## Shared metadata fields

FilaScan keeps the visible extra-field list intentionally small and follows the
same metadata contract as the Bambu Lab plugin:

| Field | Target | Purpose |
|---|---|---|
| `article_number` | Filament | Canonical five-digit Bambu color/product code. |
| `article_number` | Spool | Display mirror so the code is visible on FilaMan's spool detail page. |
| `bambu_filament_length_m` | Spool | Visible source filament length in metres. |
| `bambu_production_date` | Spool | Visible Bambu production timestamp. |

The Tray UUID is stored canonically in the spool's native `external_id` rather
than duplicated as an extra field. Color names and RGB colors use FilaMan's
native color records. Material identity and temperature/drying metadata remain
on the shared filament record for integration compatibility but are not
registered as additional visible fields. Physical RFID-chip fields remain
owned exclusively by the Bambu Lab plugin.

The filament value remains authoritative; FilaScan fills an empty spool mirror
without overwriting an existing spool value. Upgrading from 0.1.10 removes
obsolete FilaScan-owned field definitions on the
next scan while retaining existing record data. Legacy `bambu_color_code` and
`bambu_product_code` values remain readable, but new imports write only the
canonical `article_number`.

## Installation

Download `filascan-import-<version>.zip` from the
[GitHub Releases page](https://github.com/MasonVX/filascan-import/releases) and
upload it in FilaMan under **System → Plugins**. The ZIP contains the required
top-level `filascan_import/` directory. Installed integration routers are
mounted under `/api/v1` by FilaMan.

After installation:

1. Restart FilaMan when requested.
2. Open **FilaScan Spool Import** from FilaMan's plugin navigation.
3. Select the registered FilaScan device and save the allow-list.
4. Configure the matching FilaMan URL and Device token on FilaScan.

## Compatibility and independence

This repository contains only the FilaMan integration plugin. FilaScan firmware
is maintained separately in [MasonVX/FilaScan](https://github.com/MasonVX/FilaScan).
The plugin is independent of the Bambu Cloud Connect integration and the Bambu
Lab printer driver, but all three can safely identify the same physical spool
through `external_id=bambulab:<TRAY_UUID>`.

FilaMan loads integration plugins against its own backend APIs. Continuous
integration therefore checks this plugin against the current public
[Fire-Devils/filaman-system](https://github.com/Fire-Devils/filaman-system)
backend before building a release artifact.

## Local tests

The tests need a FilaMan backend checkout on `PYTHONPATH`:

```bash
PYTHONPATH=/path/to/filaman/backend:. \
  /path/to/filaman/backend/.venv/bin/python -m unittest discover -s tests -v
```

## Building a release

The release builder validates all version declarations, required manifest and
page metadata, archive layout, licensing, and ZIP integrity. It produces a
deterministic ZIP and SHA-256 checksum:

```bash
python3 scripts/build_release.py
```

Artifacts are written to `dist/`. Pushing a tag named
`filascan-import-v<version>` runs the complete test suite and publishes the ZIP
and checksum as a GitHub release. See [RELEASE.md](RELEASE.md) for the maintainer
checklist and [CHANGELOG.md](CHANGELOG.md) for release notes.

## Security

Do not commit FilaMan Device tokens, database files, environment files, or
deployment secrets. Please report a security issue privately to the repository
owner rather than opening a public issue with credentials or RFID data.

## AI-assisted development

Generative AI tools, including OpenAI Codex, assisted with parts of the
implementation, testing and documentation. The human maintainer remains
responsible for reviewing and publishing all changes.

## License

FilaScan Import is released under the [MIT License](LICENSE). FilaMan, FilaScan,
Bambu Lab and related product names remain the property of their respective
owners. This community plugin is not affiliated with or endorsed by Bambu Lab.
