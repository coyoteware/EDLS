"""
EDLS/config.py

The pure-data layer. Everything here is toolkit-blind and engine-blind; it imports only the standard
library and is imported by both main.py and ui.py without creating a cycle. It
owns four things:

  1. AppState / ShutdownScope   -- the shared vocabulary
  2. AppDescriptor              -- one app's definition, with path resolution
  3. REGISTRY                   -- list of default apps
  4. Settings + load/save       -- persistence, w/ merge-on-load and migration

Adding a companion app is a one-line entry in REGISTRY below. Both config dialogs
and the launch engine read from it, so there's no longer a four-places-to-edit
problem.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Callable, Optional

log = logging.getLogger("edls.config")

# Bump when the on-disk schema changes; load_settings() can migrate on read.
SETTINGS_VERSION = 1

# Display version, shown in the UI and used for the build's file metadata.
# Single source of truth (replaces the original $script:AppVersion).
APP_VERSION = "1.1.0"

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
    CLOSED = "Closed"
    WAITING = "Waiting..."
    LAUNCHING = "Launching..."
    ONLINE = "Online"
    RUNNING = "Running"

    @property
    def label(self) -> str:
        return self.value

    @property
    def is_running(self) -> bool:
        return self in (AppState.LAUNCHING, AppState.ONLINE, AppState.RUNNING)


class ShutdownScope(Enum):
    """What SHUTDOWN (and the on-exit teardown) is allowed to close.
    This is a user setting -- see Settings.shutdown_scope."""
    LAUNCHED_ONLY = "launched_only"   # only processes EDLS itself started
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


# =============================================================================
# App registry -- add or remove default apps here only
# =============================================================================
REGISTRY: list[AppDescriptor] = [
    AppDescriptor("EDMarketConnector", "EDMarketConnector",
                  r"%ProgramFiles(x86)%\EDMarketConnector\EDMarketConnector.exe"),
    AppDescriptor("SrvSurvey", "SrvSurvey",
                  resolver=_find_srvsurvey),
    AppDescriptor("OdysseyMaterials", "Elite Dangerous Odyssey Materials Helper",
                  r"%LOCALAPPDATA%\Elite Dangerous Odyssey Materials Helper Launcher"
                  r"\program\Elite Dangerous Odyssey Materials Helper.exe"),
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
                  r"%ProgramFiles%\VoiceAttack\VoiceAttack.exe"),
    AppDescriptor("EDCoPTER", "EDCoPTER",
                  r"%LOCALAPPDATA%\Programs\EDCoPTER\EDCoPTER.exe"),
    AppDescriptor("Elite Observatory", "ObservatoryCore",
                  r"%LOCALAPPDATA%\Programs\Elite Observatory\ObservatoryCore.exe"),
]

REGISTRY_BY_NAME = {a.name: a for a in REGISTRY}


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
    theme: Theme = Theme.DARK
    apps: list[AppDescriptor] = field(default_factory=list)
    removed_defaults: list[str] = field(default_factory=list)


def _clone_registry() -> list[AppDescriptor]:
    """Fresh copies so user edits never mutate REGISTRY (resolvers are shared
    callables, which is fine)."""
    return [replace(a) for a in REGISTRY]


def default_settings() -> Settings:
    return Settings(apps=_clone_registry())


def _app_from_dict(d: dict) -> AppDescriptor:
    name = d.get("name", "")
    reg = REGISTRY_BY_NAME.get(name)
    return AppDescriptor(
        name=name,
        process_name=d.get("process", ""),
        path=d.get("path") or "",
        enabled=bool(d.get("enabled", True)),
        # Re-attach the resolver for known defaults; custom apps have none.
        resolver=reg.resolver if reg else None,
    )


def _parse_scope(value) -> ShutdownScope:
    try:
        return ShutdownScope(value)
    except ValueError:
        return ShutdownScope.LAUNCHED_ONLY


def _parse_theme(value) -> Theme:
    try:
        return Theme(value)
    except ValueError:
        return Theme.DARK


def _from_dict(raw: dict) -> Settings:
    return Settings(
        cmdr_name=raw.get("cmdr_name", Settings.cmdr_name),
        elite_app_id=int(raw.get("elite_app_id", Settings.elite_app_id)),
        setup_complete=bool(raw.get("setup_complete", False)),
        auto_start=bool(raw.get("auto_start", False)),
        show_inactive_cards=bool(raw.get("show_inactive_cards", True)),
        shutdown_scope=_parse_scope(raw.get("shutdown_scope")),
        theme=_parse_theme(raw.get("theme")),
        apps=[_app_from_dict(d) for d in raw.get("apps", [])],
        removed_defaults=list(raw.get("removed_defaults", [])),
    )


def _merge_new_defaults(s: Settings) -> bool:
    """Append any registry app missing from settings -- unless the user
    deliberately removed it (tracked in removed_defaults). Lets a new default
    added in a later version appear for existing installs. Returns True if it
    changed anything."""
    existing = {a.name for a in s.apps}
    changed = False
    for reg in REGISTRY:
        if reg.name not in existing and reg.name not in s.removed_defaults:
            s.apps.append(replace(reg))
            changed = True
    return changed


def load_settings() -> Settings:
    os.makedirs(DATA_DIR, exist_ok=True)

    if not os.path.exists(SETTINGS_FILE):
        s = default_settings()
        save_settings(s)
        return s

    try:
        with open(SETTINGS_FILE, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("settings.json unreadable (%s); recreating from defaults", exc)
        s = default_settings()
        save_settings(s)
        return s

    s = _from_dict(raw)
    # Migrate/repair: a fresh file with no apps, or a file predating new defaults.
    if not s.apps:
        s.apps = _clone_registry()
        save_settings(s)
    elif _merge_new_defaults(s) or raw.get("version") != SETTINGS_VERSION:
        save_settings(s)
    return s


def save_settings(s: Settings) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    data = {
        "version": SETTINGS_VERSION,
        "cmdr_name": s.cmdr_name,
        "elite_app_id": s.elite_app_id,
        "setup_complete": s.setup_complete,
        "auto_start": s.auto_start,
        "show_inactive_cards": s.show_inactive_cards,
        "shutdown_scope": s.shutdown_scope.value,
        "theme": s.theme.value,
        "removed_defaults": s.removed_defaults,
        "apps": [
            {"name": a.name, "process": a.process_name,
             "path": a.path, "enabled": a.enabled}
            for a in s.apps
        ],
    }
    # Atomic write: never leave a half-written settings.json behind on a crash.
    tmp = SETTINGS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except OSError as exc:
        log.warning("Failed to write settings.json: %s", exc)


def recompute_removed_defaults(apps: list[AppDescriptor],
                               previously_removed: list[str]) -> list[str]:
    """Given the app list the user just saved, return the updated set of default
    apps to suppress on future loads, so a removed default isn't auto-re-added by
    _merge_new_defaults. Lives here to keep all RemovedDefaults bookkeeping in
    one place; the settings dialog calls it on save."""
    kept = {a.name for a in apps}
    newly_removed = [name for name in REGISTRY_BY_NAME if name not in kept]
    return sorted(set(previously_removed) | set(newly_removed))
