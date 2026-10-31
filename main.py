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

import http.client
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import contextmanager
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
#             .close_on_shutdown .allow_elevation .theme
#             .apps (list[AppDescriptor])
#             .removed_defaults (list[str]) .check_for_updates
#   is_newer(), LATEST_RELEASE_API                   the update check
#   load_settings() -> Settings          (problems land in LOAD_WARNINGS)
#   save_settings(settings) -> bool      (False if the file couldn't be written)
#
# ui.py provides:
#   run(engine, settings) -> int     builds the window, shows the first-run
#                                     dialog if not settings.setup_complete,
#                                     triggers engine.launch() if auto_start,
#                                     starts the update check,
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
CLOSE_GRACE = 3        # a tool asked to close gets this long before it's killed
CLOSE_DELAY = 5        # countdown before EDLS closes itself after a session
# The window closed while tools were still being closed (SHUTDOWN, a card's
# STOP, the end of a session): EDLS stays, invisibly, until that's finished --
# normally CLOSE_GRACE -- but no longer than this.
EXIT_WAIT = 10

# Tools that need admin rights (EDHM-UI's manifest asks for them). Popen can't
# elevate -- CreateProcess just fails with 740 -- so those go through the
# shell's "runas" instead, which shows the UAC prompt (Settings.allow_elevation).
ERROR_ELEVATION_REQUIRED = 740
ERROR_CANCELLED = 1223     # the CMDR answered the UAC prompt with No
ELEVATED_FIND_WAIT = 3     # an elevated launch to show up (the shell gives no PID)
LAUNCHER_WAIT = 30         # a tool's launcher (it may update the tool first) to start it
ELEVATED_CLOSE_WAIT = CLOSE_GRACE + KILL_WAIT + 4   # the elevated close to finish
CLOSE_ELEVATED_FLAG = "--close-elevated"   # see _close_elevated()

# Settings.start_minimized: the show state a tool is started with -- minimized
# on the taskbar without taking focus from the game. Only a hint: a tool that
# picks its own window state (a launcher, a splash screen, a remembered
# position) opens normally anyway. EDLS never minimizes a window afterwards.
SW_SHOWNORMAL = 1
SW_SHOWMINNOACTIVE = 7

# The status watcher's scan interval. One scan is a single process-table pass
# (~6-9 ms of CPU), so even the fast rate costs well under 1% of one core.
WATCH_FAST = 2         # while something is in flight, or just after a click
WATCH_SLOW = 5         # idle
WATCH_ACTIVE_FOR = 20  # how long a click keeps the fast rate

log = logging.getLogger("edls")


def _stem(process_name: str) -> str:
    """Process names compare case-insensitively, with or without '.exe'."""
    return process_name.lower().removesuffix(".exe")


# Some tools run as several processes with one name: an Electron app (EDHM-UI)
# is a main process that owns the window plus helpers it starts itself. Asking
# a helper to close does nothing -- it has no window -- so EDLS always talks to
# the main one, and makes sure the helpers go with it.
def _root(procs: list[psutil.Process]) -> psutil.Process:
    """Of the same-name processes found, the main one: whose parent isn't one
    of the others. (The parent PID comes from a system snapshot, so this works
    for a tool running as administrator too.)"""
    if len(procs) == 1:
        return procs[0]
    pids = {p.pid for p in procs}
    for proc in procs:
        try:
            if proc.ppid() not in pids:
                return proc
        except psutil.Error:
            continue
    return procs[0]


def _same_name_descendants(proc: psutil.Process) -> list[psutil.Process]:
    """The processes `proc` started, at any depth, that carry its name -- an
    Electron app's helpers. Anything else it started (a browser it opened,
    say) is left alone."""
    try:
        stem = _stem(proc.name())
        children = proc.children(recursive=True)
    except psutil.Error:
        return []
    same = []
    for child in children:
        try:
            if _stem(child.name()) == stem:
                same.append(child)
        except psutil.Error:
            pass   # already gone
    return same


# EDLS must never kill Steam or the game -- force-closing Elite violates
# Frontier's ToS. ProcessManager.stop() refuses these outright, so no setting,
# custom app entry, or future code path can get there by accident.
PROTECTED_PROCESSES = frozenset(_stem(n) for n in (STEAM_PROCESS, ELITE_PROCESS))


# States that mean "on its way": the watcher leaves these alone until the
# process actually shows up (a Steam that's still starting isn't "Offline").
_PENDING_STATES = (AppState.LAUNCHING, AppState.WAITING)

# Watcher bookkeeping: an app it has never reported on (startup, or just added).
_UNSEEN = object()

# The update check only asks GitHub which release is latest. EDLS never
# downloads or runs an installer: the CMDR gets the release page.
UPDATE_TIMEOUT = 10    # seconds


class StopResult(Enum):
    STOPPED = "stopped"           # it was running and is now gone
    NOT_RUNNING = "not_running"   # nothing to close
    FAILED = "failed"             # still running, e.g. it runs as administrator
    PROTECTED = "protected"       # Steam or the game -- never closed


class NeedsAdmin(Exception):
    """A tool asks Windows for administrator rights (its manifest, or a "Run as
    administrator" compatibility tick) and wasn't started. `declined`: the CMDR
    said No to the UAC prompt; otherwise EDLS wasn't allowed to ask."""

    def __init__(self, declined: bool) -> None:
        super().__init__("UAC prompt declined" if declined
                         else "needs administrator rights")
        self.declined = declined

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


def _ask_to_close(pid: int) -> None:
    """Ask a tool to close the way its own X button would: `taskkill` without
    /F posts WM_CLOSE to its windows, so it can save and exit cleanly. One that
    ignores it (hidden in the tray, a "save changes?" prompt, a console app) is
    killed once its grace period runs out -- see ProcessManager.stop()."""
    try:
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True,
                       timeout=5,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        pass   # stop() kills it anyway


def _run_as_admin(path: str, args: str = "", cwd: Optional[str] = None,
                  show: int = 1) -> None:
    """Start `path` with administrator rights: Windows shows the UAC prompt,
    and this returns once it's answered. Raises OSError -- winerror
    ERROR_CANCELLED if the CMDR said No. Windows, not EDLS, creates the
    elevated process, so it's outside our Job Object as well."""
    if not hasattr(os, "startfile"):
        raise OSError("starting a program as administrator needs Windows")
    os.startfile(path, "runas", arguments=args, cwd=cwd, show_cmd=show)


def _winerror(exc: OSError) -> Optional[int]:
    return getattr(exc, "winerror", None)


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


def install_crash_logging() -> None:
    """Send uncaught exceptions -- on the GUI thread (Qt hands a slot's error
    to sys.excepthook) or on any worker -- to launcher.log. The installed build
    has no console, so without this a crash leaves nothing to report. The
    previous hooks still run, so a terminal shows tracebacks as before."""
    prev_hook, prev_thread_hook = sys.excepthook, threading.excepthook

    def on_error(exc_type, exc, tb) -> None:
        if not issubclass(exc_type, KeyboardInterrupt):
            log.critical("Uncaught exception", exc_info=(exc_type, exc, tb))
        if sys.stderr is not None:
            prev_hook(exc_type, exc, tb)

    def on_thread_error(args) -> None:
        if not issubclass(args.exc_type, SystemExit):
            name = args.thread.name if args.thread else "a worker thread"
            log.critical("Uncaught exception in %s", name,
                         exc_info=(args.exc_type, args.exc_value,
                                   args.exc_traceback))
        if sys.stderr is not None:
            prev_thread_hook(args)

    sys.excepthook = on_error
    threading.excepthook = on_thread_error


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

    It tracks what *we* launched or took over (`launched`: a tool that was
    already running when LAUNCH or its START reached it counts too) separately
    from what merely happens to be running, so ShutdownScope can be honored
    precisely."""

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
        """The running process matching `process_name` (with or without .exe)
        -- for a program that runs as several, its main one (see _root)."""
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

        matches: dict[str, list[psutil.Process]] = {}
        if not wanted:
            return {}
        for proc in psutil.process_iter(["name"]):
            try:
                name = (proc.info["name"] or "").lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            original = wanted.get(name)
            if original:
                matches.setdefault(original, []).append(proc)
        return {original: _root(procs) for original, procs in matches.items()}

    def adopt(self, descriptor: AppDescriptor) -> Optional[psutil.Process]:
        """Take the tool over if it's already running: it's recorded as ours,
        so scoped shutdown closes it. None if it isn't running."""
        existing = self.find(descriptor.process_name)
        if existing:
            with self._lock:
                self.launched[descriptor.name] = existing.pid
        return existing

    def launch(self, descriptor: AppDescriptor,
               resolved_path: Optional[str] = None,
               elevate: Optional[Callable[[], bool]] = None,
               minimized: bool = False) -> Optional[psutil.Process]:
        """Launch the app if it isn't already up. Returns the process (existing
        or new) or None on failure. Records it as ours for scoped shutdown.
        `resolved_path`, if given, is used instead of re-resolving the
        descriptor's path -- callers that already resolved it (e.g. to check
        existence) skip a redundant lookup, which matters for resolvers like
        SrvSurvey's that walk a directory tree.

        A tool that needs admin rights is only started if `elevate()` says
        Windows may ask (the UAC prompt, which blocks until it's answered);
        otherwise, or on a No, this raises NeedsAdmin.

        `minimized` asks Windows to start it minimized (SW_SHOWMINNOACTIVE);
        a copy that's already running is left as it is."""
        existing = self.adopt(descriptor)
        if existing:
            return existing

        path = resolved_path if resolved_path is not None else descriptor.resolve_path()
        if not path or not os.path.exists(path):
            return None
        cwd = os.path.dirname(path) or None
        extra = {}
        if minimized and hasattr(subprocess, "STARTUPINFO"):
            extra["startupinfo"] = subprocess.STARTUPINFO(
                dwFlags=subprocess.STARTF_USESHOWWINDOW,
                wShowWindow=SW_SHOWMINNOACTIVE)
        try:
            popen = _popen_detached([path], cwd=cwd, **extra)
            if _stem(os.path.basename(path)) != _stem(descriptor.process_name):
                # A launcher (Odyssey Materials Helper's updates it, starts it
                # and exits): the tool is the process to wait for and track.
                return self._adopt_by_name(descriptor, LAUNCHER_WAIT,
                                           "through its launcher")
            with self._lock:
                self.launched[descriptor.name] = popen.pid
            return psutil.Process(popen.pid)
        except OSError as exc:
            if _winerror(exc) == ERROR_ELEVATION_REQUIRED:
                if elevate is None or not elevate():
                    raise NeedsAdmin(declined=False) from None
                return self._launch_elevated(descriptor, path, cwd, minimized)
            log.warning("Failed to launch %s: %s", descriptor.name, exc)
            return None
        except psutil.Error as exc:
            log.warning("Failed to launch %s: %s", descriptor.name, exc)
            return None

    def _launch_elevated(self, descriptor: AppDescriptor, path: str,
                         cwd: Optional[str], minimized: bool = False
                         ) -> Optional[psutil.Process]:
        """launch()'s second try for a tool that needs admin rights."""
        try:
            _run_as_admin(path, cwd=cwd, show=(SW_SHOWMINNOACTIVE if minimized
                                               else SW_SHOWNORMAL))
        except OSError as exc:
            if _winerror(exc) == ERROR_CANCELLED:
                raise NeedsAdmin(declined=True) from None
            log.warning("Failed to launch %s as administrator: %s",
                        descriptor.name, exc)
            return None
        # The shell hands back no PID.
        return self._adopt_by_name(descriptor, ELEVATED_FIND_WAIT,
                                   "as administrator")

    def _adopt_by_name(self, descriptor: AppDescriptor, wait: float,
                       how: str) -> Optional[psutil.Process]:
        """Wait up to `wait` s for a tool we started without its PID to show
        up, then record it as ours. Until then it's ours by name (PID 0 makes
        _target look it up by name), which a timeout leaves in place."""
        with self._lock:
            self.launched[descriptor.name] = 0
        deadline = time.monotonic() + wait
        while (proc := self.find(descriptor.process_name)) is None:
            if time.monotonic() >= deadline:
                log.warning("%s was started %s but hasn't appeared yet.",
                            descriptor.name, how)
                return None
            time.sleep(0.25)
        with self._lock:
            self.launched[descriptor.name] = proc.pid
        return proc

    def _target(self, descriptor: AppDescriptor, forget: bool
                ) -> tuple[Optional[psutil.Process], Optional[int]]:
        """The running process to close for `descriptor` -- our tracked PID if
        it still belongs to that program, else the first one by name -- plus
        the PID we had tracked. `forget` drops it from `launched` (stop() puts
        it back if the close fails)."""
        with self._lock:
            pid = (self.launched.pop(descriptor.name, None) if forget
                   else self.launched.get(descriptor.name))
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
        return proc, pid

    def ask_to_close(self, descriptor: AppDescriptor) -> None:
        """Ask one app to close (see _ask_to_close) without waiting for it.
        _teardown asks every tool up front, so their grace periods overlap
        instead of adding up."""
        if _stem(descriptor.process_name) in PROTECTED_PROCESSES:
            return
        proc = self._target(descriptor, forget=False)[0]
        if proc is not None:
            _ask_to_close(proc.pid)

    def stop(self, descriptor: AppDescriptor,
             deadline: Optional[float] = None,
             asked: bool = False) -> StopResult:
        """Close a single app and wait until it's really gone: ask it to close
        (unless the caller already `asked`), give it until `deadline` (default:
        CLOSE_GRACE from now), then kill it. Our tracked PID first, then by
        process name. Never touches Steam or the game (PROTECTED_PROCESSES)."""
        if _stem(descriptor.process_name) in PROTECTED_PROCESSES:
            log.warning("Refusing to kill protected process %s",
                        descriptor.process_name)
            return StopResult.PROTECTED
        proc, pid = self._target(descriptor, forget=True)
        if proc is None:
            return StopResult.NOT_RUNNING
        helpers = _same_name_descendants(proc)
        if deadline is None:
            deadline = time.monotonic() + CLOSE_GRACE
        if (self._close(proc, descriptor.name, deadline, asked)
                and self._close_helpers(helpers, descriptor.name)):
            return StopResult.STOPPED
        if pid is not None:   # still ours -- keep it in scope for a later SHUTDOWN
            with self._lock:
                self.launched[descriptor.name] = proc.pid
        return StopResult.FAILED

    @staticmethod
    def _close(proc: psutil.Process, name: str, deadline: float,
               asked: bool) -> bool:
        """Ask one process to close (unless already `asked`), give it until
        `deadline`, then kill it. True once it's gone."""
        if not asked:
            _ask_to_close(proc.pid)
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            return True                        # it closed itself
        except psutil.NoSuchProcess:
            return True
        except psutil.TimeoutExpired:
            log.info("%s didn't close when asked - killing it.", name)
        except psutil.Error:
            pass   # can't wait on it (e.g. access denied): straight to the kill
        try:
            proc.kill()
            proc.wait(timeout=KILL_WAIT)
            return True
        except psutil.NoSuchProcess:
            return True
        except psutil.Error as exc:   # AccessDenied (elevated app) or TimeoutExpired
            log.warning("Failed to stop %s: %s", name, exc)
            return False

    def _close_helpers(self, helpers: list[psutil.Process], name: str) -> bool:
        """The main process is gone; its same-name helpers (an Electron app's)
        normally follow within a moment. Any still running after KILL_WAIT get
        the same ask-then-kill. True once they're all gone."""
        deadline = time.monotonic() + KILL_WAIT
        left = []
        for helper in helpers:
            try:
                helper.wait(timeout=max(0.0, deadline - time.monotonic()))
            except psutil.NoSuchProcess:
                pass
            except psutil.Error:   # still running, or can't tell
                left.append(helper)
        for helper in left:
            _ask_to_close(helper.pid)
        deadline = time.monotonic() + CLOSE_GRACE
        return all([self._close(h, name, deadline, asked=True) for h in left])

    def stop_elevated(self, descriptors: list[AppDescriptor]
                      ) -> list[AppDescriptor]:
        """Close tools that run as administrator, which this EDLS has no rights
        to: an elevated copy of EDLS runs stop() on them (_close_elevated), so
        one UAC prompt covers them all. Waits until they're gone or
        ELEVATED_CLOSE_WAIT passes, and returns the ones still running. Raises
        NeedsAdmin(declined=True) if the CMDR said No."""
        targets, pairs = [], []
        for desc in descriptors:
            if _stem(desc.process_name) in PROTECTED_PROCESSES:
                continue
            proc = self._target(desc, forget=False)[0]
            if proc is not None:
                targets.append(desc)
                pairs.append(f"{proc.pid}:{desc.process_name}")
        if not targets:
            return []
        # Frozen, the exe is EDLS itself; from source it's Python + main.py.
        script = [] if getattr(sys, "frozen", False) else [os.path.abspath(__file__)]
        args = subprocess.list2cmdline(script + [CLOSE_ELEVATED_FLAG] + pairs)
        try:
            _run_as_admin(sys.executable, args, show=0)
        except OSError as exc:
            if _winerror(exc) == ERROR_CANCELLED:
                raise NeedsAdmin(declined=True) from None
            log.warning("Couldn't start EDLS as administrator: %s", exc)
            return targets
        deadline = time.monotonic() + ELEVATED_CLOSE_WAIT
        while True:
            found = self.find_many(d.process_name for d in targets)
            left = [d for d in targets if d.process_name in found]
            if not left or time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        with self._lock:
            for desc in targets:
                if desc not in left:
                    self.launched.pop(desc.name, None)
        return left


# =============================================================================
# Updates - ask GitHub which release is latest
# =============================================================================
def _latest_release() -> dict:
    """GitHub's latest published release, as its API describes it. Raises
    OSError (offline, HTTP errors), http.client.HTTPException (a reply cut
    short) or ValueError (not JSON)."""
    req = urllib.request.Request(config.LATEST_RELEASE_API, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"EDLS/{config.APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=UPDATE_TIMEOUT) as resp:
        return json.load(resp)


def check_for_update(current: str = config.APP_VERSION) -> Optional[str]:
    """The latest release's version, if it's newer than `current`. Drafts and
    pre-releases never count (GitHub leaves them out of "latest"). Raises
    what _latest_release() does when GitHub can't be asked."""
    version = str(_latest_release().get("tag_name") or "").strip().lstrip("v")
    return version if config.is_newer(version, current) else None


# =============================================================================
# Engine - the launch -> monitor -> shutdown sequence
# =============================================================================
class Engine:
    """Runs the long sequence on a daemon worker thread and talks outward only
    through Signals. Also exposes single-app start/stop for the status cards.

    Threads: every worker starts through _spawn(). A companion app has one
    start or stop in flight at a time (_try_claim for card clicks, _claim for
    the session and the teardown), teardowns and close countdowns never
    overlap, and a card status is recorded and sent in one step (_status)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.pm = ProcessManager()

        # engine -> view
        self.on_log = Signal()      # (message: str, level: str)
        self.on_status = Signal()   # (key: str, state: AppState, pid: int | None)
        self.on_busy = Signal()     # (busy: bool)  -> toggle the LAUNCH button
        self.on_quit = Signal()     # ()  -> close the app: a full session ended
        self.on_update = Signal()   # (version: str)  -> a newer release is out
        self.on_countdown = Signal()   # (counting: bool) -> LAUNCH offers STAY OPEN

        # The view injects a function that runs a callable on the GUI thread.
        # Default runs inline, which is correct for headless use and tests.
        self.dispatch: Callable[[Callable], None] = lambda fn: fn()

        self._thread: Optional[threading.Thread] = None
        self._shutdown_thread: Optional[threading.Thread] = None
        # Card-driven Steam/Elite start/stop: those wait on processes, so they
        # run here, one at a time, never on the GUI thread.
        self._system_thread: Optional[threading.Thread] = None
        self._watch_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()     # interrupts the current waits
        self._closed = threading.Event()   # window closed: start nothing new
        self._wake = threading.Event()     # asks the watcher to scan right away
        # Ends the close countdown early: STAY OPEN, or the window closing.
        self._countdown_cut = threading.Event()
        self._last_action = 0.0

        # Companion apps with a start or stop in flight. One at a time per
        # app, whoever asks -- a card, the session, the teardown -- so a
        # double-click, or STOP during SHUTDOWN, can't start a second copy,
        # close it twice, or show the UAC prompt twice.
        self._claimed: set[str] = set()
        self._claim_cv = threading.Condition()
        # One teardown at a time (SHUTDOWN and the game exiting can coincide),
        # and one close countdown.
        self._teardown_lock = threading.Lock()
        self._countdown_lock = threading.Lock()
        # SHUTDOWN while LAUNCH is starting the tools: the ones it hasn't
        # started yet stay unstarted. Cleared as LAUNCH begins starting them,
        # so a SHUTDOWN pressed earlier (while waiting for the game) only
        # closes what was running then.
        self._tools_cancelled = threading.Event()
        # Tools being closed right now. Closing the window waits for these
        # (wait_for_closes) so one that ignored the ask still gets closed.
        self._closing = 0
        self._closing_cv = threading.Condition()

        # What each card currently claims, so the watcher only speaks up when
        # reality differs: True = running, False = not running, None = pending
        # (Launching/Waiting -- only the process appearing may change it). Kept
        # by _status(), the single place statuses go out. Re-entrant: an inline
        # dispatch (headless) runs the handlers while _status holds it.
        self._believed: dict[str, Optional[bool]] = {}
        self._set_at: dict[str, float] = {}
        self._watch_lock = threading.RLock()

    # ---- signal helpers (everything outbound goes through _emit) ----
    def _emit(self, fn: Callable[[], None]) -> None:
        """Hand `fn` to the view. Once the window has closed there's no view
        to tell -- and at exit Qt may already be gone -- so it's dropped."""
        if not self._closed.is_set():
            self.dispatch(fn)

    def _log(self, message: str, level: str = "info") -> None:
        log_level = level if level in ("info", "warning", "error") else "info"
        getattr(log, log_level)(message)
        self._emit(lambda: self.on_log.emit(message, level))

    def _status(self, key: str, state: AppState, pid: Optional[int] = None) -> None:
        # Recorded and sent under one lock, so the view receives statuses in
        # the order they were recorded: what a card shows is always what the
        # watcher believes it shows. (Sending only queues it for the GUI
        # thread, so holding the lock costs nothing.)
        with self._watch_lock:
            self._believed[key] = (None if state in _PENDING_STATES
                                   else state.is_running)
            self._set_at[key] = time.monotonic()
            self._emit(lambda: self.on_status.emit(key, state, pid))

    def _busy(self, busy: bool) -> None:
        self._emit(lambda: self.on_busy.emit(busy))

    def _quit(self) -> None:
        self._emit(lambda: self.on_quit.emit())

    def _countdown(self, counting: bool) -> None:
        self._emit(lambda: self.on_countdown.emit(counting))

    # ---- workers ----
    def _spawn(self, name: str, target: Callable, *args
               ) -> Optional[threading.Thread]:
        """Start a worker -- the only way EDLS starts one. Nothing starts once
        the window has closed (returns None). A crash is logged to
        launcher.log and the activity log, never swallowed."""
        if self._closed.is_set():
            return None

        def body() -> None:
            try:
                target(*args)
            except Exception as exc:
                log.exception("Worker %s crashed", name)
                self._log(f"Unexpected error: {exc}", "error")

        thread = threading.Thread(target=body, name=f"edls-{name}", daemon=True)
        thread.start()
        return thread

    def _try_claim(self, name: str) -> bool:
        """Claim `name` for a card's START or STOP, unless something else is
        already starting or stopping it. GUI thread: never waits."""
        with self._claim_cv:
            if name in self._claimed:
                return False
            self._claimed.add(name)
            return True

    def _release(self, name: str) -> None:
        with self._claim_cv:
            self._claimed.discard(name)
            self._claim_cv.notify_all()

    @contextmanager
    def _claim(self, names):
        """Claim every app in `names`, waiting out a START or STOP already in
        flight on any of them (the session and the teardown, never a card).
        Sorted, so two multi-claims can't deadlock -- though teardowns, the
        only ones, never overlap anyway."""
        held: list[str] = []
        try:
            for name in sorted(set(names)):
                with self._claim_cv:
                    self._claim_cv.wait_for(lambda n=name: n not in self._claimed)
                    self._claimed.add(name)
                held.append(name)
            yield
        finally:
            for name in held:
                self._release(name)

    @contextmanager
    def _closing_work(self):
        """Marks tools being closed, so closing the window waits for them."""
        with self._closing_cv:
            self._closing += 1
        try:
            yield
        finally:
            with self._closing_cv:
                self._closing -= 1
                self._closing_cv.notify_all()

    def wait_for_closes(self, timeout: float) -> bool:
        """After the window has closed: wait until no tool is still being
        closed, or `timeout` passes. True if none is."""
        with self._closing_cv:
            return self._closing_cv.wait_for(lambda: self._closing == 0,
                                             timeout)

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
        self._watch_thread = self._spawn("watcher", self._watch)

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
            label = "Elite: Dangerous" if app.name == KEY_ELITE else app.name
            line = None
            # Check, decide and set in one go: an action can't slip a status
            # in between what the watcher read and what it sends.
            with self._watch_lock:
                # >=, not >: monotonic() only ticks every ~16 ms on Windows.
                if self._set_at.get(app.name, 0.0) >= scan_started:
                    continue   # an action changed this card after we scanned
                believed = self._believed.get(app.name, _UNSEEN)
                # The session reports the game's comings and goings itself.
                speak = (not quiet and believed is not _UNSEEN
                         and not (session and app.name == KEY_ELITE))
                if proc and believed is not True:
                    self._status(app.name, self._live_state(app.name), proc.pid)
                    if speak and believed is False:
                        line = f"{label} is now running."
                elif not proc and (believed is True or believed is _UNSEEN):
                    self._status(app.name, AppState.OFFLINE)
                    if speak:
                        line = f"{label} stopped running."
            if line:
                self._log(line, "dim")

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
        self._thread = self._spawn("session", self._run)

    def manual_shutdown(self) -> None:
        """SHUTDOWN button. Honors the user's shutdown_scope setting. While
        LAUNCH is starting the tools, it also cancels the ones not started
        yet."""
        if self._closed.is_set():
            return
        if ((self._shutdown_thread and self._shutdown_thread.is_alive())
                or self._teardown_lock.locked()):
            self._log("Already closing the tools.", "dim")
            return
        self._tools_cancelled.set()
        self._kick()
        self._shutdown_thread = self._spawn("shutdown", self._manual_shutdown)

    def _manual_shutdown(self) -> None:
        self._teardown(self.settings.shutdown_scope)
        game_running = self.pm.find(ELITE_PROCESS) is not None
        if game_running:
            self._log("Quit Elite: Dangerous from the in-game menu - EDLS never "
                      "closes the game.", "warning")
        if self.settings.close_on_shutdown and self._close_countdown():
            return
        if self.session_active and game_running:
            self._log("Waiting for Elite: Dangerous to close...", "dim")

    def start_app(self, name: str) -> None:
        # Steam and the game are URI-launched and have to be waited on, so they
        # take the system thread. A companion app gets a short-lived worker of
        # its own: its Popen is quick, but one that needs admin rights waits
        # on the UAC prompt, which mustn't freeze the window.
        if name == KEY_STEAM:
            self._run_system(self._card_start_steam)
            return
        if name == KEY_ELITE:
            self._run_system(self._card_start_elite)
            return

        desc = self._find_descriptor(name)
        if not desc or not self._claim_for_card(name):
            return
        self._kick()
        if not self._spawn(f"start-{name}", self._start_one, desc):
            self._release(name)

    def _claim_for_card(self, name: str) -> bool:
        """A card's START/STOP: claim the app, or say why the click does
        nothing (a double-click, or LAUNCH/SHUTDOWN is on it)."""
        if self._closed.is_set():
            return False
        if self._try_claim(name):
            return True
        self._log(f"{name} is busy - wait for it to finish.", "dim")
        return False

    def _start_one(self, desc: AppDescriptor) -> None:
        """A card's START. The caller has claimed the app; this releases it."""
        name = desc.name
        try:
            self._start_claimed(desc)
        except Exception:
            # Don't leave the card on Launching...: the watcher never touches
            # a pending card until the process shows up. _spawn reports it.
            self._status(name, AppState.FAILED)
            raise
        finally:
            self._release(name)

    def _start_claimed(self, desc: AppDescriptor) -> None:
        name = desc.name
        proc = self.pm.adopt(desc)
        if proc:
            self._status(name, AppState.ONLINE, proc.pid)
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
        try:
            proc = self.pm.launch(desc, resolved_path=path,
                                  elevate=lambda: self._may_elevate(name),
                                  minimized=self.settings.start_minimized)
        except NeedsAdmin as exc:
            self._report_needs_admin(name, exc)
            return
        if proc:
            self._status(name, AppState.ONLINE, proc.pid)
            self._log(f"{name} online. (PID {proc.pid})", "success")
        else:
            self._status(name, AppState.FAILED)
            self._log(f"Couldn't start {name} (see launcher.log).", "error")

    def _may_elevate(self, name: str) -> bool:
        """launch() found that `name` needs admin rights: may Windows ask?"""
        if not self.settings.allow_elevation:
            return False
        self._log(f"{name} needs administrator rights - Windows will ask.")
        return True

    def _report_needs_admin(self, name: str, exc: NeedsAdmin) -> None:
        self._status(name, AppState.FAILED)
        if exc.declined:
            self._log(f"{name} not started - the administrator prompt was "
                      "declined.", "warning")
        else:
            self._log(f"{name} needs administrator rights - not started. Turn "
                      "on 'Ask for admin rights' in Settings, or start it "
                      "yourself.", "warning")

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
        if not desc or not self._claim_for_card(name):
            return
        self._kick()
        # Threaded: stop() waits for the process to really exit, which mustn't
        # freeze the window.
        if not self._spawn(f"stop-{name}", self._stop_one, desc):
            self._release(name)

    def _stop_one(self, desc: AppDescriptor) -> None:
        """A card's STOP. The caller has claimed the app; this releases it."""
        try:
            with self._closing_work():
                self._stop_claimed(desc)
        finally:
            self._release(desc.name)

    def _stop_claimed(self, desc: AppDescriptor) -> None:
        self._log(f"Stopping {desc.name}...")
        result = self.pm.stop(desc)
        if (result is StopResult.FAILED and self._may_close_as_admin()
                and not self._close_as_admin([desc])):
            result = StopResult.STOPPED
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
        hint = ("" if self.settings.allow_elevation else
                " Turn on 'Ask for admin rights' in Settings to let EDLS close it.")
        self._log(f"Couldn't close {desc.name} - it may be running as "
                  f"administrator.{hint}", "error")

    def _may_close_as_admin(self) -> bool:
        """May a tool EDLS couldn't close go to the UAC prompt? Not once the
        window has closed: a prompt with no EDLS on screen would be a mystery."""
        return self.settings.allow_elevation and not self._closed.is_set()

    def _close_as_admin(self, descs: list[AppDescriptor]) -> list[AppDescriptor]:
        """Close tools EDLS has no rights to (see ProcessManager.stop_elevated),
        one UAC prompt for all of them. Returns the ones still running."""
        names = ", ".join(d.name for d in descs)
        self._log(f"Windows will ask for admin rights to close {names}.")
        try:
            return self.pm.stop_elevated(descs)
        except NeedsAdmin:
            self._log("The administrator prompt was declined.", "warning")
            return descs

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
        self._system_thread = self._spawn(fn.__name__.lstrip("_"), fn)

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
        leaving it up, so a refusal is reported, not escalated. Never asked
        while the game runs, either: what Steam does to a running game on its
        way out is Steam's call, and EDLS doesn't take that risk."""
        if not self.pm.find(STEAM_PROCESS):
            self._status(KEY_STEAM, AppState.OFFLINE)
            return
        if self.pm.find(ELITE_PROCESS):
            self._log("EDLS won't ask Steam to exit while Elite: Dangerous is "
                      "running - quit the game first.", "warning")
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

    # ---- updates ----
    def check_for_updates(self) -> None:
        """Look for a newer release once, in the background (if the CMDR allows
        it). Quiet unless one is found: a failed check only goes to the log
        file -- being offline is no reason to nag."""
        if not self.settings.check_for_updates:
            return
        self._spawn("update-check", self._check_for_updates)

    def _check_for_updates(self) -> None:
        try:
            version = check_for_update()
        except (OSError, ValueError, http.client.HTTPException) as exc:
            log.info("Update check failed: %s", exc)
            return
        if version is None:
            log.info("Update check: EDLS %s is the latest.", config.APP_VERSION)
            return
        log.info("Update check: EDLS %s is available.", version)
        self._emit(lambda: self.on_update.emit(version))

    def stay_open(self) -> None:
        """STAY OPEN, clicked during the close countdown: EDLS doesn't close
        after all."""
        self._countdown_cut.set()

    def close(self) -> None:
        """Window is closing: stop the workers, but leave the game and tools
        running -- closing the launcher should not close everything. Also
        blocks anything queued (e.g. a late auto-start) from starting, and
        stops anything more reaching the view. A close already under way
        (SHUTDOWN, a card's STOP, the end of a session) still finishes:
        main() waits for it (wait_for_closes), with no new UAC prompt."""
        self._closed.set()
        self._stop.set()
        self._wake.set()
        self._countdown_cut.set()

    # ---- the sequence ----
    def _run(self) -> None:
        log.info("=== Session started ===")
        close_after = False
        try:
            # The window closing stops the sequence between steps: EDLS may
            # outlive its window briefly (main's wait_for_closes), and must
            # not open Steam or the game in that time.
            paths = self._preflight()
            if paths is None or self._stop.is_set():
                return
            if not self._start_steam() or self._stop.is_set():
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
            self._launch_tools(paths)
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
            self._busy(False)
            if close_after:
                self._close_countdown()

    def _close_countdown(self) -> bool:
        """Close EDLS after a short, logged countdown, while LAUNCH offers
        STAY OPEN (stay_open()). True if EDLS is going -- the countdown ran
        out, or the window closed -- and False if the CMDR chose to stay. The
        game and Steam are never touched. Only one runs at a time (SHUTDOWN's
        and the session's can coincide): a second returns False at once and
        leaves it to the first."""
        if not self._countdown_lock.acquire(blocking=False):
            return False
        try:
            return self._count_down()
        finally:
            self._countdown_lock.release()

    def _count_down(self) -> bool:
        self._countdown_cut.clear()
        if self._closed.is_set():
            return True
        self._countdown(True)
        self._log(f"EDLS will close in {CLOSE_DELAY} seconds.")
        if not self._countdown_cut.wait(CLOSE_DELAY - 1):
            self._log("Farewell, CMDR. o7", "success")
            if not self._countdown_cut.wait(1):
                self._quit()
                return True
        if self._closed.is_set():
            return True
        self._countdown(False)
        self._log("EDLS will stay open.")
        return False

    def _preflight(self) -> Optional[dict[str, str]]:
        """Check Steam is installed (None if not) and find each enabled tool:
        its path by name, for _launch_tools -- resolved once, since a resolver
        like SrvSurvey's walks a directory tree."""
        self._log("Preflight checks...", "dim")
        if winreg is not None:
            try:
                winreg.OpenKey(winreg.HKEY_CURRENT_USER, STEAM_REG_KEY).Close()
            except OSError:
                self._status(KEY_STEAM, AppState.NOT_FOUND)
                self._log("Steam does not appear to be installed.", "error")
                return None
        paths: dict[str, str] = {}
        for app in self.enabled_apps:
            path = app.resolve_path()
            if path and os.path.exists(path):
                paths[app.name] = path
            else:
                self._log(f"{app.name} not found - will be skipped.", "warning")
                self._status(app.name, AppState.NOT_FOUND)
        return paths

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
        seconds (None = forever). Returns None on timeout or close(). A game
        that's already running (LAUNCH re-attaching after EDLS was closed
        mid-session) is adopted as-is: Steam never gets a second launch
        request."""
        proc = self.pm.find(ELITE_PROCESS)
        if proc:
            self._log("Elite: Dangerous is already running - attaching.", "dim")
        else:
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

    def _launch_tools(self, paths: dict[str, str]) -> list[AppDescriptor]:
        """Start each enabled tool preflight found (`paths`, by name). Each
        waits out a START or STOP its card has in flight. Stops early if the
        window closes, or SHUTDOWN is pressed."""
        launched: list[AppDescriptor] = []
        self._tools_cancelled.clear()
        for app in self.enabled_apps:
            path = paths.get(app.name)
            if path is None:
                continue  # already reported NOT_FOUND in preflight
            with self._claim([app.name]):
                if self._stop.is_set():
                    break             # the window closed: start nothing new
                if self._tools_cancelled.is_set():
                    self._log("SHUTDOWN pressed - no more tools will be "
                              "started this session.")
                    break
                if self._launch_tool(app, path):
                    launched.append(app)
        return launched

    def _launch_tool(self, app: AppDescriptor, path: str) -> bool:
        """One tool for _launch_tools. True if it's running now."""
        proc = self.pm.adopt(app)
        if proc:
            self._log(f"{app.name} already running - skipping.", "dim")
            self._status(app.name, AppState.ONLINE, proc.pid)
            return True
        self._log(f"Launching {app.name}...")
        self._status(app.name, AppState.LAUNCHING)
        try:
            proc = self.pm.launch(
                app, resolved_path=path,
                elevate=lambda: self._may_elevate(app.name),
                minimized=self.settings.start_minimized)
        except NeedsAdmin as exc:
            self._report_needs_admin(app.name, exc)
            return False
        if proc:
            self._status(app.name, AppState.ONLINE, proc.pid)
            self._log(f"{app.name} online. (PID {proc.pid})", "success")
            return True
        self._log(f"Failed to launch {app.name}.", "warning")
        self._status(app.name, AppState.FAILED)
        return False

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
        """Close the companion apps the scope covers (SHUTDOWN and game exit).
        One teardown at a time: a second waits for the first, then finds
        little or nothing left. It claims every app before it looks, so a
        START or STOP a card (or LAUNCH) has in flight finishes first and the
        teardown sees what that left running."""
        settings = self.settings
        with (self._teardown_lock, self._closing_work(),
              self._claim(a.name for a in settings.apps)):
            if scope is ShutdownScope.LAUNCHED_ONLY:
                launched_names = self.pm.launched_names()
                targets = [a for a in settings.apps if a.name in launched_names]
            else:  # ALL_ENABLED
                targets = [a for a in settings.apps if a.enabled]
            self._close_tools(targets)

    def _close_tools(self, targets: list[AppDescriptor]) -> None:
        found = self.pm.find_many(a.process_name for a in targets)
        running = [a for a in targets if a.process_name in found]
        if not running:
            self._log("No tools running.", "dim")
            return
        self._log("Closing third-party tools...")
        # Ask them all first, so their grace periods run side by side: the
        # teardown waits CLOSE_GRACE at most, however many ignore the ask.
        for app in running:
            self.pm.ask_to_close(app)
        deadline = time.monotonic() + CLOSE_GRACE
        failed: list[AppDescriptor] = []
        for app in running:
            self._log(f"Stopping {app.name}...")
            result = self.pm.stop(app, deadline=deadline, asked=True)
            if result in (StopResult.STOPPED, StopResult.NOT_RUNNING):
                self._status(app.name, AppState.OFFLINE)
            elif result is StopResult.FAILED:
                failed.append(app)
            else:
                self._report_unstoppable(app, result)
        # What wouldn't close probably runs as administrator: one UAC prompt
        # covers the lot, after everything else is already closed.
        if failed and self._may_close_as_admin():
            left = self._close_as_admin(failed)
            for app in failed:
                if app not in left:
                    self._status(app.name, AppState.OFFLINE)
            failed = left
        for app in failed:
            self._report_unstoppable(app, StopResult.FAILED)
        if failed:
            names = ", ".join(a.name for a in failed)
            self._log(f"Some tools could not be closed: {names}.", "warning")
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
def _close_elevated(pairs: list[str]) -> int:
    """The --close-elevated mode: EDLS started as administrator by
    ProcessManager.stop_elevated(), to close tools that run as administrator.
    Each `pairs` entry is "pid:process_name". It's the usual polite-then-kill
    stop() -- protected processes and the PID-reuse check included -- just
    with the rights to do it. No window and no log file (the parent rescans
    to see what closed). Exit code 0 if everything closed."""
    pm = ProcessManager()
    targets: list[AppDescriptor] = []
    for pair in pairs:
        pid, _, process_name = pair.partition(":")
        if pid.isdigit() and process_name:
            desc = AppDescriptor(process_name, process_name)
            pm.launched[desc.name] = int(pid)
            targets.append(desc)
    for desc in targets:
        pm.ask_to_close(desc)
    deadline = time.monotonic() + CLOSE_GRACE
    results = [pm.stop(d, deadline=deadline, asked=True) for d in targets]
    closed = (StopResult.STOPPED, StopResult.NOT_RUNNING)
    return 0 if all(r in closed for r in results) else 1


def main() -> int:
    # Before logging, Qt and the one-copy check: this copy only closes tools.
    if sys.argv[1:2] == [CLOSE_ELEVATED_FLAG]:
        return _close_elevated(sys.argv[2:])
    setup_logging()
    install_crash_logging()
    import ui  # late import: driving the engine headless needs no GUI toolkit
    # Before anything touches settings.json: a second copy just brings the
    # running one to the front and leaves.
    if not ui.acquire_single_instance():
        log.info("EDLS is already running - showed that window instead.")
        return 0
    settings = config.load_settings()
    engine = Engine(settings)
    code = ui.run(engine, settings)
    # The window is gone, but a tool may still be being closed (SHUTDOWN, a
    # card's STOP, the end of a session): every worker is a daemon, so
    # returning now would end that mid-way, and a tool that ignored the ask
    # would never be closed. Finish it first -- briefly, and out of sight.
    if not engine.wait_for_closes(EXIT_WAIT):
        log.warning("Exiting with tools still being closed.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
