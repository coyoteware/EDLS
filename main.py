"""
EDLS - Elite: Dangerous Launch Suite (Python rewrite)
main.py - entry point + launch engine.

This is the "machinery": the process manager, the launch / monitor / shutdown
sequence, logging, and the wiring that bolts everything together. It contains
NO GUI-toolkit imports, so the engine can be driven and tested without a window.

Dependency direction (kept acyclic on purpose):

    config.py   pure data: app registry, Settings, AppState, ShutdownScope
        |  (imported by)
        v
    main.py     this file: engine + composition root
        |  (imports + injects engine into)
        v
    ui.py       the view -- the only toolkit-dependent file

ui.py must NOT import main.py. The engine reaches the view only through Signals
(engine -> view) and public methods (view -> engine), so the view can be swapped
(PySide6, web-render, ...) without the engine knowing or caring.

Targets Windows 10/11 + Steam, matching the original.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import subprocess
import threading
import time
from typing import Callable, Optional

import psutil

import config
from config import (
    AppDescriptor, AppState, Settings, ShutdownScope,
    KEY_STEAM, KEY_ELITE, STEAM_PROCESS, ELITE_PROCESS,
)

try:
    import winreg  # Windows-only, used for the Steam preflight check
except ImportError:  # pragma: no cover - non-Windows dev machines
    winreg = None


# =============================================================================
# Contract implemented by the other two files
# -----------------------------------------------------------------------------
# config.py provides:
#   DATA_DIR, SETTINGS_FILE, LOG_FILE                paths under %LOCALAPPDATA%\EDLS
#   AppState (Enum) with .is_running                 OFFLINE NOT_FOUND FAILED
#                                                    STOPPED CLOSED WAITING
#                                                    LAUNCHING ONLINE RUNNING
#   ShutdownScope (Enum): LAUNCHED_ONLY | ALL_ENABLED
#   AppDescriptor: .name .process_name .enabled  +  .resolve_path() -> str | None
#   Settings: .cmdr_name .elite_app_id .setup_complete .auto_start
#             .show_inactive_cards .shutdown_scope .theme .apps (list[AppDescriptor])
#             .removed_defaults (list[str])
#   load_settings() -> Settings
#   save_settings(settings) -> None
#
# ui.py provides:
#   run(engine, settings) -> int     builds the window, shows the first-run
#                                     dialog if not settings.setup_complete,
#                                     triggers engine.launch() if auto_start,
#                                     enters the event loop, returns an exit code.
#                                     It also injects engine.dispatch (a function
#                                     that runs a callable on the GUI thread) and
#                                     persists settings via config.save_settings.
# =============================================================================

STEAM_REG_KEY = r"Software\Valve\Steam"

# steam:// URIs are how Steam and the game are driven -- neither can be started
# from a path, so they never go through ProcessManager.launch().
STEAM_URI_MAIN = "steam://open/main"
STEAM_URI_EXIT = "steam://exit"

# Process-wait tuning (seconds). POLL is the gap between process scans; the rest
# are how long we'll wait for something to appear or disappear before giving up.
POLL_SECONDS = 2
STEAM_WAIT = 20        # Steam's client to show up
STEAM_EXIT_WAIT = 15   # steam://exit to actually take effect
ELITE_WAIT = 300       # the game, started from a card (min-ed-launcher is slow)

# EDLS must never kill Steam or the game -- force-closing Elite violates
# Frontier's ToS. ProcessManager.stop() refuses these outright, so no setting,
# custom app entry, or future code path can get there by accident.
PROTECTED_PROCESSES = frozenset(
    n.lower().removesuffix(".exe") for n in (STEAM_PROCESS, ELITE_PROCESS))

# If EDLS itself runs inside a kill-on-close Job Object (VS Code's debugpy
# launcher does this), anything we start joins that job and Windows kills it
# the moment EDLS exits -- that's how closing EDLS took Steam and the game down
# with it. Breaking away keeps what we launch independent of our lifetime.
_BREAKAWAY = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)

log = logging.getLogger("edls")


def _popen_detached(args: list[str], **kwargs) -> subprocess.Popen:
    """Popen outside our Job Object when the job allows it. A job that forbids
    breakaway raises OSError; fall back to a normal launch rather than fail."""
    flags = kwargs.pop("creationflags", 0)
    if _BREAKAWAY:
        try:
            return subprocess.Popen(args, creationflags=flags | _BREAKAWAY,
                                    **kwargs)
        except OSError:
            pass
    return subprocess.Popen(args, creationflags=flags, **kwargs)


def setup_logging() -> None:
    """Persistent on-disk log with rotation. The UI gets its live feed from the
    Engine.on_log signal instead, so this is purely the durable record."""
    os.makedirs(config.DATA_DIR, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        config.LOG_FILE, maxBytes=512_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)s %(message)s",
                           datefmt="%Y-%m-%d %H:%M:%S")
    )
    log.setLevel(logging.INFO)
    log.addHandler(handler)


# =============================================================================
# Signal - a tiny toolkit-agnostic observer
# =============================================================================
class Signal:
    """Minimal subscribe/emit. Handlers may be invoked from a worker thread;
    marshaling onto the GUI thread is the view's job (it injects that wrapper
    via Engine.dispatch)."""

    def __init__(self) -> None:
        self._handlers: list[Callable] = []

    def connect(self, fn: Callable) -> None:
        self._handlers.append(fn)

    def emit(self, *args) -> None:
        for fn in list(self._handlers):
            fn(*args)


# =============================================================================
# ProcessManager - the single home for find / launch / kill
# =============================================================================
class ProcessManager:
    """Owns runtime process state for every app. Having one of these means the
    launch engine, the per-card START/STOP buttons, and the manual SHUTDOWN all
    behave identically instead of reimplementing process handling three ways.

    It tracks what *we* launched (`launched`) separately from what merely happens
    to be running, so ShutdownScope can be honored precisely."""

    def __init__(self) -> None:
        self.launched: dict[str, int] = {}   # app name -> pid we started
        # `launched` is read/written from the engine's worker thread, the
        # manual_shutdown() thread, and the GUI thread (start_app/stop_app on
        # card clicks) -- guard every mutation/snapshot so those can't interleave.
        self._lock = threading.Lock()

    def launched_names(self) -> set[str]:
        """Thread-safe snapshot of the app names we've launched."""
        with self._lock:
            return set(self.launched)

    @classmethod
    def find(cls, process_name: str) -> Optional[psutil.Process]:
        """First running process matching `process_name` (with or without .exe)."""
        return cls.find_many([process_name]).get(process_name)

    @staticmethod
    def find_many(process_names) -> dict[str, psutil.Process]:
        """Resolve several process names in a single process_iter() pass --
        used by the monitor loop so checking N apps costs one system scan per
        tick instead of N. Keyed by the original (un-lowercased) name passed in."""
        wanted: dict[str, str] = {}   # lowercased candidate -> original name
        for name in process_names:
            key = name.lower()
            wanted.setdefault(key, name)
            if not key.endswith(".exe"):
                wanted.setdefault(key + ".exe", name)

        found: dict[str, psutil.Process] = {}
        if not wanted:
            return found
        for proc in psutil.process_iter(["name"]):
            try:
                name = (proc.info["name"] or "").lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            original = wanted.get(name)
            if original and original not in found:
                found[original] = proc
        return found

    def is_running(self, descriptor: AppDescriptor) -> Optional[psutil.Process]:
        return self.find(descriptor.process_name)

    def launch(self, descriptor: AppDescriptor,
               resolved_path: Optional[str] = None) -> Optional[psutil.Process]:
        """Launch the app if it isn't already up. Returns the process (existing
        or new) or None on failure. Records it as ours for scoped shutdown.
        `resolved_path`, if given, is used instead of re-resolving the
        descriptor's path -- callers that already resolved it (e.g. to check
        existence) skip a redundant lookup, which matters for resolvers like
        SrvSurvey's that walk a directory tree."""
        existing = self.find(descriptor.process_name)
        if existing:
            with self._lock:
                self.launched[descriptor.name] = existing.pid
            return existing

        path = resolved_path if resolved_path is not None else descriptor.resolve_path()
        if not path or not os.path.exists(path):
            return None
        try:
            popen = _popen_detached([path], cwd=os.path.dirname(path) or None)
            with self._lock:
                self.launched[descriptor.name] = popen.pid
            return psutil.Process(popen.pid)
        except (OSError, psutil.Error) as exc:
            log.warning("Failed to launch %s: %s", descriptor.name, exc)
            return None

    def stop(self, descriptor: AppDescriptor) -> bool:
        """Kill a single app, by our tracked PID first, then by process name.
        Returns True if something was actually killed. Never touches Steam or
        the game (PROTECTED_PROCESSES)."""
        if descriptor.process_name.lower().removesuffix(".exe") \
                in PROTECTED_PROCESSES:
            log.warning("Refusing to kill protected process %s",
                        descriptor.process_name)
            return False
        with self._lock:
            pid = self.launched.get(descriptor.name)
        proc: Optional[psutil.Process] = None
        if pid:
            try:
                proc = psutil.Process(pid)
            except psutil.NoSuchProcess:
                proc = None
        if proc is None:
            proc = self.find(descriptor.process_name)

        killed = False
        if proc:
            try:
                proc.kill()
                killed = True
            except psutil.Error as exc:
                log.warning("Failed to stop %s: %s", descriptor.name, exc)
        with self._lock:
            self.launched.pop(descriptor.name, None)
        return killed


# =============================================================================
# Engine - the launch -> monitor -> shutdown sequence
# =============================================================================
class Engine:
    """Runs the long sequence on a daemon worker thread and talks outward only
    through Signals. Also exposes single-app start/stop for the status cards."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.pm = ProcessManager()

        # engine -> view
        self.on_log = Signal()      # (message: str, level: str)
        self.on_status = Signal()   # (key: str, state: AppState, pid: int | None)
        self.on_busy = Signal()     # (busy: bool)  -> toggle the LAUNCH button
        self.on_quit = Signal()     # ()  -> close the app: a full session ended

        # The view injects a function that runs a callable on the GUI thread.
        # Default runs inline, which is correct for headless use and tests.
        self.dispatch: Callable[[Callable], None] = lambda fn: fn()

        self._thread: Optional[threading.Thread] = None
        self._shutdown_thread: Optional[threading.Thread] = None
        # Card-driven Steam/Elite start/stop: those wait on processes, so they
        # can't run on the GUI thread like a companion app's quick Popen can.
        self._system_thread: Optional[threading.Thread] = None
        self._refresh_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ---- signal helpers (everything outbound goes through dispatch) ----
    def _log(self, message: str, level: str = "info") -> None:
        log_level = level if level in ("info", "warning", "error") else "info"
        getattr(log, log_level)(message)
        self.dispatch(lambda: self.on_log.emit(message, level))

    def _status(self, key: str, state: AppState, pid: Optional[int] = None) -> None:
        self.dispatch(lambda: self.on_status.emit(key, state, pid))

    def _busy(self, busy: bool) -> None:
        self.dispatch(lambda: self.on_busy.emit(busy))

    def _quit(self) -> None:
        self.dispatch(lambda: self.on_quit.emit())

    @property
    def enabled_apps(self) -> list[AppDescriptor]:
        return [a for a in self.settings.apps if a.enabled]

    def _find_descriptor(self, name: str) -> Optional[AppDescriptor]:
        """Resolve a status-card key to a descriptor. Falls back to SYSTEM_APPS
        so Steam and Elite resolve too -- they aren't in settings.apps, which is
        why their cards' START/STOP used to silently do nothing."""
        return (next((a for a in self.settings.apps if a.name == name), None)
                or config.SYSTEM_APPS.get(name))

    @property
    def tracked_apps(self) -> list[AppDescriptor]:
        """Everything that can have a status card: Steam, the game, and every
        app in settings -- enabled or not. Deliberately wider than
        `enabled_apps`: the view can show read-only cards for apps the CMDR
        hasn't enabled, and those still need honest status. (Emitting status for
        an app with no card is harmless -- StatusPanel.update_card no-ops on an
        unknown key.) `enabled_apps` stays the launch/shutdown set."""
        return list(config.SYSTEM_APPS.values()) + list(self.settings.apps)

    @staticmethod
    def _live_state(name: str) -> AppState:
        """What to report for a process that's up. The game says RUNNING,
        everything else ONLINE -- matching the launch sequence's wording."""
        return AppState.RUNNING if name == KEY_ELITE else AppState.ONLINE

    # ---- public API (called by the view) ----
    def refresh_status(self) -> None:
        """REFRESH button: reconcile every card with what's actually running.
        This is how a game or tool started *before* (or outside of) EDLS stops
        showing as Offline.

        Threaded, because the first process scan of a session can take a couple
        of seconds to warm up -- inline it would freeze the window."""
        if self._refresh_thread and self._refresh_thread.is_alive():
            return
        self._refresh_thread = threading.Thread(target=self._refresh, daemon=True)
        self._refresh_thread.start()

    def _refresh(self) -> None:
        apps = self.tracked_apps
        running = self.pm.find_many(a.process_name for a in apps)
        # Nothing found here is recorded as launched-by-us, so an app the user
        # started by hand stays safe from a LAUNCHED_ONLY shutdown.
        for app in apps:
            if self._stop.is_set():
                return          # window closed mid-scan; nothing to report to
            proc = running.get(app.process_name)
            if proc:
                self._status(app.name, self._live_state(app.name), proc.pid)
            elif not (self._thread and self._thread.is_alive()):
                # Checked per app, not once up front: a launch sequence may have
                # started during the scan above (it can take seconds on a cold
                # process table), and while it runs it owns the status narrative
                # -- better to say nothing than to contradict a "Waiting..." .
                self._status(app.name, AppState.OFFLINE)
        self._log(f"Status refreshed - {len(running)} of {len(apps)} running.",
                  "dim")

    def launch(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._busy(True)
        self._log("Launch sequence initiated.", "success")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def manual_shutdown(self) -> None:
        """SHUTDOWN button. Honors the user's shutdown_scope setting."""
        if self._shutdown_thread and self._shutdown_thread.is_alive():
            return   # already tearing down -- ignore a double-click
        self._shutdown_thread = threading.Thread(
            target=self._shutdown, args=(self.settings.shutdown_scope,), daemon=True
        )
        self._shutdown_thread.start()

    def start_app(self, name: str) -> None:
        # Steam and the game are URI-launched and have to be waited on, so they
        # take the threaded path; a companion app's Popen returns immediately
        # and stays inline on the calling (GUI) thread.
        if name == KEY_STEAM:
            self._run_system(self._card_start_steam)
            return
        if name == KEY_ELITE:
            self._run_system(self._card_start_elite)
            return

        desc = self._find_descriptor(name)
        if not desc:
            return
        self._status(name, AppState.LAUNCHING)
        proc = self.pm.launch(desc)
        if proc:
            self._status(name, AppState.ONLINE, proc.pid)
        else:
            self._status(name, AppState.FAILED)

    def stop_app(self, name: str) -> None:
        # Steam gets asked to quit politely rather than killed (see
        # _card_stop_steam). The game has no graceful exit and killing it
        # violates the ToS, so EDLS simply won't -- the CMDR quits from the
        # in-game menu.
        if name == KEY_STEAM:
            self._run_system(self._card_stop_steam)
            return
        if name == KEY_ELITE:
            self._log("EDLS doesn't close Elite: Dangerous - quit from the "
                      "in-game menu.", "warning")
            return

        desc = self._find_descriptor(name)
        if not desc:
            return
        self.pm.stop(desc)
        self._status(name, AppState.STOPPED)

    # ---- card-driven Steam / Elite control ----
    def _run_system(self, fn: Callable[[], None]) -> None:
        """Run a system-app (Steam/Elite) action on a short-lived worker, unless
        the full launch sequence or a previous action is still going."""
        if self._thread and self._thread.is_alive():
            self._log("Launch sequence already running.", "warning")
            return
        if self._system_thread and self._system_thread.is_alive():
            return
        self._system_thread = threading.Thread(target=fn, daemon=True)
        self._system_thread.start()

    def _card_start_steam(self) -> None:
        existing = self.pm.find(STEAM_PROCESS)
        if existing:
            self._status(KEY_STEAM, AppState.ONLINE, existing.pid)
            self._log("Steam is already running.", "dim")
            return
        self._status(KEY_STEAM, AppState.LAUNCHING)
        self._log("Launching Steam...")
        self._open_uri(STEAM_URI_MAIN)
        proc = self._await_process(STEAM_PROCESS, STEAM_WAIT)
        if self._stop.is_set():
            return                      # window closed mid-wait
        if proc:
            self._status(KEY_STEAM, AppState.ONLINE, proc.pid)
            self._log(f"Steam online. (PID {proc.pid})", "success")
        else:
            self._status(KEY_STEAM, AppState.FAILED)
            self._log(f"Steam did not start within {STEAM_WAIT}s.", "error")

    def _card_stop_steam(self) -> None:
        """Ask Steam to exit via steam://exit. Deliberately NOT a kill: killing
        steam.exe out from under a running game or download is worse than
        leaving it up, so a refusal is reported, not escalated."""
        if not self.pm.find(STEAM_PROCESS):
            self._status(KEY_STEAM, AppState.OFFLINE)
            return
        self._log("Asking Steam to exit...")
        self._open_uri(STEAM_URI_EXIT)
        if self._await_gone(STEAM_PROCESS, STEAM_EXIT_WAIT):
            self._status(KEY_STEAM, AppState.STOPPED)
            self._log("Steam closed.", "success")
        elif not self._stop.is_set():
            self._log("Steam is still running - it won't exit while a game or "
                      "download is active.", "warning")

    def _card_start_elite(self) -> None:
        existing = self.pm.find(ELITE_PROCESS)
        if existing:
            self._status(KEY_ELITE, AppState.RUNNING, existing.pid)
            self._log("Elite: Dangerous is already running.", "dim")
            return
        self._log("Starting the game only - no companion apps, no session "
                  "monitor. Use LAUNCH for the full sequence.", "dim")
        proc = self._launch_elite(timeout=ELITE_WAIT)
        if proc is None and not self._stop.is_set():
            self._status(KEY_ELITE, AppState.FAILED)
            self._log(f"Elite: Dangerous did not start within {ELITE_WAIT}s.",
                      "error")

    def close(self) -> None:
        """Window is closing: stop the worker, but leave the game and tools
        running -- closing the launcher should not close everything."""
        self._stop.set()

    # ---- the sequence ----
    def _run(self) -> None:
        log.info("=== Session started ===")
        try:
            if not self._preflight():
                return
            if not self._start_steam():
                return
            elite = self._launch_elite()
            if not elite:
                return
            launched = self._launch_tools()
            self._log("All systems nominal.", "success")
            if not self._monitor(elite, launched):
                # The window was closed mid-session, not the game exiting --
                # leave the game and tools running (see Engine.close()).
                return
            self._shutdown(self.settings.shutdown_scope)
            self._log("Farewell, CMDR. o7", "success")
            self._quit()   # session's done: Elite exited and tools are torn down
        except Exception as exc:
            log.exception("Launch sequence crashed")
            self._log(f"Unexpected error: {exc}", "error")
        finally:
            log.info("=== Session ended ===")
            self._busy(False)

    def _preflight(self) -> bool:
        self._log("Preflight checks...", "dim")
        if winreg is not None:
            try:
                winreg.OpenKey(winreg.HKEY_CURRENT_USER, STEAM_REG_KEY).Close()
            except OSError:
                self._status(KEY_STEAM, AppState.NOT_FOUND)
                self._log("Steam does not appear to be installed.", "error")
                return False
        for app in self.enabled_apps:
            path = app.resolve_path()
            if not path or not os.path.exists(path):
                self._log(f"{app.name} not found - will be skipped.", "warning")
                self._status(app.name, AppState.NOT_FOUND)
        return True

    def _start_steam(self) -> bool:
        """Bring Steam up for the launch sequence. Returns False only if the
        window was closed mid-wait -- a Steam that's merely slow isn't fatal,
        since steam://run brings the client up on its own."""
        steam = self.pm.find(STEAM_PROCESS)
        if not steam:
            self._status(KEY_STEAM, AppState.LAUNCHING)
            self._log("Steam offline - launching...")
            self._open_uri(STEAM_URI_MAIN)
            steam = self._await_process(STEAM_PROCESS, STEAM_WAIT)
            if self._stop.is_set():
                return False
        if steam:
            self._status(KEY_STEAM, AppState.ONLINE, steam.pid)
            self._log("Steam online.", "success")
        else:
            # Press on, and let _launch_elite correct the card once the game
            # proves Steam came up after all.
            self._status(KEY_STEAM, AppState.WAITING)
            self._log(f"Steam not up after {STEAM_WAIT}s - continuing anyway.",
                      "warning")
        return True

    def _launch_elite(
        self, timeout: Optional[float] = None
    ) -> Optional[psutil.Process]:
        """Open the game through Steam and wait for its process. `timeout=None`
        waits indefinitely (the launch sequence); the card path passes a limit."""
        self._status(KEY_ELITE, AppState.WAITING)
        self._log("Opening Elite: Dangerous via Steam...")
        self._open_uri(f"steam://run/{self.settings.elite_app_id}")
        self._log("Waiting for EliteDangerous64.exe...")
        proc = self._await_process(ELITE_PROCESS, timeout)
        if not proc:
            return None
        self._status(KEY_ELITE, AppState.RUNNING, proc.pid)
        self._log(f"Elite: Dangerous online. (PID {proc.pid})", "success")
        # The game started through Steam, so Steam is definitely up now --
        # correct its card if the wait above timed out.
        steam = self.pm.find(STEAM_PROCESS)
        if steam:
            self._status(KEY_STEAM, AppState.ONLINE, steam.pid)
        return proc

    def _launch_tools(self) -> list[AppDescriptor]:
        launched: list[AppDescriptor] = []
        for app in self.enabled_apps:
            path = app.resolve_path()
            if not path or not os.path.exists(path):
                continue  # already reported NOT_FOUND in preflight
            if self.pm.find(app.process_name):
                self._log(f"{app.name} already running - skipping.", "dim")
                proc = self.pm.launch(app, resolved_path=path)   # records the existing process
                self._status(app.name, AppState.ONLINE, proc.pid if proc else None)
                launched.append(app)
                continue
            self._log(f"Launching {app.name}...")
            self._status(app.name, AppState.LAUNCHING)
            proc = self.pm.launch(app, resolved_path=path)
            if proc:
                self._status(app.name, AppState.ONLINE, proc.pid)
                self._log(f"{app.name} online. (PID {proc.pid})", "success")
                launched.append(app)
            else:
                self._log(f"Failed to launch {app.name}.", "warning")
                self._status(app.name, AppState.FAILED)
        return launched

    def _monitor(self, elite: psutil.Process, launched: list[AppDescriptor]) -> bool:
        """Watches Elite until it exits. Returns False (without touching status/
        logs) if a stop was requested instead -- the caller distinguishes "game
        exited" from "window closed" so it knows whether shutdown should run."""
        self._log("Monitoring Elite: Dangerous...", "dim")
        noted_offline: set[str] = set()
        while elite.is_running():
            if self._sleep(2):
                return False
            pending = [a for a in launched if a.name not in noted_offline]
            if pending:
                running = self.pm.find_many(a.process_name for a in pending)
                for app in pending:
                    if app.process_name not in running:
                        self._status(app.name, AppState.OFFLINE)
                        noted_offline.add(app.name)
        self._status(KEY_ELITE, AppState.OFFLINE)
        self._log("Elite: Dangerous offline.")
        return True

    def _shutdown(self, scope: ShutdownScope) -> None:
        if scope is ShutdownScope.LAUNCHED_ONLY:
            launched_names = self.pm.launched_names()
            targets = [a for a in self.settings.apps if a.name in launched_names]
        else:  # ALL_ENABLED
            targets = self.enabled_apps

        running = [a for a in targets if self.pm.is_running(a)]
        if not running:
            self._log("No tools running.", "dim")
            return
        self._log("Closing third-party tools...")
        for app in running:
            self._log(f"Stopping {app.name}...")
            if self.pm.stop(app):
                self._status(app.name, AppState.CLOSED)
        self._log("Third-party tools shut down.", "success")

    # ---- small helpers ----
    @staticmethod
    def _open_uri(uri: str) -> None:
        if hasattr(os, "startfile"):
            # Not os.startfile: when Steam isn't running, ShellExecute makes
            # steam.exe (and so the game) our child, inside our Job Object. Going
            # through a detached `cmd /c start` keeps them out of it.
            try:
                _popen_detached(["cmd", "/c", "start", "", uri],
                                creationflags=subprocess.CREATE_NO_WINDOW)
            except OSError:
                os.startfile(uri)
        else:                                       # dev fallback on non-Windows
            subprocess.Popen(["xdg-open", uri])

    def _sleep(self, seconds: float) -> bool:
        """Interruptible sleep. Returns True if a stop was requested while waiting."""
        return self._stop.wait(timeout=seconds)

    def _await_process(self, process_name: str,
                       timeout: Optional[float] = None
                       ) -> Optional[psutil.Process]:
        """Poll until `process_name` appears. Returns it, or None if the timeout
        elapsed or close() was requested. `timeout=None` waits indefinitely."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._stop.is_set():
            proc = self.pm.find(process_name)
            if proc:
                return proc
            if deadline is not None and time.monotonic() >= deadline:
                return None
            if self._sleep(POLL_SECONDS):
                return None
        return None

    def _await_gone(self, process_name: str, timeout: float) -> bool:
        """The inverse: poll until `process_name` is gone. False on timeout or
        if close() was requested."""
        deadline = time.monotonic() + timeout
        while not self._stop.is_set():
            if not self.pm.find(process_name):
                return True
            if time.monotonic() >= deadline:
                return False
            if self._sleep(POLL_SECONDS):
                return False
        return False


# =============================================================================
# Composition root
# =============================================================================
def main() -> int:
    setup_logging()
    settings = config.load_settings()
    engine = Engine(settings)
    import ui  # late import: driving the engine headless needs no GUI toolkit
    return ui.run(engine, settings)


if __name__ == "__main__":
    raise SystemExit(main())
