# FilaScan Spool Import

Version 0.1.11. This directory is the installable FilaMan plugin payload.

FilaMan integration plugin providing:

- `GET /api/v1/devices/filascan/status`
- `POST /api/v1/devices/filascan/import-spool?type=bambu`
- `POST /api/v1/devices/filascan/spools/{spool_id}/location`
- `POST /api/v1/devices/filascan/spools/{spool_id}/archive`

Requests authenticate with an existing FilaMan Device token. Open the plugin
page in FilaMan, select the already registered FilaScan device, and save the
allow-list. Until a device is explicitly selected, every import is denied.

The request's 32-character Block-9 Tray UUID is normalized and stored as
`external_id=bambulab:<TRAY_UUID>`. Physical RFID chip identifiers are not part
of the schema and are never stored. A required positive `location_id` selects a
regular user-managed storage location for newly created spools. Bambu and other
printer-driver-managed locations are rejected. Manufacturer, ordered colors,
filament, location and spool are resolved or created in a single idempotent
transaction; re-importing an existing Tray UUID never moves its spool.

Existing spools can be moved explicitly through the location endpoint. The
plugin validates the destination again, records the move against the authorized
FilaScan device, and emits `spools_changed`. Choosing the current location is
an idempotent success and creates no duplicate movement event.

The archive endpoint performs a soft archive through FilaMan's normal status
event path. The event is attributed to the authorized FilaScan device. Repeated
archive calls create no duplicate event, and re-importing the same Tray UUID
restores the archived record without changing its location or creating another
spool.

For Bambu imports carrying a five-digit product/color code, an existing Bambu
Lab filament with the same code and material type is reused even when its
FilamentDB display name differs. Duplicate FilamentDB rows for the same product
are resolved deterministically in favor of the canonical FilamentDB record.
Explicitly conflicting Bambu material or variant metadata is never merged.

FilaScan shares the Bambu plugin's canonical `article_number` product-code
field. The color name and RGB values use FilaMan's native filament/color
records. Only `article_number`, `bambu_filament_length_m`, and
`bambu_production_date` are registered as visible FilaScan extra fields; legacy
definitions are removed without deleting retained record metadata.

The configuration page follows the signed-in FilaMan user's English or German
language preference and defaults to FilaScan's dark brand theme. It remains a
standalone plugin page and does not render a duplicate FilaMan sidebar.

This plugin is distributed under the MIT License included in the release ZIP.
