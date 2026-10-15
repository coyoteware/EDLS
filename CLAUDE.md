# CLAUDE.md

Guidance for Claude Code when working in this repo.

## What this is
EDLS — Elite: Dangerous Launch Suite. A Windows (10/11) desktop launcher that
starts Steam → Elite: Dangerous → up to ~11 companion apps, monitors the game,
and tears the tools down on exit. This is a Python/PySide6 rewrite of an original
PowerShell/WPF tool. See `README.md` for user-facing setup (it requires
[min-ed-launcher] and [Legendary] configured first).

## Architecture — three files, one hard rule
Keep it at **three modules**. Andrew explicitly rejected an 11-file layout; do not
re-proliferate. The rule that earns its keep:

> **Core logic has zero GUI-toolkit imports.** Only `ui.py` may import Qt.

- **`config.py`** — pure data layer (stdlib only). The single-source app
  `REGISTRY`, `AppState`/`ShutdownScope` enums, `AppDescriptor` (with
  `resolve_path()`), `Settings`, `load_settings`/`save_settings` (merge-on-load +
  `removed_defaults` bookkeeping). Imported by both other files; imports neither.
- **`main.py`** — engine + entry point, **no Qt**. `Signal` (tiny observer),
  `ProcessManager` (the ONE home for find/launch/kill; tracks what *we* launched
  vs. merely-running), `Engine` (launch→monitor→shutdown on a worker thread,
  emits `on_log`/`on_status`/`on_busy`). `main()` wires config → engine → ui.
- **`ui.py`** — the only Qt (PySide6) file. The engine↔view seam is
  `engine.dispatch` (ui injects a GUI-thread invoker) + signals for engine→view,
  and plain method calls for view→engine.

**`ui.py` must never be imported by `main.py`** except the late `import ui` inside
`main()` (this avoids an import cycle). Adding a companion app = one entry in
`config.REGISTRY`, nowhere else.

## Commands
```powershell
pip install -r requirements.txt   # PySide6 + psutil
python main.py                     # run the app
python selftest.py                 # headless (offscreen) smoke test — run after UI edits
```
`selftest.py` builds the real window, drives every `AppState`/log level, the
dialogs, the boot animation, and the glyph icons. It catches runtime errors
(bad enum refs, throwing QSS, ctor-arg mismatches) but **does not verify pixels**.

## Verification
You can run the app but **cannot see the result** — Andrew is the eyes. After a
visual change, run `selftest.py`, then ask Andrew to look or describe. Don't claim
a visual outcome you haven't confirmed with him.

## Conventions / gotchas
- **QSS font-family** uses single quotes: `font-family: 'Euro Caps'`. Double quotes
  caused an escaping mess — avoid.
- **Fonts** (`assets/`): `EUROCAPS.TTF` → family **"Euro Caps"** (with a space),
  the display/body face. `ShareTechMono-Regular.ttf` → "Share Tech Mono", used by
  `font_mono()` for status text + the activity log; it falls back to Consolas only
  if the TTF is missing.
- **JSON settings keys are snake_case** — intentionally NOT compatible with the old
  PowerShell PascalCase (settings reset once on migration; that's fine).
- **`ShutdownScope` is a user setting** (`LAUNCHED_ONLY` | `ALL_ENABLED`); both the
  SHUTDOWN button and the on-exit teardown honor it. Don't hardcode one behavior.
- **EDLS never closes Steam or the game** (force-closing Elite breaks Frontier's
  ToS). `ProcessManager.stop()` refuses `PROTECTED_PROCESSES`; never add a kill
  path around it, and the Elite card has no STOP.
- **Every card status goes out through `Engine._status()`.** The status watcher
  (which replaced REFRESH) compares each scan with what `_status()` recorded and only
  emits differences — emitting any other way makes cards stale or flicker.
- **Display names are a UI concern** (`_DISPLAY_NAMES` in `ui.py`); `descriptor.name`
  stays the key for status routing + settings.
- **`config.APP_VERSION`** is the single source of truth for the version string.
- **The panes are the dark HUD in both themes** (the "Full Daylight" look): day mode
  lightens the window, header, bottom bar and card keys, not the status/log panes or
  cards. Pane surfaces and everything on a card read the fixed `theme.PANE.*`, never
  the themed globals of the same name. Only text drawn straight on a pane (its title,
  the activity log) themes, via `PANE_TITLE` / `PANE_META` / `LOG_TIME` / `LOG_COLORS`,
  because the see-through pane comes out mid-grey on the light window.
- Assets are loaded by relative path from `assets/` and must degrade gracefully if
  missing (the app should always run). Art is SVG, rendered directly with
  `QSvgRenderer` — there are no PNGs (`icon.ico` is the only raster asset).

## How Andrew works
Casual, direct, technical. Values honest risk-flagging over false confidence; don't
pad or over-apologize. Likes momentum and iterative visual feedback. Pushes back on
over-engineering. Flag when something shouldn't ship blind.

[min-ed-launcher]: https://github.com/rfvgyhn/min-ed-launcher
[Legendary]: https://github.com/derrod/legendary
