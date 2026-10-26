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
        # Cards for apps the user hasn't enabled are read-only: dimmed, DISABLED
        # tag, no control button. Driving every state over BOTH kinds is what catches
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
        for card in panel.cards.values():
            card.update_state(config.AppState.OFFLINE, None)
        assert panel.cards["OffApp"].state_lbl.text() == "", \
            "disabled card still says OFFLINE"
        assert panel.cards["OnApp"].state_lbl.text() == \
            config.AppState.OFFLINE.label, "enabled card lost its OFFLINE label"
        panel.deleteLater()
    run("inactive (not-enabled) cards", inactive_cards)

    def visible_apps_toggle():
        # The setting that was doing nothing: on -> every app gets a card,
        # enabled ones first, off -> only enabled ones.
        from dataclasses import replace as _replace
        total, enabled = len(settings.apps), sum(a.enabled for a in settings.apps)
        win._settings = _replace(settings, show_inactive_cards=True)
        assert len(win._visible_apps()) == total, "setting ON should show all apps"
        mixed = [_replace(a, name=f"{a.name}{i}", enabled=i % 2 == 0)
                 for i, a in enumerate(settings.apps[:1] * 4)]
        win._settings = _replace(settings, apps=mixed, show_inactive_cards=True)
        assert [a.name for a in win._visible_apps()] == \
            [a.name for a in mixed[0::2] + mixed[1::2]], \
            "enabled cards should come first, each group in Settings order"
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

    def crash_logging():
        # Uncaught errors -- a worker's, or a slot's via sys.excepthook -- must
        # reach launcher.log: the installed build has no console to print to.
        # The previous hooks are swapped for no-ops so nothing prints here.
        import logging
        import threading
        records = []

        class Grab(logging.Handler):
            def emit(self, record):
                records.append(record)

        grab = Grab()
        eng.log.addHandler(grab)
        saved = sys.excepthook, threading.excepthook
        sys.excepthook = lambda *a: None
        threading.excepthook = lambda a: None
        try:
            eng.install_crash_logging()
            worker = threading.Thread(target=lambda: 1 / 0)
            worker.start()
            worker.join()
            try:
                raise RuntimeError("selftest slot error")
            except RuntimeError:
                sys.excepthook(*sys.exc_info())
        finally:
            sys.excepthook, threading.excepthook = saved
            eng.log.removeHandler(grab)
        crit = [r for r in records if r.levelname == "CRITICAL" and r.exc_info]
        assert len(crit) == 2, records
    run("uncaught errors reach launcher.log", crash_logging)

    def game_safety_guards():
        # R-01: never steam://exit while the game runs. R-02: LAUNCH attaches
        # to a running game instead of asking Steam to start it again. Fakes
        # only -- nothing here may reach a real Steam, game or process.
        w = eng.Engine(settings)
        running, opened, logs = set(), [], []

        def fake_open(uri):
            opened.append(uri)
            if uri == eng.STEAM_URI_EXIT:
                running.discard(config.STEAM_PROCESS)
            else:                       # steam://run/<id>
                running.add(config.ELITE_PROCESS)

        class P:   # stands in for a psutil.Process
            pid = 4242

        w._open_uri = fake_open
        w.pm.find = lambda name: P() if name in running else None
        w.on_log.connect(lambda m, lvl: logs.append(lvl))

        running.update({config.STEAM_PROCESS, config.ELITE_PROCESS})
        w._card_stop_steam()
        assert not opened and logs[-1] == "warning", (opened, logs)
        assert w._launch_elite(timeout=0) is not None and not opened, opened
        # ...while the normal paths still work.
        running.discard(config.ELITE_PROCESS)
        assert w._launch_elite(timeout=0) is not None
        assert opened == [f"steam://run/{settings.elite_app_id}"], opened
        running.discard(config.ELITE_PROCESS)
        w._card_stop_steam()
        assert opened[-1] == eng.STEAM_URI_EXIT, opened
    run("game safety: no Steam exit or relaunch mid-game", game_safety_guards)

    def polite_close():
        # R-17: a tool is asked to close first and only killed if it ignores
        # that. Fake processes and a fake taskkill: a real PID must never
        # reach _ask_to_close from here.
        import time
        from dataclasses import replace as _replace
        import psutil
        asked, events = [], []
        real_ask = eng._ask_to_close

        def fake_ask(pid):
            asked.append(pid)
            events.append(("ask", pid))

        class Fake:
            def __init__(self, pid, obeys):
                self.pid, self.obeys, self.killed = pid, obeys, False

            def name(self):
                return "FakeTool.exe"

            def children(self, recursive=False):
                return []

            def wait(self, timeout=None):
                events.append(("wait", self.pid))
                if (self.obeys and self.pid in asked) or self.killed:
                    return 0
                raise psutil.TimeoutExpired(timeout)

            def kill(self):
                self.killed = True

        def tool(name):
            return _replace(settings.apps[0], name=name, process_name=name,
                            enabled=True)

        eng._ask_to_close = fake_ask
        try:
            pm = eng.ProcessManager()
            for obeys in (True, False):
                asked.clear()
                proc = Fake(990001, obeys)
                pm.find = lambda name, proc=proc: proc
                result = pm.stop(tool("FakeTool"), deadline=time.monotonic())
                assert result is eng.StopResult.STOPPED, result
                assert asked == [proc.pid], asked
                assert proc.killed is not obeys, (obeys, proc.killed)
            # Teardown asks every tool before it waits on any of them, so the
            # grace periods overlap.
            asked.clear()
            events.clear()
            procs = {"ToolA": Fake(990002, True), "ToolB": Fake(990003, True)}
            w = eng.Engine(_replace(settings, apps=[tool("ToolA"), tool("ToolB")]))
            w.pm.find = procs.get
            w.pm.find_many = lambda names: {n: procs[n] for n in names
                                            if n in procs}
            w._teardown(config.ShutdownScope.ALL_ENABLED)
            kinds = [kind for kind, _pid in events]
            assert kinds[:2] == ["ask", "ask"] and "ask" not in kinds[2:], events
            assert not any(p.killed for p in procs.values())
        finally:
            eng._ask_to_close = real_ask
    run("tools are asked to close before they're killed", polite_close)

    def multi_process_tools():
        # CA-05: an Electron app (EDHM-UI) is a main process that owns the
        # window plus same-name helpers. EDLS must find the main one wherever
        # Windows lists it, ask only it, and see the helpers off after it --
        # never touching anything else the tool started. Fakes only: no real
        # process is listed, asked or killed.
        import time
        import psutil
        real = (eng.psutil.process_iter, eng._ask_to_close)
        asked = []

        class Fake:   # stands in for a psutil.Process
            def __init__(self, pid, ppid, exe="EDHM-UI-V3.exe",
                         alone=False, obeys=True):
                self.pid, self._ppid, self._exe = pid, ppid, exe
                self.info = {"name": exe}
                self.alone, self.obeys = alone, obeys   # exits by itself / when asked
                self.kids, self.killed, self.touched = [], False, False

            def name(self):
                return self._exe

            def ppid(self):
                return self._ppid

            def children(self, recursive=False):
                out = []
                for kid in self.kids:
                    out += [kid] + (kid.children(True) if recursive else [])
                return out

            def wait(self, timeout=None):
                self.touched = True
                if self.killed or self.alone or (self.obeys and self.pid in asked):
                    return 0
                raise psutil.TimeoutExpired(timeout)

            def kill(self):
                self.touched = self.killed = True

        main = Fake(990010, ppid=990009)             # its launcher stub is gone
        quick = Fake(990011, ppid=main.pid, alone=True)
        stuck = Fake(990012, ppid=main.pid, obeys=False)   # no window to close
        other = Fake(990013, ppid=main.pid, exe="browser.exe")
        main.kids = [quick, stuck, other]

        eng.psutil.process_iter = lambda attrs=None: iter([quick, other, main,
                                                          stuck])
        eng._ask_to_close = asked.append
        try:
            pm = eng.ProcessManager()
            assert pm.find("EDHM-UI-V3") is main, "picked a helper, not the main"
            desc = config.AppDescriptor("EDHM_UI", "EDHM-UI-V3")
            result = pm.stop(desc, deadline=time.monotonic())
            assert result is eng.StopResult.STOPPED, result
            assert asked == [main.pid, stuck.pid], asked
            assert stuck.killed and not main.killed and not quick.killed
            assert not other.touched, "touched a different program it started"
        finally:
            eng.psutil.process_iter, eng._ask_to_close = real
    run("multi-process tools: main process first, helpers after",
        multi_process_tools)

    def admin_tools():
        # A tool that needs admin rights (EDHM-UI) makes Popen fail with
        # WinError 740. Windows may then ask (the UAC prompt) only while
        # allow_elevation is on, and closing one goes through an elevated copy
        # of EDLS. Fakes only: no real UAC prompt, launch, PID, taskkill or kill.
        import tempfile
        from dataclasses import replace as _replace
        # find_many via __dict__: the staticmethod itself, so it restores as one.
        real = (eng._popen_detached, eng._run_as_admin, eng._ask_to_close,
                eng.ProcessManager.__dict__["find_many"])
        S = config.AppState
        running, prompts, statuses, logs, decline = {}, [], [], [], [False]

        class P:   # stands in for a psutil.Process
            def __init__(self, pid):
                self.pid = pid

        def winerror(code):
            exc = OSError(f"fake WinError {code}")
            exc.winerror = code
            return exc

        def needs_admin(args, **kwargs):
            raise winerror(eng.ERROR_ELEVATION_REQUIRED)

        def fake_runas(path, args="", cwd=None, show=1):
            prompts.append((path, args))
            if decline[0]:
                raise winerror(eng.ERROR_CANCELLED)
            if eng.CLOSE_ELEVATED_FLAG in args:
                running.clear()          # the elevated copy closed them
            else:
                running["FakeAdmin"] = P(990101)

        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as fh:
            exe = fh.name
        tools = [_replace(settings.apps[0], name=n, process_name=n, path=exe,
                          enabled=True, resolver=None)
                 for n in ("FakeAdmin", "FakeAdmin2")]

        def engine(allow):
            w = eng.Engine(_replace(settings, apps=tools, allow_elevation=allow))
            w.pm.find = running.get
            w.pm.find_many = lambda names: {n: running[n] for n in names
                                            if n in running}
            w.pm.stop = lambda *a, **k: eng.StopResult.FAILED
            w.on_status.connect(lambda key, state, pid: statuses.append(state))
            w.on_log.connect(lambda msg, lvl: logs.append((lvl, msg)))
            return w

        eng._popen_detached = needs_admin
        eng._run_as_admin = fake_runas
        eng._ask_to_close = lambda pid: None
        try:
            # Launch: allowed -> one prompt, Online, tracked as ours.
            w = engine(True)
            w._start_one(tools[0])
            assert prompts == [(exe, "")], prompts
            assert statuses[-1] is S.ONLINE, statuses
            assert "FakeAdmin" in w.pm.launched_names()
            # Declined -> Failed, and the log says so.
            running.clear()
            decline[0] = True
            w._start_one(tools[0])
            assert statuses[-1] is S.FAILED and logs[-1][0] == "warning", logs
            assert "declined" in logs[-1][1], logs[-1]
            # Not allowed -> no prompt at all, Failed, pointed at Settings.
            decline[0] = False
            prompts.clear()
            w = engine(False)
            w._launch_tools()
            assert not prompts and statuses[-1] is S.FAILED, (prompts, statuses)
            assert "Settings" in logs[-1][1], logs[-1]

            # Teardown: what won't close gets ONE prompt for all of it...
            running.update(FakeAdmin=P(990101), FakeAdmin2=P(990102))
            statuses.clear()
            w = engine(True)
            w._teardown(config.ShutdownScope.ALL_ENABLED)
            assert len(prompts) == 1, prompts
            path, args = prompts[0]
            assert path == sys.executable and eng.CLOSE_ELEVATED_FLAG in args
            assert "990101:FakeAdmin" in args and "990102:FakeAdmin2" in args
            assert statuses == [S.OFFLINE, S.OFFLINE], statuses
            assert logs[-1][0] == "success", logs[-1]
            # ...a No leaves both on Error, and so does the setting being off.
            for allow, prompted in ((True, True), (False, False)):
                running.update(FakeAdmin=P(990101), FakeAdmin2=P(990102))
                prompts.clear()
                statuses.clear()
                decline[0] = allow
                engine(allow)._teardown(config.ShutdownScope.ALL_ENABLED)
                assert bool(prompts) is prompted, (allow, prompts)
                assert statuses == [S.ERROR, S.ERROR], (allow, statuses)
                assert "could not be closed" in logs[-1][1], logs[-1]

            # The elevated copy: never touches Steam or the game, skips junk,
            # and reports a tool that's already gone as closed.
            asked = []
            eng._ask_to_close = asked.append
            eng.ProcessManager.find_many = staticmethod(lambda names: {})
            assert eng._close_elevated(["4:steam.exe", "junk"]) == 1
            assert eng._close_elevated(["0:FakeTool"]) == 0   # 0: by name only
            assert not asked, asked
        finally:
            (eng._popen_detached, eng._run_as_admin, eng._ask_to_close,
             eng.ProcessManager.find_many) = real
            os.remove(exe)
    run("admin tools: Windows asks only when allowed", admin_tools)

    def launcher_tools():
        # Odyssey Materials Helper: its launcher is preferred over the helper
        # exe, an install still on the old default path moves over, and a
        # launch through a launcher waits for (and tracks) the tool itself.
        # Fakes only: no real launch or PID.
        import tempfile
        from dataclasses import replace as _replace
        real_env = os.environ.get("LOCALAPPDATA")
        real = (eng._popen_detached, eng.LAUNCHER_WAIT)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["LOCALAPPDATA"] = tmp
                launcher = config._expand_env(config._ODYSSEY_LAUNCHER)
                helper = config._expand_env(config._ODYSSEY_HELPER)
                os.makedirs(os.path.dirname(helper))
                open(helper, "w").close()
                odyssey = config.REGISTRY_BY_NAME["OdysseyMaterials"]
                assert odyssey.resolve_path() == helper, odyssey.resolve_path()
                open(launcher, "w").close()
                assert odyssey.resolve_path() == launcher, odyssey.resolve_path()

                old = config._app_from_dict({
                    "name": "OdysseyMaterials", "process": odyssey.process_name,
                    "path": config._ODYSSEY_HELPER})
                assert old.path == "" and old.resolve_path() == launcher, old
                mine = config._app_from_dict({
                    "name": "OdysseyMaterials", "path": r"D:\Mats\Helper.exe"})
                assert mine.path == r"D:\Mats\Helper.exe", mine

                class P:   # stands in for a psutil.Process
                    pid = 990201
                scans = []

                def find(name):
                    scans.append(name)   # existing-copy check, then the waits
                    return P() if len(scans) >= 3 else None

                eng._popen_detached = lambda args, **kw: P()
                pm = eng.ProcessManager()
                pm.find = find
                tool = _replace(odyssey, path=launcher, resolver=None)
                assert pm.launch(tool).pid == P.pid
                assert pm.launched == {"OdysseyMaterials": P.pid}, pm.launched
                # The tool never shows: no process, still ours by name.
                eng.LAUNCHER_WAIT = 0
                pm.find = lambda name: None
                assert pm.launch(tool) is None
                assert pm.launched == {"OdysseyMaterials": 0}, pm.launched
        finally:
            eng._popen_detached, eng.LAUNCHER_WAIT = real
            if real_env is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = real_env
    run("launcher tools: prefer the launcher, track the tool", launcher_tools)

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
            mark = ui.InfoMark()
            assert not mark.grab().isNull()
            mark._hover = True
            assert not mark.grab().isNull()
    run("HUD toggles + info mark paint", toggles_paint)

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
        # Every tip names a real toggle, and only the switched-on ones show --
        # on the toggle's row, marked with an InfoMark.
        for key, (shown, tip) in ui._TOGGLE_TIPS.items():
            row = getattr(dlg, key).parentWidget()
            assert row.toolTip() == (tip if shown else ""), key
            assert (row.findChild(ui.InfoMark) is not None) == shown, key
        got = []
        dlg.saved.connect(got.append)
        # Flip the close-EDLS, admin and update toggles and check they come
        # back out.
        dlg.toggle_close_exit.setChecked(not settings.close_on_game_exit)
        dlg.toggle_close_shutdown.setChecked(not settings.close_on_shutdown)
        dlg.toggle_admin.setChecked(not settings.allow_elevation)
        dlg.toggle_updates.setChecked(not settings.check_for_updates)
        dlg._save()   # exercises recompute_removed_defaults + replace path
        assert got and got[0].close_on_game_exit != settings.close_on_game_exit
        assert got[0].close_on_shutdown != settings.close_on_shutdown
        assert got[0].allow_elevation != settings.allow_elevation
        assert got[0].check_for_updates != settings.check_for_updates

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

    def settings_input_guards():
        # R-08: Steam and Elite are the built-in cards' keys, so a custom app
        # can't take either name. R-27: a path pasted in quotes (Explorer's
        # "Copy as path") loses them.
        sd = ui.SettingsDialog(settings)
        taken = sd._existing_names()
        assert "Steam" in taken and "Elite" in taken, taken
        entry = ui.AppEntryDialog(taken_names=taken)
        assert entry._validation_error("elite") is not None
        assert entry._validation_error("Elite Observatory 2") is None
        quoted = ui.AppEntryDialog("X", '  "C:\\x\\Foo.exe"  ')
        assert quoted.values() == ("X", r"C:\x\Foo.exe"), quoted.values()
        for w in (sd, entry, quoted):
            w.deleteLater()
    run("settings: reserved names, quoted paths", settings_input_guards)

    def emptied_app_list_stays_empty():
        # R-06: an app list the CMDR emptied on purpose doesn't refill with the
        # defaults on the next start. Runs in a temp dir, never on the real
        # settings.json.
        import tempfile
        saved = config.DATA_DIR, config.SETTINGS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            config.DATA_DIR = tmp
            config.SETTINGS_FILE = os.path.join(tmp, "settings.json")
            try:
                s = config.default_settings()
                s.apps, s.removed_defaults = [], sorted(config.REGISTRY_BY_NAME)
                assert config.save_settings(s)
                assert config.load_settings().apps == [], "defaults came back"
                s.removed_defaults = []   # nothing removed: the defaults return
                assert config.save_settings(s)
                assert len(config.load_settings().apps) == len(config.REGISTRY)
            finally:
                config.DATA_DIR, config.SETTINGS_FILE = saved
    run("emptied app list stays empty", emptied_app_list_stays_empty)

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

    # ---- updates: offline fakes only. Nothing here may reach GitHub, write
    # the CMDR's settings.json, or start a real installer.
    import hashlib
    import io
    import json
    import tempfile

    class FakeResp(io.BytesIO):
        def __init__(self, data: bytes, url: str = "https://github.com/x"):
            super().__init__(data)
            self.headers = {"Content-Length": str(len(data))}
            self._url = url

        def geturl(self):
            return self._url

    def fake_urls(table):
        """Point main._open_url at `table` (url -> bytes); returns a restorer."""
        real = eng._open_url

        def fake(url, accept=""):
            if not eng._trusted_url(url):
                raise eng.UpdateError(f"refusing to download from {url}")
            return FakeResp(table[url], url)
        eng._open_url = fake
        return lambda: setattr(eng, "_open_url", real)

    installer = b"MZ fake installer " * 1000
    sha = hashlib.sha256(installer).hexdigest()
    base = "https://github.com/coyoteware/EDLS/releases/download/9.0.0/"
    release = {
        "tag_name": "9.0.0", "body": "## New\n- things", "html_url": base,
        "assets": [
            {"name": "EDLS-Setup-9.0.0.exe",
             "browser_download_url": base + "EDLS-Setup-9.0.0.exe"},
            {"name": "EDLS-Setup-9.0.0.exe.sha256",
             "browser_download_url": base + "EDLS-Setup-9.0.0.exe.sha256"},
        ],
    }
    update_info = config.UpdateInfo(
        version="9.0.0", notes="## New\n- things", page_url=base,
        installer_name="EDLS-Setup-9.0.0.exe",
        installer_url=base + "EDLS-Setup-9.0.0.exe",
        checksum_url=base + "EDLS-Setup-9.0.0.exe.sha256")

    def update_versions():
        pv, newer = config.parse_version, config.is_newer
        assert pv("1.2") == (1, 2, 0) and pv("v1.3.4-beta") == (1, 3, 4)
        assert pv("latest") is None and pv("") is None
        assert not newer("1.2", "1.2.1"), "the 1.2 tag must not nag 1.2.1"
        assert newer("1.2.2", "1.2.1") and newer("2", "1.9.9")
        assert not newer("1.2.1", "1.2.1") and not newer("junk", "1.0")
        # The two settings round-trip (in a scratch dir, not the real one).
        saved = config.DATA_DIR, config.SETTINGS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            config.DATA_DIR = tmp
            config.SETTINGS_FILE = os.path.join(tmp, "settings.json")
            try:
                s = config.default_settings()
                assert s.check_for_updates and s.skipped_version == ""
                s.check_for_updates, s.skipped_version = False, "9.0.0"
                assert config.save_settings(s)
                back = config.load_settings()
                assert (back.check_for_updates, back.skipped_version) == \
                    (False, "9.0.0"), back
            finally:
                config.DATA_DIR, config.SETTINGS_FILE = saved
    run("updates: version order + settings", update_versions)

    def update_check():
        assert eng._trusted_url(base) and eng._trusted_url(
            "https://objects.githubusercontent.com/a")
        assert not eng._trusted_url("http://github.com/a")
        assert not eng._trusted_url("https://github.com.evil.example/a")
        restore = fake_urls({config.LATEST_RELEASE_API:
                             json.dumps(release).encode()})
        try:
            info = eng.check_for_update("1.2.1")
            assert info and info.version == "9.0.0" and info.installable, info
            assert info.installer_url == update_info.installer_url
            assert eng.check_for_update("9.0.0") is None, "same version"
            bare = dict(release, assets=[])
            restore()
            restore = fake_urls({config.LATEST_RELEASE_API:
                                 json.dumps(bare).encode()})
            info = eng.check_for_update("1.2.1")
            assert info and not info.installable, "no assets -> page only"
        finally:
            restore()
    run("updates: release check (faked GitHub)", update_check)

    def update_download():
        saved_dir = eng.UPDATE_DIR
        with tempfile.TemporaryDirectory() as tmp:
            eng.UPDATE_DIR = os.path.join(tmp, "upd")
            table = {update_info.installer_url: installer,
                     update_info.checksum_url:
                         f"{sha}  EDLS-Setup-9.0.0.exe\n".encode()}
            restore = fake_urls(table)
            try:
                seen = []
                path = eng.download_update(update_info, seen.append)
                assert open(path, "rb").read() == installer and seen[-1] == 100
                table[update_info.checksum_url] = b"0" * 64
                try:
                    eng.download_update(update_info)
                    raise AssertionError("a bad checksum was accepted")
                except eng.UpdateError:
                    pass
                assert not os.listdir(eng.UPDATE_DIR), "bad download left behind"
                bad = config.UpdateInfo("9.0.0", installer_name="x.exe",
                                        installer_url="https://evil.example/x",
                                        checksum_url=update_info.checksum_url)
                table[update_info.checksum_url] = sha.encode()
                try:
                    eng.download_update(bad)
                    raise AssertionError("a non-GitHub URL was fetched")
                except eng.UpdateError:
                    pass
            finally:
                restore()
                eng.UPDATE_DIR = saved_dir
    run("updates: download + checksum (faked GitHub)", update_download)

    def update_engine():
        w = eng.Engine(settings)
        running, ran, quits, offered = set(), [], [], []

        class P:
            pid = 4242
        w.pm.find = lambda name: P() if name in running else None
        w.pm.run_installer = ran.append
        w.on_quit.connect(lambda: quits.append(1))
        w.on_update.connect(offered.append)
        real_dl, real_check, saved_dir = (eng.download_update,
                                          eng.check_for_update, eng.UPDATE_DIR)
        eng.download_update = lambda info, progress, cancelled: "C:/fake/setup.exe"
        eng.check_for_update = lambda: update_info
        try:
            with tempfile.TemporaryDirectory() as tmp:
                eng.UPDATE_DIR = os.path.join(tmp, "upd")
                # The game is running: refused, nothing downloaded or run.
                running.add(config.ELITE_PROCESS)
                assert w.install_update(update_info), "allowed mid-game"
                assert w._update_thread is None and not ran
                # A card START in flight (maybe on a UAC prompt): refused too.
                running.clear()
                w._starting.add("FakeTool")
                assert w.install_update(update_info), "allowed mid-START"
                assert w._update_thread is None and not ran
                w._starting.clear()
                # Idle: downloads, runs the installer, closes EDLS.
                assert w.install_update(update_info) is None
                w._update_thread.join(5)
                assert ran == ["C:/fake/setup.exe"] and quits == [1], (ran, quits)
                # The check offers a new version, but not a skipped one.
                w.settings = config.default_settings()
                w._check_for_updates()
                assert offered == [update_info], offered
                w.settings.skipped_version = "9.0.0"
                w._check_for_updates()
                assert len(offered) == 1, "a skipped version was offered"
        finally:
            eng.download_update, eng.check_for_update = real_dl, real_check
            eng.UPDATE_DIR = saved_dir
    run("updates: engine gate + install path (fakes)", update_engine)

    def update_ui():
        from PySide6.QtCore import QTimer
        bare = config.UpdateInfo("9.0.0", notes="")
        for info in (update_info, bare):
            dlg = ui.UpdateDialog(info, win)
            dlg.show(); dlg.close(); dlg.deleteLater()
        win._on_update(update_info)
        assert win._btns.update_lbl.isVisibleTo(win), "no update link"
        win._on_update_progress(42)
        assert "42%" in win._btns.update_lbl.text()
        win._open_update()   # ignored mid-download: no dialog to dismiss
        win._on_update_progress(None)
        win._build_content()   # a theme rebuild keeps the link
        assert win._btns.update_lbl.isVisibleTo(win), "link lost on rebuild"
        # SKIP THIS VERSION through the real dialog, without touching the
        # CMDR's settings.json.
        saved_skip = win._settings.skipped_version
        win._save_settings = lambda s: None
        try:
            QTimer.singleShot(0, lambda: QApplication.activeModalWidget()
                              ._choose("skip"))
            win._open_update()
            assert win._settings.skipped_version == "9.0.0"
            assert not win._btns.update_lbl.isVisibleTo(win)
        finally:
            win._settings.skipped_version = saved_skip
            del win._save_settings
    run("updates: link, dialog, skip", update_ui)

    def theme_switch():
        from dataclasses import replace
        # Drive a live light/dark switch both ways through the real save path,
        # which calls apply_theme() + rebuilds all content.
        win._on_settings_saved(replace(settings, theme=config.Theme.LIGHT))
        win._on_settings_saved(replace(settings, theme=config.Theme.DARK))
    run("light/dark theme switch + content rebuild", theme_switch)

    def theme_preview():
        from dataclasses import replace
        # The Light mode toggle switches the theme live, as a preview: Settings
        # rebuilds in the new colours keeping what's been typed and ticked, and
        # anything but Save puts the opening theme back. Never saves to disk.
        dark, light = config.Theme.DARK, config.Theme.LIGHT
        ui.apply_theme(dark)

        def flip_to_light():
            dlg = ui.SettingsDialog(replace(settings, theme=dark), parent=win)
            dlg.theme_preview.connect(win._set_theme)
            dlg.cmdr_edit.setText("CMDR Preview")
            rows = dlg._rows()
            dlg.toggle_light.setChecked(True)
            dlg._preview_theme()   # what the toggle's timer runs once it lands
            assert ui.IS_LIGHT, "the toggle should switch the theme live"
            assert dlg.cmdr_edit.text() == "CMDR Preview", "lost the typed name"
            assert dlg._rows() == rows, "the rebuild changed the app list"
            return dlg

        dlg = flip_to_light()
        dlg.reject()
        assert not ui.IS_LIGHT, "Cancel should put the opening theme back"

        dlg = flip_to_light()
        got = []
        dlg.saved.connect(got.append)
        dlg._save()
        assert got and got[0].theme is light and ui.IS_LIGHT, \
            "Save should keep the previewed theme"
        win._set_theme(dark)
    run("settings: theme toggle previews live, Cancel reverts", theme_preview)

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
