# Changelog

All notable changes to this project are documented in this file.

## [0.2.0] - 2026-09-12

### Added

- Idempotent OpenPrintTag spool import through `type=openprinttag`.
- Namespaced OpenPrintTag instance identity using FilaMan's native
  `external_id` field.
- Reuse of the existing FilaScan location selection, movement, archival and
  restoration workflow for OpenPrintTag spools.

OpenPrintTag imports do not create additional FilaMan system fields.

## [0.1.13] - 2026-08-30

### Changed

- Removed the short-lived spool-level `article_number` mirror. The canonical
  product/color code is stored only once on the shared filament again.
- Kept existing spool custom-field values untouched; no data cleanup migration
  is performed.

## [0.1.12] - 2026-08-30

### Fixed

- Registered `article_number` for spools as well as filaments so FilaMan's
  spool detail page displays the Bambu color/product code.
- Mirrored the authoritative filament article number into new and rescanned
  spools only when the spool field is empty.

## [0.1.11] - 2026-08-30

### Changed

- Aligned Bambu product metadata with the Bambu Lab plugin by writing the
  five-digit product/color code to canonical `article_number`.
- Reduced FilaScan's visible system extra fields from fifteen to three:
  `article_number`, `bambu_filament_length_m`, and `bambu_production_date`.
- Kept the color name and RGB color in FilaMan's native filament/color records
  instead of duplicating them as spool and filament extra fields.
- Kept material and temperature/drying metadata only on the shared filament,
  avoiding repeated copies on every physical spool.
- Stored product images in shared `filament_image_*` metadata instead of using
  the product-page `shop_url` field.

### Compatibility

- Obsolete FilaScan-owned field definitions are removed on the next scan while
  existing record metadata remains intact.
- Legacy `bambu_color_code` and `bambu_product_code` values remain accepted for
  matching existing records.

## [0.1.10] - 2026-08-22

### Added

- English and German translations for the complete plugin page, including
  dynamic status and validation messages.
- Automatic language selection from the signed-in FilaMan user profile with a
  local browser preference as fallback.
- Explicit location selection for newly imported spools.
- An authenticated endpoint for moving existing FilaScan spools between
  regular user-managed locations.
- Idempotent soft archival and restoration of existing spools.
- Release validation and deterministic ZIP packaging for GitHub releases.

### Changed

- Restored the standalone plugin-page layout without a duplicated FilaMan
  sidebar.
- Restored FilaScan's dark brand theme as the default while retaining explicit
  FilaMan light and dark theme preferences.
- Standardized physical spool identity on the normalized Bambu Tray UUID.

### Security

- Device access is denied until an administrator explicitly selects an active
  registered FilaScan device on the plugin page.
- Physical RFID chip UIDs are rejected and are neither logged nor stored.
