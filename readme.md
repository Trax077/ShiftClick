# ShiftClick

ShiftClick is a small Windows-only Python utility for simulated left mouse clicking with a simple desktop GUI.

It is designed for quick use with a global hotkey and two operating modes:

- **Hold** — click while holding `Shift + Left Mouse Button`
- **Toggle** — press `Shift + Left Mouse Button` to start, press it again to stop

The app also supports an **Armed** state, configurable click interval in milliseconds, and persistent settings saved to a local JSON config file.

## Features

- Simple GUI built with `tkinter`
- Global input handling outside the app window
- Left-click simulation using Windows `SendInput`
- Configurable click interval in milliseconds
- `0 ms` option for maximum possible speed
- **Hold** and **Toggle** modes
- **Armed / Disarmed** safety state
- Status indicator:
  - `DISARMED`
  - `ARMED`
  - `CLICKING`
- Saves last used settings to `shiftclick_config.json`

## Planned behavior

### Hold mode
When the app is **Armed**:

- normal left click behaves normally
- holding `Shift + LMB` starts autoclicking
- releasing the condition stops autoclicking

### Toggle mode
When the app is **Armed**:

- pressing `Shift + LMB` toggles autoclick **ON**
- pressing `Shift + LMB` again toggles autoclick **OFF**
- in the first version, a normal plain `LMB` click also stops currently running autoclick

## Requirements

- Windows
- Python 3.10+ recommended
- [`pynput`](https://pypi.org/project/pynput/)

`tkinter` is included with standard Python installations on Windows.

## Installation

Clone the repository:

```bash
git clone https://github.com/yourname/shiftclick.git
cd shiftclick