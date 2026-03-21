# ShiftClick

ShiftClick is a small Windows-only Python autoclicker with a simple `tkinter` GUI. It uses `SendInput` for simulated left mouse clicks and supports global input handling through `pynput`.

## Features

- Hold mode: click while holding `Shift + Left Mouse Button`
- Toggle mode: press `Shift + Left Mouse Button` to start, press it again to stop
- Armed / Disarmed safety state
- Configurable click interval in milliseconds
- `0 ms` option for maximum possible speed
- Status indicator: `DISARMED`, `ARMED`, `CLICKING`
- Persistent settings in `%APPDATA%\ShiftClick\config.json`

## Behavior

### Hold mode

When the app is armed:

- normal left click behaves normally
- holding `Shift + LMB` starts autoclicking
- releasing the condition stops autoclicking

### Toggle mode

When the app is armed:

- pressing `Shift + LMB` toggles autoclick on
- pressing `Shift + LMB` again toggles autoclick off
- a plain `LMB` click also stops currently running autoclick

## Requirements

- Windows
- Python 3.10+
- [`pynput`](https://pypi.org/project/pynput/)

`tkinter` is included with standard Python installations on Windows.

## Installation

```bash
git clone https://github.com/Trax077/ShiftClick.git
cd ShiftClick
python -m pip install pynput pytest pyinstaller
```

## Run

```bash
python ShiftClick.py
```

## Tests

```bash
python -m pytest -q
```

## Build

The repository includes a PyInstaller spec file:

```bash
pyinstaller ShiftClick.spec
```
