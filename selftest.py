"""
selftest.py - headless runtime smoke test for EDLS.

Run on a machine WITH PySide6 installed:

    python selftest.py

It uses Qt's "offscreen" platform, so no window appears and no display is
needed. It builds the real MainWindow and drives a representative set of
interactions to surface the runtime errors that static/AST checks can't:
bad enum references, QSS that throws, constructor-arg mismatches, signal
wiring, icon rendering, and the boot animation.

Each stage is isolated, so a failure points at the exact spot, and the run
keeps going to collect every failure rather than stopping at the first.
Exits 0 if everything passes, 1 otherwise.

Note: this calls config.load_settings(), which will create the app's normal
settings.json under %LOCALAPPDATA%\\EDLS if you don't already have one.
"""
import os
import sys
import traceback

# Must be set before QApplication is created.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def run(name, fn):
    try:
        fn()
        PASS.append(name)
        print(f"  [PASS] {name}")
    except Exception as exc:  # noqa: BLE001 - we want to catch and report all
        FAIL.append((name, traceback.format_exc()))
        print(f"  [FAIL] {name}: {exc}")


def summarize():
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("\n--- failure detail ---")
        for name, tb in FAIL:
            print(f"\n### {name}\n{tb}")
        sys.exit(1)
    print("Headless smoke test clean - the UI constructs and the signal/render "
          "paths run without error.")


def main():
    import config
    import main as eng
    import ui
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer

    app = QApplication.instance() or QApplication(sys.argv)
    ui._load_fonts()

    ctx = {}
    run("load settings", lambda: ctx.update(settings=config.load_settings()))
    run("build engine",  lambda: ctx.update(engine=eng.Engine(ctx["settings"])))
    run("build window",  lambda: ctx.update(
        win=ui.MainWindow(ctx["engine"], ctx["settings"])))

    if "win" not in ctx:
        summarize()
        return

    win = ctx["win"]
    settings = ctx["settings"]
    keys = ["Steam", "Elite"] + [a.name for a in settings.apps if a.enabled]

    def all_states():
        for i, st in enumerate(config.AppState):
            win._on_status(keys[i % len(keys)], st, 4321)
    run("render every AppState on a card", all_states)

    def all_logs():
        for lvl in ("success", "default", "warning", "error", "dim", "info"):
            win._on_log(f"selftest {lvl} line", lvl)
    run("render every log level", all_logs)

    run("busy toggle (launch button)",
        lambda: (win._btns.set_busy(True), win._btns.set_busy(False)))

    run("boot animation", win._play_boot_animation)

    run("button glyph icons",
        lambda: [ui._glyph_icon(c, ui.C_AMBER)
                 for c in ("\u23FB", "\u27F3", "\u2699", "\u25B6")])

    def settings_dialog(first_run):
        dlg = ui.SettingsDialog(settings, first_run=first_run)
        dlg._save()   # exercises recompute_removed_defaults + replace path

    run("settings dialog (normal) build + save",
        lambda: settings_dialog(False))
    run("settings dialog (first-run) build + save",
        lambda: settings_dialog(True))

    def pump_events():
        QTimer.singleShot(60, app.quit)
        app.exec()
    run("event loop / timers / animations", pump_events)

    summarize()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        print("\nSELFTEST CRASHED before staged checks could run.")
        sys.exit(1)
