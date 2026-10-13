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

    def assets_decode():
        # A corrupt PNG doesn't raise -- libpng prints a line and Qt hands back
        # a null image, so the UI silently falls back to a flat fill. (That's
        # how truncated window-bg PNGs went unnoticed for months.)
        import glob
        from PySide6.QtGui import QImage
        pngs = glob.glob(os.path.join(ui.ASSETS_DIR, "*.png"))
        bad = [os.path.basename(p) for p in pngs if QImage(p).isNull()]
        assert not bad, f"assets that don't decode: {bad}"
    run("asset PNGs decode", assets_decode)

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

    def status_refresh():
        # The REFRESH button's whole job: reconcile the cards with what's really
        # running. Runs the scan inline here (engine.refresh_status() threads it)
        # so a failure surfaces in this stage instead of in a daemon thread.
        eng_obj = ctx["engine"]
        seen = {}
        eng_obj.on_status.connect(lambda k, s, p: seen.__setitem__(k, s))
        eng_obj._refresh()
        # Status goes out through engine.dispatch, which the view marshals onto
        # the GUI thread -- so the emissions are queued until the loop spins.
        app.processEvents()
        tracked = {a.name for a in eng_obj.tracked_apps}
        assert tracked <= set(seen), \
            f"refresh skipped cards: {tracked - set(seen)}"
    run("status refresh reports every card", status_refresh)

    def system_card_paint():
        # Steam/Elite are ordinary cards now (no more double-wide Elite). Render
        # explicitly: offscreen Qt doesn't guarantee a repaint, so a throwing
        # paintEvent would otherwise slip through the whole test.
        from PySide6.QtGui import QPixmap
        for key in ("Steam", "Elite"):
            card = win._status.cards[key]
            assert card.ctrl_btn is not None, f"{key} card has no START/STOP button"
            card.resize(190, 96)
            for st in (config.AppState.OFFLINE, config.AppState.LAUNCHING,
                       config.AppState.RUNNING, config.AppState.FAILED):
                card.update_state(st, 4321)
                card.render(QPixmap(card.size()))
    run("system card paint (every state)", system_card_paint)

    def inactive_cards():
        # Cards for apps the user hasn't enabled are read-only: dimmed, OFF tag,
        # no control button. Driving every state over BOTH kinds is what catches
        # a missing `if self.ctrl_btn is not None` guard in update_state.
        from dataclasses import replace as _replace
        from PySide6.QtGui import QPixmap
        base = settings.apps[0]
        panel = ui.StatusPanel([_replace(base, name="OnApp", enabled=True),
                                _replace(base, name="OffApp", enabled=False)])
        assert panel.cards["OnApp"].ctrl_btn is not None, \
            "enabled card lost its START button"
        assert panel.cards["OffApp"].ctrl_btn is None, \
            "not-enabled card should have no START button"
        for card in panel.cards.values():
            card.resize(190, 96)
            for st in config.AppState:
                card.update_state(st, 4321)
                card.render(QPixmap(card.size()))
        panel.deleteLater()
    run("inactive (not-enabled) cards", inactive_cards)

    def visible_apps_toggle():
        # The setting that was doing nothing: on -> every app gets a card,
        # off -> only enabled ones.
        from dataclasses import replace as _replace
        total, enabled = len(settings.apps), sum(a.enabled for a in settings.apps)
        win._settings = _replace(settings, show_inactive_cards=True)
        assert len(win._visible_apps()) == total, "setting ON should show all apps"
        win._settings = _replace(settings, show_inactive_cards=False)
        assert len(win._visible_apps()) == enabled, \
            "setting OFF should show only enabled apps"
        win._settings = settings
    run("show_inactive_cards selects the card list", visible_apps_toggle)

    def system_descriptors():
        # The regression that was shipping: Steam/Elite aren't in settings.apps,
        # so their cards' START/STOP resolved to nothing and silently no-opped.
        # Deliberately NOT calling _on_start_stop -- that would really launch Steam.
        for key in ("Steam", "Elite"):
            assert ctx["engine"]._find_descriptor(key) is not None, \
                f"{key} has no descriptor - its card button would no-op"
    run("system app descriptor lookup", system_descriptors)

    def protected_processes():
        # Killing the game violates the ToS; EDLS must refuse even if asked.
        # Safe to call for real: the guard returns before any process lookup.
        import main as _main
        for key in ("Steam", "Elite"):
            desc = config.SYSTEM_APPS[key]
            assert _main.ProcessManager().stop(desc) is False, \
                f"ProcessManager.stop would kill {key}"
    run("Steam / Elite are never killed", protected_processes)

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
