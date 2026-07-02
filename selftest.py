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

WARNING: the first stage DELETES the app's data dir (%LOCALAPPDATA%\\EDLS —
settings.json, launcher.log) so every run exercises the fresh-install path.
Afterwards the dir holds fresh defaults, and the next real launch will show
the first-run setup dialog again.
"""
import os
import shutil
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

    def wipe_app_data():
        # Fresh-install state every run. No ignore_errors: a partial wipe (e.g.
        # launcher.log locked by a running EDLS) must FAIL loudly, not fake it.
        if os.path.isdir(config.DATA_DIR):
            shutil.rmtree(config.DATA_DIR)

    ctx = {}
    run("wipe preexisting app data", wipe_app_data)
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

    def app_entry_dialog():
        # The add/edit dialog + its process-derivation, driven without .exec()
        # (which would block headless). Also add a row through the non-modal
        # insert path, then save the whole settings dialog with it in place.
        assert ui._derive_process(r"C:\x\Foo.exe") == "Foo"
        assert ui._derive_process("") == ""
        dlg = ui.AppEntryDialog("MyApp", r"C:\x\Foo.exe")
        assert dlg.values() == ("MyApp", r"C:\x\Foo.exe")

        sd = ui.SettingsDialog(settings)
        before = sd.table.rowCount()
        sd._insert_app_row("MyApp", r"C:\x\Foo.exe", "Foo")
        assert sd.table.rowCount() == before + 1
        sd._save()

    run("app entry dialog + row insert + save", app_entry_dialog)

    def duplicate_name_guard():
        # A name is the routing/launch key, so the dialog must reject duplicates
        # (case-insensitive) while allowing an unused name. Uses _validation_error
        # directly to avoid popping a modal warning headless.
        sd = ui.SettingsDialog(config.default_settings())
        taken = sd._existing_names()
        assert "SrvSurvey" in taken
        dlg = ui.AppEntryDialog(taken_names=taken)
        assert dlg._validation_error("SrvSurvey") is not None
        assert dlg._validation_error("srvsurvey") is not None   # case-insensitive
        assert dlg._validation_error("") is not None
        assert dlg._validation_error("A Brand New App") is None
        # Editing a row must allow keeping its own name (excluded from 'taken').
        assert "SrvSurvey" not in sd._existing_names(exclude_row=1)

    run("duplicate-name guard", duplicate_name_guard)

    def renamed_default_keeps_resolver():
        # Renaming a resolver-based default (SrvSurvey has no path) must not drop
        # its resolver — the row's registry origin survives the rename.
        sd = ui.SettingsDialog(config.default_settings())
        def _name(r):
            it = sd.table.item(r, 1)
            return it.text() if it else ""
        row = next(r for r in range(sd.table.rowCount())
                   if _name(r) == "SrvSurvey")
        sd._set_row(row, "My Survey", "SrvSurvey", "", True, origin="SrvSurvey")
        captured = {}
        sd.saved.connect(lambda s: captured.update(s=s))
        sd._save()
        app = next(a for a in captured["s"].apps if a.name == "My Survey")
        assert app.resolver is not None

    run("renamed default keeps resolver", renamed_default_keeps_resolver)

    run("settings pre-warm (off-screen realize)", win._prewarm_settings)

    def theme_switch():
        from dataclasses import replace
        # Drive a live light/dark switch both ways through the real save path,
        # which calls apply_theme() + rebuilds all content.
        win._on_settings_saved(replace(settings, theme=config.Theme.LIGHT))
        win._on_settings_saved(replace(settings, theme=config.Theme.DARK))
    run("light/dark theme switch + content rebuild", theme_switch)

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
