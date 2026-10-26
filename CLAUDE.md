# CLAUDE.md

Guidance for Claude Code when working in this repo.

## What this is
EDLS — Elite: Dangerous Launch Suite. A Windows (10/11) desktop launcher that
starts Steam → Elite: Dangerous → up to ~12 companion apps, monitors the game,
and tears the tools down on exit. This is a Python/PySide6 rewrite of an original
PowerShell/WPF tool. See `README.md` for user-facing setup (it requires
[min-ed-launcher] and [Legendary] configured first).

## Architecture — three modules (+ theme), one hard rule
Keep it at **three modules**, plus `theme.py` (the view's design tokens). Andrew
explicitly rejected an 11-file layout; do not re-proliferate. The rule that earns its
keep:

> **Core logic has zero GUI-toolkit imports.** Only the view (`ui.py` and `theme.py`)
> may import Qt.

- **`config.py`** — pure data layer (stdlib only). The single-source app
  `REGISTRY`, `AppState`/`ShutdownScope` enums, `AppDescriptor` (with
  `resolve_path()`), `Settings`, `load_settings`/`save_settings` (merge-on-load +
  `removed_defaults` bookkeeping). Imported by both other files; imports neither.
- **`main.py`** — engine + entry point, **no Qt**. `Signal` (tiny observer),
  `ProcessManager` (the ONE home for find/launch/kill; tracks what *we* launched
  vs. merely-running), `Engine` (launch→monitor→shutdown on a worker thread,
  emits `on_log`/`on_status`/`on_busy`). `main()` sets up logging (uncaught errors
  → launcher.log via `install_crash_logging()`), then wires config → engine → ui.
- **`ui.py`** — the Qt (PySide6) view. The engine↔view seam is
  `engine.dispatch` (ui injects a GUI-thread invoker) + signals for engine→view,
  and plain method calls for view→engine.
- **`theme.py`** — palette, type and shape tokens from the EDLS Design System, plus
  `apply_theme()`, `font_*()` and `power_icon()`. Imports Qt; only `ui.py` uses it.

**`ui.py` must never be imported by `main.py`** except the late `import ui` inside
`main()` (this avoids an import cycle). Adding a companion app = one entry in
`config.REGISTRY`, nowhere else.

## Commands
```powershell
pip install -r requirements.txt   # PySide6-Essentials + psutil (pinned)
python main.py                     # run the app
python selftest.py                 # headless (offscreen) smoke test — run after UI edits
.\build.ps1                        # release: PyInstaller one-folder app + Inno Setup
                                   # installer -> dist\ (own build\venv; needs Inno Setup 6)
```
`selftest.py` builds the real window, drives every `AppState`/log level, the
dialogs, the boot animation, and the glyph icons. It catches runtime errors
(bad enum refs, throwing QSS, ctor-arg mismatches) but **does not verify pixels**.
Its process checks run on fakes (monkeypatched `find`/`find_many`/`_open_uri`/
`_ask_to_close`): a selftest must never hand a real PID to taskkill or kill, or open a
`steam://` URI.

## Verification
You can run the app but **cannot see the result** — Andrew is the eyes. After a
visual change, run `selftest.py`, then ask Andrew to look or describe. Don't claim
a visual outcome you haven't confirmed with him.

## Conventions / gotchas
- **QSS font-family** uses single quotes: `font-family: 'Euro Caps'`. Double quotes
  caused an escaping mess — avoid.
- **Fonts** (`assets/`): `EUROCAPS.TTF` → family **"Euro Caps"** (with a space),
  the display/body face. `ShareTechMono-Regular.ttf` → "Share Tech Mono", used by
  `font_mono()` for status text, the activity log and Settings; it falls back to
  Consolas only if the TTF is missing (checked via `QFontInfo` — `exactMatch()` is
  False at some point sizes even when the font is loaded).
- **JSON settings keys are snake_case** — intentionally NOT compatible with the old
  PowerShell PascalCase (settings reset once on migration; that's fine).
- **`ShutdownScope` is a user setting** (`LAUNCHED_ONLY` | `ALL_ENABLED`); both the
  SHUTDOWN button and the on-exit teardown honor it. Don't hardcode one behavior.
- **EDLS never closes Steam or the game** (force-closing Elite breaks Frontier's
  ToS). `ProcessManager.stop()` refuses `PROTECTED_PROCESSES`; never add a kill
  path around it, and the Elite card has no STOP. The Steam card's STOP is refused
  while the game runs, and LAUNCH attaches to an already-running game instead of
  sending Steam a second `steam://run`.
- **Closing a tool is polite first.** `ProcessManager.stop()` asks (WM_CLOSE via
  `taskkill` without /F) and kills only what's still running after `CLOSE_GRACE`
  (3 s); `_teardown` asks every target up front so the waits overlap. Keep all of it
  inside `ProcessManager`. A tool can run as several same-name processes (Electron:
  EDHM-UI): `find_many` returns the main one (`_root`, whose parent isn't one of
  them), and `stop()` then sees off its same-name helpers, nothing else it started.
- **Admin rights only on request.** EDLS never runs elevated (nor do Steam or the game).
  A tool whose manifest asks for admin rights (EDHM-UI-V3: `highestAvailable`) makes
  Popen fail with WinError 740. Only then, and only if `Settings.allow_elevation` is
  on, does `ProcessManager` retry via `_run_as_admin` (the UAC prompt). A tool EDLS
  can't close is closed by an elevated copy of EDLS (`--close-elevated` →
  `_close_elevated`) that runs the same `ProcessManager.stop()`. Never add an
  elevated `taskkill`/kill around it. UAC prompts block their thread, so companion
  START runs on a worker.
- **A path whose exe isn't the watched process is a launcher.** `launch()` then waits
  (`LAUNCHER_WAIT`) for the tool itself and tracks that by name. Odyssey Materials
  Helper's resolver prefers its updating launcher over the helper exe; `_RETIRED_PATHS`
  moves a saved old default path over on load.
- **Every card status goes out through `Engine._status()`.** The status watcher
  (which replaced REFRESH) compares each scan with what `_status()` recorded and only
  emits differences — emitting any other way makes cards stale or flicker.
- **Display names are a UI concern** (`_DISPLAY_NAMES` in `ui.py`); `descriptor.name`
  stays the key for status routing + settings.
- **`config.APP_VERSION`** is the single source of truth for the version string
  (`build.ps1` reads it for the exe's version resource and the installer).
- **The panes are the dark HUD in both themes** (the "Full Daylight" look): day mode
  lightens the window, header, bottom bar and card keys, not the status/log panes or
  cards. Pane surfaces and everything on a card read the fixed `theme.PANE.*`, never
  the themed globals of the same name. Only text drawn straight on a pane (its title,
  the activity log) themes, via `PANE_TITLE` / `PANE_META` / `LOG_TIME` / `LOG_COLORS`,
  because the see-through pane comes out mid-grey on the light window.
- **No native popups.** Small dialogs are `HudPopover` subclasses (frameless,
  chamfered, centred on the parent); notices/confirms go through `hud_notice()` /
  `hud_confirm()`, never `QMessageBox`. Settings (a normal window) and the popovers
  take their colours from `_dialog_colors()` and buttons from `_key()`.
- Assets are loaded by relative path from `assets/` and must degrade gracefully if
  missing (the app should always run). Art is SVG, rendered directly with
  `QSvgRenderer` — there are no PNGs (`icon.ico` is the only raster asset).

## How Andrew works
Casual, direct, technical. Values honest risk-flagging over false confidence; don't
pad or over-apologize. Likes momentum and iterative visual feedback. Pushes back on
over-engineering. Flag when something shouldn't ship blind.

[min-ed-launcher]: https://github.com/rfvgyhn/min-ed-launcher
[Legendary]: https://github.com/derrod/legendary
