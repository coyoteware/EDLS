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
from enum import Enum
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
#                                                    STOPPED WAITING LAUNCHING
#                                                    ONLINE RUNNING ERROR
#   ShutdownScope (Enum): LAUNCHED_ONLY | ALL_ENABLED
#   AppDescriptor: .name .process_name .enabled  +  .resolve_path() -> str | None
#   Settings: .cmdr_name .elite_app_id .setup_complete .auto_start
#             .show_inactive_cards .shutdown_scope .close_on_game_exit
#             .close_on_shutdown .theme .apps (list[AppDescriptor])
#             .removed_defaults (list[str])
#   load_settings() -> Settings          (problems land in LOAD_WARNINGS)
#   save_settings(settings) -> bool      (False if the file couldn't be written)
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
ELITE_WAIT = 60        # the game to appear after steam://run (LAUNCH and card)
KILL_WAIT = 3          # a killed tool to actually be gone
CLOSE_DELAY = 5        # countdown before EDLS closes itself after a session

# The status watcher's scan interval. One scan is a single process-table pass
# (~6-9 ms of CPU), so even the fast rate costs well under 1% of one core.
WATCH_FAST = 2         # while something is in flight, or just after a click
WATCH_SLOW = 5         # idle
WATCH_ACTIVE_FOR = 20  # how long a click keeps the fast rate

log = logging.getLogger("edls")


def _stem(process_name: str) -> str:
    """Process names compare case-insensitively, with or without '.exe'."""
    return process_name.lower().removesuffix(".exe")


# EDLS must never kill Steam or the game -- force-closing Elite violates
# Frontier's ToS. ProcessManager.stop() refuses these outright, so no setting,
# custom app entry, or future code path can get there by accident.
PROTECTED_PROCESSES = frozenset(_stem(n) for n in (STEAM_PROCESS, ELITE_PROCESS))


# States that mean "on its way": the watcher leaves these alone until the
# process actually shows up (a Steam that's still starting isn't "Offline").
_PENDING_STATES = (AppState.LAUNCHING, AppState.WAITING)

# Watcher bookkeeping: an app it has never reported on (startup, or just added).
_UNSEEN = object()


class StopResult(Enum):
    STOPPED = "stopped"           # it was running and is now gone
    NOT_RUNNING = "not_running"   # nothing to close
    FAILED = "failed"             # still running, e.g. it runs as administrator
    PROTECTED = "protected"       # Steam or the game -- never closed

# If EDLS itself runs inside a kill-on-close Job Object (VS Code's debugpy
# launcher does this), anything we start joins that job and Windows kills it
# the moment EDLS exits -- that's how closing EDLS took Steam and the game down
# with it. Breaking away keeps what we launch independent of our lifetime.
_BREAKAWAY = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)


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

    def stop(self, descriptor: AppDescriptor) -> StopResult:
        """Kill a single app, by our tracked PID first, then by process name,
        and wait until it's really gone. Never touches Steam or the game
        (PROTECTED_PROCESSES)."""
        if _stem(descriptor.process_name) in PROTECTED_PROCESSES:
            log.warning("Refusing to kill protected process %s",
                        descriptor.process_name)
            return StopResult.PROTECTED
        with self._lock:
            pid = self.launched.pop(descriptor.name, None)
        proc: Optional[psutil.Process] = None
        if pid:
            try:
                tracked = psutil.Process(pid)
                # Windows reuses PIDs: only trust ours if it's still that program,
                # or SHUTDOWN could kill whatever inherited the number.
                if _stem(tracked.name()) == _stem(descriptor.process_name):
                    proc = tracked
            except psutil.Error:
                pass
        if proc is None:
            proc = self.find(descriptor.process_name)
        if proc is None:
            return StopResult.NOT_RUNNING
        try:
            proc.kill()
            proc.wait(timeout=KILL_WAIT)
            return StopResult.STOPPED
        except psutil.NoSuchProcess:
            return StopResult.STOPPED
        except psutil.Error as exc:   # AccessDenied (elevated app) or TimeoutExpired
            log.warning("Failed to stop %s: %s", descriptor.name, exc)
            if pid:   # it's still ours -- keep it in scope for a later SHUTDOWN
                with self._lock:
                    self.launched[descriptor.name] = proc.pid
            return StopResult.FAILED


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
        self._watch_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()     # interrupts the current waits
        self._closed = threading.Event()   # window closed: start nothing new
        self._wake = threading.Event()     # asks the watcher to scan right away
        self._last_action = 0.0

        # What each card currently claims, so the watcher only speaks up when
        # reality differs: True = running, False = not running, None = pending
        # (Launching/Waiting -- only the process appearing may change it). Kept
        # by _status(), the single place statuses go out.
        self._believed: dict[str, Optional[bool]] = {}
        self._set_at: dict[str, float] = {}
        self._watch_lock = threading.Lock()

    # ---- signal helpers (everything outbound goes through dispatch) ----
    def _log(self, message: str, level: str = "info") -> None:
        log_level = level if level in ("info", "warning", "error") else "info"
        getattr(log, log_level)(message)
        self.dispatch(lambda: self.on_log.emit(message, level))

    def _status(self, key: str, state: AppState, pid: Optional[int] = None) -> None:
        with self._watch_lock:
            self._believed[key] = (None if state in _PENDING_STATES
                                   else state.is_running)
            self._set_at[key] = time.monotonic()
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

    @property
    def session_active(self) -> bool:
        """A LAUNCH session is running (from LAUNCH until the game exits and
        the teardown is done). The app list is locked for its duration."""
        return self._thread is not None and self._thread.is_alive()

    # ---- status watcher ----
    def start_watching(self) -> None:
        """Start the background watcher that keeps every card honest -- this
        replaced the REFRESH button. Call once, after the view is wired up."""
        if self._watch_thread and self._watch_thread.is_alive():
            return
        self._watch_thread = threading.Thread(target=self._watch, daemon=True)
        self._watch_thread.start()

    def request_scan(self) -> None:
        """The view's card list changed (e.g. Settings saved): scan right away
        so new or renamed apps don't wait for the next tick."""
        self._kick()

    def _kick(self) -> None:
        """A click happened: scan now, then stay on the fast rate for a while."""
        self._last_action = time.monotonic()
        self._wake.set()

    def _watch_interval(self) -> float:
        busy = any(t is not None and t.is_alive() for t in
                   (self._thread, self._system_thread, self._shutdown_thread))
        recent = time.monotonic() - self._last_action < WATCH_ACTIVE_FOR
        return WATCH_FAST if busy or recent else WATCH_SLOW

    def _watch(self) -> None:
        first = True
        while not self._closed.is_set():
            scan_started = time.monotonic()
            apps = self.tracked_apps
            try:
                found = self.pm.find_many(a.process_name for a in apps)
            except Exception:   # never let one bad scan kill the watcher
                log.exception("Status scan failed")
                found = None
            if found is not None and not self._closed.is_set():
                self._watch_tick(apps, found, scan_started, quiet=first)
                if first:
                    self._log(f"Status check: {len(found)} of {len(apps)} "
                              "running.", "dim")
                    first = False
            if self._wake.wait(timeout=self._watch_interval()):
                self._wake.clear()

    def _watch_tick(self, apps: list[AppDescriptor], found: dict,
                    scan_started: float, quiet: bool = False) -> None:
        """Compare one scan with what the cards claim and report only the
        differences. Separate from _watch() so selftest can feed it fake scans."""
        session = self.session_active
        for app in apps:
            proc = found.get(app.process_name)
            with self._watch_lock:
                # >=, not >: monotonic() only ticks every ~16 ms on Windows.
                if self._set_at.get(app.name, 0.0) >= scan_started:
                    continue   # an action changed this card after we scanned
                believed = self._believed.get(app.name, _UNSEEN)
            label = "Elite: Dangerous" if app.name == KEY_ELITE else app.name
            # The session reports the game's comings and goings itself.
            speak = (not quiet and believed is not _UNSEEN
                     and not (session and app.name == KEY_ELITE))
            if proc and believed is not True:
                self._status(app.name, self._live_state(app.name), proc.pid)
                if speak and believed is False:
                    self._log(f"{label} is now running.", "dim")
            elif not proc and (believed is True or believed is _UNSEEN):
                self._status(app.name, AppState.OFFLINE)
                if speak:
                    self._log(f"{label} stopped running.", "dim")

    # ---- public API (called by the view) ----
    def launch(self) -> None:
        if self._closed.is_set() or self.session_active:
            return
        if self._system_thread and self._system_thread.is_alive():
            self._log("Steam or the game is still being started from its card "
                      "- wait for it to finish.", "warning")
            return
        self._stop.clear()
        self._kick()
        self._busy(True)
        self._log("Launch sequence initiated.", "success")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def manual_shutdown(self) -> None:
        """SHUTDOWN button. Honors the user's shutdown_scope setting."""
        if self._shutdown_thread and self._shutdown_thread.is_alive():
            return   # already tearing down -- ignore a double-click
        self._kick()
        self._shutdown_thread = threading.Thread(
            target=self._manual_shutdown, daemon=True)
        self._shutdown_thread.start()

    def _manual_shutdown(self) -> None:
        self._teardown(self.settings.shutdown_scope)
        game_running = self.pm.find(ELITE_PROCESS) is not None
        if game_running:
            self._log("Quit Elite: Dangerous from the in-game menu - EDLS never "
                      "closes the game.", "warning")
        if self.settings.close_on_shutdown:
            self._close_countdown()
        elif self.session_active and game_running:
            self._log("Waiting for Elite: Dangerous to close...", "dim")

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
        self._kick()
        if self.pm.find(desc.process_name):
            proc = self.pm.launch(desc)   # adopts the running copy
            self._status(name, AppState.ONLINE, proc.pid if proc else None)
            self._log(f"{name} is already running.", "dim")
            return
        path = desc.resolve_path()
        if not path or not os.path.exists(path):
            self._status(name, AppState.NOT_FOUND)
            self._log(f"{name} not found - check its path in Settings.",
                      "warning")
            return
        self._status(name, AppState.LAUNCHING)
        self._log(f"Starting {name}...")
        proc = self.pm.launch(desc, resolved_path=path)
        if proc:
            self._status(name, AppState.ONLINE, proc.pid)
            self._log(f"{name} online. (PID {proc.pid})", "success")
        else:
            self._status(name, AppState.FAILED)
            self._log(f"Couldn't start {name} (see launcher.log).", "error")

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
        self._kick()
        # Threaded: stop() waits for the process to really exit, which mustn't
        # freeze the window.
        threading.Thread(target=self._stop_one, args=(desc,), daemon=True).start()

    def _stop_one(self, desc: AppDescriptor) -> None:
        self._log(f"Stopping {desc.name}...")
        result = self.pm.stop(desc)
        if result is StopResult.STOPPED:
            self._status(desc.name, AppState.STOPPED)
            self._log(f"{desc.name} stopped.", "success")
        elif result is StopResult.NOT_RUNNING:
            self._status(desc.name, AppState.STOPPED)
            self._log(f"{desc.name} wasn't running.", "dim")
        else:
            self._report_unstoppable(desc, result)

    def _report_unstoppable(self, desc: AppDescriptor,
                            result: StopResult) -> None:
        """A tool EDLS couldn't (or won't) close: say so, and for a real failure
        flag its card ERROR -- it's still running, so 'Stopped' would lie."""
        if result is StopResult.PROTECTED:
            self._log(f"EDLS never closes {desc.name} - it's Steam or the game.",
                      "warning")
            return
        proc = self.pm.find(desc.process_name)
        self._status(desc.name, AppState.ERROR, proc.pid if proc else None)
        self._log(f"Couldn't close {desc.name} - it may be running as "
                  "administrator.", "error")

    # ---- card-driven Steam / Elite control ----
    def _run_system(self, fn: Callable[[], None]) -> None:
        """Run a system-app (Steam/Elite) action on a short-lived worker, unless
        the full launch sequence or a previous action is still going."""
        if self._thread and self._thread.is_alive():
            self._log("Launch sequence already running.", "warning")
            return
        if self._system_thread and self._system_thread.is_alive():
            return
        self._kick()
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
            # If it turns up later anyway, the watcher flips the card to Running.
            self._status(KEY_ELITE, AppState.FAILED)
            self._log(f"Elite: Dangerous did not start within {ELITE_WAIT}s.",
                      "error")

    def close(self) -> None:
        """Window is closing: stop the workers, but leave the game and tools
        running -- closing the launcher should not close everything. Also
        blocks anything queued (e.g. a late auto-start) from starting."""
        self._closed.set()
        self._stop.set()
        self._wake.set()

    # ---- the sequence ----
    def _run(self) -> None:
        log.info("=== Session started ===")
        close_after = False
        try:
            if not self._preflight():
                return
            if not self._start_steam():
                return
            elite = self._launch_elite(timeout=ELITE_WAIT)
            if not elite:
                if not self._stop.is_set():
                    # The watcher still flips the card if the game turns up late.
                    self._status(KEY_ELITE, AppState.FAILED)
                    self._log(f"Elite: Dangerous didn't start within "
                              f"{ELITE_WAIT}s - launch cancelled. Press LAUNCH "
                              "to try again.", "error")
                return
            self._launch_tools()
            self._log("All systems nominal.", "success")
            if not self._monitor(elite):
                # The window was closed mid-session, not the game exiting --
                # leave the game and tools running (see Engine.close()).
                return
            self._teardown(self.settings.shutdown_scope)
            if self.settings.close_on_game_exit:
                close_after = True
            else:
                self._log("Session ended.", "success")
        except Exception as exc:
            log.exception("Launch sequence crashed")
            self._log(f"Unexpected error: {exc}", "error")
        finally:
            log.info("=== Session ended ===")
            if close_after:
                self._close_countdown()   # LAUNCH stays disabled until we go
            else:
                self._busy(False)

    def _close_countdown(self) -> None:
        """Close EDLS after a short, logged countdown (the window closing
        interrupts it). The game and Steam are never touched."""
        self._busy(True)
        self._log(f"EDLS will close in {CLOSE_DELAY} seconds.")
        if self._closed.wait(CLOSE_DELAY - 1):
            return
        self._log("Farewell, CMDR. o7", "success")
        if self._closed.wait(1):
            return
        self._quit()

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
        """Open the game through Steam and wait for its process, up to `timeout`
        seconds (None = forever). Returns None on timeout or close()."""
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

    def _monitor(self, elite: psutil.Process) -> bool:
        """Watches Elite until it exits. Returns False (without touching status/
        logs) if a stop was requested instead -- the caller distinguishes "game
        exited" from "window closed" so it knows whether shutdown should run.
        Tools' own comings and goings are the status watcher's job."""
        self._log("Monitoring Elite: Dangerous...", "dim")
        while elite.is_running():
            if self._sleep(POLL_SECONDS):
                return False
        self._status(KEY_ELITE, AppState.OFFLINE)
        self._log("Elite: Dangerous offline.")
        return True

    def _teardown(self, scope: ShutdownScope) -> None:
        """Close the companion apps the scope covers (SHUTDOWN and game exit)."""
        if scope is ShutdownScope.LAUNCHED_ONLY:
            launched_names = self.pm.launched_names()
            targets = [a for a in self.settings.apps if a.name in launched_names]
        else:  # ALL_ENABLED
            targets = self.enabled_apps

        found = self.pm.find_many(a.process_name for a in targets)
        running = [a for a in targets if a.process_name in found]
        if not running:
            self._log("No tools running.", "dim")
            return
        self._log("Closing third-party tools...")
        failed: list[str] = []
        for app in running:
            self._log(f"Stopping {app.name}...")
            result = self.pm.stop(app)
            if result in (StopResult.STOPPED, StopResult.NOT_RUNNING):
                self._status(app.name, AppState.OFFLINE)
            else:
                self._report_unstoppable(app, result)
                if result is StopResult.FAILED:
                    failed.append(app.name)
        if failed:
            self._log(f"Some tools could not be closed: {', '.join(failed)}.",
                      "warning")
        else:
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
    import ui  # late import: driving the engine headless needs no GUI toolkit
    # Before anything touches settings.json: a second copy just brings the
    # running one to the front and leaves.
    if not ui.acquire_single_instance():
        log.info("EDLS is already running - showed that window instead.")
        return 0
    settings = config.load_settings()
    engine = Engine(settings)
    return ui.run(engine, settings)


if __name__ == "__main__":
    raise SystemExit(main())
