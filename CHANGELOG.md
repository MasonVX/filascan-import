# Changelog

All notable changes to this project are documented in this file.

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
