"""
EDLS/ui.py
PySide6 view layer in Qt.

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

Fonts: Euro Caps (display/body) and Share Tech Mono (terminal) are loaded from
assets/ if present, falling back to system fonts so the app is always runnable.

Contract with main.py:
    run(engine, settings) -> int
    engine.dispatch is set to a Qt-thread-safe invoker.
    engine.on_log.connect(...)       (message: str, level: str)
    engine.on_status.connect(...)    (key: str, state: AppState, pid: int | None)
    engine.on_busy.connect(...)      (busy: bool)
    engine.on_quit.connect(...)      ()  -- a full session ended; close the app
"""
from __future__ import annotations

import getpass
import os
import sys
import time
from dataclasses import replace
from typing import Optional

from PySide6.QtCore import (
    QEasingCurve, QEvent, QParallelAnimationGroup, QPoint, Property,
    QPropertyAnimation, QRect, QSize, Qt, QTimer, QUrl, Signal, Slot,
)
from PySide6.QtGui import (
    QBrush, QColor, QDesktopServices, QFont, QFontDatabase, QIcon,
    QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygon, QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication, QFrame, QGraphicsOpacityEffect,
    QHBoxLayout, QLabel, QMainWindow, QSizePolicy,
    QSpacerItem, QSplitter, QStyle, QStyleOptionButton, QStylePainter,
    QTextEdit, QVBoxLayout, QWidget, QPushButton,
    QDialog, QCheckBox, QLineEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QDialogButtonBox, QMessageBox,
    QFileDialog, QScrollArea,
)
from PySide6.QtNetwork import QLocalServer, QLocalSocket

import config
from config import (
    AppDescriptor, AppState, Settings, ShutdownScope, Theme,
    KEY_STEAM, KEY_ELITE,
)

import theme as _theme
from theme import (
    C_GOLD, C_DARK, C_PANEL, C_BORDER, C_AMBER, C_ORANGE, C_DIM,
    C_TMID, C_TDIM, C_GREEN, C_RED, C_INACT, C_NAME, C_CMDR,
    C_SUCCESS, C_LOGDIM, C_PANEL_T, CARD_BG, CARD_BORDER, CARD_BORDER_OFF,
    BG_IMAGE, BANNER_IMAGE, BANNER_FILL, BANNER_ACCENT,
    IS_LIGHT, CHAMFER_CARD, CHAMFER_PANEL, PULSE_MS,
    BOOT_DURATION, BOOT_OFFSET, BOOT_STAGGER,
    CARD_BORDER_ONLINE, CARD_BORDER_LAUNCHING, CARD_BORDER_FAILED,
    TOGGLE_OFF, TOGGLE_ON, TRACK_BANNER,
    font_display, font_body, font_mono, power_icon,
)

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
        AppState.ERROR:     (C_RED,    C_RED),
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
    _theme.apply_theme(theme)
    is_light = theme is Theme.LIGHT
    globals().update(
        _theme._LIGHT_PALETTE if is_light else _theme._DARK_PALETTE)
    globals()["IS_LIGHT"] = is_light
    _build_color_maps()


_build_color_maps()   # seed the maps for the default (dark) palette

# Friendly card labels that differ from the registry/identifier name. Presentation
# only — the descriptor.name is still the key for status routing and settings.
_DISPLAY_NAMES = {
    "OdysseyMaterials": "Odyssey Mats Helper",
}

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")

# How long the Elite card keeps START greyed after the game finishes starting
# or closing -- long enough to swallow an accidental double-click.
ELITE_HOLD_MS = 3000

# Where the "report issue" credit link points (the GitHub issues page).
ISSUES_URL = "https://github.com/coyoteware/EDLS-py/issues"


# ── Font helpers ──────────────────────────────────────────────────────────────
def _load_fonts() -> None:
    for name in ("EUROCAPS.TTF", "ShareTechMono-Regular.ttf"):
        path = os.path.join(ASSETS_DIR, name)
        if os.path.exists(path):
            QFontDatabase.addApplicationFont(path)




# ── ChamferedFrame ────────────────────────────────────────────────────────────
class ChamferedFrame(QFrame):
    """QFrame that clips itself to a polygon with a chamfered top-right corner,
    approximating the CSS clip-path used throughout the mockup. The clip size
    is configurable; pass `glow_color` to draw a coloured border."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        chamfer: int = CHAMFER_PANEL,
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
        # Canvas is larger than the 7px core so the emissive halo (drawn for live
        # states) has room to bleed — the HUD's "backlit display" look.
        self.setFixedSize(16, 16)
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
            self._anim.setDuration(PULSE_MS)
            self._anim.setEasingCurve(QEasingCurve.Type.SineCurve)
            self._anim.setLoopCount(-1)
            self._anim.start()
        else:
            self._opacity = 1.0
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        cx = cy = w / 2.0
        core = 7  # solid dot diameter (matches the old 7px core)

        # Emissive halo: a soft radial bloom in the dot colour, only for live
        # states (those that pulse). Modulated by the pulse opacity so the glow
        # breathes with the dot. Offline/failed dots stay flat.
        if self._anim is not None:
            grad = QRadialGradient(cx, cy, w / 2.0)
            c0 = QColor(self._color); c0.setAlphaF(0.65 * self._opacity)
            c1 = QColor(self._color); c1.setAlphaF(0.0)
            grad.setColorAt(0.0, c0)
            grad.setColorAt(1.0, c1)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(grad))
            p.drawEllipse(0, 0, w, w)

        color = QColor(self._color)
        color.setAlphaF(self._opacity)
        p.setBrush(QBrush(color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(int(cx - core / 2), int(cy - core / 2), core, core)
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
    """One app's status tile. Wired to engine.on_status. Every card — Steam and
    the game included — is the same tile with the same header rhythm."""

    start_stop_clicked = Signal(str, bool)   # (app_name, currently_running)
    notice = Signal(str)   # a greyed-out button was clicked: here's why

    def __init__(self, descriptor: AppDescriptor,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, chamfer=CHAMFER_CARD,
                         bg=CARD_BG, border=CARD_BORDER)
        self._name = descriptor.name
        self._state = AppState.OFFLINE
        # Set while the button is greyed out. It still takes clicks, so the
        # click can be answered in the activity log instead of silently ignored.
        self._blocked_reason: Optional[str] = None
        # The Elite card holds START back briefly after the game finishes
        # starting or closing, so a quick double-click can't launch it twice.
        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.timeout.connect(self._refresh_button)
        # An app the CMDR hasn't enabled still gets a card when they ask for one
        # (Settings → show cards for apps that aren't enabled). It's read-only:
        # dimmed, tagged OFF, and no START button, since LAUNCH won't touch it.
        self._inactive = not descriptor.enabled
        self.setMinimumHeight(90)
        # Capped so a sparse grid (one row) doesn't stretch the tiles tall.
        self.setMaximumHeight(110)

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
        name_col = C_TDIM if self._inactive else C_NAME
        name_lbl.setStyleSheet(
            f"color: {name_col.name()}; letter-spacing: 1px;")
        hdr.addWidget(self.dot)
        hdr.addWidget(name_lbl, 1)

        # state label — inline beside the name, right-justified. The PID line is
        # gone (PIDs are in the activity log) and the status sits up here beside
        # the name rather than on its own row, to cut a row of clutter.
        self.state_lbl = QLabel(AppState.OFFLINE.label)
        self.state_lbl.setStyleSheet(f"color: {C_INACT.name()};")
        self.state_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.state_lbl.setFont(font_mono(12))
        hdr.addWidget(self.state_lbl, 0, Qt.AlignmentFlag.AlignTop)

        vbox.addLayout(hdr)
        vbox.addStretch(1)

        # Bottom row: the control button, or — on a card the CMDR hasn't enabled
        # — an OFF tag in its place, so every card keeps the same height.
        self.ctrl_btn: Optional[QPushButton] = None
        if self._inactive:
            vbox.addWidget(make_badge("OFF", C_TDIM), 0,
                           Qt.AlignmentFlag.AlignLeft)
        else:
            self.ctrl_btn = QPushButton("START")
            self.ctrl_btn.setFont(font_display(7))
            self.ctrl_btn.setFixedHeight(20)
            self.ctrl_btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            self.ctrl_btn.setStyleSheet(_btn_ctrl_style(active=False))
            self.ctrl_btn.clicked.connect(self._on_ctrl)
            vbox.addWidget(self.ctrl_btn)

        self.update_state(AppState.OFFLINE, None)

    def _on_ctrl(self) -> None:
        if self._blocked_reason:
            self.notice.emit(self._blocked_reason)
            return
        self.start_stop_clicked.emit(self._name, self._state.is_running)

    @property
    def state(self) -> AppState:
        return self._state

    def update_state(self, state: AppState, pid: Optional[int]) -> None:
        prev, self._state = self._state, state
        label_col, dot_col = STATE_COLORS.get(state, (C_INACT, C_INACT))
        pulse = state in (AppState.ONLINE, AppState.RUNNING,
                          AppState.LAUNCHING, AppState.WAITING)

        self.dot.set_color(dot_col, pulse=pulse)
        self.state_lbl.setText(state.label)
        self.state_lbl.setStyleSheet(f"color: {label_col.name()};")

        if (self._name == KEY_ELITE and not state.is_running
                and state is not AppState.WAITING
                and prev in (AppState.WAITING, AppState.RUNNING)):
            self._hold.start(ELITE_HOLD_MS)
        self._refresh_button()

        # update border colour to match state
        if state in (AppState.ONLINE, AppState.RUNNING):
            self._border = CARD_BORDER_ONLINE
        elif state in (AppState.LAUNCHING, AppState.WAITING):
            self._border = CARD_BORDER_LAUNCHING
        elif state in (AppState.FAILED, AppState.ERROR):
            self._border = CARD_BORDER_FAILED
        else:
            self._border = CARD_BORDER_OFF
        self.update()

    def _refresh_button(self) -> None:
        """Set the control button's label, look, and (for a greyed-out button)
        the reason a click gets answered with."""
        if self.ctrl_btn is None:          # not-enabled card: OFF tag, no button
            return
        state = self._state
        reason: Optional[str] = None
        if self._name == KEY_ELITE:
            if state is AppState.WAITING:
                text, reason = "STARTING", "The game is already starting."
            elif state.is_running:
                # EDLS never closes the game (ToS) -- no STOP on this card.
                text, reason = "QUIT IN-GAME", (
                    "EDLS doesn't close Elite: Dangerous - quit from the "
                    "in-game menu.")
            elif self._hold.isActive():
                text, reason = "START", (
                    "Give the game a moment before starting it again.")
            else:
                text = "START"
            style = _btn_ctrl_style(muted=reason is not None)
        elif state is AppState.ERROR:
            text, style = "ERROR", _btn_ctrl_style(error=True)
        else:
            text = "STOP" if state.is_running else "START"
            style = _btn_ctrl_style(active=state.is_running)
        self._blocked_reason = reason
        self.ctrl_btn.setText(text)
        self.ctrl_btn.setStyleSheet(style)


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
        """ + _thin_scrollbar_qss())

    def append_line(self, message: str, level: str = "default") -> None:
        color = LOG_COLORS.get(level, C_AMBER)
        ts = time.strftime("[%H:%M:%S]")
        ts_html = f'<span style="color:{C_TMID.name()};">{ts}</span>'
        msg_html = f'<span style="color:{color.name()};">&nbsp;{_esc(message)}</span>'
        self.append(f"{ts_html}{msg_html}")
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())


def _thin_scrollbar_qss() -> str:
    """The slim HUD scrollbar shared by the activity log and the card grid."""
    return f"""
        QScrollBar:vertical {{ background: transparent; width: 4px; }}
        QScrollBar::handle:vertical {{
            background: {C_DIM.name()}; border-radius: 2px; min-height: 20px;
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0;
        }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
            background: none;
        }}
    """


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
                 chamfer: int = CHAMFER_CARD, **colors) -> None:
        super().__init__(text, parent)
        self._ch = chamfer
        self._c: dict = {}
        # Muted = drawn greyed out but still clickable, so the owner can answer
        # the click (e.g. explain in the log) instead of it vanishing.
        self._muted = False
        self.set_colors(**colors)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        # Tab reaches it (Space presses it); a mouse click doesn't take focus,
        # so the focus outline only appears for keyboard users.
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    def set_muted(self, muted: bool) -> None:
        self._muted = muted
        self.update()

    @property
    def muted(self) -> bool:
        return self._muted

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
        en    = self.isEnabled() and not self._muted
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
        if self.hasFocus():
            # Keyboard focus: an inset outline that follows the chamfer.
            ring = QPainterPath()
            ring.addPolygon(self._poly(rect.adjusted(3, 3, -3, -3)))
            ring.closeSubpath()
            p.setPen(QPen(c["bd_h"], 1, Qt.PenStyle.DashLine))
            p.drawPath(ring)
        p.end()

        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        pal = QPalette(self.palette())
        pal.setColor(QPalette.ColorRole.ButtonText, fg)
        opt.palette = pal
        sp = QStylePainter(self)
        sp.drawControl(QStyle.ControlElement.CE_PushButtonLabel, opt)
        sp.end()


def _btn_ctrl_style(active: bool = False, *, muted: bool = False,
                    error: bool = False) -> str:
    """Card button QSS. active = STOP (green), muted = greyed out (still
    clickable, so the click can be explained), error = couldn't close (red)."""
    focus = "#C25E0A" if IS_LIGHT else "#FF8C1A"   # keyboard focus outline
    base = ("QPushButton {{ font-family: 'Euro Caps'; font-size: 7px;"
            " font-weight:700; letter-spacing:1px; border:1px solid {bd};"
            " color:{fg}; background:{bg}; padding: 0 4px; }}"
            " QPushButton:hover {{ border-color:{bdh}; color:{fgh}; }}"
            " QPushButton:focus {{ border:1px dashed " + focus + "; }}")
    if IS_LIGHT:
        # Bright buttons for the light cards: filled green STOP, light grey START.
        if error:
            return base.format(bd="#C0322B", fg="#A0261F", bg="#F6D5D3",
                               bdh="#8E1F19", fgh="#8E1F19")
        if muted:
            return base.format(bd="#D0D5DB", fg="#A3ABB5", bg="#EEF0F3",
                               bdh="#D0D5DB", fgh="#A3ABB5")
        if active:
            return base.format(bd="#3FA85F", fg="#1C6B36", bg="#CDEED7",
                               bdh="#2E9E55", fgh="#15592C")
        return base.format(bd="#B4BCC6", fg="#5A6573", bg="#E9EDF1",
                           bdh="#C25E0A", fgh="#C25E0A")
    # Dark theme (original).
    if error:
        return base.format(bd="#CC3D3D", fg="#CC3D3D", bg="#1A0A0A",
                           bdh="#FF5555", fgh="#FF6666")
    if muted:
        return base.format(bd="#181818", fg="#303030", bg="#0A0A0A",
                           bdh="#181818", fgh="#303030")
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


# Filled-badge text colour: always the canonical near-black so dark text stays
# legible on any accent fill, in either theme (gold/orange never invert well to
# the themed C_DARK). Outline badges use the accent itself.
_BADGE_FILL_FG = "#090905"


def make_badge(text: str, color: QColor, *, filled: bool = False) -> QLabel:
    """The design-system Badge: a small bordered, tracked-caps HUD tag (the CMDR
    identity tag is the canonical use). Outline by default; `filled` inverts to
    dark text on the accent `color`. Tracking/padding mirror --tracking-wider
    (0.14em) and the 2px/8px badge box."""
    lbl = QLabel(text.upper())
    f = font_display(7, QFont.Weight.DemiBold)
    f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 114)  # --tracking-wider
    lbl.setFont(f)
    fg = _BADGE_FILL_FG if filled else color.name()
    bg = color.name() if filled else "transparent"
    lbl.setStyleSheet(
        f"color:{fg}; background:{bg}; border:1px solid {color.name()};"
        " padding:2px 8px;")
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Maximum)
    return lbl


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
        self._banner = QPixmap(os.path.join(ASSETS_DIR, BANNER_IMAGE))
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
        r = self.rect()
        p.fillRect(r, BANNER_FILL)
        p.fillRect(r.left(), r.top(), r.width(), 2, BANNER_ACCENT)
        if not self._outpost.isNull():
            icon = self._outpost.scaledToHeight(int(r.height() * 0.60),
                       Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap(22, (r.height() - icon.height()) // 2, icon)
        f = font_display(13, QFont.Weight.ExtraBold)
        f.setPixelSize(max(20, int(r.height() * 0.46)))
        f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, TRACK_BANNER)
        f.setKerning(False)   # even tracked caps; stop the font's LA pair from fusing
        p.setFont(f)
        p.setPen(BANNER_ACCENT)
        p.drawText(r, Qt.AlignmentFlag.AlignCenter,
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

        # CMDR row, framed like the System Status / Activity Log panes
        cmdr_frame = ChamferedFrame(chamfer=CHAMFER_PANEL, bg=C_PANEL_T)
        cmdr_row = QHBoxLayout(cmdr_frame)
        cmdr_row.setContentsMargins(14, 6, 14, 6)
        cmdr_row.setSpacing(10)
        cmdr_row.addStretch()

        badge = make_badge("CMDR", C_AMBER)
        cmdr_row.addWidget(badge)

        self.cmdr_lbl = QLabel(cmdr_name.upper())
        self.cmdr_lbl.setFont(font_body(12, QFont.Weight.DemiBold))
        self.cmdr_lbl.setStyleSheet(
            f"color: {C_CMDR.name()}; letter-spacing: 4px;")
        cmdr_row.addWidget(self.cmdr_lbl)
        cmdr_row.addStretch()
        vbox.addWidget(cmdr_frame)

    def set_cmdr(self, name: str) -> None:
        self.cmdr_lbl.setText(name.upper())


# ── Status panel ──────────────────────────────────────────────────────────────
class StatusPanel(ChamferedFrame):
    start_stop_clicked = Signal(str, bool)
    notice = Signal(str)   # forwarded from the cards

    def __init__(self, apps: list[AppDescriptor],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, chamfer=CHAMFER_PANEL, bg=C_PANEL_T)
        self.cards: dict[str, StatusCard] = {}

        vbox = QVBoxLayout(self)
        vbox.setSpacing(8)
        vbox.setContentsMargins(14, 10, 14, 10)

        lbl = QLabel("S Y S T E M   S T A T U S")
        lbl.setFont(font_display(7))
        lbl.setStyleSheet(f"color: {C_TMID.name()}; letter-spacing: 3px;")
        vbox.addWidget(lbl)

        # The card grid scrolls, so the panel can shrink to about one row on a
        # small screen (1024x768) however many apps there are. Everything in
        # it is transparent: the panel paints its own chamfered background.
        scroll = QScrollArea()
        scroll.setObjectName("cardScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumHeight(100)          # one card row
        host = QWidget()
        host.setObjectName("cardGrid")
        scroll.setWidget(host)
        scroll.viewport().setAutoFillBackground(False)
        scroll.setStyleSheet(
            "#cardScroll, #cardGrid, #qt_scrollarea_viewport"
            " { background: transparent; }" + _thin_scrollbar_qss())
        vbox.addWidget(scroll, 1)
        host_box = QVBoxLayout(host)
        host_box.setContentsMargins(0, 0, 0, 0)

        # card grid: 4 columns
        from PySide6.QtWidgets import QGridLayout
        grid = QGridLayout()
        grid.setSpacing(8)

        col, row = 0, 0

        def _add(card: StatusCard) -> None:
            nonlocal col, row
            grid.addWidget(card, row, col)
            card.start_stop_clicked.connect(self.start_stop_clicked)
            card.notice.connect(self.notice)
            col += 1
            if col >= 4:
                col = 0
                row += 1

        # Steam and the game get the same tile as every companion app — they
        # used to be special-cased (Steam single, Elite double-wide).
        for key in (KEY_STEAM, KEY_ELITE):
            card = StatusCard(config.SYSTEM_APPS[key])
            self.cards[key] = card
            _add(card)

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

        host_box.addLayout(grid)
        # leftover height sits below the grid; cards stay pinned to the top
        host_box.addStretch(1)

    def update_card(self, key: str, state: AppState,
                    pid: Optional[int]) -> None:
        card = self.cards.get(key)
        if card:
            card.update_state(state, pid)





# ── Log panel ─────────────────────────────────────────────────────────────────
class LogPanel(ChamferedFrame):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, chamfer=CHAMFER_PANEL, bg=C_PANEL_T)
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
        l1 = QLabel("CMDR Coyote Bongwater")
        l1.setFont(font_mono(9))
        l1.setStyleSheet(f"color: {C_TDIM.name()};")
        cred_vbox.addWidget(l1)

        # Version chip (gold filled badge) + report-issue link. C_SUCCESS is the
        # *themed* gold — gold on dark, orange in day mode — matching the design's
        # "gold reads as orange on light surfaces" rule for the accent.
        ver_row = QHBoxLayout()
        ver_row.setSpacing(8)
        ver_row.addWidget(make_badge(version, C_SUCCESS, filled=True))
        issue = QLabel(f"<a href='{ISSUES_URL}' "
                       f"style='color:{C_TMID.name()};'>report issue</a>")
        issue.setFont(font_mono(9))
        issue.setStyleSheet(f"color: {C_TDIM.name()};")
        issue.setOpenExternalLinks(True)
        # Reachable with Tab, opened with Enter -- not just a mouse target.
        issue.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction)
        ver_row.addWidget(issue)
        ver_row.addStretch()
        cred_vbox.addLayout(ver_row)
        hbox.addLayout(cred_vbox)
        hbox.addStretch()

        self.shutdown_btn  = ChamferedButton("SHUTDOWN",   chamfer=CHAMFER_CARD, **_C_DANGER)
        self.autostart_btn = ChamferedButton("AUTO START", chamfer=CHAMFER_CARD,
                                             **(_C_SECONDARY_ON if auto_start else _C_SECONDARY))
        self.settings_btn  = ChamferedButton("SETTINGS",  chamfer=CHAMFER_CARD, **_C_SECONDARY)
        self.launch_btn    = ChamferedButton("LAUNCH",    chamfer=CHAMFER_CARD, **_C_LAUNCH)

        self.shutdown_btn.setFont(font_display(9))
        self.autostart_btn.setFont(font_display(9))
        self.settings_btn.setFont(font_display(9))
        self.launch_btn.setFont(font_display(11))

        self.shutdown_btn.setIcon(power_icon(QColor("#B85555")))
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
        # 3-4 px around the 48x24 track leaves room for the focus ring.
        self.setFixedSize(56, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._kb_focus = False
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

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.setChecked(not self._checked)
        elif key == Qt.Key.Key_Left:
            self.setChecked(False)
        elif key == Qt.Key.Key_Right:
            self.setChecked(True)
        else:
            super().keyPressEvent(event)

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        # Ring only for keyboard focus -- not after every mouse click.
        self._kb_focus = event.reason() in (
            Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
            Qt.FocusReason.ShortcutFocusReason)
        self.update()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self.update()

    def _get_pos(self) -> float:
        return self._pos

    def _set_pos(self, v: float) -> None:
        self._pos = v
        self.update()

    knobPos = Property(float, _get_pos, _set_pos)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.hasFocus() and self._kb_focus:
            ring = self.rect().adjusted(1, 1, -1, -1)
            p.setPen(QPen(TOGGLE_ON, 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(ring, ring.height() / 2.0, ring.height() / 2.0)
        r = self.rect().adjusted(4, 3, -4, -3)   # the 48x24 track
        radius = r.height() / 2.0

        off, on = TOGGLE_OFF, TOGGLE_ON
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


def _derive_process(path: str) -> str:
    """The process name EDLS watches/kills is the exe's filename stem — that's
    what psutil reports as proc.name(). So for a user-added app we derive it from
    the chosen file instead of making them type it. Empty path -> empty string."""
    base = os.path.basename(path.rstrip("\\/"))
    return os.path.splitext(base)[0]


# ── Add / edit app dialog ─────────────────────────────────────────────────────
class AppEntryDialog(QDialog):
    """Modal editor for a single companion app: a Name plus a browsable file
    path. The process name is derived from the file (see `_derive_process`), so
    it isn't shown here. Reuses the parent SettingsDialog's stylesheet so it
    picks up the active theme with no duplicated QSS."""

    def __init__(self, name: str = "", path: str = "", *,
                 taken_names: Optional[list[str]] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setModal(True)
        self.setWindowTitle("Edit app" if name else "Add app")
        self.setMinimumWidth(460)
        # Names already in use (case-insensitive) — a name is the key for status
        # routing and launch tracking, so duplicates would collide silently.
        self._taken = {n.strip().lower() for n in (taken_names or [])}
        if parent is not None:
            self.setStyleSheet(parent.styleSheet())

        vbox = QVBoxLayout(self)
        vbox.setSpacing(12)
        vbox.setContentsMargins(20, 18, 20, 16)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("NAME"))
        self.name_edit = QLineEdit(name)
        name_row.addWidget(self.name_edit, 1)
        vbox.addLayout(name_row)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("FILE"))
        self.path_edit = QLineEdit(path)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse_btn)
        vbox.addLayout(path_row)

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save |
            QDialogButtonBox.StandardButton.Cancel)
        btn_box.accepted.connect(self._on_accept)
        btn_box.rejected.connect(self.reject)
        vbox.addWidget(btn_box)

    def _browse(self) -> None:
        start = os.path.dirname(self.path_edit.text().strip())
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Select application", start,
            "Executables (*.exe);;All files (*)")
        if chosen:
            self.path_edit.setText(os.path.normpath(chosen))

    def _validation_error(self, name: str) -> Optional[str]:
        """Reason the entry can't be saved, or None if it's valid. Split out from
        the message box so it's unit-testable without a modal popup."""
        if not name:
            return "Please enter a name for the app."
        if name.lower() in self._taken:
            return (f"An app named '{name}' already exists.\n"
                    "Please choose a different name.")
        return None

    def _on_accept(self) -> None:
        err = self._validation_error(self.name_edit.text().strip())
        if err:
            QMessageBox.warning(self, "Invalid entry", err)
            return
        self.accept()

    def values(self) -> tuple[str, str]:
        """(name, path), both stripped."""
        return self.name_edit.text().strip(), self.path_edit.text().strip()


# ── Settings dialog ───────────────────────────────────────────────────────────
class SettingsDialog(QDialog):
    """Data-driven settings editor (collapses the two dialogs into one).
    In first-run mode it shows a welcome header and all apps checked by default."""

    saved = Signal(object)   # emits updated Settings
    locked_attempt = Signal()   # tried to edit the app list during a session

    # Shown when the app list is touched while a LAUNCH session runs. The list
    # decides what the session launches and later closes, so it can't change
    # under a running session.
    LOCKED_MSG = ("The companion app list is locked while a session is running."
                  "\n\nIt unlocks when the game closes, or when you restart EDLS.")

    def __init__(self, settings: Settings, first_run: bool = False,
                 locked: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._locked = locked
        self._lock_reported = False
        self.setWindowTitle("EDLS — First-Time Setup" if first_run else "EDLS — Settings")
        self.setModal(True)
        self.setMinimumSize(620, 500)
        # The dialog follows the active theme: a dark amber popover in dark mode,
        # a light slate-on-grey one in day mode. (_fg = body text, _amber = accent,
        # _gold = highlight.)
        if settings.theme is Theme.LIGHT:
            _bg, _fg, _field, _border = "#E6E9ED", "#252C36", "#FFFFFF", "#B4BCC6"
            _amber, _gold, _field_fg = "#C25E0A", "#C25E0A", "#252C36"
            _head_fg, _btn_bg = "#5A6573", "#DCE0E5"
            _alt, _sel = "#EFF1F4", "#F0E3CC"
        else:
            _bg, _fg, _field, _border = "#0C0C0F", "#C8760A", "#0A0A0D", "#252530"
            _amber, _gold, _field_fg = "#C8760A", "#FFB700", "#FFB700"
            _head_fg, _btn_bg = "#666670", "#111114"
            _alt, _sel = "#101014", "#1C1408"
        # Dim brush for the secondary (process/path) cells, so the app NAME reads
        # as primary and the rest recedes — mirrors the design's name/proc split.
        self._row_dim = QColor(_head_fg)
        self.setStyleSheet(f"""
            QDialog   {{ background: {_bg}; color: {_fg}; }}
            QLabel    {{ color: {_fg}; }}
            QLineEdit {{ background: {_field}; color: {_field_fg};
                         border: 1px solid {_border}; padding: 4px 6px; }}
            QCheckBox {{ color: {_fg}; spacing: 6px; }}
            QCheckBox::indicator {{ width:14px; height:14px; border:1px solid {_border};
                                    background:{_field}; }}
            QCheckBox::indicator:hover {{ border-color:{_amber}; }}
            QCheckBox::indicator:checked {{ background:{_amber}; border-color:{_amber}; }}
            QTableWidget {{ background:{_field}; color:{_fg};
                            alternate-background-color:{_alt};
                            gridline-color:transparent; border:1px solid {_border};
                            outline:none; }}
            QTableWidget::item {{ padding:6px 8px; border:none; }}
            QTableWidget::item:selected {{ background:{_sel}; color:{_gold}; }}
            QTableCornerButton::section {{ background:{_field}; border:none; }}
            QPushButton {{ background:{_btn_bg}; color:{_fg};
                           border:1px solid {_border}; padding:4px 12px; }}
            QPushButton:hover {{ border-color:{_amber}; color:{_gold}; }}
            QPushButton:focus {{ border:1px dashed {_amber}; }}
            QLineEdit:focus {{ border-color:{_amber}; }}
            QTableWidget:focus {{ border:1px solid {_amber}; }}
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
            "Show cards for apps that aren't enabled",
            settings.show_inactive_cards)
        self.toggle_scope = _toggle_row(
            "Shutdown closes all enabled apps (not just those EDLS launched)",
            settings.shutdown_scope is ShutdownScope.ALL_ENABLED)
        self.toggle_close_exit = _toggle_row(
            "Close EDLS when the game exits",
            settings.close_on_game_exit)
        self.toggle_close_shutdown = _toggle_row(
            "Close EDLS after SHUTDOWN",
            settings.close_on_shutdown)
        self.toggle_light = _toggle_row(
            "Light mode",
            settings.theme is Theme.LIGHT)

        # Companion-app list. Styled as a flat HUD row-list rather than a grid:
        # no column headers, no gridlines, zebra rows, hairline container.
        apps_lbl = QLabel("C O M P A N I O N   A P P S")
        apps_lbl.setFont(font_display(8))
        apps_lbl.setStyleSheet(f"color: {_head_fg}; letter-spacing:3px;")
        if locked:
            apps_hdr = QHBoxLayout()
            apps_hdr.addWidget(apps_lbl)
            apps_hdr.addStretch()
            lock_lbl = QLabel("LOCKED WHILE A SESSION IS RUNNING")
            lock_lbl.setFont(font_display(7))
            lock_lbl.setStyleSheet(f"color: {_amber}; letter-spacing:2px;")
            apps_hdr.addWidget(lock_lbl)
            vbox.addLayout(apps_hdr)
        else:
            vbox.addWidget(apps_lbl)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["ON", "NAME", "PROCESS", "PATH"])
        hh = self.table.horizontalHeader()
        hh.setVisible(False)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnHidden(2, True)   # PROCESS is derived, not user-facing
        self.table.setShowGrid(False)
        self.table.setFrameShape(QFrame.Shape.NoFrame)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        # Rows are edited through AppEntryDialog (add / double-click / EDIT), not
        # inline, so the file path gets a Browse button and the process name is
        # derived rather than hand-typed.
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.cellDoubleClicked.connect(
            lambda row, _col: self._edit_row(row))
        # Keyboard: Space ticks ON, Enter edits, Delete removes (see eventFilter).
        self.table.installEventFilter(self)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self._populate_table(settings.apps)
        vbox.addWidget(self.table, 1)

        # add / remove buttons
        tbl_btns = QHBoxLayout()
        add_btn = QPushButton("+ ADD")
        edit_btn = QPushButton("EDIT")
        # The pencil is drawn as a coloured icon (via 'Segoe UI Symbol') rather
        # than a text glyph: in the button's inherited font U+270E falls back to
        # an emoji font that ignores the QSS colour and clashes with the theme.
        edit_btn.setIcon(_glyph_icon("✎", QColor(_fg), px=14))
        edit_btn.setIconSize(QSize(14, 14))
        rem_btn = QPushButton("— REMOVE")
        add_btn.clicked.connect(self._add_row)
        edit_btn.clicked.connect(self._edit_selected)
        rem_btn.clicked.connect(self._remove_row)
        tbl_btns.addWidget(add_btn)
        tbl_btns.addWidget(edit_btn)
        tbl_btns.addWidget(rem_btn)
        tbl_btns.addStretch()
        vbox.addLayout(tbl_btns)

        # Bottom bar: utility buttons (reset + open log folder) on the left,
        # Save / Cancel on the right. Reset/folder don't close the dialog.
        reset_btn = QPushButton("RESTORE DEFAULTS")
        folder_btn = QPushButton("OPEN LOG FOLDER")
        reset_btn.clicked.connect(self._reset_defaults)
        folder_btn.clicked.connect(self._open_data_dir)

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save |
            QDialogButtonBox.StandardButton.Cancel)
        btn_box.accepted.connect(self._save)
        btn_box.rejected.connect(self.reject)

        bottom = QHBoxLayout()
        bottom.addWidget(reset_btn)
        bottom.addWidget(folder_btn)
        bottom.addStretch()
        bottom.addWidget(btn_box)
        vbox.addLayout(bottom)

    def _set_row(self, r: int, name: str, process: str,
                 path: str, enabled: bool, origin: str = "") -> None:
        """Fill an (already inserted) table row: a centred ON checkbox plus the
        name/process/path cells. Shared by initial fill, + ADD, and reset.
        `origin` is the registry name this row derives from (empty for custom
        apps); it's stashed on the name cell so a rename still re-attaches the
        default's resolver/path on save (see `_save`)."""
        chk = QCheckBox()
        chk.setChecked(enabled)
        chk.setEnabled(not self._locked)
        # Not a Tab stop of its own (11 of them would bury the buttons);
        # Space on the selected row ticks it instead.
        chk.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        cell = QWidget()
        cl = QHBoxLayout(cell)
        cl.addWidget(chk)
        cl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cl.setContentsMargins(0, 0, 0, 0)
        self.table.setCellWidget(r, 0, cell)
        name_item = QTableWidgetItem(name)
        name_item.setData(Qt.ItemDataRole.UserRole, origin)
        self.table.setItem(r, 1, name_item)
        proc_item = QTableWidgetItem(process)
        path_item = QTableWidgetItem(path)
        proc_item.setForeground(self._row_dim)
        path_item.setForeground(self._row_dim)
        self.table.setItem(r, 2, proc_item)
        self.table.setItem(r, 3, path_item)

    def _populate_table(self, apps: list[AppDescriptor]) -> None:
        """Replace all table rows with the given app list."""
        self.table.setRowCount(0)
        for app in apps:
            r = self.table.rowCount()
            self.table.insertRow(r)
            origin = app.name if app.name in config.REGISTRY_BY_NAME else ""
            self._set_row(r, app.name, app.process_name, app.path or "",
                          app.enabled, origin)

    def _existing_names(self, exclude_row: Optional[int] = None) -> list[str]:
        """Current app names in the table (col 1), optionally skipping one row —
        used to block duplicate names in the add/edit dialog."""
        names = []
        for r in range(self.table.rowCount()):
            if r == exclude_row:
                continue
            item = self.table.item(r, 1)
            if item and item.text().strip():
                names.append(item.text().strip())
        return names

    def _insert_app_row(self, name: str, path: str, process: str,
                        enabled: bool = True) -> None:
        """Append one app row. Kept separate from the modal add/edit flow so the
        row-mutation path stays callable (and testable) without a dialog."""
        r = self.table.rowCount()
        self.table.insertRow(r)
        self._set_row(r, name, process, path, enabled)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """Keyboard control of the app list: Space ticks the selected row's ON
        box, Enter edits it, Delete removes it."""
        if obj is self.table and event.type() == QEvent.Type.KeyPress:
            row = self.table.currentRow()
            key = event.key()
            if row >= 0 and key == Qt.Key.Key_Space:
                if not self._blocked():
                    cell = self.table.cellWidget(row, 0)
                    chk = cell.findChild(QCheckBox) if cell else None
                    if chk:
                        chk.toggle()
                return True
            if row >= 0 and key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._edit_row(row)
                return True
            if row >= 0 and key == Qt.Key.Key_Delete:
                self._remove_row()
                return True
        return super().eventFilter(obj, event)

    def _blocked(self) -> bool:
        """True -- after explaining why -- if the app list is locked. The first
        attempt per dialog is also reported, so it lands in the activity log."""
        if not self._locked:
            return False
        if not self._lock_reported:
            self._lock_reported = True
            self.locked_attempt.emit()
        QMessageBox.information(self, "App list locked", self.LOCKED_MSG)
        return True

    def _add_row(self) -> None:
        if self._blocked():
            return
        dlg = AppEntryDialog(taken_names=self._existing_names(), parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name, path = dlg.values()
        self._insert_app_row(name, path, _derive_process(path) or name)

    def _edit_selected(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if rows:
            self._edit_row(rows[0].row())

    def _edit_row(self, row: int) -> None:
        if self._blocked():
            return
        name_item = self.table.item(row, 1)
        old_process = self.table.item(row, 2)
        path_item = self.table.item(row, 3)
        old_process = old_process.text() if old_process else ""
        # Registry origin stashed on the name cell survives a rename, so an edited
        # default keeps its resolver/default path on save.
        origin = (name_item.data(Qt.ItemDataRole.UserRole) or "") if name_item else ""

        dlg = AppEntryDialog(
            name_item.text() if name_item else "",
            path_item.text() if path_item else "",
            taken_names=self._existing_names(exclude_row=row),
            parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name, path = dlg.values()
        # Re-derive the process from the (maybe new) file, but keep the existing
        # one when the path is blank — resolver-based apps (e.g. SrvSurvey) carry
        # no path yet still need a process name to be watched/killed by.
        process = _derive_process(path) or old_process or name
        cell = self.table.cellWidget(row, 0)
        chk = cell.findChild(QCheckBox) if cell else None
        enabled = chk.isChecked() if chk else True
        self._set_row(row, name, process, path, enabled, origin)

    def _remove_row(self) -> None:
        rows = self.table.selectedIndexes()
        if rows and not self._blocked():
            self.table.removeRow(rows[0].row())

    def _reset_defaults(self) -> None:
        """Restore every field to factory defaults (CMDR name, toggles, and the
        full default app list). Nothing is written until the user hits Save, so
        this is reversible with Cancel."""
        if self._blocked():   # a reset replaces the app list too
            return
        if QMessageBox.question(
                self, "Reset to default",
                "Reset all settings, apps, and config to their defaults?\n\n"
                "Nothing is saved until you press Save.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        defaults = config.default_settings()
        self.cmdr_edit.setText(defaults.cmdr_name)
        self.toggle_inactive.setChecked(defaults.show_inactive_cards)
        self.toggle_scope.setChecked(
            defaults.shutdown_scope is ShutdownScope.ALL_ENABLED)
        self.toggle_close_exit.setChecked(defaults.close_on_game_exit)
        self.toggle_close_shutdown.setChecked(defaults.close_on_shutdown)
        self.toggle_light.setChecked(defaults.theme is Theme.LIGHT)
        self._populate_table(defaults.apps)
        # Clear any suppressed defaults so the reset list isn't re-filtered on save.
        self._settings = replace(self._settings, removed_defaults=[])

    def _open_data_dir(self) -> None:
        """Open the EDLS log/data directory (%LOCALAPPDATA%\\EDLS, where
        launcher.log and settings.json live) in the file manager. Creates it
        first so the button never fails on a fresh install."""
        os.makedirs(config.DATA_DIR, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(config.DATA_DIR))

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
            # Prefer the row's registry origin so a renamed default still re-links
            # to its resolver/default path; fall back to the (unchanged) name.
            name_item = self.table.item(r, 1)
            origin = (name_item.data(Qt.ItemDataRole.UserRole) or "") if name_item else ""
            reg = config.REGISTRY_BY_NAME.get(origin) or config.REGISTRY_BY_NAME.get(name)
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
            close_on_game_exit=self.toggle_close_exit.isChecked(),
            close_on_shutdown=self.toggle_close_shutdown.isChecked(),
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
# Timings (BOOT_DURATION/OFFSET/STAGGER) come from theme.py.
BOOT_ENABLED = True
BOOT_TOTAL = BOOT_STAGGER * 3 + BOOT_DURATION

# Window sizing. The minimum fits a 1024x768 screen with the taskbar showing
# (about 1004x697 usable); the card grid scrolls, so any number of apps fits.
WIN_MIN = QSize(900, 640)
WIN_DEFAULT = QSize(1000, 880)
_QWIDGETSIZE_MAX = (1 << 24) - 1


class MainWindow(QMainWindow):
    def __init__(self, engine, settings: Settings) -> None:
        super().__init__()
        self._engine = engine
        self._settings = settings

        self.setWindowTitle("Elite: Dangerous · Launch Suite")
        self.setMinimumSize(WIN_MIN)
        self._fit_to_screen()

        # Wire engine.dispatch to the GUI thread
        self._invoker = _Invoker()
        engine.dispatch = self._invoker.invoke
        self._busy = False   # tracked so a theme rebuild can restore button state
        # Last state per card. Rebuilding the cards (Settings save, theme switch)
        # re-applies these, so "Waiting..." or ERROR survive the rebuild.
        self._card_states: dict[str, tuple[AppState, Optional[int]]] = {}

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
        engine.on_quit.connect(self._on_quit)

        self._build_content()

        self._log.append("Elite: Dangerous Launch Suite ready.", "success")
        for warning in config.LOAD_WARNINGS:
            self._log.append(warning, "warning")

    def _visible_apps(self) -> list[AppDescriptor]:
        """Apps that get a status card: the enabled ones, plus — when the CMDR
        asks for them — the ones they haven't enabled (shown read-only). Both
        panel-build paths go through here so the rule can't drift between them."""
        if self._settings.show_inactive_cards:
            return list(self._settings.apps)
        return [a for a in self._settings.apps if a.enabled]

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
        self._status = self._make_status_panel()
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
        self._btns.launch_clicked.connect(self._on_launch_clicked)
        self._btns.set_busy(self._busy)
        self._update_launch_mute()
        root.addWidget(self._btns)

    def _make_status_panel(self) -> "StatusPanel":
        """A fresh status panel for the current app list, wired up and showing
        the last known state of every card."""
        panel = StatusPanel(self._visible_apps())
        panel.start_stop_clicked.connect(self._on_start_stop)
        panel.notice.connect(lambda msg: self._log.append(msg, "warning"))
        for key, (state, pid) in self._card_states.items():
            panel.update_card(key, state, pid)
        return panel

    def _update_launch_mute(self) -> None:
        """LAUNCH greys out while the Elite card is starting the game on its
        own, so the two can't race (it stays clickable to explain why)."""
        elite = self._card_states.get(KEY_ELITE, (AppState.OFFLINE, None))[0]
        self._btns.launch_btn.set_muted(
            not self._busy and elite is AppState.WAITING)

    def _on_launch_clicked(self) -> None:
        if self._btns.launch_btn.muted:
            self._log.append("The game is already being started from its card "
                             "- wait for it to finish.", "warning")
            return
        self._engine.launch()

    def _fit_to_screen(self) -> None:
        """Open at the default size, shrunk to fit a small screen, centred."""
        screen = self.screen().availableGeometry()
        w = max(WIN_MIN.width(),
                min(WIN_DEFAULT.width(), int(screen.width() * 0.92)))
        h = max(WIN_MIN.height(),
                min(WIN_DEFAULT.height(), int(screen.height() * 0.92)))
        self.resize(w, h)
        self.move(screen.x() + (screen.width() - w) // 2,
                  screen.y() + max(0, (screen.height() - h) // 2 - 16))

    def _play_boot_animation(self) -> None:
        """Staggered fade-up of the four panels. Runs once, after the layout has
        positioned everything (called via a 0ms timer post-show). The opacity
        effect is removed on completion so it can't dull later text rendering.
        The window is locked to its size meanwhile: the animation moves panels
        to positions captured up front, and a resize mid-way would strand them."""
        if not BOOT_ENABLED:
            return
        self.setFixedSize(self.size())
        QTimer.singleShot(BOOT_TOTAL + 20, self._end_boot_lock)
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

    def _end_boot_lock(self) -> None:
        """Unlock the window after the boot animation, and re-seat the panels
        in case anything moved them while it ran."""
        self.setMinimumSize(WIN_MIN)
        self.setMaximumSize(_QWIDGETSIZE_MAX, _QWIDGETSIZE_MAX)
        layout = self.centralWidget().layout()
        layout.invalidate()
        layout.activate()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._bg.setGeometry(self.rect())

    @Slot(str, str)
    def _on_log(self, message: str, level: str) -> None:
        self._log.append(message, level)

    @Slot(str, object, object)
    def _on_status(self, key: str, state: AppState,
                   pid: Optional[int]) -> None:
        self._card_states[key] = (state, pid)
        self._status.update_card(key, state, pid)
        if key == KEY_ELITE:
            self._update_launch_mute()

    @Slot(bool)
    def _on_busy(self, busy: bool) -> None:
        self._busy = busy
        self._btns.set_busy(busy)
        self._update_launch_mute()

    @Slot()
    def _on_quit(self) -> None:
        self.close()

    @Slot()
    def _bring_to_front(self) -> None:
        """Another copy of EDLS was started: show this one instead. (Windows
        may only flash the taskbar button rather than hand over focus.)"""
        server = _instance_server
        while server is not None and server.hasPendingConnections():
            server.nextPendingConnection().deleteLater()
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    @Slot(str, bool)
    def _on_start_stop(self, name: str, is_running: bool) -> None:
        if is_running:
            self._engine.stop_app(name)
        else:
            self._engine.start_app(name)

    @Slot(bool)
    def _on_autostart(self, on: bool) -> None:
        self._settings.auto_start = on
        self._save_settings(self._settings)

    def _save_settings(self, settings: Settings) -> None:
        if not config.save_settings(settings):
            self._log.append("Couldn't save settings.json - changes will be lost "
                             "when EDLS closes (see launcher.log).", "error")

    def _open_settings(self) -> None:
        dlg = SettingsDialog(
            self._settings,
            first_run=not self._settings.setup_complete,
            locked=self._engine.session_active,
            parent=self,
        )
        dlg.saved.connect(self._on_settings_saved)
        dlg.locked_attempt.connect(lambda: self._log.append(
            "The companion app list is locked during a session - it unlocks "
            "when the game closes, or when EDLS restarts.", "warning"))
        dlg.exec()

    def _prewarm_settings(self) -> None:
        """Pay Qt's one-time first-dialog realization cost off-screen while idle,
        so the user's first Settings open is snappy. Construction is ~10ms; the
        warm-up is the off-screen show/hide (native window + stylesheet + fonts)."""
        dlg = SettingsDialog(self._settings, parent=self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        dlg.show()
        dlg.hide()
        dlg.deleteLater()

    @Slot(object)
    def _on_settings_saved(self, updated: Settings) -> None:
        theme_changed = updated.theme != self._settings.theme
        self._settings = updated
        self._save_settings(updated)
        self._engine.settings = updated

        if theme_changed:
            # A theme switch must rebuild all content so every baked-in
            # stylesheet picks up the new palette. Carry the activity log across
            # the swap (its per-line accent colours are theme-independent).
            # Card states carry over via _card_states.
            log_html = self._log.log.toHtml()
            apply_theme(updated.theme)
            _apply_app_palette()
            self._build_content()
            self._log.log.setHtml(log_html)
            sb = self._log.log.verticalScrollBar()
            sb.setValue(sb.maximum())
            self._bg.update()
        else:
            self._header.set_cmdr(updated.cmdr_name)
            # Rebuild the status panel with the new app list
            old_status = self._status
            self._status = self._make_status_panel()
            self._splitter.replaceWidget(0, self._status)
            old_status.deleteLater()
        # Apps added or renamed have no state yet: have the watcher look now.
        self._engine.request_scan()

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


# ── Single instance ───────────────────────────────────────────────────────────
# A per-user local socket: the first EDLS listens on it, a second one connects,
# asks the first to come to the front, and exits. Two copies would both launch
# the game, both tear tools down, and both write settings.json.
_instance_server: Optional[QLocalServer] = None


def _instance_key() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no user name is no reason to fail
        user = "user"
    return f"EDLS-py-{user}"


def acquire_single_instance(key: Optional[str] = None) -> bool:
    """True if this is the only EDLS running. Otherwise the running copy is
    asked to show itself and this returns False -- the caller should exit."""
    global _instance_server
    QApplication.instance() or QApplication(sys.argv)
    key = key or _instance_key()
    probe = QLocalSocket()
    probe.connectToServer(key)
    if probe.waitForConnected(500):
        probe.write(b"show\n")
        probe.waitForBytesWritten(500)
        probe.disconnectFromServer()
        return False
    QLocalServer.removeServer(key)   # a crashed copy's leftover (non-Windows)
    server = QLocalServer()
    if server.listen(key):
        _instance_server = server
    return True   # if listening failed we just run unguarded


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
    if _instance_server is not None:
        _instance_server.newConnection.connect(win._bring_to_front)
    win.show()

    # Keep every card honest from here on -- including a game or tool the CMDR
    # started before EDLS. Its own thread, so the cold first process scan
    # doesn't stall the window appearing.
    engine.start_watching()

    # Boot animation first (after layout settles), then any follow-on action.
    QTimer.singleShot(0, win._play_boot_animation)
    follow_delay = BOOT_TOTAL if BOOT_ENABLED else 0

    # Show first-run setup once the panels have animated in; otherwise honour
    # auto-start. The auto-start message is logged here, next to the decision,
    # so it can never claim a launch that isn't going to happen.
    if not settings.setup_complete:
        if settings.auto_start:
            win._log.append("Auto-start skipped - finish first-time setup "
                            "first.", "dim")
        QTimer.singleShot(follow_delay, win._open_settings)
    else:
        if settings.auto_start:
            win._log.append("Auto-start enabled — launching...", "dim")
            QTimer.singleShot(follow_delay, engine.launch)
        # Warm the Settings dialog off-screen after boot settles so the first
        # real open is instant (first-run already opens it, so skip that case).
        QTimer.singleShot(follow_delay + 50, win._prewarm_settings)

    return app.exec()
