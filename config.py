"""
EDLS - Elite: Dangerous Launch Suite (Python rewrite)
config.py - the pure-data layer.

Everything here is toolkit-blind and engine-blind: it imports only the standard
library and is imported by both main.py and ui.py without creating a cycle. It
owns five things:

  1. AppState / ShutdownScope   -- the shared vocabulary
  2. AppDescriptor              -- one app's definition, with path resolution
  3. REGISTRY                   -- THE list of default apps (single source of truth)
  4. Settings + load/save       -- persistence, with merge-on-load and migration
  5. parse_version / is_newer   -- version order for the update check

Adding a companion app is a one-line entry in REGISTRY below. Both config dialogs
and the launch engine read from it, so there's no longer a four-places-to-edit
problem.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Callable, Optional

log = logging.getLogger("edls.config")

# Problems hit by the last load_settings() -- bad values that fell back to a
# default, an unreadable file, a failed write. They also go to launcher.log; the
# view repeats them in the activity log at startup so the CMDR actually sees them.
LOAD_WARNINGS: list[str] = []


def _warn(message: str) -> None:
    LOAD_WARNINGS.append(message)
    log.warning(message)

# Bump when the on-disk schema changes; load_settings() can migrate on read.
SETTINGS_VERSION = 1

# Display version, shown in the UI and used for the build's file metadata.
# Single source of truth (replaces the original $script:AppVersion).
APP_VERSION = "1.3.0"

# Where updates come from: the repo's GitHub Releases. A release's tag must be
# its version (e.g. "1.3.0", matching APP_VERSION when it was built). EDLS only
# looks: the update popover sends the CMDR to the release page.
LATEST_RELEASE_URL = "https://github.com/coyoteware/EDLS/releases/latest"
LATEST_RELEASE_API = ("https://api.github.com/repos/coyoteware/EDLS/"
                      "releases/latest")

# All persistent state lives under %LOCALAPPDATA%\EDLS (with a ~ fallback so the
# module is importable/testable on non-Windows dev machines).
_LOCALAPPDATA = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
DATA_DIR = os.path.join(_LOCALAPPDATA, "EDLS")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
LOG_FILE = os.path.join(DATA_DIR, "launcher.log")

# Status-card keys + process names for the two rows that aren't companion apps
# (Steam, Elite itself). Shared here so main.py (routing) and ui.py (the
# placeholder cards) can't drift apart by hand-duplicating these strings.
KEY_STEAM = "Steam"
KEY_ELITE = "Elite"
STEAM_PROCESS = "steam.exe"
ELITE_PROCESS = "EliteDangerous64.exe"


# =============================================================================
# Shared vocabulary
# =============================================================================
class AppState(Enum):
    """Semantic status of a tracked process. The value is the display label;
    colour is intentionally NOT here -- ui.py maps states to colours so the
    data layer carries no styling."""
    OFFLINE = "Offline"
    NOT_FOUND = "Not found"
    FAILED = "Failed"
    STOPPED = "Stopped"
    WAITING = "Waiting..."
    LAUNCHING = "Launching..."
    ONLINE = "Online"
    RUNNING = "Running"
    ERROR = "Error"       # EDLS tried to close it and couldn't -- still running

    @property
    def label(self) -> str:
        return self.value

    @property
    def is_running(self) -> bool:
        return self in (AppState.LAUNCHING, AppState.ONLINE, AppState.RUNNING,
                        AppState.ERROR)


class ShutdownScope(Enum):
    """What SHUTDOWN (and the on-exit teardown) is allowed to close.
    This is a user setting -- see Settings.shutdown_scope."""
    LAUNCHED_ONLY = "launched_only"   # only what EDLS started or took over
    ALL_ENABLED = "all_enabled"       # any enabled companion app that's running


class Theme(Enum):
    """UI colour scheme (a user setting -- see Settings.theme). DARK is the
    original near-black HUD; LIGHT is a cool light-grey "day mode" (dark text,
    orange accents) for cheap / low-contrast monitors where the dark theme reads
    as a black void. The actual colours live in ui.py; this just records the
    choice."""
    DARK = "dark"
    LIGHT = "light"


# =============================================================================
# App descriptor + path resolution
# =============================================================================
@dataclass
class AppDescriptor:
    """One companion app. `path` is a template that may contain %ENV% vars; if
    it's empty, `resolver` (a function) is used instead -- that's how the
    awkward installs (e.g. SrvSurvey's ClickOnce dir) are handled without
    special-casing them in the engine."""
    name: str
    process_name: str
    path: str = ""
    enabled: bool = True
    resolver: Optional[Callable[[], Optional[str]]] = field(
        default=None, compare=False, repr=False
    )
    # The REGISTRY default this app came from ("" for one the CMDR added). It's
    # saved, so a renamed default keeps its resolver after a restart.
    origin: str = ""

    def resolve_path(self) -> Optional[str]:
        if self.path:
            return _expand_env(self.path)
        if self.resolver:
            return self.resolver()
        return None


# %VAR% expansion done by hand so it behaves identically on every platform
# (os.path.expandvars only handles %VAR% on Windows), which also makes it
# testable off-Windows. Unknown vars are left untouched.
_ENV_PATTERN = re.compile(r"%([^%]+)%")


def _expand_env(template: str) -> str:
    return _ENV_PATTERN.sub(
        lambda m: os.environ.get(m.group(1), m.group(0)), template
    )


def _find_srvsurvey() -> Optional[str]:
    """SrvSurvey installs via ClickOnce under %LOCALAPPDATA%\\Apps\\2.0 in a
    hashed subfolder, so its path can't be hardcoded -- search for the exe."""
    base = os.path.join(_LOCALAPPDATA, "Apps", "2.0")
    if not os.path.isdir(base):
        return None
    for root, _dirs, files in os.walk(base):
        for name in files:
            if name.lower() == "srvsurvey.exe":
                return os.path.join(root, name)
    return None


_ODYSSEY_LAUNCHER = (r"%LOCALAPPDATA%\Elite Dangerous Odyssey Materials Helper Launcher"
                     r"\Elite Dangerous Odyssey Materials Helper Launcher.exe")
_ODYSSEY_HELPER = (r"%LOCALAPPDATA%\Elite Dangerous Odyssey Materials Helper Launcher"
                   r"\program\Elite Dangerous Odyssey Materials Helper.exe")


def _find_odyssey_materials() -> Optional[str]:
    """Odyssey Materials Helper's launcher checks for an update, starts the
    helper and exits -- start that if it's there, else the helper itself."""
    launcher = _expand_env(_ODYSSEY_LAUNCHER)
    return launcher if os.path.exists(launcher) else _expand_env(_ODYSSEY_HELPER)


def _first_found(templates: tuple[str, ...]) -> Optional[str]:
    """The first of these %ENV% path templates that's on this PC, if any."""
    for template in templates:
        path = _expand_env(template)
        if os.path.exists(path):
            return path
    return None


# Where EDDI's installer puts it: for this account only (its default), or for
# all users.
_EDDI_PATHS = (r"%LOCALAPPDATA%\EDDI\Application\EDDI.exe",
               r"%ProgramFiles%\EDDI\EDDI.exe")


def _find_eddi() -> Optional[str]:
    """EDDI installs for this account only (the installer's default) or for
    all users -- whichever is on this PC, this account's first."""
    return _first_found(_EDDI_PATHS)


# Where VoiceAttack 2, then VoiceAttack 1, install by default: each version's
# own installer (v1 as 64-bit, then 32-bit), then its copy in Steam's default
# library. Both versions run as VoiceAttack.exe, so one card serves either.
_VOICEATTACK_PATHS = (
    r"%ProgramFiles%\VoiceAttack2\VoiceAttack.exe",
    r"%ProgramFiles(x86)%\Steam\steamapps\common\VoiceAttack2\VoiceAttack.exe",
    r"%ProgramFiles%\VoiceAttack\VoiceAttack.exe",
    r"%ProgramFiles(x86)%\VoiceAttack\VoiceAttack.exe",
    r"%ProgramFiles(x86)%\Steam\steamapps\common\VoiceAttack\VoiceAttack.exe",
)


def _find_voiceattack() -> Optional[str]:
    """VoiceAttack 2 if it's on this PC, else VoiceAttack 1."""
    return _first_found(_VOICEATTACK_PATHS)


# =============================================================================
# THE registry -- add or remove default apps here, nowhere else
# =============================================================================
REGISTRY: list[AppDescriptor] = [
    AppDescriptor("EDMarketConnector", "EDMarketConnector",
                  r"%ProgramFiles(x86)%\EDMarketConnector\EDMarketConnector.exe"),
    AppDescriptor("SrvSurvey", "SrvSurvey",
                  resolver=_find_srvsurvey),
    AppDescriptor("OdysseyMaterials", "Elite Dangerous Odyssey Materials Helper",
                  resolver=_find_odyssey_materials),
    AppDescriptor("EDCoPilot", "EDCoPilot",
                  r"C:\EDCoPilot\EDCoPilot.exe"),
    AppDescriptor("EDHM_UI", "EDHM-UI-V3",
                  r"%LOCALAPPDATA%\EDHM-UI-V3\EDHM-UI-V3.exe"),
    AppDescriptor("opentrack", "opentrack",
                  r"%ProgramFiles(x86)%\opentrack\opentrack.exe"),
    AppDescriptor("ICARUS Terminal", "ICARUS Terminal",
                  r"%ProgramFiles(x86)%\ICARUS Terminal\ICARUS Terminal.exe"),
    AppDescriptor("EDDiscovery", "EDDiscovery",
                  r"%ProgramFiles%\EDDiscovery\EDDiscovery.exe"),
    AppDescriptor("VoiceAttack", "VoiceAttack",
                  resolver=_find_voiceattack),
    AppDescriptor("EDCoPTER", "EDCoPTER",
                  r"%LOCALAPPDATA%\Programs\EDCoPTER\EDCoPTER.exe"),
    AppDescriptor("Elite Observatory", "ObservatoryCore",
                  r"%LOCALAPPDATA%\Programs\Elite Observatory\ObservatoryCore.exe"),
    AppDescriptor("Elite Studio", "EliteStudio",
                  r"%ProgramFiles%\Elite Studio\EliteStudio.exe"),
    AppDescriptor("EDDI", "EDDI",
                  resolver=_find_eddi),
]

REGISTRY_BY_NAME = {a.name: a for a in REGISTRY}

# Default paths a later version replaced. A saved path still equal to its old
# default is dropped on load so the app moves to the new one; a path the CMDR
# set themselves is kept.
_RETIRED_PATHS = {
    "OdysseyMaterials": _ODYSSEY_HELPER,
    "VoiceAttack": r"%ProgramFiles%\VoiceAttack\VoiceAttack.exe",   # v1, 64-bit only
}

# The two non-companion rows: Steam and the game itself. They aren't in REGISTRY
# (they're not user-configurable, and they launch via steam:// URIs rather than a
# path), but both the status cards and the engine's per-card start/stop need a
# descriptor for them -- so it lives here once instead of being constructed by
# hand in ui.py. Read-only: these are shared instances, don't mutate them.
SYSTEM_APPS: dict[str, AppDescriptor] = {
    KEY_STEAM: AppDescriptor(KEY_STEAM, STEAM_PROCESS),
    KEY_ELITE: AppDescriptor(KEY_ELITE, ELITE_PROCESS),
}


# =============================================================================
# Settings
# =============================================================================
@dataclass
class Settings:
    cmdr_name: str = "Epstein Didn't Kill Himself"
    elite_app_id: int = 359320
    setup_complete: bool = False
    auto_start: bool = False
    show_inactive_cards: bool = True
    shutdown_scope: ShutdownScope = ShutdownScope.LAUNCHED_ONLY
    # Whether EDLS closes itself (after a short countdown) once its work is done.
    # Tools are torn down either way; these only decide if the window goes too.
    close_on_game_exit: bool = True
    close_on_shutdown: bool = False
    # A tool that needs admin rights (EDHM-UI asks for them) gets the UAC prompt
    # to start, and again if EDLS has to close it. Off: EDLS never prompts.
    allow_elevation: bool = True
    theme: Theme = Theme.DARK
    apps: list[AppDescriptor] = field(default_factory=list)
    removed_defaults: list[str] = field(default_factory=list)
    check_for_updates: bool = True
    # Ask Windows to start each tool minimized (a hint: some tools ignore it).
    start_minimized: bool = False


# =============================================================================
# Updates
# =============================================================================
def parse_version(text: str) -> Optional[tuple[int, int, int]]:
    """'1.2.1', 'v1.3' or '1.2.1-beta' -> (major, minor, patch), missing parts
    as 0 (so a "1.2" tag reads older than 1.2.1). None if it isn't a version
    at all."""
    m = re.match(r"\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", text or "")
    if not m:
        return None
    return tuple(int(g or 0) for g in m.groups())


def is_newer(candidate: str, current: str = APP_VERSION) -> bool:
    new, cur = parse_version(candidate), parse_version(current)
    return new is not None and cur is not None and new > cur


def _fresh_default(reg: AppDescriptor) -> AppDescriptor:
    """A REGISTRY default as it joins the CMDR's list (first run, RESTORE
    DEFAULTS, or a default added in a later version): ticked only if its
    program is on this PC, so a tool they don't have doesn't greet every
    LAUNCH with "not found". A copy, so user edits never mutate REGISTRY
    (resolvers are shared callables, which is fine)."""
    path = reg.resolve_path()
    return replace(reg, origin=reg.name,
                   enabled=bool(path and os.path.exists(path)))


def default_settings() -> Settings:
    return Settings(apps=[_fresh_default(a) for a in REGISTRY])


# Hand-edited settings.json can hold anything. Each reader below returns the
# default for a missing key silently, and for a wrong-typed one with a warning,
# so a typo costs one setting instead of crashing EDLS at startup.
_TRUE_WORDS = {"true", "1", "yes", "on"}
_FALSE_WORDS = {"false", "0", "no", "off"}


def _bad(key: str, default) -> None:
    _warn(f"settings.json: '{key}' has an invalid value - using {default!r}.")


def _as_bool(value, default: bool, key: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    _bad(key, default)
    return default


def _as_int(value, default: int, key: str) -> int:
    if value is None:
        return default
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    _bad(key, default)
    return default


def _as_str(value, default: str, key: str) -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    _bad(key, default)
    return default


def _as_list(value, key: str) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    _bad(key, [])
    return []


def _as_enum(enum_cls, value, default, key: str):
    if value is None:
        return default
    try:
        return enum_cls(value)
    except ValueError:
        _bad(key, default.value)
        return default


def _app_from_dict(d) -> Optional[AppDescriptor]:
    """One `apps` entry, or None (with a warning) if it isn't usable."""
    name = d.get("name") if isinstance(d, dict) else None
    if not isinstance(name, str) or not name.strip():
        _warn(f"settings.json: skipped an app entry with no valid name: {d!r}")
        return None
    name = name.strip()
    # The default it came from: its saved origin, so a renamed default keeps
    # its resolver -- else its name (files from before 1.3.0 have no origin).
    origin = _as_str(d.get("origin"), "", f"{name}.origin").strip()
    reg = REGISTRY_BY_NAME.get(origin) or REGISTRY_BY_NAME.get(name)
    path = _as_str(d.get("path"), "", f"{name}.path")
    if reg and path == _RETIRED_PATHS.get(reg.name):
        path = reg.path
    return AppDescriptor(
        name=name,
        process_name=_as_str(d.get("process"), name, f"{name}.process") or name,
        path=path,
        enabled=_as_bool(d.get("enabled"), True, f"{name}.enabled"),
        # Re-attach the resolver for known defaults; custom apps have none.
        resolver=reg.resolver if reg else None,
        origin=reg.name if reg else "",
    )


def _from_dict(raw: dict) -> Settings:
    d = Settings()   # field defaults
    cmdr = _as_str(raw.get("cmdr_name"), d.cmdr_name, "cmdr_name").strip()
    apps = [_app_from_dict(e) for e in _as_list(raw.get("apps"), "apps")]
    return Settings(
        cmdr_name=cmdr or d.cmdr_name,
        elite_app_id=_as_int(raw.get("elite_app_id"), d.elite_app_id,
                             "elite_app_id"),
        setup_complete=_as_bool(raw.get("setup_complete"), False,
                                "setup_complete"),
        auto_start=_as_bool(raw.get("auto_start"), False, "auto_start"),
        show_inactive_cards=_as_bool(raw.get("show_inactive_cards"),
                                     d.show_inactive_cards,
                                     "show_inactive_cards"),
        shutdown_scope=_as_enum(ShutdownScope, raw.get("shutdown_scope"),
                                d.shutdown_scope, "shutdown_scope"),
        close_on_game_exit=_as_bool(raw.get("close_on_game_exit"),
                                    d.close_on_game_exit, "close_on_game_exit"),
        close_on_shutdown=_as_bool(raw.get("close_on_shutdown"),
                                   d.close_on_shutdown, "close_on_shutdown"),
        allow_elevation=_as_bool(raw.get("allow_elevation"),
                                 d.allow_elevation, "allow_elevation"),
        theme=_as_enum(Theme, raw.get("theme"), d.theme, "theme"),
        apps=[a for a in apps if a is not None],
        removed_defaults=[n for n in _as_list(raw.get("removed_defaults"),
                                              "removed_defaults")
                          if isinstance(n, str)],
        check_for_updates=_as_bool(raw.get("check_for_updates"),
                                   d.check_for_updates, "check_for_updates"),
        start_minimized=_as_bool(raw.get("start_minimized"),
                                 d.start_minimized, "start_minimized"),
    )


def _merge_new_defaults(s: Settings) -> bool:
    """Append any registry app missing from settings -- unless the user
    deliberately removed it (tracked in removed_defaults). Lets a new default
    added in a later version appear for existing installs, ticked only if it's
    installed (_fresh_default). Returns True if it changed anything."""
    # By origin too: a renamed default is still that default.
    existing = {a.name for a in s.apps} | {a.origin for a in s.apps}
    changed = False
    for reg in REGISTRY:
        if reg.name not in existing and reg.name not in s.removed_defaults:
            s.apps.append(_fresh_default(reg))
            changed = True
    return changed


def _save_or_warn(s: Settings) -> None:
    if not save_settings(s):
        _warn("Couldn't write settings.json - changes won't be kept "
              "(see launcher.log).")


def load_settings() -> Settings:
    LOAD_WARNINGS.clear()
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except OSError as exc:
        _warn(f"Couldn't create {DATA_DIR} ({exc}).")

    if not os.path.exists(SETTINGS_FILE):
        s = default_settings()
        _save_or_warn(s)
        return s

    try:
        # utf-8-sig, not utf-8: it also accepts a leading BOM, which Notepad's
        # "UTF-8 with BOM" adds and plain utf-8 rejects as a corrupt file.
        with open(SETTINGS_FILE, encoding="utf-8-sig") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            raise ValueError("expected a JSON object at the top level")
    except (OSError, ValueError) as exc:   # incl. JSON and Unicode decode errors
        # Keep the unreadable file so a hand-edit isn't lost for good.
        kept = ""
        try:
            shutil.copyfile(SETTINGS_FILE, SETTINGS_FILE + ".bad")
            kept = " The old file was kept as settings.json.bad."
        except OSError:
            pass
        _warn(f"settings.json couldn't be read ({exc}) - settings were reset "
              f"to defaults.{kept}")
        s = default_settings()
        _save_or_warn(s)
        return s

    s = _from_dict(raw)
    # Migrate/repair: bring back any default the file lacks (no apps at all, or
    # a file predating a new default) -- but never one the CMDR removed, so an
    # app list they emptied on purpose stays empty.
    if _merge_new_defaults(s) or raw.get("version") != SETTINGS_VERSION:
        _save_or_warn(s)
    return s


def save_settings(s: Settings) -> bool:
    """Write settings.json atomically. Returns False (and logs why) if it
    couldn't -- callers tell the CMDR, since the change is otherwise lost."""
    data = {
        "version": SETTINGS_VERSION,
        "cmdr_name": s.cmdr_name,
        "elite_app_id": s.elite_app_id,
        "setup_complete": s.setup_complete,
        "auto_start": s.auto_start,
        "show_inactive_cards": s.show_inactive_cards,
        "shutdown_scope": s.shutdown_scope.value,
        "close_on_game_exit": s.close_on_game_exit,
        "close_on_shutdown": s.close_on_shutdown,
        "allow_elevation": s.allow_elevation,
        "theme": s.theme.value,
        "removed_defaults": s.removed_defaults,
        "check_for_updates": s.check_for_updates,
        "start_minimized": s.start_minimized,
        "apps": [
            {"name": a.name, "process": a.process_name,
             "path": a.path, "enabled": a.enabled, "origin": a.origin}
            for a in s.apps
        ],
    }
    # Atomic write: never leave a half-written settings.json behind on a crash.
    tmp = SETTINGS_FILE + ".tmp"
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, SETTINGS_FILE)
        return True
    except OSError as exc:
        log.warning("Failed to write settings.json: %s", exc)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def recompute_removed_defaults(apps: list[AppDescriptor],
                               previously_removed: list[str]) -> list[str]:
    """Given the app list the user just saved, return the updated set of default
    apps to suppress on future loads, so a removed default isn't auto-re-added by
    _merge_new_defaults. Lives here to keep all RemovedDefaults bookkeeping in
    one place; the settings dialog calls it on save."""
    kept = {a.name for a in apps}
    newly_removed = [name for name in REGISTRY_BY_NAME if name not in kept]
    return sorted(set(previously_removed) | set(newly_removed))
