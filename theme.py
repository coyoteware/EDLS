"""
EDLS — theme.py
Single source of truth for the EDLS HUD palette, type metrics, and shape specs,
extracted from the EDLS Design System. ui.py imports the names it needs from
here and calls apply_theme(settings.theme) before building window content.

Every value here is identical to the design system's tokens/*.css, so the web
mockups and the Qt app stay in lockstep — change a colour in one place.

Requires PySide6 (same as ui.py).
"""
from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap

# Avoid a hard import cycle: Theme is an enum in config.py. We only compare by
# .name so a light fallback works even if config isn't importable in isolation.
try:
    from config import Theme  # type: ignore
    _LIGHT = Theme.LIGHT
except Exception:  # pragma: no cover
    _LIGHT = "LIGHT"


# ── Fixed accent ──────────────────────────────────────────────────────────────
# Gold is only ever drawn on dark surfaces (banner, dark buttons, dark dialog),
# so it never themes.
C_GOLD = QColor("#FFB700")

# ── Dark palette (canonical) ──────────────────────────────────────────────────
C_DARK   = QColor("#090905")
C_PANEL  = QColor("#0D0B07")
C_BORDER = QColor("#1C1208")
C_AMBER  = QColor("#C8760A")    # primary text / amber accent
C_ORANGE = QColor("#FF8C1A")    # active accent
C_DIM    = QColor("#4A3A20")
C_TMID   = QColor("#A07A3A")    # secondary text
C_TDIM   = QColor("#6A5030")    # tertiary text
C_GREEN  = QColor("#3DCC6A")
C_RED    = QColor("#CC3D3D")
C_INACT  = QColor("#454545")
C_NAME   = QColor("#C8860A")    # app-name labels
C_CMDR   = QColor("#FF8C1A")    # CMDR identity name
C_SUCCESS = QColor("#FFB700")   # success log lines
C_LOGDIM  = QColor("#5A4A2A")   # dim log lines
C_PANEL_T = QColor(13, 11, 7, 115)     # translucent panel fill
CARD_BG = QColor(10, 8, 4, 188)        # status-card fill
CARD_BORDER = QColor("#1A1208")
CARD_BORDER_OFF = QColor("#181818")
BG_IMAGE = "window-bg.svg"

# ── Panes ─────────────────────────────────────────────────────────────────────
# The status + log panes and the app cards are the dark HUD in BOTH themes: day
# mode ("Full Daylight") lightens the window around them — background, header,
# bottom bar, card keys, offline card borders — not the panes. Pane surfaces and
# everything on the cards read PANE.<name>, never the themed globals of the
# same name.
PANE = SimpleNamespace(
    C_PANEL_T=C_PANEL_T, C_BORDER=C_BORDER, CARD_BG=CARD_BG,
    CARD_BORDER=CARD_BORDER, C_TDIM=C_TDIM, C_NAME=C_NAME, C_ORANGE=C_ORANGE,
    C_GREEN=C_GREEN, C_RED=C_RED, C_INACT=C_INACT, C_DIM=C_DIM,
)

# Text drawn straight on a pane (its title, the activity log) rather than on a
# card. The see-through pane comes out mid-grey on the light window, so day mode
# swaps these for light tints -- readable, though mid-grey caps the contrast.
PANE_TITLE = C_TMID     # S Y S T E M   S T A T U S / A C T I V I T Y   L O G
PANE_META  = C_TDIM     # launcher.log
LOG_TIME   = C_TMID     # [HH:MM:SS]
LOG_COLORS = {
    "success": C_SUCCESS, "default": C_AMBER, "info": C_AMBER,
    "warning": C_ORANGE, "error": C_RED, "dim": C_LOGDIM,
}

# State-tinted card borders (the "glow" stand-in). Match StatusCard.update_state.
CARD_BORDER_ONLINE    = QColor(61, 204, 106, 72)
CARD_BORDER_LAUNCHING = QColor(200, 118, 10, 76)
CARD_BORDER_FAILED    = QColor(204, 61, 61, 76)

# Toggle-switch track colours.
TOGGLE_OFF = QColor("#8A929C")
TOGGLE_ON  = QColor("#C25E0A")

# ── Header banner ───────────────────────────────────────────────────────────────
# TitleBanner is a translucent fill strip with a top accent line, the title mark,
# and the suite title. Gold never themes, so light mode swaps in its own fill,
# accent, and mark.
BANNER_FILL   = QColor(0x14, 0x0E, 0x03, 140)   # dark strip, 55% opaque
BANNER_ACCENT = C_GOLD              # top line + title text
TITLE_MARK    = "title-mark.svg"

IS_LIGHT = False

_THEMED_NAMES = (
    "C_DARK", "C_PANEL", "C_BORDER", "C_AMBER", "C_ORANGE", "C_TMID",
    "C_TDIM", "C_CMDR", "C_SUCCESS", "CARD_BORDER_OFF", "BG_IMAGE",
    "BANNER_FILL", "BANNER_ACCENT", "TITLE_MARK",
    "PANE_TITLE", "PANE_META", "LOG_TIME", "LOG_COLORS",
)
_DARK_PALETTE = {name: globals()[name] for name in _THEMED_NAMES}

# ── Light "day mode" ──────────────────────────────────────────────────────────
# Everything outside the panes (see PANE above).
_LIGHT_PALETTE = {
    "C_DARK":   QColor("#D7DBE0"),
    "C_PANEL":  QColor("#C8CDD4"),
    "C_BORDER": QColor("#A6AEB9"),
    "C_AMBER":  QColor("#252C36"),
    "C_ORANGE": QColor("#C25E0A"),
    "C_TMID":   QColor("#586573"),
    "C_TDIM":   QColor("#7E8893"),
    "C_CMDR":   QColor("#243042"),
    "C_SUCCESS": QColor("#B5560C"),
    "CARD_BORDER_OFF": QColor("#C6CCD4"),   # light hairline on the dark cards
    "PANE_TITLE": QColor("#EEF1F4"),
    "PANE_META":  QColor("#D7DBE0"),
    "LOG_TIME":   QColor("#D7DBE0"),
    "LOG_COLORS": {
        "success": QColor("#FFE08A"), "default": QColor("#F4F6F8"),
        "info": QColor("#F4F6F8"), "warning": QColor("#FFC489"),
        "error": QColor("#FFB8B0"), "dim": QColor("#C3C8CE"),
    },
    "BG_IMAGE": "window-bg-light.svg",
    "BANNER_FILL":  QColor(0xC8, 0xCD, 0xD4, 140),   # silver strip, 55% opaque
    "BANNER_ACCENT": QColor("#C25E0A"),  # orange accent (no gold on light)
    "TITLE_MARK": "title-mark-light.svg",
}


def apply_theme(theme) -> None:
    """Rebind the themed palette globals. Call before building window content."""
    is_light = getattr(theme, "name", theme) == getattr(_LIGHT, "name", _LIGHT)
    globals().update(_LIGHT_PALETTE if is_light else _DARK_PALETTE)
    globals()["IS_LIGHT"] = is_light


# ── Type metrics ──────────────────────────────────────────────────────────────
# Two faces only. Euro Caps is uppercased + tracked; Share Tech Mono is the
# terminal face. Letter-spacing values are percentages (Qt PercentageSpacing).
FONT_DISPLAY = "Euro Caps"
FONT_MONO = "Share Tech Mono"
TRACK_DISPLAY = 108   # font_display
TRACK_BODY = 104      # font_body
TRACK_BANNER = 122    # title banner


def font_display(size: int, weight: QFont.Weight = QFont.Weight.Bold) -> QFont:
    f = QFont(FONT_DISPLAY, size, int(weight))
    f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, TRACK_DISPLAY)
    return f


def font_body(size: int, weight: QFont.Weight = QFont.Weight.Medium) -> QFont:
    f = QFont(FONT_DISPLAY, size, int(weight))
    f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, TRACK_BODY)
    return f


def font_mono(size: int) -> QFont:
    f = QFont(FONT_MONO, size)
    if not f.exactMatch():
        f = QFont("Consolas", size)
    return f


# ── Shape / motion specs ──────────────────────────────────────────────────────
CHAMFER_CARD = 10     # cards + buttons
CHAMFER_PANEL = 14    # panels
PULSE_MS = 1800       # status-dot pulse loop
BOOT_DURATION = 320   # panel boot-in
BOOT_OFFSET = 6
BOOT_STAGGER = 60


# ── Font-free power icon ──────────────────────────────────────────────────────
def power_icon(color: QColor, px: int = 18) -> QIcon:
    """The IEC power symbol (U+23FB), drawn with QPainter paths instead of a font
    glyph. Use this in place of _glyph_icon("⏻", …) so the SHUTDOWN icon renders
    on every system, including those without 'Segoe UI Symbol'."""
    hi = px * 2  # 2x for hiDPI crispness
    pm = QPixmap(hi, hi)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    pen = QPen(color, max(2.0, hi * 0.085))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)

    m = hi * 0.24
    ring = QRectF(m, m, hi - 2 * m, hi - 2 * m)
    # Arc with an ~80° gap centred at the top (12 o'clock = 90°).
    p.drawArc(ring, int(130 * 16), int(280 * 16))
    # Vertical bar through the gap.
    cx = hi / 2.0
    p.drawLine(int(cx), int(hi * 0.16), int(cx), int(hi * 0.46))
    p.end()

    pm.setDevicePixelRatio(2.0)
    return QIcon(pm)


# Drop-in icon size constant (matches ui.py BTN_ICON).
ICON_SIZE = QSize(18, 18)
