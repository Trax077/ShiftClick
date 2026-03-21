# Changelog

All notable changes to this project will be documented in this file.

## [1.0.1] - 2026-03-21

### Changed
- Moved configuration storage to `%APPDATA%\ShiftClick\config.json` so installed builds can save settings reliably.
- Restored saved window geometry automatically during startup instead of requiring a separate external call.
- Stopping active autoclicking is now enforced when switching between `hold` and `toggle` modes to keep behavior predictable.
- Added type hints to core pure helper functions for better readability and maintenance.

### Fixed
- Saving settings now creates the target configuration directory before writing the file.
- Configuration save failures are now shown to the user instead of failing silently.
- A `0 ms` interval no longer runs as a tight spin loop; the worker now uses a short wait to reduce unnecessary CPU load.
- Listener shutdown now avoids swallowing every exception silently and reports unexpected failures to `stderr`.

### UI
- Added an in-app warning that `0 ms` interval can increase CPU usage.
- Removed redundant font duplication in style setup and documented the global Tk font size adjustment.

## [1.0.0] - 2026-03-21

### Added
- Initial Windows desktop release of ShiftClick.
- Global Shift + Left Mouse Button autoclick support with `hold` and `toggle` modes.
- Live click statistics, click test area, persisted interval/mode settings, and restored window geometry.
