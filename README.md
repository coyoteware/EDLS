# EDLS — Elite: Dangerous Launch Suite

> Starts the game and all your companion tools in one click, watches the game
> while you fly, and shuts the tools back down when you're done. o7

EDLS is a Windows desktop launcher for [Elite: Dangerous](https://www.elitedangerous.com/).
It brings up Steam → Elite → up to a dozen companion apps in the right order,
shows a live HUD-style status board while you play, and tears the third-party
tools down automatically when the game closes.

This is the **Python rewrite** of the original PowerShell/WPF suite. It keeps the
look and behaviour of the original while replacing the 1,500-line `.ps1` monolith
with three small, testable modules.

---

## Features

- **One-click launch** — Steam, Elite: Dangerous, and every enabled companion app,
  started in sequence. Optionally bypasses Frontier's native launcher (see
  [Prerequisites](#prerequisites)).
- **Live status board** — a card per app showing Offline / Launching / Online /
  Running, with pulsing status dots and PIDs.
- **Automatic teardown** — when Elite exits, EDLS closes the companion tools for you.
- **Manual shutdown** — a SHUTDOWN button to close the tools on demand.
- **Per-app start/stop** — bring a single tool up or down from its status card.
- **Configurable shutdown scope** — close *only what EDLS launched*, or *every
  enabled app that's running* — your choice in Settings.
- **Single-source app registry** — enable/disable apps and edit their paths in a
  built-in settings table. Auto-start is available and off by default.
- **Persistent config** — settings and a rotating log live under
  `%LOCALAPPDATA%\EDLS`.

---

## Requirements

- **Windows 10 / 11** (64-bit)
- **Python 3.11+**
- **Steam**, with Elite: Dangerous in your library
- Python packages: `PySide6` and `psutil` (see [Installation](#installation))
- **[min-ed-launcher](https://github.com/rfvgyhn/min-ed-launcher)** and
  **[Legendary](https://github.com/derrod/legendary)** configured first — see
  [Prerequisites](#prerequisites) below.

---

## Prerequisites

EDLS launches Elite through Steam and expects the launch to **skip Frontier's
default launcher** and drop you straight into the game. That hand-off is handled
by two external, open-source tools that you must install and configure **before**
running EDLS:

### 1. min-ed-launcher

[min-ed-launcher](https://github.com/rfvgyhn/min-ed-launcher) is a minimal,
fast replacement for the official Elite: Dangerous launcher. It is what actually
starts the game once Steam hands off, and it lets EDLS go from click to cockpit
without the Frontier launcher window in the way.

- Download and install it from the
  [min-ed-launcher releases page](https://github.com/rfvgyhn/min-ed-launcher/releases).
- Follow its
  [setup guide](https://github.com/rfvgyhn/min-ed-launcher#setup) to register it
  as the launcher Steam uses for Elite: Dangerous (the project documents the
  Steam launch-option / `EDLaunch.exe` replacement steps for each account type).
- Confirm Elite starts correctly through min-ed-launcher on its own **before**
  bringing EDLS into the picture.

### 2. Legendary

[Legendary](https://github.com/derrod/legendary) is an open-source, command-line
Epic Games Store client. If your Elite: Dangerous license is on an **Epic
account**, min-ed-launcher uses Legendary to authenticate without needing the
full Epic Games launcher installed.

- Install Legendary per its
  [README](https://github.com/derrod/legendary#installation).
- Log in once with `legendary auth` so your Epic credentials are stored.
- Point min-ed-launcher at Legendary as described in min-ed-launcher's
  [Epic setup notes](https://github.com/rfvgyhn/min-ed-launcher#epic).

> **In short:** get Elite launching cleanly through *min-ed-launcher* (with
> *Legendary* providing Epic auth if you're an Epic player) first. Once that
> chain works, EDLS simply triggers it and adds the companion-app orchestration
> on top.

---

## Installation

```powershell
# 1. Clone the repo
git clone https://github.com/coyoteware/EDLS-py.git
cd EDLS-py

# 2. (Recommended) create a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. Install dependencies
pip install -r requirements.txt
```

## Running

```powershell
python main.py
```

On first launch, EDLS shows a **first-time setup** dialog: set your CMDR name and
tick the companion apps you want it to manage. You can change all of this later
via the **SETTINGS** button.

---

## Configuration

Open **SETTINGS** from the main window to:

- Set your **CMDR name**.
- Toggle **status cards for inactive apps**.
- Choose the **shutdown scope** — close only apps EDLS launched, or all enabled
  apps that are running.
- **Add, remove, enable/disable** companion apps and edit each app's executable
  path. Paths support `%ENV%` variables (e.g. `%ProgramFiles%`).

Settings and the activity log are stored under:

```
%LOCALAPPDATA%\EDLS\settings.json
%LOCALAPPDATA%\EDLS\launcher.log
```

### Companion apps known out of the box

EDLS ships with default registry entries for these tools (each can be enabled,
disabled, or have its path edited):

| App | App | App |
| --- | --- | --- |
| EDMarketConnector | SrvSurvey | Odyssey Materials Helper |
| EDCoPilot | EDHM-UI | opentrack |
| ICARUS Terminal | EDDiscovery | VoiceAttack |
| EDCoPTER | Elite Observatory | |

Don't see your tool? Add it in Settings — give it a name, its process/executable
name, and a path.

---

## How it works

EDLS is intentionally three small files with one hard rule: **the core logic has
zero GUI imports**, so the engine can be driven and tested headlessly.

- **`config.py`** — the pure data layer: the app `REGISTRY`, the `AppState` /
  `ShutdownScope` vocabulary, the `Settings` model, and load/save with
  merge-on-load migration. Standard library only.
- **`main.py`** — the engine + entry point (no Qt): `ProcessManager` (the single
  home for find/launch/kill) and `Engine` (the launch → monitor → shutdown
  sequence on a worker thread), talking to the view only through signals.
- **`ui.py`** — the only Qt (PySide6) file: the Elite-HUD-styled window, status
  cards, activity log, settings dialog, and the framed title banner.

You can run a headless smoke test of the UI (uses Qt's offscreen platform, no
display needed):

```powershell
python selftest.py
```

---

## Credits

- **CMDR Coyote Bongwater** author of EDLS.
- **Claude** (Anthropic) — pair-programmer on the Python rewrite.
- The Elite: Dangerous tool community behind the companion apps above.
- [min-ed-launcher](https://github.com/rfvgyhn/min-ed-launcher) by rfvgyhn and
  [Legendary](https://github.com/derrod/legendary) by derrod — the launch-chain
  tools EDLS builds on.

## License

[MIT](LICENSE) © 2026

---

*Fly safe, CMDR. o7*