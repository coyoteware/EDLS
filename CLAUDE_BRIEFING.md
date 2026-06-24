# CLAUDE_BRIEFING.md

> A handoff note from the Claude instance that did the initial Python rewrite, to
> whichever Claude picks this up next (likely in VS Code / Claude Code). This is
> NOT documentation for a human — it's here to reconstruct working context that
> won't survive a new session. Andrew (CMDR Coyote Bongwater) is the person.

## What this project is
EDLS — Elite: Dangerous Launch Suite. A Windows desktop launcher that starts
Steam → Elite: Dangerous → up to ~11 companion apps, monitors the game, and
tears the tools down on exit. We are **rewriting it from PowerShell/WPF into
Python**. The original was a single 1,529-line `.ps1` monolith; the rewrite
exists to engineer out that monolith's structural problems (a 4-place app
registry, two divergent config dialogs, duplicated process handling, empty
catch blocks, a broken build-version regex).

## Architecture (three files — deliberately consolidated, do not re-proliferate)
Andrew explicitly pushed back on an 11-file layout. Keep it at three. The one
hard rule that earns its keep: **core logic has zero GUI imports.**

- **config.py** — pure data layer. The single-source app `REGISTRY`, `AppState`
  / `ShutdownScope` enums, `AppDescriptor` (with `resolve_path()`), `Settings`,
  and `load_settings` / `save_settings` (merge-on-load + `RemovedDefaults`
  bookkeeping + `recompute_removed_defaults`). Imports only stdlib. JSON keys are
  snake_case (intentionally NOT compatible with the old PowerShell PascalCase —
  settings reset once on migration, which is fine).
- **main.py** — engine + entry point, no Qt. `Signal` (tiny observer),
  `ProcessManager` (the ONE home for find/launch/kill, tracks what *we* launched
  vs. merely-running), `Engine` (launch→monitor→shutdown on a worker thread,
  emits `on_log` / `on_status` / `on_busy`). `main()` wires config → engine → ui.
- **ui.py** — the only Qt file (PySide6). `ChamferedFrame` (paints the clipped
  polygon corners QSS can't do), `PulsingDot`, `StatusCard`, `LogView`,
  `HeaderPanel`, `StatusPanel`, `LogPanel`, `ButtonBar`, `SettingsDialog`,
  `MainWindow`, `Background`. `run(engine, settings)` is the entry the engine
  calls. The engine→view seam is `engine.dispatch` (ui injects a GUI-thread
  invoker) + signals; ui→engine is plain method calls. **ui.py must never be
  imported by main.py except the late import inside `main()`** (avoids a cycle).

## Decisions already made (don't relitigate without reason)
- **PySide6, native, with QSS** — chose this over a web-render shell even though
  a polished HTML/SVG mockup exists. Andrew said "let's be brave and use QSS."
- **ShutdownScope is a user setting** (`LAUNCHED_ONLY` | `ALL_ENABLED`) — resolves
  the original's ambiguity where the in-game-exit teardown killed only launched
  tools but the manual SHUTDOWN button killed all enabled. Both now honor the
  setting. This was Andrew's explicit call ("leave that as a setting").
- **Single-source registry** — adding a companion app is one entry in
  `config.REGISTRY`. The old 4-places-to-edit problem is designed out.
- **Display names are a UI concern** — `_DISPLAY_NAMES` in ui.py maps identifier →
  friendly label (currently just `OdysseyMaterials` → "Odyssey Mats Helper").
  The descriptor.name stays the key for status routing + settings.

## Assets (in assets/, loaded by relative path)
- `EUROCAPS.TTF` — family name is **"Euro Caps"** (with a space). This is the
  display + body font now; QSS uses `font-family: 'Euro Caps'` (single quotes —
  double quotes caused an escaping mess via sed, avoid).
- `window-bg.png` — the real background (starfield/nebula/dot-matrix + corner
  brackets), painted scaled-to-cover in `Background`. Replaced a hand-drawn hex grid.
- `title-bar.png` (wide header frame, chevrons framing an empty center) and
  `outpost.png` (amber station icon) — **NOT yet integrated.** Reserved for the
  header redesign (see Next).
- `icon.ico` — window/app icon, wired up.

## Verification workflow (important — match this rigor)
The previous sandbox had no display, so the standard was: AST-parse + a custom
**cross-file contract audit** (grep every `config.*` / `AppState.*` /
`ShutdownScope.*` reference against what config actually defines; check for unused
imports). This caught real bugs before runtime: a `config.APP_VERSION` mismatch
and an over-escaped QSS `font-family`. **In VS Code you have Andrew's real machine
+ display**, so you can actually run `python main.py` and `python selftest.py`
(an offscreen 11-stage smoke test that constructs the window, drives every
AppState/log level, the dialogs, the boot animation, the glyph icons). Run
selftest after edits. You can launch the app but you CANNOT see pixels —
**Andrew is the eyes now**; ask him to look or describe.

## Current state (visually confirmed via offscreen renders)
Main window and settings dialog both render correctly. Done + verified: Euro Caps
font, window-bg, icon, panel/card transparency, smaller CMDR name, Odyssey
rename, button icons resized ~40% + the broken shutdown power glyph fixed (it's
rendered via Segoe UI Symbol on Windows; the old inline glyph inherited Euro Caps
which lacks it), card-clipping fixed via taller card minimums (104 normal / 90
wide) and a taller default window (1000×880, min 900×800), log min-height 130.

## Next up (the deferred, coupled pass)
Three coupled items, intentionally held so Andrew can eyeball iterations:
1. **Phone aspect-ratio lock** (~2:1 portrait — Andrew was unsure of the exact
   ratio, wants to see it). The original used a WM_SIZING hook; in Qt enforce via
   resizeEvent with a recursion guard. This needs a **portrait relayout** — the
   current 4-column status grid + horizontal button bar is landscape; portrait
   likely wants ~2 columns and a wrapped/stacked button bar. Don't bolt the
   aspect lock on without the relayout or it'll look cramped.
2. **`title-bar.png` header redesign** — use it as the header banner; its chevrons
   frame the center where the title text goes. Drop the current drawn ◆ diamonds /
   separator so decorations don't double up. Watch the 14.5:1 aspect (very thin).
3. **`outpost.png`** placement in the header (amber station icon, left side).
Settings dialog stays 1:1 / its own size.

## How Andrew works (calibrate to this)
Casual, direct, technical. Values honesty and genuine risk-flagging over false
confidence or over-hedging. Likes momentum + iterative visual feedback. Pushes
back on over-engineering (the 11→3 file consolidation was him). Appreciated being
told when something shouldn't be shipped blind. Don't pad; don't over-apologize;
own mistakes plainly and move on. Will call you Big C when he is pleased.
