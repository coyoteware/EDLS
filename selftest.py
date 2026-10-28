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
        for lvl in ("success", "default", "warning", "error", "dim", "info",
                    "update"):
            win._on_log(f"selftest {lvl} line", lvl)
    run("render every log level", all_logs)

    run("busy toggle (launch button)",
        lambda: (win._btns.set_busy(True), win._btns.set_busy(False)))

    def log_keeps_place():
        # R-25: someone scrolled up to read isn't dragged to the bottom by new
        # lines; at the bottom, the log follows them.
        view = ui.LogView()
        view.resize(400, 120)
        view.show()
        for i in range(60):
            view.append_line(f"line {i}")
        QApplication.processEvents()
        sb = view.verticalScrollBar()
        assert 0 < sb.maximum() == sb.value(), (sb.value(), sb.maximum())
        sb.setValue(0)
        view.append_line("while reading")
        assert sb.value() == 0, "jumped to the bottom"
        sb.setValue(sb.maximum())
        view.append_line("at the bottom")
        assert sb.value() == sb.maximum(), "stopped following"
        view.close()
        view.deleteLater()
    run("log keeps the reader's place", log_keeps_place)

    def names_plain_text():
        # R-19: a CMDR or app name that looks like HTML shows as typed.
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QLabel
        plain = Qt.TextFormat.PlainText
        header = ui.HeaderPanel("<b>Jameson</b>")
        assert header.cmdr_lbl.textFormat() == plain
        card = ui.StatusCard(config.AppDescriptor("<i>Tool</i>", "Tool"))
        assert card.findChild(ui.ElidingLabel).textFormat() == plain
        entry = ui.AppEntryDialog(parent=win)
        assert entry.error_lbl.textFormat() == plain
        msg = ui._HudMessage(win, "Title", "<b>Tool</b> is <i>here</i>", "OK")
        text = next(lbl for lbl in msg.findChildren(QLabel)
                    if lbl.text().startswith("<b>"))
        assert text.textFormat() == plain
        for w in (header, card, entry, msg):
            w.deleteLater()
    run("names show as plain text", names_plain_text)

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
        # (The game stays up in this scan, so its card has nothing to report.)
        before = _time.monotonic()
        w._status("Steam", S.ONLINE)
        assert "Steam" not in tick({elite: P()}, started=before)
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
            w._launch_tools({t.name: exe for t in tools})
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

    def install_search():
        # EDDI (this account's install, then all users') and VoiceAttack (v2,
        # then v1) are found where their installers put them, the best one
        # first. A VoiceAttack still on 1.2.1's default path moves to that
        # search; a path the CMDR set is kept. Temp files only.
        import tempfile
        keys = ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)")
        real_env = {k: os.environ.get(k) for k in keys}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                for key in keys:
                    os.environ[key] = os.path.join(tmp, key)
                for name, templates in (("EDDI", config._EDDI_PATHS),
                                        ("VoiceAttack", config._VOICEATTACK_PATHS)):
                    app = config.REGISTRY_BY_NAME[name]
                    assert app.resolve_path() is None, (name, app.resolve_path())
                    # Least wanted first: each new one should win.
                    for exe in reversed([config._expand_env(t) for t in templates]):
                        os.makedirs(os.path.dirname(exe), exist_ok=True)
                        open(exe, "w").close()
                        assert app.resolve_path() == exe, (name, app.resolve_path())

                va2 = config._expand_env(config._VOICEATTACK_PATHS[0])
                old = config._app_from_dict({
                    "name": "VoiceAttack", "process": "VoiceAttack",
                    "path": config._RETIRED_PATHS["VoiceAttack"]})
                assert old.path == "" and old.resolve_path() == va2, old
                mine = config._app_from_dict({
                    "name": "VoiceAttack", "path": r"D:\VA\VoiceAttack.exe"})
                assert mine.path == r"D:\VA\VoiceAttack.exe", mine
        finally:
            for key, value in real_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    run("install search: EDDI and VoiceAttack, best match first", install_search)

    def start_minimized():
        # Settings.start_minimized asks Windows to start a tool minimized
        # (SW_SHOWMINNOACTIVE), through Popen and through the UAC prompt alike,
        # from LAUNCH and from a card's START. Fakes only: no real launch or
        # prompt (P's PID is our own, and nothing here closes anything).
        import subprocess
        import tempfile
        from dataclasses import replace as _replace
        real = (eng._popen_detached, eng._run_as_admin)
        popens, shows, found = [], [], []

        class P:   # stands in for a psutil.Process
            pid = os.getpid()

        def popen(args, **kw):
            popens.append(kw.get("startupinfo"))
            return P()

        def needs_admin(args, **kw):
            exc = OSError("fake WinError 740")
            exc.winerror = eng.ERROR_ELEVATION_REQUIRED
            raise exc

        def fake_runas(path, args="", cwd=None, show=1):
            shows.append(show)
            found.append(P())   # the elevated copy turns up at once

        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as fh:
            exe = fh.name
        stem = os.path.splitext(os.path.basename(exe))[0]
        tool = _replace(settings.apps[0], name="FakeTool", process_name=stem,
                        path=exe, enabled=True, resolver=None)
        try:
            eng._popen_detached = popen
            pm = eng.ProcessManager()
            pm.find = lambda name: None
            pm.launch(tool, resolved_path=exe, minimized=True)
            pm.launch(tool, resolved_path=exe)
            si, plain = popens
            if hasattr(subprocess, "STARTUPINFO"):
                assert si.wShowWindow == eng.SW_SHOWMINNOACTIVE, si.wShowWindow
                assert si.dwFlags & subprocess.STARTF_USESHOWWINDOW
            assert plain is None, "a normal start got a show state"

            eng._popen_detached = needs_admin
            eng._run_as_admin = fake_runas
            pm.find = lambda name: found[-1] if found else None
            for minimized in (True, False):
                found.clear()
                pm.launch(tool, resolved_path=exe, elevate=lambda: True,
                          minimized=minimized)
            assert shows == [eng.SW_SHOWMINNOACTIVE, eng.SW_SHOWNORMAL], shows

            # The Engine hands the setting to both ways of starting a tool.
            for on in (True, False):
                w = eng.Engine(_replace(settings, apps=[tool],
                                        start_minimized=on))
                asked = []
                w.pm.find = lambda name: None
                w.pm.launch = lambda *a, **kw: asked.append(kw["minimized"])
                w._start_one(tool)
                w._launch_tools({tool.name: exe})
                assert asked == [on, on], (on, asked)

            # The setting round-trips (in a scratch dir, not the real one).
            saved = config.DATA_DIR, config.SETTINGS_FILE
            with tempfile.TemporaryDirectory() as tmp:
                config.DATA_DIR = tmp
                config.SETTINGS_FILE = os.path.join(tmp, "settings.json")
                try:
                    s = config.default_settings()
                    assert not s.start_minimized, "on by default"
                    s.start_minimized = True
                    assert config.save_settings(s)
                    assert config.load_settings().start_minimized
                finally:
                    config.DATA_DIR, config.SETTINGS_FILE = saved
        finally:
            eng._popen_detached, eng._run_as_admin = real
            os.remove(exe)
    run("tools can start minimized", start_minimized)

    def resolve_once():
        # LAUNCH finds each tool once: preflight hands its paths on, so a slow
        # resolver (SrvSurvey's directory walk) runs once, not twice. Fakes
        # only: no registry lookup, process scan or launch.
        import tempfile
        from dataclasses import replace as _replace
        real_winreg = eng.winreg
        calls, started = [], []
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as fh:
            exe = fh.name

        class P:   # stands in for a psutil.Process
            pid = 990301

        def engine(found):
            tool = config.AppDescriptor(
                "Walker", "Walker",
                resolver=lambda: calls.append(1) or (exe if found else exe + ".x"))
            w = eng.Engine(_replace(settings, apps=[tool]))
            w.pm.find = lambda name: None
            w.pm.launch = lambda app, resolved_path=None, elevate=None, \
                minimized=False: (started.append(resolved_path) or P())
            return w

        try:
            eng.winreg = None   # skip the Steam registry check
            w = engine(found=True)
            paths = w._preflight()
            assert paths == {"Walker": exe}, paths
            w._launch_tools(paths)
            assert started == [exe] and calls == [1], (started, calls)
            # Nothing found is an empty result, not "Steam is missing" (None).
            assert engine(found=False)._preflight() == {}
        finally:
            eng.winreg = real_winreg
            os.remove(exe)
    run("LAUNCH resolves each tool's path once", resolve_once)

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
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent
        dlg = ui.SettingsDialog(settings, locked=True)
        reports, popups = [], []
        dlg.locked_attempt.connect(lambda: reports.append(1))
        real = ui.hud_notice
        ui.hud_notice = lambda *a, **k: popups.append(a)
        try:
            rows = dlg._rows()
            dlg._add_row(); dlg._edit_row(0); dlg._reset_defaults()
            dlg.table.selectRow(0); dlg._remove_row()
            app.sendEvent(dlg.table, QKeyEvent(
                QEvent.Type.KeyPress, Qt.Key.Key_Down,
                Qt.KeyboardModifier.ControlModifier))
        finally:
            ui.hud_notice = real
        assert dlg._rows() == rows, "locked list was changed"
        assert len(popups) == 5 and reports == [1], (popups, reports)
        assert not dlg.table.dragEnabled(), "locked list can be dragged"
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
        dlg.toggle_minimized.setChecked(not settings.start_minimized)
        dlg._save()   # exercises recompute_removed_defaults + replace path
        assert got and got[0].close_on_game_exit != settings.close_on_game_exit
        assert got[0].close_on_shutdown != settings.close_on_shutdown
        assert got[0].allow_elevation != settings.allow_elevation
        assert got[0].check_for_updates != settings.check_for_updates
        assert got[0].start_minimized != settings.start_minimized

    run("settings dialog (normal) build + save",
        lambda: settings_dialog(False))
    run("settings dialog (first-run) build + save",
        lambda: settings_dialog(True))

    def ghost_cmdr_name():
        # The default CMDR name is placeholder (ghost) text, never real text,
        # and saving an empty field resets the name to the default.
        from dataclasses import replace
        default = config.Settings().cmdr_name

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

    def settings_reorder():
        # Rows move by drag or Ctrl+Up/Down, keeping their tick and origin,
        # and SAVE writes the apps in the new order (the cards' and launch order).
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent

        def ctrl(k):
            app.sendEvent(dlg.table, QKeyEvent(
                QEvent.Type.KeyPress, k, Qt.KeyboardModifier.ControlModifier))
        dlg = ui.SettingsDialog(settings)
        rows = dlg._rows()
        assert len(rows) >= 3, rows
        dlg._move_row(0, 2)
        assert dlg._rows() == [rows[1], rows[2], rows[0]] + rows[3:], dlg._rows()
        assert dlg.table.currentRow() == 2
        dlg._fill(rows)
        dlg.table.setCurrentCell(0, 1)
        ctrl(Qt.Key.Key_Up)   # already at the top
        assert dlg._rows() == rows, "Ctrl+Up moved the top row"
        ctrl(Qt.Key.Key_Down)
        assert dlg._rows() == [rows[1], rows[0]] + rows[2:], dlg._rows()
        assert dlg.table.currentRow() == 1
        got = []
        dlg.saved.connect(got.append)
        dlg._save()
        want = [r[0] for r in dlg._rows() if r[0]]
        assert [a.name for a in got[0].apps] == want, got[0].apps
        # The drop gap under the cursor: a row's upper half is above it, its
        # lower half below it, the space under the last row the end.
        t = dlg.table
        y1, h = t.rowViewportPosition(1), t.rowHeight(1)
        assert t._gap_at(y1 + 2) == 1 and t._gap_at(y1 + h - 2) == 2
        last = t.rowCount() - 1
        assert t._gap_at(t.rowViewportPosition(last) + t.rowHeight(last) + 5) \
            == t.rowCount()
        # The drop line paints, mid-drag and with nothing being dragged.
        t._src, t._drop_y = 0, y1 + h - 2
        assert not t.grab().isNull()
        t._src = None
        assert not t.grab().isNull()
        t._drop_y = None
        dlg.deleteLater()
    run("settings: reorder the app list", settings_reorder)

    def settings_fits_small_screen():
        # Landscape Settings must still fit 1024x768 with the taskbar.
        for first_run in (False, True):
            dlg = ui.SettingsDialog(settings, first_run=first_run)
            dlg.layout().activate()
            ms = dlg.minimumSize().expandedTo(dlg.layout().totalMinimumSize())
            assert ms.width() <= 1004 and ms.height() <= 697, (first_run, ms)
            dlg.deleteLater()
    run("settings fits 1024x768", settings_fits_small_screen)

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

    def defaults_ticked_when_found():
        # R-26 and upgrades: a default joins the list ticked only if its
        # program is on this PC -- first run, RESTORE DEFAULTS, or a default
        # that's new in this version (Elite Studio, for 1.2.1 users). A tick
        # the CMDR set is never changed. Temp files only.
        import tempfile
        from dataclasses import replace as _replace

        def found(app):
            path = app.resolve_path()
            return bool(path and os.path.exists(path))

        with tempfile.TemporaryDirectory() as tmp:
            exe = os.path.join(tmp, "Tool.exe")
            open(exe, "w").close()
            here = config.AppDescriptor("Here", "Tool", exe, enabled=False)
            gone = config.AppDescriptor("Gone", "Gone",
                                        os.path.join(tmp, "Gone.exe"))
            fresh = config._fresh_default(here)
            assert fresh.enabled and fresh.origin == "Here", fresh
            assert not config._fresh_default(gone).enabled, "missing one ticked"
        for app in config.default_settings().apps:
            assert app.enabled == found(app), app.name
            assert app.origin == app.name, app
        # An upgrade: the file has one default (unticked by the CMDR) and
        # lacks the rest, which are new to it.
        mine = _replace(config.REGISTRY[0], enabled=False)
        s = config.Settings(apps=[mine])
        assert config._merge_new_defaults(s)
        assert s.apps[0].enabled is False, "the CMDR's own tick changed"
        assert len(s.apps) == len(config.REGISTRY)
        for app in s.apps[1:]:
            assert app.enabled == found(app), app.name
    run("defaults start ticked only when installed", defaults_ticked_when_found)

    def renamed_default_keeps_resolver():
        # R-07: a renamed default (SrvSurvey has no path, only a resolver)
        # still finds its program after a restart, because settings.json keeps
        # where it came from. Temp settings dir only.
        import tempfile
        from dataclasses import replace as _replace
        srv = config.REGISTRY_BY_NAME["SrvSurvey"]
        renamed = _replace(srv, name="My SRV", origin="SrvSurvey")
        saved = config.DATA_DIR, config.SETTINGS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            config.DATA_DIR = tmp
            config.SETTINGS_FILE = os.path.join(tmp, "settings.json")
            try:
                s = config.Settings(
                    apps=[renamed,
                          config.AppDescriptor("Mine", "Mine", r"C:\Mine.exe")],
                    removed_defaults=sorted(config.REGISTRY_BY_NAME))
                assert config.save_settings(s)
                back, custom = config.load_settings().apps
                assert (back.name, back.origin) == ("My SRV", "SrvSurvey"), back
                assert back.resolver is srv.resolver, "lost its resolver"
                assert custom.origin == "" and custom.resolver is None, custom
            finally:
                config.DATA_DIR, config.SETTINGS_FILE = saved
        # Renamed is still that default: it doesn't come back a second time,
        # even without removed_defaults.
        s = config.Settings(apps=[renamed])
        config._merge_new_defaults(s)
        assert [a.origin for a in s.apps].count("SrvSurvey") == 1, s.apps
        # A file from before 1.3.0 has no origin: a default is known by name.
        old = config._app_from_dict({"name": "SrvSurvey",
                                     "process": "SrvSurvey"})
        assert old.origin == "SrvSurvey" and old.resolver is srv.resolver, old
        # The Settings table carries it through a save.
        dlg = ui.SettingsDialog(config.Settings(apps=[renamed]), parent=win)
        got = []
        dlg.saved.connect(got.append)
        dlg._save()
        app, = got[0].apps
        assert app.origin == "SrvSurvey" and app.resolver is srv.resolver, app
        dlg.deleteLater()
    run("renamed default keeps its auto-find", renamed_default_keeps_resolver)

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

    def dialogs_freed():
        # A finished dialog (a notice, a confirm, Settings) is deleted, not
        # left hidden under the main window until EDLS exits.
        from PySide6.QtCore import QCoreApplication, QEvent, QTimer
        from PySide6.QtWidgets import QDialog

        def flush():
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

        def answer(accept):
            dlg = QApplication.activeModalWidget()
            dlg.accept() if accept else dlg.reject()

        flush()
        before = len(win.findChildren(QDialog))
        QTimer.singleShot(0, lambda: answer(True))
        ui.hud_notice(win, "Notice", "Done.")
        QTimer.singleShot(0, lambda: answer(False))
        assert ui.hud_confirm(win, "Confirm", "Sure?") is False
        QTimer.singleShot(0, lambda: answer(False))
        win._open_settings()
        flush()
        assert len(win.findChildren(QDialog)) == before, \
            (before, len(win.findChildren(QDialog)))
    run("finished dialogs are freed", dialogs_freed)

    def close_countdown():
        # R-36: STAY OPEN cancels the close countdown, left alone it closes
        # EDLS, and the window closing ends it quietly. Headless engine, a
        # short countdown; the only process lookup is faked.
        import threading
        from dataclasses import replace as _replace
        real = eng.CLOSE_DELAY
        w = eng.Engine(settings)
        counts, quits, logs = [], [], []
        w.on_countdown.connect(counts.append)
        w.on_quit.connect(lambda: quits.append(1))
        w.on_log.connect(lambda m, lvl: logs.append(m))
        try:
            eng.CLOSE_DELAY = 2
            threading.Timer(0.3, w.stay_open).start()
            assert w._close_countdown() is False, "STAY OPEN didn't stop it"
            assert counts == [True, False] and not quits, (counts, quits)
            assert logs[-1] == "EDLS will stay open.", logs
            # Mid-session SHUTDOWN, then STAY OPEN: back to waiting for the
            # game, which is still running.
            logs.clear()
            alive = threading.Event()
            w._thread = threading.Thread(target=alive.wait, daemon=True)
            w._thread.start()

            class P:   # stands in for a psutil.Process
                pid = 4242
            w.pm.find = lambda name: P()
            w.settings = _replace(settings, close_on_shutdown=True,
                                  shutdown_scope=config.ShutdownScope.LAUNCHED_ONLY)
            threading.Timer(0.3, w.stay_open).start()
            w._manual_shutdown()
            alive.set()
            assert logs[-1] == "Waiting for Elite: Dangerous to close...", logs
            assert not quits, quits
            # Left alone, it closes EDLS.
            eng.CLOSE_DELAY = 1
            counts.clear(); logs.clear()
            assert w._close_countdown() is True and quits == [1], quits
            assert counts == [True] and "Farewell, CMDR. o7" in logs, logs
            # The window closing mid-countdown: no quit of its own.
            eng.CLOSE_DELAY = 2
            quits.clear()
            threading.Timer(0.3, w.close).start()
            assert w._close_countdown() is True and not quits, quits
        finally:
            eng.CLOSE_DELAY = real
    run("close countdown: STAY OPEN, run out, window closed", close_countdown)

    def stay_open_key():
        # The view side of STAY OPEN, and R-22: when the countdown runs out
        # with a dialog open, EDLS stays open instead of closing behind it.
        from PySide6.QtCore import QTimer
        btn = win._btns.launch_btn
        stays, closes, seen = [], [], {}
        win._engine.stay_open = lambda: stays.append(1)
        win.close = lambda: closes.append(1)
        try:
            win._on_busy(True)
            win._on_countdown(True)
            assert btn.text() == "STAY OPEN" and btn.isEnabled(), btn.text()
            win._on_launch_clicked()
            assert stays == [1], "STAY OPEN didn't reach the engine"
            win._on_countdown(False)
            assert btn.text() == "RUNNING..." and not btn.isEnabled()
            win._on_busy(False)
            assert btn.text() == "LAUNCH" and btn.isEnabled()

            win._on_countdown(True)

            def countdown_ends():   # with the notice below still open
                try:
                    win._on_quit()
                    seen.update(closes=list(closes), counting=win._counting)
                finally:
                    QApplication.activeModalWidget().accept()
            QTimer.singleShot(0, countdown_ends)
            ui.hud_notice(win, "Settings", "Still open.")
            assert seen == {"closes": [], "counting": False}, seen
            assert btn.text() == "LAUNCH", btn.text()
            win._on_quit()   # nothing open now: it closes
            assert closes == [1], closes
        finally:
            vars(win._engine).pop("stay_open", None)
            vars(win).pop("close", None)
            win._on_countdown(False)
            win._on_busy(False)
    run("STAY OPEN key; no closing behind a dialog", stay_open_key)

    # ---- updates: offline fakes only. Nothing here may reach GitHub, write
    # the CMDR's settings.json, or open a browser.
    import tempfile

    def update_versions():
        pv, newer = config.parse_version, config.is_newer
        assert pv("1.2") == (1, 2, 0) and pv("v1.3.4-beta") == (1, 3, 4)
        assert pv("latest") is None and pv("") is None
        assert not newer("1.2", "1.2.1"), "a 1.2 tag must not nag 1.2.1"
        assert newer("1.2.2", "1.2.1") and newer("2", "1.9.9")
        assert not newer("1.2.1", "1.2.1") and not newer("junk", "1.0")
        # The setting round-trips (in a scratch dir, not the real one).
        saved = config.DATA_DIR, config.SETTINGS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            config.DATA_DIR = tmp
            config.SETTINGS_FILE = os.path.join(tmp, "settings.json")
            try:
                s = config.default_settings()
                assert s.check_for_updates
                s.check_for_updates = False
                assert config.save_settings(s)
                assert not config.load_settings().check_for_updates
            finally:
                config.DATA_DIR, config.SETTINGS_FILE = saved
    run("updates: version order + setting", update_versions)

    def update_check():
        real = eng._latest_release
        try:
            for tag, current, want in (("9.0.0", "1.2.1", "9.0.0"),
                                       ("v9.0.0", "1.2.1", "9.0.0"),
                                       ("9.0.0", "9.0.0", None),
                                       ("1.2", "1.2.1", None),
                                       ("junk", "1.2.1", None),
                                       (None, "1.2.1", None)):
                eng._latest_release = lambda: {"tag_name": tag}
                got = eng.check_for_update(current)
                assert got == want, (tag, current, got)
        finally:
            eng._latest_release = real
    run("updates: release check (faked GitHub)", update_check)

    def update_engine():
        w = eng.Engine(settings)
        offered = []
        w.on_update.connect(offered.append)
        real = eng.check_for_update
        try:
            eng.check_for_update = lambda: "9.0.0"
            w._check_for_updates()
            assert offered == ["9.0.0"], offered

            def offline():
                raise OSError("no network")
            eng.check_for_update = offline
            w._check_for_updates()   # quiet: nothing offered, nothing raised
            assert offered == ["9.0.0"], offered

            def cut_short():   # not an OSError: a reply that stops halfway
                import http.client
                raise http.client.IncompleteRead(b"")
            eng.check_for_update = cut_short
            w._check_for_updates()
            assert offered == ["9.0.0"], offered
        finally:
            eng.check_for_update = real
    run("updates: engine offers a newer version (fakes)", update_engine)

    def update_ui():
        from types import SimpleNamespace
        from PySide6.QtCore import QTimer
        win._on_update("9.0.0")
        badge = win._header.update_btn
        assert badge.isVisibleTo(win), "no update badge"
        assert "9.0.0" in badge.text(), badge.text()
        # The badge's extra header row must still fit 1024x768.
        hint = win.minimumSizeHint()
        assert hint.width() <= 1004 and hint.height() <= 697, hint
        win._header.show_update("9.0.0&x")   # & is text, not a shortcut key
        assert "9.0.0&&x" in badge.text(), badge.text()
        win._build_content()   # a theme rebuild keeps the badge
        badge = win._header.update_btn
        assert badge.isVisibleTo(win), "badge lost on rebuild"
        assert badge.text() == "UPDATE 9.0.0 AVAILABLE", badge.text()
        # Both answers through the real popover, the first from a click on the
        # badge; the browser is a stub.
        opened = []
        real = ui.QDesktopServices
        ui.QDesktopServices = SimpleNamespace(
            openUrl=lambda url: opened.append(url.toString()))
        try:
            QTimer.singleShot(0, lambda: QApplication.activeModalWidget()
                              .accept())
            badge.click()
            assert opened == [config.LATEST_RELEASE_URL], opened
            QTimer.singleShot(0, lambda: QApplication.activeModalWidget()
                              .reject())
            win._open_update()
            assert len(opened) == 1, "LATER opened the release page"
        finally:
            ui.QDesktopServices = real
            win._update = None
            win._show_update_link()
        assert not win._header.update_btn.isVisibleTo(win), "badge stayed"
    run("updates: badge + release-page popover", update_ui)

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
