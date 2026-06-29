"""
EDLS - ui.py
PySide6 view layer. The only file that imports Qt.

Design target: faithfully approximate the Elite: Dangerous HUD aesthetic from
the HTML mockup — dark warm background, amber/gold palette, chamfered panel
corners, dot-matrix body background, glowing status dots, monospace log terminal.

QSS gaps and how we close them:
  clip-path polygon   → ChamferedFrame: custom QFrame that paints a clipped
                        polygon in paintEvent, giving us the real chamfered corner
  box-shadow / glow   → not used directly; approximated by state-coloured
                        borders (running cards, amber header) since QSS has no
                        inset shadow and a real glow needs a graphics effect
  @keyframes pulsing  → QPropertyAnimation on a custom 'opacity' property
  hex body background → tiled SVG QPixmap rendered once at startup
  text-shadow         → not available; compensated by bright base color
  ::before/::after    → extra QLabel / QFrame children placed in layouts

Fonts: Orbitron (display) and Rajdhani (body) are loaded from assets/ if present,
falling back to system monospace so the app is always runnable.

Contract with main.py:
    run(engine, settings) -> int
    engine.dispatch is set to a Qt-thread-safe invoker.
    engine.on_log.connect(...)       (message: str, level: str)
    engine.on_status.connect(...)    (key: str, state: AppState, pid: int | None)
    engine.on_busy.connect(...)      (busy: bool)
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import replace
from typing import Optional

from PySide6.QtCore import (
    QEasingCurve, QParallelAnimationGroup, QPoint, Property,
    QPropertyAnimation, QRect, QSize, Qt, QTimer, Signal, Slot,
)
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontDatabase, QIcon,
    QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygon,
)
from PySide6.QtWidgets import (
    QApplication, QFrame, QGraphicsOpacityEffect,
    QHBoxLayout, QLabel, QMainWindow, QSizePolicy,
    QSpacerItem, QSplitter, QStyle, QStyleOptionButton, QStylePainter,
    QTextEdit, QVBoxLayout, QWidget, QPushButton,
    QDialog, QCheckBox, QLineEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QDialogButtonBox,
)

import config
from config import AppDescriptor, AppState, Settings, ShutdownScope, Theme

# ── Palette ──────────────────────────────────────────────────────────────────
# Two complete palettes (dark, and a light "day mode"). Every colour below is
# themed by apply_theme() EXCEPT C_GOLD, which always stays gold because it's
# only ever drawn on dark surfaces (the title banner, the dark action buttons,
# the dark settings dialog). Widgets bake colours into stylesheets at build
# time, so a live theme switch must rebuild the window content afterwards
# (MainWindow._on_settings_saved does).
C_GOLD   = QColor("#FFB700")    # fixed accent — dark surfaces only

# Live globals = the dark-theme values; apply_theme() rebinds them.
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
C_CMDR   = QColor("#FF8C1A")    # CMDR identity name in the header
C_SUCCESS = QColor("#FFB700")   # success log lines
C_LOGDIM  = QColor("#5A4A2A")   # dim log lines
# Semi-transparent panel fill so the window background shows through (status/log).
C_PANEL_T = QColor(13, 11, 7, 115)
# Card surfaces (themed): pulled out of StatusCard so the theme can swap them.
CARD_BG = QColor(10, 8, 4, 188)
CARD_BORDER = QColor("#1A1208")
CARD_BORDER_OFF = QColor("#181818")
# Window background image filename (themed: dark art vs. light day-mode art).
BG_IMAGE = "window-bg.png"

# True while the light "day mode" is active. Used by the few widgets whose colours
# are picked by branch (the card buttons, the settings dialog) rather than a
# single themed constant.
IS_LIGHT = False

_THEMED_NAMES = (
    "C_DARK", "C_PANEL", "C_BORDER", "C_AMBER", "C_ORANGE", "C_DIM",
    "C_TMID", "C_TDIM", "C_GREEN", "C_RED", "C_INACT", "C_NAME", "C_CMDR",
    "C_SUCCESS", "C_LOGDIM", "C_PANEL_T", "CARD_BG", "CARD_BORDER",
    "CARD_BORDER_OFF", "BG_IMAGE",
)
# Dark palette references the literals above (single source; no value drift).
_DARK_PALETTE = {name: globals()[name] for name in _THEMED_NAMES}

# Light "day mode": dark slate text on a cool light-grey HUD. Orange is kept ONLY
# as an accent (active states, dots); green/red status colours are darkened so
# they read on light; panels/cards become light translucent surfaces and the
# background swaps to a cool light asset.
_LIGHT_PALETTE = {
    "C_DARK":   QColor("#D7DBE0"),   # window base (cool light grey)
    "C_PANEL":  QColor("#C8CDD4"),
    "C_BORDER": QColor("#A6AEB9"),
    "C_AMBER":  QColor("#252C36"),   # primary text -> dark slate
    "C_ORANGE": QColor("#C25E0A"),   # accent orange (kept)
    "C_DIM":    QColor("#AEB6C0"),
    "C_TMID":   QColor("#586573"),   # secondary text
    "C_TDIM":   QColor("#7E8893"),   # tertiary text
    "C_GREEN":  QColor("#1E9E4C"),   # online (darker for light bg)
    "C_RED":    QColor("#C0322B"),
    "C_INACT":  QColor("#9AA2AC"),   # offline grey
    "C_NAME":   QColor("#2A313B"),   # app names -> dark
    "C_CMDR":   QColor("#243042"),   # CMDR name -> dark slate (readable on light)
    "C_SUCCESS": QColor("#B5560C"),  # success log -> deep orange
    "C_LOGDIM": QColor("#98A0A8"),
    "C_PANEL_T": QColor(255, 255, 255, 96),    # light translucent panels
    "CARD_BG":  QColor(255, 255, 255, 150),    # light cards
    "CARD_BORDER": QColor("#B4BCC6"),
    "CARD_BORDER_OFF": QColor("#C6CCD4"),
    "BG_IMAGE": "window-bg-light.png",
}

# State → (label_colour, dot_colour) and log-level → colour. Rebuilt on each
# theme switch since they reference themed colours.
STATE_COLORS: dict = {}
LOG_COLORS: dict = {}


def _build_color_maps() -> None:
    global STATE_COLORS, LOG_COLORS
    STATE_COLORS = {
        AppState.ONLINE:    (C_GREEN,  C_GREEN),
        AppState.RUNNING:   (C_GREEN,  C_GREEN),
        AppState.LAUNCHING: (C_ORANGE, C_ORANGE),
        AppState.WAITING:   (C_ORANGE, C_ORANGE),
        AppState.OFFLINE:   (C_INACT,  C_INACT),
        AppState.NOT_FOUND: (C_INACT,  C_INACT),
        AppState.FAILED:    (C_RED,    C_RED),
        AppState.STOPPED:   (C_INACT,  C_INACT),
        AppState.CLOSED:    (C_INACT,  C_INACT),
    }
    LOG_COLORS = {
        "success": C_SUCCESS,
        "default": C_AMBER,
        "warning": C_ORANGE,
        "error":   C_RED,
        "dim":     C_LOGDIM,
        "info":    C_AMBER,
    }


def apply_theme(theme: Theme) -> None:
    """Rebind the palette globals + colour maps to the chosen Theme. Call before
    building (or rebuilding) the window content."""
    globals().update(
        _LIGHT_PALETTE if theme is Theme.LIGHT else _DARK_PALETTE)
    globals()["IS_LIGHT"] = theme is Theme.LIGHT
    _build_color_maps()


_build_color_maps()   # seed the maps for the default (dark) palette

# Friendly card labels that differ from the registry/identifier name. Presentation
# only — the descriptor.name is still the key for status routing and settings.
_DISPLAY_NAMES = {
    "OdysseyMaterials": "Odyssey Mats Helper",
}

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")

# Where the "report issue" credit link points (the GitHub issues page).
ISSUES_URL = "https://github.com/coyoteware/EDLS-py/issues"


# ── Font helpers ──────────────────────────────────────────────────────────────
# Euro Caps (the ED-style display face) is the primary UI font, loaded from
# assets/. It's an all-caps face, which suits the HUD look since the labels are
# uppercased anyway. The activity log keeps a monospace face for the terminal feel.
def _load_fonts() -> None:
    for name in ("EUROCAPS.TTF", "ShareTechMono-Regular.ttf"):
        path = os.path.join(ASSETS_DIR, name)
        if os.path.exists(path):
            QFontDatabase.addApplicationFont(path)


def font_display(size: int, weight: QFont.Weight = QFont.Weight.Bold) -> QFont:
    f = QFont("Euro Caps", size, int(weight))
    f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 108)
    return f


def font_body(size: int, weight: QFont.Weight = QFont.Weight.Medium) -> QFont:
    f = QFont("Euro Caps", size, int(weight))
    f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 104)
    return f


def font_mono(size: int) -> QFont:
    f = QFont("Share Tech Mono", size)
    if not f.exactMatch():
        f = QFont("Consolas", size)
    return f




# ── ChamferedFrame ────────────────────────────────────────────────────────────
class ChamferedFrame(QFrame):
    """QFrame that clips itself to a polygon with a chamfered top-right corner,
    approximating the CSS clip-path used throughout the mockup. The clip size
    is configurable; pass `glow_color` to draw a coloured border."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        chamfer: int = 14,
        bg: Optional[QColor] = None,
        border: Optional[QColor] = None,
        glow_color: Optional[QColor] = None,
    ) -> None:
        super().__init__(parent)
        self._chamfer = chamfer
        # Resolve defaults from the live (themed) globals, not import-time values.
        self._bg = bg if bg is not None else C_PANEL
        self._border = border if border is not None else C_BORDER
        self._glow = glow_color
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setContentsMargins(12, 10, 12, 10)

    def _polygon(self, r: QRect) -> QPolygon:
        c = self._chamfer
        return QPolygon([
            QPoint(r.left(),          r.top()),
            QPoint(r.right() - c,     r.top()),
            QPoint(r.right(),         r.top() + c),
            QPoint(r.right(),         r.bottom()),
            QPoint(r.left() + c,      r.bottom()),
            QPoint(r.left(),          r.bottom() - c),
        ])

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(1, 1, -1, -1)
        poly = self._polygon(rect)
        path = QPainterPath()
        path.addPolygon(poly)
        path.closeSubpath()

        # fill
        p.setClipPath(path)
        p.fillPath(path, QBrush(self._bg))

        # border
        border_color = self._glow if self._glow else self._border
        pen = QPen(border_color, 1)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setClipping(False)
        p.drawPath(path)
        p.end()


# ── Pulsing dot ───────────────────────────────────────────────────────────────
class PulsingDot(QWidget):
    """7 px circle that fades in/out for running states."""

    def __init__(self, color: QColor = C_GREEN, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._color = color
        self._opacity = 1.0
        self.setFixedSize(10, 10)
        self._anim: Optional[QPropertyAnimation] = None

    def _get_opacity(self) -> float:
        return self._opacity

    def _set_opacity(self, v: float) -> None:
        self._opacity = v
        self.update()

    # Qt property so QPropertyAnimation can drive b"dotOpacity"
    dotOpacity = Property(float, _get_opacity, _set_opacity)

    def set_color(self, color: QColor, pulse: bool = True) -> None:
        self._color = color
        if self._anim:
            self._anim.stop()
            self._anim = None
        if pulse:
            self._anim = QPropertyAnimation(self, b"dotOpacity", self)
            self._anim.setStartValue(1.0)
            self._anim.setEndValue(0.45)
            self._anim.setDuration(1800)
            self._anim.setEasingCurve(QEasingCurve.Type.SineCurve)
            self._anim.setLoopCount(-1)
            self._anim.start()
        else:
            self._opacity = 1.0
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(self._color)
        color.setAlphaF(self._opacity)
        p.setBrush(QBrush(color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(2, 2, 7, 7)
        p.end()


# ── Eliding label ─────────────────────────────────────────────────────────────
class ElidingLabel(QLabel):
    """QLabel that truncates with an ellipsis to its current width instead of
    wrapping or forcing the layout wider. Used for card names so a long name
    yields space to the inline status text rather than colliding with it."""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._full = text
        # Ignored width => the label takes the space the layout leaves it (after
        # the status label) and elides into it, rather than demanding its full
        # natural width.
        self.setSizePolicy(QSizePolicy.Policy.Ignored,
                           QSizePolicy.Policy.Preferred)
        super().setText(text)

    def setText(self, text: str) -> None:  # noqa: N802
        self._full = text
        self._relayout()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._relayout()

    def _relayout(self) -> None:
        elided = self.fontMetrics().elidedText(
            self._full, Qt.TextElideMode.ElideRight, max(0, self.width()))
        super().setText(elided)


# ── Status card ───────────────────────────────────────────────────────────────
class StatusCard(ChamferedFrame):
    """One app's status tile. Wired to engine.on_status."""

    start_stop_clicked = Signal(str, bool)   # (app_name, currently_running)

    def __init__(self, descriptor: AppDescriptor,
                 wide: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, chamfer=10,
                         bg=CARD_BG, border=CARD_BORDER)
        self._name = descriptor.name
        self._wide = wide
        self._state = AppState.OFFLINE
        self.setMinimumHeight(90)
        if wide:
            self.setMinimumWidth(200)

        vbox = QVBoxLayout(self)
        vbox.setSpacing(4)
        vbox.setContentsMargins(10, 8, 10, 8)

        # header row: dot + name (+ inline status on the compact cards)
        hdr = QHBoxLayout()
        hdr.setSpacing(7)
        self.dot = PulsingDot(C_INACT, self)
        self.dot.set_color(C_INACT, pulse=False)
        display = _DISPLAY_NAMES.get(descriptor.name, descriptor.name)
        name_lbl = ElidingLabel(display.upper())
        name_lbl.setFont(font_body(10, QFont.Weight.DemiBold))
        name_lbl.setStyleSheet(f"color: {C_NAME.name()}; letter-spacing: 1px;")
        hdr.addWidget(self.dot)
        hdr.addWidget(name_lbl, 1)

        # state label — right-justified. The PID line is gone (PIDs are in the
        # activity log); on compact cards the status moves up beside the name to
        # cut a row of clutter, while the featured Elite card keeps it big below.
        self.state_lbl = QLabel(AppState.OFFLINE.label)
        self.state_lbl.setStyleSheet(f"color: {C_INACT.name()};")
        self.state_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        if wide:
            self.state_lbl.setFont(font_mono(16))
            vbox.addLayout(hdr)
            vbox.addWidget(self.state_lbl)
            vbox.addStretch(1)
            self.ctrl_btn = None
        else:
            self.state_lbl.setFont(font_mono(12))
            hdr.addWidget(self.state_lbl, 0, Qt.AlignmentFlag.AlignTop)
            vbox.addLayout(hdr)
            vbox.addStretch(1)
            self.ctrl_btn = QPushButton("START")
            self.ctrl_btn.setFont(font_display(7))
            self.ctrl_btn.setFixedHeight(20)
            self.ctrl_btn.setStyleSheet(_btn_ctrl_style(active=False))
            self.ctrl_btn.clicked.connect(self._on_ctrl)
            vbox.addWidget(self.ctrl_btn)

        self.update_state(AppState.OFFLINE, None)

    def _on_ctrl(self) -> None:
        self.start_stop_clicked.emit(self._name, self._state.is_running)

    def update_state(self, state: AppState, pid: Optional[int]) -> None:
        self._state = state
        label_col, dot_col = STATE_COLORS.get(state, (C_INACT, C_INACT))
        pulse = state in (AppState.ONLINE, AppState.RUNNING,
                          AppState.LAUNCHING, AppState.WAITING)

        self.dot.set_color(dot_col, pulse=pulse)
        self.state_lbl.setText(state.label)
        self.state_lbl.setStyleSheet(f"color: {label_col.name()};")

        if self.ctrl_btn:
            is_running = state.is_running
            self.ctrl_btn.setText("STOP" if is_running else "START")
            self.ctrl_btn.setStyleSheet(_btn_ctrl_style(active=is_running))

        # update border colour to match state
        if state in (AppState.ONLINE, AppState.RUNNING):
            self._border = QColor(61, 204, 106, 72)
        elif state in (AppState.LAUNCHING, AppState.WAITING):
            self._border = QColor(200, 118, 10, 76)
        elif state in (AppState.FAILED,):
            self._border = QColor(204, 61, 61, 76)
        else:
            self._border = CARD_BORDER_OFF
        self.update()


# ── Log view ──────────────────────────────────────────────────────────────────
class LogView(QTextEdit):
    """Read-only terminal-style log. Appends coloured lines; auto-scrolls."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(font_mono(11))
        self.setStyleSheet(f"""
            QTextEdit {{
                background: transparent;
                color: {C_AMBER.name()};
                border: none;
                padding: 4px 8px;
            }}
            QScrollBar:vertical {{
                background: transparent; width: 4px;
            }}
            QScrollBar::handle:vertical {{
                background: {C_DIM.name()}; border-radius: 2px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
        """)

    def append_line(self, message: str, level: str = "default") -> None:
        color = LOG_COLORS.get(level, C_AMBER)
        ts = time.strftime("[%H:%M:%S]")
        ts_html = f'<span style="color:{C_TMID.name()};">{ts}</span>'
        msg_html = f'<span style="color:{color.name()};">&nbsp;{_esc(message)}</span>'
        self.append(f"{ts_html}{msg_html}")
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))


# ── Button styles ─────────────────────────────────────────────────────────────
BTN_H = 38   # shared height for the four bottom-bar buttons

# ── Colour palettes for the four bottom buttons ───────────────────────────────
_C_LAUNCH = dict(
    bg="#120C00", bg_h="#1A1000", bg_p="#0A0800",
    bd="#C8760A", bd_h="#FFB700", fg="#FFB700", fg_h="#FFD040",
)
_C_SECONDARY = dict(
    bg="#111114", bg_h="#120F09", bg_p="#0A0A0A",
    bd="#2A2A35", bd_h="#C8760A", fg="#686868", fg_h="#FF8C1A",
)
_C_SECONDARY_ON = dict(
    bg="#120C00", bg_h="#1A1000", bg_p="#0A0800",
    bd="#C8760A", bd_h="#FFB700", fg="#FF8C1A", fg_h="#FFB700",
)
_C_DANGER = dict(
    bg="#0D0805", bg_h="#140A0A", bg_p="#0A0505",
    bd="#2A1515", bd_h="#CC3D3D", fg="#7A3A3A", fg_h="#CC3D3D",
)


# ── Chamfered button ──────────────────────────────────────────────────────────
class ChamferedButton(QPushButton):
    """QPushButton whose body is a chamfered polygon instead of a rectangle.
    Hover/press/disabled states are handled entirely in paintEvent; no QSS."""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None,
                 chamfer: int = 10, **colors) -> None:
        super().__init__(text, parent)
        self._ch = chamfer
        self._c: dict = {}
        self.set_colors(**colors)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def set_colors(self, *, bg, bg_h, bg_p, bd, bd_h, fg, fg_h) -> None:
        self._c = dict(bg=QColor(bg), bg_h=QColor(bg_h), bg_p=QColor(bg_p),
                       bd=QColor(bd), bd_h=QColor(bd_h),
                       fg=QColor(fg), fg_h=QColor(fg_h))
        self.update()

    def _poly(self, r: QRect) -> QPolygon:
        c = self._ch
        return QPolygon([
            QPoint(r.left(),      r.top()),
            QPoint(r.right() - c, r.top()),
            QPoint(r.right(),     r.top() + c),
            QPoint(r.right(),     r.bottom()),
            QPoint(r.left() + c,  r.bottom()),
            QPoint(r.left(),      r.bottom() - c),
        ])

    def paintEvent(self, _event) -> None:  # noqa: N802
        en    = self.isEnabled()
        hover = en and self.underMouse()
        down  = en and self.isDown()
        c     = self._c

        if not en:
            bg, bd, fg = QColor("#0A0A0A"), QColor("#181818"), QColor("#303030")
        else:
            bg = c["bg_p"] if down  else (c["bg_h"] if hover else c["bg"])
            bd = c["bd_h"] if hover else c["bd"]
            fg = c["fg_h"] if (hover or down) else c["fg"]

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addPolygon(self._poly(rect))
        path.closeSubpath()
        p.fillPath(path, QBrush(bg))
        p.setPen(QPen(bd, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setClipping(False)
        p.drawPath(path)
        p.end()

        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        pal = QPalette(self.palette())
        pal.setColor(QPalette.ColorRole.ButtonText, fg)
        opt.palette = pal
        sp = QStylePainter(self)
        sp.drawControl(QStyle.ControlElement.CE_PushButtonLabel, opt)
        sp.end()


def _btn_ctrl_style(active: bool = False) -> str:
    base = ("QPushButton {{ font-family: 'Euro Caps'; font-size: 7px;"
            " font-weight:700; letter-spacing:1px; border:1px solid {bd};"
            " color:{fg}; background:{bg}; padding: 0 4px; }}"
            " QPushButton:hover {{ border-color:{bdh}; color:{fgh}; }}")
    if IS_LIGHT:
        # Bright buttons for the light cards: filled green STOP, light grey START.
        if active:
            return base.format(bd="#3FA85F", fg="#1C6B36", bg="#CDEED7",
                               bdh="#2E9E55", fgh="#15592C")
        return base.format(bd="#B4BCC6", fg="#5A6573", bg="#E9EDF1",
                           bdh="#C25E0A", fgh="#C25E0A")
    # Dark theme (original).
    if active:
        return base.format(bd="#226622", fg="#44AA44", bg="#0D1A0D",
                           bdh="#44AA44", fgh="#66CC66")
    return base.format(bd="#1E1E1E", fg="#484848", bg="#0C0C0C",
                       bdh="#C8760A", fgh="#C8760A")


# Icon size for the bottom-bar buttons (~40% larger than the old inline glyphs).
BTN_ICON = 18


def _glyph_icon(char: str, color: QColor, px: int = BTN_ICON) -> QIcon:
    """Render a single glyph as a coloured QIcon. We draw it in 'Segoe UI Symbol'
    (which carries the IEC power symbol U+23FB and friends) rather than relying on
    the button's display font — that's why the SHUTDOWN icon was previously tofu."""
    hi = px * 2                       # 2x for crispness on hiDPI
    pm = QPixmap(hi, hi)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    f = QFont("Segoe UI Symbol")
    f.setPixelSize(int(hi * 0.86))
    p.setFont(f)
    p.setPen(color)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, char)
    p.end()
    pm.setDevicePixelRatio(2.0)
    return QIcon(pm)


# ── Header banner ─────────────────────────────────────────────────────────────
class TitleBanner(QWidget):
    """Paints title-bar.png (the wide chamfered frame with the framing chevrons)
    scaled to the panel width, with the outpost station icon at the left and the
    suite title centred in the empty span the chevrons bracket. Everything is
    drawn in paintEvent so the overlay positions are exact regardless of layout.
    Falls back to a plain amber-edged strip if the banner asset is missing, so
    the app always runs."""

    # title-bar.png is ~3200×220 — a very wide ~14.5:1 frame.
    _ASPECT = 3200 / 220

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._banner = QPixmap(os.path.join(ASSETS_DIR, "title-bar.png"))
        self._outpost = QPixmap(os.path.join(ASSETS_DIR, "outpost.png"))
        self._scaled: Optional[QPixmap] = None
        self._scaled_for = -1
        self.setMinimumHeight(50)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # Lock height to the banner's aspect so the frame art never distorts.
        if not self._banner.isNull() and self.width() > 0:
            h = max(50, min(round(self.width() / self._ASPECT), 88))
            if h != self.height():
                self.setFixedHeight(h)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = self.rect()

        if not self._banner.isNull():
            if self._scaled_for != rect.width():
                self._scaled = self._banner.scaled(
                    rect.width(), rect.height(),
                    Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                self._scaled_for = rect.width()
            p.drawPixmap(0, 0, self._scaled)
        else:
            p.fillRect(rect, QColor("#0E0A04"))
            p.setPen(QPen(C_AMBER, 1))
            p.drawRect(rect.adjusted(0, 0, -1, -1))

        # Outpost station icon at the left, vertically centred inside the frame.
        if not self._outpost.isNull():
            icon = self._outpost.scaledToHeight(
                int(rect.height() * 0.60),
                Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap(22, (rect.height() - icon.height()) // 2, icon)

        # Suite title, centred between the framing chevrons. Size scales with the
        # banner height so it fills the bar rather than floating thin in the middle.
        f = font_display(13, QFont.Weight.ExtraBold)
        f.setPixelSize(max(20, int(rect.height() * 0.46)))
        f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 122)
        p.setFont(f)
        p.setPen(C_GOLD)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                   "ELITE : DANGEROUS  ·  LAUNCH SUITE")
        p.end()


# ── Header panel ──────────────────────────────────────────────────────────────
class HeaderPanel(QWidget):
    """The title banner plus the CMDR identity row beneath it. The banner art
    (title-bar.png + outpost.png) carries the framing decoration, so the old
    hand-drawn ◆ diamonds and separator line are gone — they'd double up with
    the frame's own brackets and chevrons."""

    def __init__(self, cmdr_name: str,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        vbox = QVBoxLayout(self)
        vbox.setSpacing(8)
        vbox.setContentsMargins(0, 0, 0, 0)

        self._banner = TitleBanner()
        vbox.addWidget(self._banner)

        # CMDR row
        cmdr_row = QHBoxLayout()
        cmdr_row.setSpacing(10)
        cmdr_row.addStretch()

        badge = QLabel("CMDR")
        badge.setFont(font_display(7, QFont.Weight.DemiBold))
        badge.setStyleSheet(
            f"color: {C_AMBER.name()}; border: 1px solid {C_AMBER.name()};"
            " padding: 2px 7px;")
        cmdr_row.addWidget(badge)

        self.cmdr_lbl = QLabel(cmdr_name.upper())
        self.cmdr_lbl.setFont(font_body(12, QFont.Weight.DemiBold))
        self.cmdr_lbl.setStyleSheet(
            f"color: {C_CMDR.name()}; letter-spacing: 4px;")
        cmdr_row.addWidget(self.cmdr_lbl)
        cmdr_row.addStretch()
        vbox.addLayout(cmdr_row)

    def set_cmdr(self, name: str) -> None:
        self.cmdr_lbl.setText(name.upper())


# ── Status panel ──────────────────────────────────────────────────────────────
KEY_STEAM     = "Steam"
KEY_ELITE     = "Elite"
STEAM_PROCESS = "steam"


class StatusPanel(ChamferedFrame):
    start_stop_clicked = Signal(str, bool)

    def __init__(self, apps: list[AppDescriptor],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, chamfer=14, bg=C_PANEL_T)
        self.cards: dict[str, StatusCard] = {}

        vbox = QVBoxLayout(self)
        vbox.setSpacing(8)
        vbox.setContentsMargins(14, 10, 14, 10)

        lbl = QLabel("S Y S T E M   S T A T U S")
        lbl.setFont(font_display(7))
        lbl.setStyleSheet(f"color: {C_TMID.name()}; letter-spacing: 3px;")
        vbox.addWidget(lbl)

        # card grid: 4 columns
        from PySide6.QtWidgets import QGridLayout
        grid = QGridLayout()
        grid.setSpacing(8)

        col, row = 0, 0

        def _add(card: StatusCard, span: int = 1) -> None:
            nonlocal col, row
            grid.addWidget(card, row, col, 1, span)
            card.start_stop_clicked.connect(self.start_stop_clicked)
            col += span
            if col >= 4:
                col = 0
                row += 1

        # Steam (single)
        steam = StatusCard(
            AppDescriptor(KEY_STEAM, STEAM_PROCESS), wide=False)
        self.cards[KEY_STEAM] = steam
        _add(steam)

        # Elite (double-wide)
        elite_desc = AppDescriptor(KEY_ELITE, "EliteDangerous64")
        elite = StatusCard(elite_desc, wide=True)
        self.cards[KEY_ELITE] = elite
        _add(elite, span=2)

        # companion apps
        for desc in apps:
            card = StatusCard(desc)
            self.cards[desc.name] = card
            _add(card)

        # fill remaining columns in last row if needed
        while col % 4 != 0:
            grid.addItem(
                QSpacerItem(0, 0, QSizePolicy.Policy.Expanding,
                            QSizePolicy.Policy.Minimum),
                row, col)
            col += 1

        vbox.addLayout(grid)

    def update_card(self, key: str, state: AppState,
                    pid: Optional[int]) -> None:
        card = self.cards.get(key)
        if card:
            card.update_state(state, pid)





# ── Log panel ─────────────────────────────────────────────────────────────────
class LogPanel(ChamferedFrame):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, chamfer=14, bg=C_PANEL_T)
        vbox = QVBoxLayout(self)
        vbox.setSpacing(0)
        vbox.setContentsMargins(0, 0, 0, 0)

        # header
        hdr = QHBoxLayout()
        hdr.setContentsMargins(14, 8, 14, 6)
        lbl = QLabel("A C T I V I T Y   L O G")
        lbl.setFont(font_display(7))
        lbl.setStyleSheet(f"color: {C_TMID.name()}; letter-spacing: 3px;")
        file_lbl = QLabel("launcher.log")
        file_lbl.setFont(font_mono(9))
        file_lbl.setStyleSheet(f"color: {C_TDIM.name()};")
        hdr.addWidget(lbl)
        hdr.addStretch()
        hdr.addWidget(file_lbl)
        vbox.addLayout(hdr)

        # amber glow divider
        div = QFrame()
        div.setFixedHeight(1)
        div.setStyleSheet(f"background: {C_AMBER.name()}; opacity: 0.2;")
        vbox.addWidget(div)

        self.log = LogView()
        self.setMinimumHeight(130)
        vbox.addWidget(self.log, 1)

    def append(self, message: str, level: str = "default") -> None:
        self.log.append_line(message, level)


# ── Button bar ────────────────────────────────────────────────────────────────
class ButtonBar(QWidget):
    shutdown_clicked  = Signal()
    autostart_toggled = Signal(bool)
    settings_clicked  = Signal()
    launch_clicked    = Signal()

    def __init__(self, version: str, auto_start: bool,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._auto_start = auto_start
        hbox = QHBoxLayout(self)
        hbox.setContentsMargins(0, 6, 0, 0)
        hbox.setSpacing(10)

        # credits (left)
        cred_vbox = QVBoxLayout()
        cred_vbox.setSpacing(1)
        l1 = QLabel("CMDR Coyote Bongwater (and Claude)")
        l1.setFont(font_mono(9))
        l1.setStyleSheet(f"color: {C_TDIM.name()};")
        l2 = QLabel(f"{version}  ·  <a href='{ISSUES_URL}' "
                    f"style='color:{C_TMID.name()};'>report issue</a>")
        l2.setFont(font_mono(9))
        l2.setStyleSheet(f"color: {C_TDIM.name()};")
        l2.setOpenExternalLinks(True)
        cred_vbox.addWidget(l1)
        cred_vbox.addWidget(l2)
        hbox.addLayout(cred_vbox)
        hbox.addStretch()

        self.shutdown_btn  = ChamferedButton("SHUTDOWN",   chamfer=10, **_C_DANGER)
        self.autostart_btn = ChamferedButton("AUTO START", chamfer=10,
                                             **(_C_SECONDARY_ON if auto_start else _C_SECONDARY))
        self.settings_btn  = ChamferedButton("SETTINGS",  chamfer=10, **_C_SECONDARY)
        self.launch_btn    = ChamferedButton("LAUNCH",    chamfer=10, **_C_LAUNCH)

        self.shutdown_btn.setFont(font_display(9))
        self.autostart_btn.setFont(font_display(9))
        self.settings_btn.setFont(font_display(9))
        self.launch_btn.setFont(font_display(11))

        self.shutdown_btn.setIcon(_glyph_icon("⏻", QColor("#B85555")))
        self.autostart_btn.setIcon(_glyph_icon("⟳", C_ORANGE))
        self.settings_btn.setIcon(_glyph_icon("⚙", QColor("#9A9AA0")))
        self.launch_btn.setIcon(_glyph_icon("▶", C_GOLD))
        for b in (self.shutdown_btn, self.autostart_btn,
                  self.settings_btn, self.launch_btn):
            b.setIconSize(QSize(BTN_ICON, BTN_ICON))
            b.setFixedHeight(BTN_H)
            b.setMinimumWidth(120)
            hbox.addWidget(b, 0, Qt.AlignmentFlag.AlignBottom)

        self.shutdown_btn.clicked.connect(self.shutdown_clicked)
        self.autostart_btn.clicked.connect(self._toggle_autostart)
        self.settings_btn.clicked.connect(self.settings_clicked)
        self.launch_btn.clicked.connect(self.launch_clicked)

    def _toggle_autostart(self) -> None:
        self._auto_start = not self._auto_start
        self.autostart_btn.set_colors(
            **(_C_SECONDARY_ON if self._auto_start else _C_SECONDARY))
        self.autostart_toggled.emit(self._auto_start)

    def set_busy(self, busy: bool) -> None:
        self.launch_btn.setEnabled(not busy)
        self.launch_btn.setText("RUNNING..." if busy else "LAUNCH")


# ── Background widget (hex grid + vignette) ───────────────────────────────────
class Background(QWidget):
    """Paints window-bg.png (starfield/nebula/dot-matrix with corner brackets),
    scaled to cover the window and centered. Falls back to a flat dark fill if
    the asset is missing, so the app still runs."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._pix = QPixmap()
        self._loaded: Optional[str] = None
        self._scaled: Optional[QPixmap] = None
        self._scaled_for: Optional[QSize] = None

    def _ensure_pixmap(self) -> None:
        """(Re)load the themed background image if the active theme changed it.
        The widget persists across theme switches, so this is how it repaints
        with the right asset after apply_theme()."""
        if self._loaded != BG_IMAGE:
            self._pix = QPixmap(os.path.join(ASSETS_DIR, BG_IMAGE))
            self._loaded = BG_IMAGE
            self._scaled = None
            self._scaled_for = None

    def paintEvent(self, _event) -> None:  # noqa: N802
        self._ensure_pixmap()
        p = QPainter(self)
        p.fillRect(self.rect(), C_DARK)
        if not self._pix.isNull():
            if self._scaled_for != self.size():
                self._scaled = self._pix.scaled(
                    self.size(),
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation)
                self._scaled_for = self.size()
            x = (self.width() - self._scaled.width()) // 2
            y = (self.height() - self._scaled.height()) // 2
            p.drawPixmap(x, y, self._scaled)
        p.end()


# ── Toggle switch ─────────────────────────────────────────────────────────────
class ToggleSwitch(QWidget):
    """An iOS-style sliding switch: a rounded track with a circular knob that
    animates between off (grey) and on (amber). Exposes a QCheckBox-like API
    (isChecked / setChecked / toggled) so it drops in for a checkbox."""

    toggled = Signal(bool)

    def __init__(self, checked: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._checked = checked
        self._pos = 1.0 if checked else 0.0   # knob travel, 0 (off) .. 1 (on)
        self.setFixedSize(50, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._anim = QPropertyAnimation(self, b"knobPos", self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)

    def isChecked(self) -> bool:  # noqa: N802
        return self._checked

    def setChecked(self, on: bool) -> None:  # noqa: N802
        if on == self._checked:
            return
        self._checked = on
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()
        self.toggled.emit(on)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.setChecked(not self._checked)
        super().mousePressEvent(event)

    def _get_pos(self) -> float:
        return self._pos

    def _set_pos(self, v: float) -> None:
        self._pos = v
        self.update()

    knobPos = Property(float, _get_pos, _set_pos)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect().adjusted(1, 1, -1, -1)
        radius = r.height() / 2.0

        off, on = QColor("#8A929C"), QColor("#C25E0A")
        t = self._pos
        track = QColor(int(off.red()   + (on.red()   - off.red())   * t),
                       int(off.green() + (on.green() - off.green()) * t),
                       int(off.blue()  + (on.blue()  - off.blue())  * t))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(r, radius, radius)

        margin = 3
        d = r.height() - 2 * margin
        x = r.left() + margin + int(self._pos * (r.width() - d - 2 * margin))
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(x, r.top() + margin, d, d)
        p.end()


# ── Settings dialog ───────────────────────────────────────────────────────────
class SettingsDialog(QDialog):
    """Data-driven settings editor (collapses the two PowerShell dialogs into one).
    In first-run mode it shows a welcome header and all apps checked by default."""

    saved = Signal(object)   # emits updated Settings

    def __init__(self, settings: Settings, first_run: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("EDLS — First-Time Setup" if first_run else "EDLS — Settings")
        self.setModal(True)
        self.setMinimumSize(620, 500)
        # The dialog follows the active theme: a dark amber popover in dark mode,
        # a light slate-on-grey one in day mode. (_fg = body text, _amber = accent,
        # _gold = highlight.)
        if settings.theme is Theme.LIGHT:
            _bg, _fg, _field, _border = "#E6E9ED", "#252C36", "#FFFFFF", "#B4BCC6"
            _amber, _gold, _field_fg = "#C25E0A", "#C25E0A", "#252C36"
            _head_bg, _head_fg, _grid, _btn_bg = "#DCE0E5", "#5A6573", "#D0D5DB", "#DCE0E5"
        else:
            _bg, _fg, _field, _border = "#0C0C0F", "#C8760A", "#0A0A0D", "#252530"
            _amber, _gold, _field_fg = "#C8760A", "#FFB700", "#FFB700"
            _head_bg, _head_fg, _grid, _btn_bg = "#111114", "#666670", "#1A1A20", "#111114"
        self.setStyleSheet(f"""
            QDialog   {{ background: {_bg}; color: {_fg}; }}
            QLabel    {{ color: {_fg}; }}
            QLineEdit {{ background: {_field}; color: {_field_fg};
                         border: 1px solid {_border}; padding: 4px 6px; }}
            QCheckBox {{ color: {_fg}; spacing: 6px; }}
            QCheckBox::indicator {{ width:13px; height:13px; border:1px solid {_border};
                                    background:{_field}; }}
            QCheckBox::indicator:checked {{ background: {_amber}; }}
            QTableWidget {{ background:{_field}; color:{_fg};
                            gridline-color:{_grid}; border:1px solid {_border}; }}
            QHeaderView::section {{ background:{_head_bg}; color:{_head_fg};
                                    border:none; padding:4px; font-size:9px; }}
            QPushButton {{ background:{_btn_bg}; color:{_fg};
                           border:1px solid {_border}; padding:4px 12px; }}
            QPushButton:hover {{ border-color:{_amber}; color:{_gold}; }}
        """)

        vbox = QVBoxLayout(self)
        vbox.setSpacing(12)
        vbox.setContentsMargins(20, 18, 20, 16)

        if first_run:
            welcome = QLabel("Welcome to EDLS, CMDR")
            welcome.setFont(font_display(14))
            welcome.setStyleSheet(f"color: {_gold};")
            vbox.addWidget(welcome)
            desc = QLabel(
                "Select which companion apps to enable on launch. "
                "You can add or remove them at any time in Settings.\n\n"
                "Please report any crashes using the 'report issue' link. o7"
            )
            desc.setWordWrap(True)
            desc.setFont(font_mono(10))
            desc.setStyleSheet(f"color: {_fg};")
            vbox.addWidget(desc)
        else:
            hdr = QLabel("S E T T I N G S")
            hdr.setFont(font_display(9))
            hdr.setStyleSheet(f"color: {C_TDIM.name()}; letter-spacing:3px;")
            vbox.addWidget(hdr)

        # CMDR name
        cmdr_row = QHBoxLayout()
        cmdr_row.addWidget(QLabel("CMDR NAME"))
        self.cmdr_edit = QLineEdit(settings.cmdr_name)
        cmdr_row.addWidget(self.cmdr_edit, 1)
        vbox.addLayout(cmdr_row)

        # Boolean options as sliding toggles (label left, switch right).
        def _toggle_row(text: str, checked: bool) -> ToggleSwitch:
            row = QHBoxLayout()
            row.addWidget(QLabel(text))
            row.addStretch()
            tog = ToggleSwitch(checked)
            row.addWidget(tog)
            vbox.addLayout(row)
            return tog

        self.toggle_inactive = _toggle_row(
            "Show status cards for inactive apps",
            settings.show_inactive_cards)
        self.toggle_scope = _toggle_row(
            "Shutdown closes all enabled apps (not just those EDLS launched)",
            settings.shutdown_scope is ShutdownScope.ALL_ENABLED)
        self.toggle_light = _toggle_row(
            "Light theme (day mode for washed-out monitors)",
            settings.theme is Theme.LIGHT)

        # App table
        self.table = QTableWidget(len(settings.apps), 4)
        self.table.setHorizontalHeaderLabels(["ON", "NAME", "PROCESS", "PATH"])
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked)
        self.table.verticalHeader().setVisible(False)
        for r, app in enumerate(settings.apps):
            chk = QCheckBox()
            chk.setChecked(app.enabled)
            chk_cell = QWidget()
            chk_layout = QHBoxLayout(chk_cell)
            chk_layout.addWidget(chk)
            chk_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chk_layout.setContentsMargins(0, 0, 0, 0)
            self.table.setCellWidget(r, 0, chk_cell)
            self.table.setItem(r, 1, QTableWidgetItem(app.name))
            self.table.setItem(r, 2, QTableWidgetItem(app.process_name))
            self.table.setItem(r, 3, QTableWidgetItem(app.path or ""))
        vbox.addWidget(self.table, 1)

        # add / remove buttons
        tbl_btns = QHBoxLayout()
        add_btn = QPushButton("+ ADD")
        rem_btn = QPushButton("— REMOVE")
        add_btn.clicked.connect(self._add_row)
        rem_btn.clicked.connect(self._remove_row)
        tbl_btns.addWidget(add_btn)
        tbl_btns.addWidget(rem_btn)
        tbl_btns.addStretch()
        vbox.addLayout(tbl_btns)

        # OK / Cancel
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save |
            QDialogButtonBox.StandardButton.Cancel)
        btn_box.accepted.connect(self._save)
        btn_box.rejected.connect(self.reject)
        vbox.addWidget(btn_box)

    def _add_row(self) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        chk = QCheckBox()
        chk.setChecked(True)
        cell = QWidget()
        cl = QHBoxLayout(cell)
        cl.addWidget(chk)
        cl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cl.setContentsMargins(0, 0, 0, 0)
        self.table.setCellWidget(r, 0, cell)
        self.table.setItem(r, 1, QTableWidgetItem("NewApp"))
        self.table.setItem(r, 2, QTableWidgetItem("NewApp"))
        self.table.setItem(r, 3, QTableWidgetItem(""))

    def _remove_row(self) -> None:
        rows = self.table.selectedIndexes()
        if rows:
            self.table.removeRow(rows[0].row())

    def _save(self) -> None:
        def _text(row: int, col: int) -> str:
            item = self.table.item(row, col)
            return item.text().strip() if item else ""

        new_apps: list[AppDescriptor] = []
        for r in range(self.table.rowCount()):
            cell = self.table.cellWidget(r, 0)
            chk = cell.findChild(QCheckBox) if cell else None
            enabled = chk.isChecked() if chk else True

            name = _text(r, 1)
            if not name:
                continue
            reg = config._REGISTRY_BY_NAME.get(name)
            new_apps.append(AppDescriptor(
                name=name,
                process_name=_text(r, 2) or name,
                path=_text(r, 3) or (reg.path if reg else None),
                enabled=enabled,
                resolver=reg.resolver if reg else None,
            ))

        removed = config.recompute_removed_defaults(
            new_apps, self._settings.removed_defaults)

        updated = replace(
            self._settings,
            cmdr_name=self.cmdr_edit.text().strip() or self._settings.cmdr_name,
            show_inactive_cards=self.toggle_inactive.isChecked(),
            shutdown_scope=(ShutdownScope.ALL_ENABLED
                            if self.toggle_scope.isChecked()
                            else ShutdownScope.LAUNCHED_ONLY),
            theme=Theme.LIGHT if self.toggle_light.isChecked() else Theme.DARK,
            apps=new_apps,
            removed_defaults=removed,
            setup_complete=True,
        )
        self.saved.emit(updated)
        self.accept()


# ── Main window ───────────────────────────────────────────────────────────────
# Subdued boot animation: each panel fades up a few px, staggered. Mirrors the
# mockup's `bootIn` keyframe. Set BOOT_ENABLED = False for an instant show
# (a stand-in for prefers-reduced-motion, which Qt has no native signal for).
BOOT_ENABLED  = True
BOOT_DURATION = 320   # ms per panel
BOOT_OFFSET   = 6     # px upward rise
BOOT_STAGGER  = 60    # ms between panels
BOOT_TOTAL    = BOOT_STAGGER * 3 + BOOT_DURATION


class MainWindow(QMainWindow):
    def __init__(self, engine, settings: Settings) -> None:
        super().__init__()
        self._engine = engine
        self._settings = settings

        self.setWindowTitle("Elite: Dangerous · Launch Suite")
        self.setMinimumSize(900, 800)
        self.resize(1000, 880)

        # Wire engine.dispatch to the GUI thread
        self._invoker = _Invoker()
        engine.dispatch = self._invoker.invoke
        self._busy = False   # tracked so a theme rebuild can restore button state

        # Background persists across theme switches (it repaints from the themed
        # globals); only the central content is rebuilt on a theme change.
        self._bg = Background(self)
        self._bg.setGeometry(self.rect())

        # Engine signals are connected ONCE, to wrappers that delegate to the
        # current child widgets — so rebuilding content (theme switch) doesn't
        # leave dangling connections to deleted panels.
        engine.on_log.connect(self._on_log)
        engine.on_status.connect(self._on_status)
        engine.on_busy.connect(self._on_busy)

        self._build_content()

        self._log.append("Elite: Dangerous Launch Suite ready.", "success")
        if settings.auto_start:
            self._log.append("Auto-start enabled — launching...", "dim")

    def _build_content(self) -> None:
        """Build (or rebuild) the central widget and its panels from the current
        settings + active theme. Replacing the central widget deletes the old one
        and its children, so a theme switch is just: apply_theme() then this."""
        central = QWidget()
        self.setCentralWidget(central)
        central.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        central.setStyleSheet("background: transparent;")

        root = QVBoxLayout(central)
        root.setSpacing(12)
        root.setContentsMargins(18, 18, 18, 14)

        # Header
        self._header = HeaderPanel(self._settings.cmdr_name)
        root.addWidget(self._header)

        # Status + Log in a draggable splitter
        self._splitter = QSplitter(Qt.Orientation.Vertical)
        self._splitter.setHandleWidth(8)
        self._splitter.setStyleSheet(
            "QSplitter::handle:vertical { background: transparent; }"
        )
        self._status = StatusPanel(
            [a for a in self._settings.apps if a.enabled])
        self._status.start_stop_clicked.connect(self._on_start_stop)
        self._log = LogPanel()
        self._splitter.addWidget(self._status)
        self._splitter.addWidget(self._log)
        self._splitter.setStretchFactor(0, 3)
        self._splitter.setStretchFactor(1, 1)
        root.addWidget(self._splitter, 1)

        # Button bar
        self._btns = ButtonBar(config.APP_VERSION, self._settings.auto_start)
        self._btns.shutdown_clicked.connect(self._engine.manual_shutdown)
        self._btns.autostart_toggled.connect(self._on_autostart)
        self._btns.settings_clicked.connect(self._open_settings)
        self._btns.launch_clicked.connect(self._engine.launch)
        self._btns.set_busy(self._busy)
        root.addWidget(self._btns)

    def _play_boot_animation(self) -> None:
        """Staggered fade-up of the four panels. Runs once, after the layout has
        positioned everything (called via a 0ms timer post-show). The opacity
        effect is removed on completion so it can't dull later text rendering."""
        if not BOOT_ENABLED:
            return
        panels = [self._header, self._status, self._log, self._btns]
        self._boot_anims: list[QParallelAnimationGroup] = []

        for i, panel in enumerate(panels):
            effect = QGraphicsOpacityEffect(panel)
            effect.setOpacity(0.0)
            panel.setGraphicsEffect(effect)

            final_pos = panel.pos()
            start_pos = QPoint(final_pos.x(), final_pos.y() + BOOT_OFFSET)
            panel.move(start_pos)

            group = QParallelAnimationGroup(self)

            fade = QPropertyAnimation(effect, b"opacity", self)
            fade.setStartValue(0.0)
            fade.setEndValue(1.0)
            fade.setDuration(BOOT_DURATION)
            fade.setEasingCurve(QEasingCurve.Type.OutCubic)

            rise = QPropertyAnimation(panel, b"pos", self)
            rise.setStartValue(start_pos)
            rise.setEndValue(final_pos)
            rise.setDuration(BOOT_DURATION)
            rise.setEasingCurve(QEasingCurve.Type.OutCubic)

            group.addAnimation(fade)
            group.addAnimation(rise)

            # Drop the effect once done — QGraphicsOpacityEffect left in place can
            # disable subpixel text AA and add a tiny per-frame cost.
            group.finished.connect(lambda p=panel: p.setGraphicsEffect(None))

            self._boot_anims.append(group)
            QTimer.singleShot(i * BOOT_STAGGER, group.start)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._bg.setGeometry(self.rect())

    @Slot(str, str)
    def _on_log(self, message: str, level: str) -> None:
        self._log.append(message, level)

    @Slot(str, object, object)
    def _on_status(self, key: str, state: AppState,
                   pid: Optional[int]) -> None:
        self._status.update_card(key, state, pid)

    @Slot(bool)
    def _on_busy(self, busy: bool) -> None:
        self._busy = busy
        self._btns.set_busy(busy)

    @Slot(str, bool)
    def _on_start_stop(self, name: str, is_running: bool) -> None:
        if is_running:
            self._engine.stop_app(name)
        else:
            self._engine.start_app(name)

    @Slot(bool)
    def _on_autostart(self, on: bool) -> None:
        self._settings.auto_start = on
        config.save_settings(self._settings)

    def _open_settings(self) -> None:
        dlg = SettingsDialog(
            self._settings,
            first_run=not self._settings.setup_complete,
            parent=self,
        )
        dlg.saved.connect(self._on_settings_saved)
        dlg.exec()

    @Slot(object)
    def _on_settings_saved(self, updated: Settings) -> None:
        theme_changed = updated.theme != self._settings.theme
        self._settings = updated
        config.save_settings(updated)
        self._engine.settings = updated

        if theme_changed:
            # A theme switch must rebuild all content so every baked-in
            # stylesheet picks up the new palette. Carry the activity log across
            # the swap (its per-line accent colours are theme-independent).
            log_html = self._log.log.toHtml()
            apply_theme(updated.theme)
            _apply_app_palette()
            self._build_content()
            self._log.log.setHtml(log_html)
            sb = self._log.log.verticalScrollBar()
            sb.setValue(sb.maximum())
            self._bg.update()
            return

        self._header.set_cmdr(updated.cmdr_name)
        # Rebuild the status panel with the new app list
        old_status = self._status
        self._status = StatusPanel(
            [a for a in updated.apps if a.enabled])
        self._status.start_stop_clicked.connect(self._on_start_stop)
        self._splitter.replaceWidget(0, self._status)
        old_status.deleteLater()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._engine.close()
        super().closeEvent(event)


# ── Thread-safe dispatch invoker ──────────────────────────────────────────────
class _Invoker(QWidget):
    """Receives callables from worker threads and runs them on the GUI thread
    via Qt's signal/slot cross-thread mechanism."""
    _sig = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._sig.connect(self._run, Qt.ConnectionType.QueuedConnection)

    def invoke(self, fn) -> None:
        self._sig.emit(fn)

    @Slot(object)
    def _run(self, fn) -> None:
        fn()


# ── Public entry point ────────────────────────────────────────────────────────
def _apply_app_palette() -> None:
    """Push the active theme's structural colours into the QApplication palette
    (the default surface colour for any widget that doesn't style itself).
    Re-called on a live theme switch."""
    app = QApplication.instance()
    if app is None:
        return
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window,     C_DARK)
    pal.setColor(QPalette.ColorRole.WindowText, C_ORANGE)
    pal.setColor(QPalette.ColorRole.Base,       C_PANEL)
    pal.setColor(QPalette.ColorRole.Text,       C_AMBER)
    app.setPalette(pal)


def run(engine, settings: Settings) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Elite: Dangerous Launch Suite")
    _icon = os.path.join(ASSETS_DIR, "icon.ico")
    if os.path.exists(_icon):
        app.setWindowIcon(QIcon(_icon))

    # Activate the saved theme before any widget is built, then seed the global
    # palette from it.
    apply_theme(settings.theme)
    _apply_app_palette()

    _load_fonts()

    win = MainWindow(engine, settings)
    win.show()

    # Boot animation first (after layout settles), then any follow-on action.
    QTimer.singleShot(0, win._play_boot_animation)
    follow_delay = BOOT_TOTAL if BOOT_ENABLED else 0

    # Show first-run setup once the panels have animated in; otherwise honour
    # auto-start. Matches the original's Window.Add_Loaded sequencing.
    if not settings.setup_complete:
        QTimer.singleShot(follow_delay, win._open_settings)
    elif settings.auto_start:
        QTimer.singleShot(follow_delay, engine.launch)

    return app.exec()
