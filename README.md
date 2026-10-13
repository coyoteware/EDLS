# **WIP README!!!**



# EDLS — Elite: Dangerous Launch Suite

> Launch _Elite_ (bypassing the Frontier launcher!) - and your preferred selection of companion apps like EDSM,
> SrvSurvey, and more, in a single click.

EDLS is a Windows desktop launcher for [Elite: Dangerous](https://www.elitedangerous.com/).
It will find and boot Steam, then Elite via legendary and min-ed-launcher (see below), then up to a dozen companion apps of your choosing,
show a live HUD-style status board while you play, and shut itself and the rest of your companion apps down when Elite closes. Note that this does require a small amount of configuration on your end; if you skip setup for min-ed-launcher and legendary, EDLS will not actually save you much of any time or effort.

>Claude opus 5.5 was used in the making of this program. I have a degree in computer science which predates the advent of shatGPT and its ilk. If you try to lump me in with mouth breathing vibecoder scrubs I will personally escort a thargoid titan up your bunghole

---

## Features

- **One-click launch** — Steam, Elite: Dangerous, and your desired programs all at once,
  bypassing Frontier's native launcher (see
  [Prerequisites](#prerequisites)).
- **Live status board** — a card per app showing Offline / Launching / Online
- **Automatic teardown** — when Elite exits, EDLS closes the companion tools for you, then itself.
- **Per-app start/stop** — bring a single tool up or down from its status card.
- **Configurable** — add/remove/enable/disable apps and edit their paths in a
  built-in settings table. Add a commander name if you please. There's also a (work in progress) light mode ui
- **Auto-start** — Off by default for easier setup. You should turn this on once you have EDLS configured to your liking, otherwise it's 2 clicks instead of 1

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

Release coming soon :)

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

- CMDR Coyote Bongwater
- VScode, Claude Opus 5.5
- The Elite: Dangerous tool community behind the companion apps above.
- [min-ed-launcher](https://github.com/rfvgyhn/min-ed-launcher) by rfvgyhn and
  [Legendary](https://github.com/derrod/legendary) by derrod — the launch-chain
  tools EDLS builds on.

## License

[MIT](LICENSE) © 2026

---

*Fly safe, CMDR. o7*
