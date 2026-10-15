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
        # A corrupt SVG doesn't raise -- Qt prints a line and hands back an
        # invalid renderer, so the UI silently drops the art. (That's how
        # truncated window-bg PNGs went unnoticed for months.)
        import glob
        from PySide6.QtSvg import QSvgRenderer
        svgs = glob.glob(os.path.join(ui.ASSETS_DIR, "*.svg"))
        assert svgs, "no SVG assets found"
        bad = [os.path.basename(p) for p in svgs
               if not QSvgRenderer(p).isValid()]
        assert not bad, f"assets that don't decode: {bad}"
    run("assets decode", assets_decode)

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

    def status_watcher():
        # The watcher replaced REFRESH. Feed _watch_tick() fake scans on a
        # headless engine (inline dispatch) and check it reports exactly the
        # changes: nothing more, nothing less.
        import time as _time
        S = config.AppState
        w = eng.Engine(settings)
        emits, logs = [], []
        w.on_status.connect(lambda k, s, p: emits.append((k, s)))
        w.on_log.connect(lambda m, lvl: logs.append(m))
        apps = w.tracked_apps
        steam = config.SYSTEM_APPS["Steam"].process_name
        elite = config.SYSTEM_APPS["Elite"].process_name

        class P:   # stands in for a psutil.Process
            pid = 4242

        def tick(found, started=None):
            # Real ticks are seconds apart; these run back to back. A plain tick
            # therefore pretends its scan began after every earlier update.
            emits.clear(); logs.clear()
            w._watch_tick(apps, found, started or _time.monotonic() + 1,
                          quiet=False)
            return dict(emits)

        # First sight: every card gets a state, and nothing is logged.
        emits.clear()
        w._watch_tick(apps, {steam: P()}, _time.monotonic(), quiet=True)
        got = dict(emits)
        assert got["Steam"] is S.ONLINE and got["Elite"] is S.OFFLINE, got
        assert len(got) == len(apps) and not logs, (got, logs)
        # Steam closes on its own -> Offline, with a log line.
        assert tick({}) == {"Steam": S.OFFLINE} and logs, logs
        # Nothing changed -> silence.
        assert tick({}) == {} and not logs
        # "Waiting..." is left alone until the game actually appears.
        w._status("Elite", S.WAITING)
        assert tick({}) == {}, "watcher overwrote Waiting..."
        assert tick({elite: P()}) == {"Elite": S.RUNNING}
        # A card set by an action after the scan began is skipped (no race).
        before = _time.monotonic()
        w._status("Steam", S.ONLINE)
        assert "Steam" not in tick({}, started=before)
        # ERROR (couldn't close) counts as running: kept while it runs,
        # cleared to Offline once it's gone.
        w._status("Steam", S.ERROR)
        assert tick({steam: P(), elite: P()}) == {}
        assert tick({elite: P()}) == {"Steam": S.OFFLINE}
    run("status watcher reports only changes", status_watcher)

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
        from dataclasses import replace as _replace
        import main as _main
        for key in ("Steam", "Elite"):
            desc = config.SYSTEM_APPS[key]
            # ...and under a custom app's name, without the .exe, in any case.
            alias = _replace(desc, name="Custom",
                             process_name=desc.process_name[:-4].upper())
            for d in (desc, alias):
                assert _main.ProcessManager().stop(d) is \
                    _main.StopResult.PROTECTED, \
                    f"ProcessManager.stop would kill {d.process_name}"
    run("Steam / Elite are never killed", protected_processes)

    def elite_card_muting():
        # R-09: while the game is starting (or running) the Elite card button
        # greys out but still answers clicks -- with a log line, never an action.
        # Clicks go to a standalone panel: a real one is wired to the engine,
        # and a regression here must not be able to start the actual game.
        S = config.AppState
        panel = ui.StatusPanel([])
        card = panel.cards["Elite"]
        notices, actions = [], []
        card.notice.connect(notices.append)
        card.start_stop_clicked.connect(lambda *a: actions.append(a))
        for st in (S.WAITING, S.RUNNING):
            card.update_state(st, None)
            card.ctrl_btn.click()
        assert len(notices) == 2 and not actions, (notices, actions)
        # Right after the game stops, START is held for a moment.
        card.update_state(S.OFFLINE, None)
        assert card._blocked_reason, "START should be held briefly"
        panel.deleteLater()
        # LAUNCH mutes while the card (not a LAUNCH session) is starting it.
        win._on_status("Elite", S.WAITING, None)
        assert win._btns.launch_btn.muted, "LAUNCH should grey out"
        win._on_status("Elite", S.OFFLINE, None)
        assert not win._btns.launch_btn.muted
    run("Elite card / LAUNCH grey each other out", elite_card_muting)

    def error_card():
        # R-11: a tool EDLS couldn't close shows ERROR, and clicking retries
        # STOP. Standalone panel, so the click can't stop a real app.
        from dataclasses import replace as _replace
        panel = ui.StatusPanel([_replace(settings.apps[0], name="Tool",
                                         enabled=True)])
        card = panel.cards["Tool"]
        card.update_state(config.AppState.ERROR, 4321)
        assert card.ctrl_btn.text() == "ERROR", card.ctrl_btn.text()
        hits = []
        card.start_stop_clicked.connect(lambda n, running: hits.append(running))
        card.ctrl_btn.click()
        assert hits == [True], "ERROR click should ask to stop it again"
        panel.deleteLater()
    run("ERROR card button", error_card)

    def locked_settings():
        # R-15: during a session the app list can't change -- every edit path
        # is refused and reported (once) for the activity log.
        dlg = ui.SettingsDialog(settings, locked=True)
        reports, popups = [], []
        dlg.locked_attempt.connect(lambda: reports.append(1))
        real = ui.hud_notice
        ui.hud_notice = lambda *a, **k: popups.append(a)
        try:
            rows = dlg.table.rowCount()
            dlg._add_row(); dlg._edit_row(0); dlg._reset_defaults()
            dlg.table.selectRow(0); dlg._remove_row()
        finally:
            ui.hud_notice = real
        assert dlg.table.rowCount() == rows, "locked list was changed"
        assert len(popups) == 4 and reports == [1], (popups, reports)
        dlg.deleteLater()
    run("settings app list locked during a session", locked_settings)

    def fits_small_screen():
        # R-18: the window must fit 1024x768 with the taskbar (~1004x697 for
        # the window), and many apps must scroll instead of growing the window.
        from dataclasses import replace as _replace
        assert win.minimumSize() == ui.WIN_MIN, win.minimumSize()
        hint = win.minimumSizeHint()
        assert hint.width() <= 1004 and hint.height() <= 697, hint
        many = [_replace(settings.apps[0], name=f"App {i}", enabled=True)
                for i in range(30)]
        panel = ui.StatusPanel(many)
        assert panel.minimumSizeHint().height() < 200, \
            f"30 apps made the status panel {panel.minimumSizeHint().height()}px"
        panel.deleteLater()
    run("window fits 1024x768, cards scroll", fits_small_screen)

    def boot_animation():
        # R-21: the window is size-locked while panels animate, then unlocked.
        win._play_boot_animation()
        if ui.BOOT_ENABLED:
            assert win.minimumSize() == win.maximumSize(), "not locked"
            win._end_boot_lock()
            assert win.minimumSize() == ui.WIN_MIN, "not unlocked"
    run("boot animation (window locked meanwhile)", boot_animation)

    def keyboard_focus():
        # R-20: toggles work from the keyboard; the app list takes Space.
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent

        def key(widget, k):
            app.sendEvent(widget, QKeyEvent(QEvent.Type.KeyPress, k,
                                            Qt.KeyboardModifier.NoModifier))
        tog = ui.ToggleSwitch(False)
        assert tog.focusPolicy() == Qt.FocusPolicy.StrongFocus
        key(tog, Qt.Key.Key_Space)
        assert tog.isChecked(), "Space should switch it on"
        key(tog, Qt.Key.Key_Left)
        assert not tog.isChecked(), "Left should switch it off"
        assert win._btns.launch_btn.focusPolicy() == Qt.FocusPolicy.TabFocus
        dlg = ui.SettingsDialog(settings)
        dlg.table.setCurrentCell(0, 1)
        chk = dlg.table.cellWidget(0, 0).findChild(ui.QCheckBox)
        before = chk.isChecked()
        key(dlg.table, Qt.Key.Key_Space)
        assert chk.isChecked() != before, "Space should tick the row's ON box"
        dlg.deleteLater()
    run("keyboard: toggles, buttons, app list", keyboard_focus)

    def toggles_paint():
        # Settings is built but never shown here, so paint the HUD toggles
        # directly: both kinds, off and on, in both themes (day mode has no glow).
        for theme in (config.Theme.LIGHT, config.Theme.DARK):
            ui.apply_theme(theme)
            for cls in (ui.ToggleSwitch, ui.ThemeToggle):
                for on in (False, True):
                    assert not cls(on).grab().isNull()
    run("HUD toggles paint (switch + theme toggle)", toggles_paint)

    def single_instance():
        # R-23: a second copy finds the first and backs off. A throwaway key,
        # so a real EDLS running alongside the test isn't involved.
        key = f"EDLS-selftest-{os.getpid()}"
        try:
            assert ui.acquire_single_instance(key), "first copy refused"
            assert not ui.acquire_single_instance(key), \
                "second copy didn't notice the first"
        finally:
            if ui._instance_server is not None:
                ui._instance_server.close()
            ui._instance_server = None
    run("only one EDLS at a time", single_instance)

    run("button glyph icons",
        lambda: [ui._glyph_icon(c, ui.C_AMBER)
                 for c in ("\u23FB", "\u27F3", "\u2699", "\u25B6")])

    def settings_dialog(first_run):
        dlg = ui.SettingsDialog(settings, first_run=first_run)
        got = []
        dlg.saved.connect(got.append)
        # Flip the two close-EDLS toggles and check they come back out.
        dlg.toggle_close_exit.setChecked(not settings.close_on_game_exit)
        dlg.toggle_close_shutdown.setChecked(not settings.close_on_shutdown)
        dlg._save()   # exercises recompute_removed_defaults + replace path
        assert got and got[0].close_on_game_exit != settings.close_on_game_exit
        assert got[0].close_on_shutdown != settings.close_on_shutdown

    run("settings dialog (normal) build + save",
        lambda: settings_dialog(False))
    run("settings dialog (first-run) build + save",
        lambda: settings_dialog(True))

    def ghost_cmdr_name():
        # The default CMDR name is placeholder (ghost) text, never real text,
        # and saving an empty field resets the name to the default.
        from dataclasses import replace
        default = config.default_settings().cmdr_name

        def saved_name(start, typed):
            dlg = ui.SettingsDialog(replace(settings, cmdr_name=start))
            got = []
            dlg.saved.connect(got.append)
            shown = dlg.cmdr_edit.text()
            assert dlg.cmdr_edit.placeholderText() == default
            if typed is not None:
                dlg.cmdr_edit.setText(typed)
            dlg._save()
            dlg.deleteLater()
            return shown, got[0].cmdr_name

        assert saved_name(default, None) == ("", default), "default isn't ghost"
        assert saved_name("Jameson", None) == ("Jameson", "Jameson")
        assert saved_name("Jameson", "   ")[1] == default, "blank didn't reset"
    run("settings: ghost-text CMDR name", ghost_cmdr_name)

    def hud_popovers():
        # The HUD stand-ins for native dialogs build and show without error; an
        # invalid app entry is reported inline rather than in another popup.
        from PySide6.QtCore import QTimer
        entry = ui.AppEntryDialog(taken_names=["Steam"], parent=win)
        entry.name_edit.setText("steam")
        entry._on_accept()
        assert entry.error_lbl.isVisibleTo(entry) and not entry.result(), \
            "duplicate name should be refused inline"
        entry.show(); entry.close(); entry.deleteLater()
        QTimer.singleShot(0, lambda: QApplication.activeModalWidget().reject())
        assert ui.hud_confirm(win, "Restore defaults", "Sure?") is False
        QTimer.singleShot(0, lambda: QApplication.activeModalWidget().accept())
        ui.hud_notice(win, "App list locked", "Locked.")
    run("HUD popovers (app editor, confirm, notice)", hud_popovers)

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
