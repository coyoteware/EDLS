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
#             .show_inactive_cards .shutdown_scope .apps (list[AppDescriptor])
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

log = logging.getLogger("edls")


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
            popen = subprocess.Popen([path], cwd=os.path.dirname(path) or None)
            with self._lock:
                self.launched[descriptor.name] = popen.pid
            return psutil.Process(popen.pid)
        except (OSError, psutil.Error) as exc:
            log.warning("Failed to launch %s: %s", descriptor.name, exc)
            return None

    def stop(self, descriptor: AppDescriptor) -> bool:
        """Kill a single app, by our tracked PID first, then by process name.
        Returns True if something was actually killed."""
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

        # The view injects a function that runs a callable on the GUI thread.
        # Default runs inline, which is correct for headless use and tests.
        self.dispatch: Callable[[Callable], None] = lambda fn: fn()

        self._thread: Optional[threading.Thread] = None
        self._shutdown_thread: Optional[threading.Thread] = None
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

    @property
    def enabled_apps(self) -> list[AppDescriptor]:
        return [a for a in self.settings.apps if a.enabled]

    def _find_descriptor(self, name: str) -> Optional[AppDescriptor]:
        return next((a for a in self.settings.apps if a.name == name), None)

    # ---- public API (called by the view) ----
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
        desc = self._find_descriptor(name)
        if not desc:
            return
        self.pm.stop(desc)
        self._status(name, AppState.STOPPED)

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
        if not self.pm.find(STEAM_PROCESS):
            self._status(KEY_STEAM, AppState.LAUNCHING)
            self._log("Steam offline - launching...")
            self._open_uri("steam://open/main")
            if self._sleep(10):           # interrupted by close()
                return False
        steam = self.pm.find(STEAM_PROCESS)
        self._status(KEY_STEAM, AppState.ONLINE, steam.pid if steam else None)
        self._log("Steam online.", "success")
        return True

    def _launch_elite(self) -> Optional[psutil.Process]:
        self._status(KEY_ELITE, AppState.WAITING)
        self._log("Opening Elite: Dangerous via Steam...")
        self._open_uri(f"steam://run/{self.settings.elite_app_id}")
        self._log("Waiting for EliteDangerous64.exe...")
        while not self._stop.is_set():
            proc = self.pm.find(ELITE_PROCESS)
            if proc:
                self._status(KEY_ELITE, AppState.RUNNING, proc.pid)
                self._log(f"Elite: Dangerous online. (PID {proc.pid})", "success")
                return proc
            if self._sleep(2):
                return None
        return None

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
            os.startfile(uri)                       # Windows: launches steam:// URIs
        else:                                       # dev fallback on non-Windows
            subprocess.Popen(["xdg-open", uri])

    def _sleep(self, seconds: float) -> bool:
        """Interruptible sleep. Returns True if a stop was requested while waiting."""
        return self._stop.wait(timeout=seconds)


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
