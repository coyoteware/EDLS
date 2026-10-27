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
  body background     → themed window-bg SVG, rendered to cover the window
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
    engine.on_update.connect(...)    (version: str)  -- a newer release is out
    engine.on_countdown.connect(...) (counting: bool) -- the close countdown:
                                     LAUNCH reads STAY OPEN meanwhile
    engine.check_for_updates()
    engine.stay_open()               STAY OPEN clicked: don't close after all
"""
from __future__ import annotations

import getpass
import os
import sys
import time
from dataclasses import replace
from typing import Optional

from PySide6.QtCore import (
    QByteArray, QEasingCurve, QEvent, QParallelAnimationGroup, QPoint, QPointF,
    Property, QPropertyAnimation, QRect, QRectF, QSize, Qt, QTimer, QUrl,
    Signal, Slot,
)
from PySide6.QtGui import (
    QBrush, QColor, QDesktopServices, QFont, QFontDatabase, QIcon,
    QLinearGradient, QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygon,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication, QFrame, QGraphicsOpacityEffect,
    QHBoxLayout, QLabel, QMainWindow, QSizePolicy,
    QSpacerItem, QSplitter, QStyle, QStyleOptionButton, QStylePainter,
    QTextEdit, QVBoxLayout, QWidget, QPushButton,
    QDialog, QCheckBox, QLineEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QFileDialog, QScrollArea,
)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtSvg import QSvgRenderer

import config
from config import (
    AppDescriptor, AppState, Settings, ShutdownScope, Theme,
    KEY_STEAM, KEY_ELITE,
)

import theme as _theme
from theme import (
    C_GOLD, C_DARK, C_PANEL, C_BORDER, C_AMBER, C_ORANGE,
    C_TMID, C_TDIM, C_CMDR, C_SUCCESS, CARD_BORDER_OFF, PANE,
    PANE_TITLE, PANE_META, LOG_TIME, LOG_COLORS,
    BG_IMAGE, BANNER_FILL, BANNER_ACCENT, TITLE_MARK,
    IS_LIGHT, CHAMFER_CARD, CHAMFER_PANEL, PULSE_MS,
    BOOT_DURATION, BOOT_OFFSET, BOOT_STAGGER,
    CARD_BORDER_ONLINE, CARD_BORDER_LAUNCHING, CARD_BORDER_FAILED,
    TOGGLE, TRACK_BANNER,
    font_display, font_body, font_mono, power_icon,
)

# State → (label_colour, dot_colour) for the cards. The cards are the dark HUD in
# both themes (theme.PANE), so this never changes. (LOG_COLORS is themed: the
# log sits straight on the pane, which comes out mid-grey in day mode.)
STATE_COLORS = {
    AppState.ONLINE:    (PANE.C_GREEN,  PANE.C_GREEN),
    AppState.RUNNING:   (PANE.C_GREEN,  PANE.C_GREEN),
    AppState.LAUNCHING: (PANE.C_ORANGE, PANE.C_ORANGE),
    AppState.WAITING:   (PANE.C_ORANGE, PANE.C_ORANGE),
    AppState.OFFLINE:   (PANE.C_INACT,  PANE.C_INACT),
    AppState.NOT_FOUND: (PANE.C_INACT,  PANE.C_INACT),
    AppState.FAILED:    (PANE.C_RED,    PANE.C_RED),
    AppState.STOPPED:   (PANE.C_INACT,  PANE.C_INACT),
    AppState.ERROR:     (PANE.C_RED,    PANE.C_RED),
}


def apply_theme(theme: Theme) -> None:
    """Rebind the palette globals to the chosen Theme. Call before building (or
    rebuilding) the window content."""
    _theme.apply_theme(theme)
    is_light = theme is Theme.LIGHT
    globals().update(
        _theme._LIGHT_PALETTE if is_light else _theme._DARK_PALETTE)
    globals()["IS_LIGHT"] = is_light


def _live_theme() -> Theme:
    """The theme the UI is drawn in right now."""
    return Theme.LIGHT if IS_LIGHT else Theme.DARK

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
ISSUES_URL = "https://github.com/coyoteware/EDLS/issues"


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

    def _path(self) -> QPainterPath:
        """The chamfered outline, inset 1 px so the hairline border isn't clipped."""
        path = QPainterPath()
        path.addPolygon(self._polygon(self.rect().adjusted(1, 1, -1, -1)))
        path.closeSubpath()
        return path

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = self._path()

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

    def __init__(self, color: QColor = PANE.C_GREEN, parent: Optional[QWidget] = None):
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
        # A name, shown as typed: never read as markup ("<b>...</b>").
        self.setTextFormat(Qt.TextFormat.PlainText)
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
                         bg=PANE.CARD_BG, border=PANE.CARD_BORDER)
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
        # dimmed, tagged DISABLED, and no START button, since LAUNCH won't
        # touch it.
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
        self.dot = PulsingDot(PANE.C_INACT, self)
        self.dot.set_color(PANE.C_INACT, pulse=False)
        display = _DISPLAY_NAMES.get(descriptor.name, descriptor.name)
        name_lbl = ElidingLabel(display.upper())
        name_lbl.setFont(font_body(10, QFont.Weight.DemiBold))
        name_col = PANE.C_TDIM if self._inactive else PANE.C_NAME
        name_lbl.setStyleSheet(
            f"color: {name_col.name()}; letter-spacing: 1px;")
        hdr.addWidget(self.dot)
        hdr.addWidget(name_lbl, 1)

        # state label — inline beside the name, right-justified. The PID line is
        # gone (PIDs are in the activity log) and the status sits up here beside
        # the name rather than on its own row, to cut a row of clutter.
        self.state_lbl = QLabel(AppState.OFFLINE.label)
        self.state_lbl.setStyleSheet(f"color: {PANE.C_INACT.name()};")
        self.state_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.state_lbl.setFont(font_mono(12))
        hdr.addWidget(self.state_lbl, 0, Qt.AlignmentFlag.AlignTop)

        vbox.addLayout(hdr)
        vbox.addStretch(1)

        # Bottom row: the control button, or — on a card the CMDR hasn't enabled
        # — a DISABLED tag in its place, so every card keeps the same height.
        self.ctrl_btn: Optional[QPushButton] = None
        if self._inactive:
            vbox.addWidget(make_badge("DISABLED", PANE.C_TDIM), 0,
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
        label_col, dot_col = STATE_COLORS.get(state, (PANE.C_INACT, PANE.C_INACT))
        pulse = state in (AppState.ONLINE, AppState.RUNNING,
                          AppState.LAUNCHING, AppState.WAITING)

        self.dot.set_color(dot_col, pulse=pulse)
        # A disabled card's DISABLED tag already says it's off; OFFLINE is noise.
        self.state_lbl.setText(
            "" if self._inactive and state is AppState.OFFLINE else state.label)
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
        if self.ctrl_btn is None:          # not-enabled card: DISABLED tag, no button
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
    """Read-only terminal-style log. Appends coloured lines, and follows them
    while it's scrolled to the bottom."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(font_mono(11))
        self.setStyleSheet(f"""
            QTextEdit {{
                background: transparent;
                color: {LOG_COLORS["default"].name()};
                border: none;
                padding: 4px 8px;
            }}
        """ + _thin_scrollbar_qss())

    def append_line(self, message: str, level: str = "default") -> None:
        color = LOG_COLORS.get(level, LOG_COLORS["default"])
        ts = time.strftime("[%H:%M:%S]")
        ts_html = f'<span style="color:{LOG_TIME.name()};">{ts}</span>'
        msg_html = f'<span style="color:{color.name()};">&nbsp;{_esc(message)}</span>'
        # Scrolled up to read something? Then new lines don't drag you down.
        sb = self.verticalScrollBar()
        follow = sb.value() >= sb.maximum()
        self.append(f"{ts_html}{msg_html}")
        if follow:
            sb.setValue(sb.maximum())


def _thin_scrollbar_qss() -> str:
    """The slim HUD scrollbar shared by the activity log and the card grid."""
    return f"""
        QScrollBar:vertical {{ background: transparent; width: 4px; }}
        QScrollBar::handle:vertical {{
            background: {PANE.C_DIM.name()}; border-radius: 2px; min-height: 20px;
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
_BAR_DARK = {
    "launch": dict(
        bg="#120C00", bg_h="#1A1000", bg_p="#0A0800",
        bd="#C8760A", bd_h="#FFB700", fg="#FFB700", fg_h="#FFD040"),
    "secondary": dict(
        bg="#111114", bg_h="#120F09", bg_p="#0A0A0A",
        bd="#2A2A35", bd_h="#C8760A", fg="#686868", fg_h="#FF8C1A"),
    "secondary_on": dict(
        bg="#120C00", bg_h="#1A1000", bg_p="#0A0800",
        bd="#C8760A", bd_h="#FFB700", fg="#FF8C1A", fg_h="#FFB700"),
    "danger": dict(
        bg="#0D0805", bg_h="#140A0A", bg_p="#0A0505",
        bd="#2A1515", bd_h="#CC3D3D", fg="#7A3A3A", fg_h="#CC3D3D"),
}
# Day mode ("Full Daylight" mockup): pale secondary keys, solid orange LAUNCH.
# The mockup has no pressed state, so bg_p is a step darker than bg.
_BAR_LIGHT = {
    "launch": dict(
        bg="#C25E0A", bg_h="#D46A12", bg_p="#A84F08",
        bd="#9E4A06", bd_h="#7E3A04", fg="#FFFFFF", fg_h="#FFFFFF"),
    "secondary": dict(
        bg="#EEF0F3", bg_h="#FFFFFF", bg_p="#E1E4E8",
        bd="#A6AEB9", bd_h="#C25E0A", fg="#3A4450", fg_h="#A84F08"),
    "secondary_on": dict(
        bg="#FBE9D8", bg_h="#FFF1E3", bg_p="#F3DCC6",
        bd="#C25E0A", bd_h="#9E4A06", fg="#A84F08", fg_h="#7E3A04"),
    "danger": dict(
        bg="#EEF0F3", bg_h="#FBEAE9", bg_p="#F3D9D7",
        bd="#C9A5A3", bd_h="#C0322B", fg="#9A3A35", fg_h="#C0322B"),
}


def _bar_colors(kind: str) -> dict:
    """Bottom-bar button palette for the active theme."""
    return (_BAR_LIGHT if IS_LIGHT else _BAR_DARK)[kind]


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

        if not en and IS_LIGHT:
            bg, bd, fg = QColor("#E1E4E8"), QColor("#C6CCD4"), QColor("#9AA2AC")
        elif not en:
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
    """Card button QSS. active = STOP, muted = greyed out (still clickable, so
    the click can be explained), error = couldn't close (red)."""
    focus = "#C25E0A" if IS_LIGHT else "#FF8C1A"   # keyboard focus outline
    base = ("QPushButton {{ font-family: 'Euro Caps'; font-size: 7px;"
            " font-weight:700; letter-spacing:1px; border:1px solid {bd};"
            " color:{fg}; background:{bg}; padding: 0 4px; }}"
            " QPushButton:hover {{ border-color:{bdh}; color:{fgh};"
            " background:{bgh}; }}"
            " QPushButton:focus {{ border:1px dashed " + focus + "; }}")

    def fmt(bd, fg, bg, bdh, fgh, bgh=None) -> str:
        return base.format(bd=bd, fg=fg, bg=bg, bdh=bdh, fgh=fgh, bgh=bgh or bg)

    if IS_LIGHT:
        # Day mode: the pale daylight keys from the bottom bar on the dark
        # cards -- light grey START, pale orange STOP.
        if error:
            return fmt("#C0322B", "#A0261F", "#F6D5D3", "#8E1F19", "#8E1F19")
        if muted:
            return fmt("#C6CCD4", "#9AA2AC", "#E1E4E8", "#C6CCD4", "#9AA2AC")
        c = _BAR_LIGHT["secondary_on" if active else "secondary"]
        return fmt(c["bd"], c["fg"], c["bg"], c["bd_h"], c["fg_h"], c["bg_h"])
    # Dark theme (original): green STOP.
    if error:
        return fmt("#CC3D3D", "#CC3D3D", "#1A0A0A", "#FF5555", "#FF6666")
    if muted:
        return fmt("#181818", "#303030", "#0A0A0A", "#181818", "#303030")
    if active:
        return fmt("#226622", "#44AA44", "#0D1A0D", "#44AA44", "#66CC66")
    return fmt("#1E1E1E", "#484848", "#0C0C0C", "#C8760A", "#C8760A")


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
    """The suite title, centred in tracked display caps. Transparent: the
    HeaderPanel around it paints the strip, the accent line, and the mark."""

    # Height tracks width at a wide ~14.5:1 strip, clamped to 50–88 px.
    _ASPECT = 14.5

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(50)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self.width() > 0:
            h = max(50, min(round(self.width() / self._ASPECT), 88))
            if h != self.height():
                self.setFixedHeight(h)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect()
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
class HeaderPanel(ChamferedFrame):
    """The title banner and the CMDR nametag in one chamfered panel: a
    translucent BANNER_FILL strip with the accent line along its top edge, the
    title mark spanning both rows at the left, the suite title, and the CMDR
    row beneath it. The mark renders straight from its SVG so it stays crisp at
    any size; if the file is missing the header just goes without it."""

    def __init__(self, cmdr_name: str,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, chamfer=CHAMFER_PANEL, bg=BANNER_FILL,
                         border=C_BORDER)
        self.setContentsMargins(0, 0, 0, 0)
        self._mark = QSvgRenderer(os.path.join(ASSETS_DIR, TITLE_MARK))
        vbox = QVBoxLayout(self)
        vbox.setSpacing(0)
        vbox.setContentsMargins(0, 0, 0, 10)
        vbox.addWidget(TitleBanner())

        cmdr_row = QHBoxLayout()
        cmdr_row.setSpacing(10)
        cmdr_row.addStretch()
        cmdr_row.addWidget(make_badge("CMDR", C_AMBER))
        self.cmdr_lbl = QLabel(cmdr_name.upper())
        self.cmdr_lbl.setTextFormat(Qt.TextFormat.PlainText)   # never markup
        self.cmdr_lbl.setFont(font_body(12, QFont.Weight.DemiBold))
        self.cmdr_lbl.setStyleSheet(
            f"color: {C_CMDR.name()}; letter-spacing: 4px;")
        cmdr_row.addWidget(self.cmdr_lbl)
        cmdr_row.addStretch()
        vbox.addLayout(cmdr_row)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)   # fill + chamfered hairline
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Clipped to the outline, so the accent line stops where the chamfer
        # cuts the top-right corner.
        p.setClipPath(self._path())
        p.fillRect(0, 1, self.width(), 2, BANNER_ACCENT)
        if self._mark.isValid():
            ms = self._mark.defaultSize()
            mh = self.height() * 0.80
            mw = mh * ms.width() / ms.height()
            self._mark.render(p, QRectF(22, (self.height() - mh) / 2, mw, mh))
        p.end()

    def set_cmdr(self, name: str) -> None:
        self.cmdr_lbl.setText(name.upper())


# ── Status panel ──────────────────────────────────────────────────────────────
class StatusPanel(ChamferedFrame):
    start_stop_clicked = Signal(str, bool)
    notice = Signal(str)   # forwarded from the cards

    def __init__(self, apps: list[AppDescriptor],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, chamfer=CHAMFER_PANEL, bg=PANE.C_PANEL_T,
                         border=PANE.C_BORDER)
        self.cards: dict[str, StatusCard] = {}

        vbox = QVBoxLayout(self)
        vbox.setSpacing(8)
        vbox.setContentsMargins(14, 10, 14, 10)

        lbl = QLabel("S Y S T E M   S T A T U S")
        lbl.setFont(font_display(7))
        lbl.setStyleSheet(f"color: {PANE_TITLE.name()}; letter-spacing: 3px;")
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
        super().__init__(parent, chamfer=CHAMFER_PANEL, bg=PANE.C_PANEL_T,
                         border=PANE.C_BORDER)
        vbox = QVBoxLayout(self)
        vbox.setSpacing(0)
        vbox.setContentsMargins(0, 0, 0, 0)

        # header
        hdr = QHBoxLayout()
        hdr.setContentsMargins(14, 8, 14, 6)
        lbl = QLabel("A C T I V I T Y   L O G")
        lbl.setFont(font_display(7))
        lbl.setStyleSheet(f"color: {PANE_TITLE.name()}; letter-spacing: 3px;")
        file_lbl = QLabel("launcher.log")
        file_lbl.setFont(font_mono(9))
        file_lbl.setStyleSheet(f"color: {PANE_META.name()};")
        hdr.addWidget(lbl)
        hdr.addStretch()
        hdr.addWidget(file_lbl)
        vbox.addLayout(hdr)

        # amber glow divider
        div = QFrame()
        div.setFixedHeight(1)
        div.setStyleSheet(
            f"background: {LOG_COLORS['default'].name()}; opacity: 0.2;")
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
    update_clicked    = Signal()

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
        # "update x.y.z" link, shown once the update check finds a release.
        self.update_lbl = QLabel()
        self.update_lbl.setFont(font_mono(9))
        self.update_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction)
        self.update_lbl.linkActivated.connect(
            lambda _href: self.update_clicked.emit())
        self.update_lbl.hide()
        ver_row.addWidget(self.update_lbl)
        ver_row.addStretch()
        cred_vbox.addLayout(ver_row)
        hbox.addLayout(cred_vbox)
        hbox.addStretch()

        self.shutdown_btn  = ChamferedButton("SHUTDOWN",   chamfer=CHAMFER_CARD, **_bar_colors("danger"))
        self.autostart_btn = ChamferedButton("AUTO START", chamfer=CHAMFER_CARD,
                                             **_bar_colors("secondary_on" if auto_start else "secondary"))
        self.settings_btn  = ChamferedButton("SETTINGS",  chamfer=CHAMFER_CARD, **_bar_colors("secondary"))
        self.launch_btn    = ChamferedButton("LAUNCH",    chamfer=CHAMFER_CARD, **_bar_colors("launch"))

        self.shutdown_btn.setFont(font_display(9))
        self.autostart_btn.setFont(font_display(9))
        self.settings_btn.setFont(font_display(9))
        self.launch_btn.setFont(font_display(11))

        # Day mode takes the icons in each button's text colour (white ▶ on the
        # orange LAUNCH); ⟳ stays the themed orange in both, as in dark mode.
        power, gear, play = (("#9A3A35", "#3A4450", "#FFFFFF") if IS_LIGHT
                             else ("#B85555", "#9A9AA0", C_GOLD))
        self.shutdown_btn.setIcon(power_icon(QColor(power)))
        self.autostart_btn.setIcon(_glyph_icon("⟳", C_ORANGE))
        self.settings_btn.setIcon(_glyph_icon("⚙", QColor(gear)))
        self.launch_btn.setIcon(_glyph_icon("▶", QColor(play)))
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
            **_bar_colors("secondary_on" if self._auto_start else "secondary"))
        self.autostart_toggled.emit(self._auto_start)

    def set_busy(self, busy: bool, counting: bool = False) -> None:
        """LAUNCH reads RUNNING... (disabled) during a session, and STAY OPEN
        while the close countdown runs -- clicking that cancels the close."""
        self.launch_btn.setEnabled(counting or not busy)
        self.launch_btn.setText("STAY OPEN" if counting
                                else "RUNNING..." if busy else "LAUNCH")

    def show_update(self, version: Optional[str]) -> None:
        """The update link: "update x.y.z available" in the themed accent, or
        hidden (version None)."""
        lbl = self.update_lbl
        if version is None:
            lbl.hide()
            return
        lbl.setText(f"<a href='#update' style='color:{C_SUCCESS.name()};'>"
                    f"update {_esc(version)} available</a>")
        lbl.show()


# ── Background widget (starfield + vignette) ──────────────────────────────────
class Background(QWidget):
    """Renders the themed window-bg SVG (starfield/nebula/dot-matrix) to cover
    the window, centred — the SVG's own `slice` scaling, which QSvgRenderer
    doesn't do itself. The render is cached per size + DPI, so it's crisp on
    any display. Falls back to a flat dark fill if the asset is missing, so the
    app still runs."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._svg = QSvgRenderer()
        self._loaded: Optional[str] = None
        self._cache: Optional[QPixmap] = None
        self._cache_for: Optional[tuple] = None

    def _ensure_renderer(self) -> None:
        """(Re)load the themed background SVG if the active theme changed it.
        The widget persists across theme switches, so this is how it repaints
        with the right asset after apply_theme()."""
        if self._loaded != BG_IMAGE:
            self._svg.load(os.path.join(ASSETS_DIR, BG_IMAGE))
            self._loaded = BG_IMAGE
            self._cache = None
            self._cache_for = None

    def _render_cover(self, dpr: float) -> QPixmap:
        vb = self._svg.viewBoxF()
        scale = max(self.width() / vb.width(), self.height() / vb.height())
        w, h = vb.width() * scale, vb.height() * scale
        pm = QPixmap(self.size() * dpr)
        pm.setDevicePixelRatio(dpr)
        pm.fill(C_DARK)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._svg.render(p, QRectF((self.width() - w) / 2,
                                   (self.height() - h) / 2, w, h))
        p.end()
        return pm

    def paintEvent(self, _event) -> None:  # noqa: N802
        self._ensure_renderer()
        p = QPainter(self)
        if self._svg.isValid() and not self.size().isEmpty():
            dpr = self.devicePixelRatioF()
            key = (self.size(), dpr)
            if self._cache_for != key:
                self._cache = self._render_cover(dpr)
                self._cache_for = key
            p.drawPixmap(0, 0, self._cache)
        else:
            p.fillRect(self.rect(), C_DARK)
        p.end()


# ── Toggle switches ───────────────────────────────────────────────────────────
def _chamfer_path(r: QRectF, cut: float) -> QPainterPath:
    """`r` with its top-right and bottom-left corners cut by `cut` px -- the
    HUD's chamfer, at sub-pixel precision for the small toggle parts."""
    path = QPainterPath()
    path.moveTo(r.left(), r.top())
    path.lineTo(r.right() - cut, r.top())
    path.lineTo(r.right(), r.top() + cut)
    path.lineTo(r.right(), r.bottom())
    path.lineTo(r.left() + cut, r.bottom())
    path.lineTo(r.left(), r.bottom() - cut)
    path.closeSubpath()
    return path


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    """Blend a -> b by t (0..1), alpha included."""
    return QColor(round(a.red() + (b.red() - a.red()) * t),
                  round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t),
                  round(a.alpha() + (b.alpha() - a.alpha()) * t))


# ThemeToggle's moon and sun, straight from the design's SVG (24x24 viewBox).
_TOGGLE_ICONS = {
    "moon": '<path d="M15.5 3.5a8.5 8.5 0 1 0 5 15 7 7 0 0 1-5-15z" fill="{c}"/>',
    "sun": ('<circle cx="12" cy="12" r="4.5" fill="{c}"/>'
            '<path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8'
            'M17.3 17.3l1.8 1.8M4.9 19.1l1.8-1.8M17.3 6.7l1.8-1.8" stroke="{c}"'
            ' stroke-width="2" stroke-linecap="round"/>'),
}


def _draw_toggle_icon(p: QPainter, name: str, color: QColor,
                      rect: QRectF) -> None:
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
           + _TOGGLE_ICONS[name].format(c=color.name()) + "</svg>")
    QSvgRenderer(QByteArray(svg.encode())).render(p, rect)


class ToggleSwitch(QWidget):
    """The design's "Chamfer Slide" switch: a chamfered track with a 1 px
    hairline, a chamfered knob with a grip notch that slides across, tiny OFF /
    ON labels, and a gold glow when on (none in day mode). Colours are the
    themed TOGGLE tokens. Exposes a QCheckBox-like API (isChecked / setChecked
    / toggled) so it drops in for a checkbox."""

    toggled = Signal(bool)

    _TRACK = (58, 24)   # the design's track size; the knob travels within it
    _PAD = 3            # room around the track for the keyboard focus ring
    SLIDE_MS = 180      # the knob's slide

    def __init__(self, checked: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._checked = checked
        self._pos = 1.0 if checked else 0.0   # knob travel, 0 (off) .. 1 (on)
        w, h = self._TRACK
        self.setFixedSize(w + 2 * self._PAD, h + 2 * self._PAD)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._kb_focus = False
        self._anim = QPropertyAnimation(self, b"knobPos", self)
        self._anim.setDuration(self.SLIDE_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

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

    def _track_rect(self) -> QRectF:
        w, h = self._TRACK
        return QRectF(self._PAD, self._PAD, w, h)

    def _begin_paint(self) -> tuple[QPainter, QRectF]:
        """Painter + track rect, with the shared bits done: dimmed when
        disabled, and the chamfered focus ring for keyboard focus."""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.5)
        track = self._track_rect()
        if self.hasFocus() and self._kb_focus:
            p.setPen(QPen(TOGGLE["on_bd"], 1, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(_chamfer_path(track.adjusted(-2.5, -2.5, 2.5, 2.5), 9))
        return p, track

    def paintEvent(self, _event) -> None:  # noqa: N802
        p, track = self._begin_paint()
        c, t = TOGGLE, self._pos
        outer = _chamfer_path(track, 7)
        p.fillPath(outer, _mix(c["off_bd"], c["on_bd"], t))       # 1 px hairline
        p.fillPath(_chamfer_path(track.adjusted(1, 1, -1, -1), 7),
                   _mix(c["off_bg"], c["on_bg"], t))
        p.setClipPath(outer)

        # Micro-labels: OFF sits right while off, ON sits left while on.
        f = font_mono(8)
        f.setPixelSize(8)
        f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 110)
        p.setFont(f)
        for text, col, alpha, rect, align in (
                ("OFF", c["off_txt"], 1 - t, track.adjusted(0, 0, -8, 0),
                 Qt.AlignmentFlag.AlignRight),
                ("ON", c["on_txt"], t, track.adjusted(9, 0, 0, 0),
                 Qt.AlignmentFlag.AlignLeft)):
            if alpha > 0.01:
                ink = QColor(col)
                ink.setAlphaF(ink.alphaF() * alpha)
                p.setPen(ink)
                p.drawText(rect, align | Qt.AlignmentFlag.AlignVCenter, text)

        # Knob: 20x16, 4 px chamfer, slides 4 -> 34 px, glows when on.
        knob = QRectF(track.left() + 4 + 30 * t, track.top() + 4, 20, 16)
        glow = c["glow"]
        if glow is not None and t > 0:
            # A faint halo. (The design's drop-shadow is mostly clipped off by
            # the knob's own clip-path, so it barely shows there either.)
            for spread in (3.0, 1.5):
                halo = QColor(glow)
                halo.setAlphaF(glow.alphaF() * t * 0.22)
                p.fillPath(_chamfer_path(
                    knob.adjusted(-spread, -spread, spread, spread),
                    4 + spread), halo)
        p.fillPath(_chamfer_path(knob, 4), _mix(c["off_knob"], c["on_knob"], t))
        # Grip notch: two 1 px dark lines.
        p.setPen(QPen(QColor(0, 0, 0, 89), 1))
        for dx in (7.5, 12.5):
            p.drawLine(QPointF(knob.left() + dx, knob.top() + 4),
                       QPointF(knob.left() + dx, knob.top() + 12))
        p.end()


class ThemeToggle(ToggleSwitch):
    """The design's "Night Sky" day/night switch. checked = light theme. A wide
    chamfered track holds a moon (left) and a sun (right); the orange knob
    slides over the active side carrying that icon, rotating it in. The track is
    plain near-black at night and daylight slate by day."""

    _TRACK = (84, 32)
    _NIGHT = QColor("#0B0A07")

    def paintEvent(self, _event) -> None:  # noqa: N802
        p, track = self._begin_paint()
        c, t = TOGGLE, self._pos
        base = p.opacity()
        p.fillPath(_chamfer_path(track, 7), c["off_bd"])   # 1 px hairline
        inner = _chamfer_path(track.adjusted(1, 1, -1, -1), 7)
        if self._checked:
            day = QLinearGradient(0, track.top(), 0, track.bottom())
            day.setColorAt(0, QColor("#E6E9ED"))
            day.setColorAt(1, QColor("#C8CDD4"))
            p.fillPath(inner, QBrush(day))
        else:
            p.fillPath(inner, self._NIGHT)

        # The idle icons in the track, at 70%.
        p.setOpacity(base * 0.7)
        _draw_toggle_icon(p, "moon", c["icon"],
                          QRectF(track.left() + 10, track.top() + 8, 16, 16))
        _draw_toggle_icon(p, "sun", c["icon"],
                          QRectF(track.right() - 26, track.top() + 8, 16, 16))
        p.setOpacity(base)

        # Knob: 36x24, slides 4 -> 44 px, carries the active icon.
        knob = QRectF(track.left() + 4 + 40 * t, track.top() + 4, 36, 24)
        p.fillPath(_chamfer_path(knob, 4), c["on_bd"])
        ink = _mix(QColor("#120C00"), QColor("#FFFFFF"), t)
        for name, alpha, angle in (("moon", 1 - t, 90 * t),
                                   ("sun", t, -90 * (1 - t))):
            if alpha > 0.01:
                p.save()
                p.setOpacity(base * alpha)
                p.translate(knob.center())
                p.rotate(angle)
                _draw_toggle_icon(p, name, ink, QRectF(-8, -8, 16, 16))
                p.restore()
        p.end()


def _derive_process(path: str) -> str:
    """The process name EDLS watches/kills is the exe's filename stem — that's
    what psutil reports as proc.name(). So for a user-added app we derive it from
    the chosen file instead of making them type it. Empty path -> empty string."""
    base = os.path.basename(path.rstrip("\\/"))
    return os.path.splitext(base)[0]


# ── Dialog styling (Settings + HUD popovers) ──────────────────────────────────
def _dialog_colors() -> dict:
    """Colours for Settings and the HUD popovers, from the live themed globals:
    the design's dark Settings panel in dark mode, and the same roles mapped onto
    the light palette in day mode (the design only draws the dark one)."""
    light = IS_LIGHT
    return dict(
        bg=C_PANEL.name(),            # dialog / popover surface
        field=C_DARK.name(),          # text fields + the app list
        edge="#A6AEB9" if light else "#2E2414",   # field + list outlines, rules
        sep="#C6CCD4" if light else C_BORDER.name(),   # list row separators
        text=C_AMBER.name(),          # body text
        label=C_TMID.name(),          # field labels, intro text
        meta=C_TDIM.name(),           # eyebrows, paths
        accent="#C25E0A" if light else C_GOLD.name(),  # field text, headings
        check="#C25E0A" if light else "#C8760A",       # ticked box, focus edge
        tick=C_DARK.name(),
        ghost="#8A929C" if light else "#5C574D",       # placeholder text
        zebra="rgba(255,255,255,0.3)" if light else "rgba(255,255,255,0.015)",
        sel="#F0E3CC" if light else "#1C1408",
        error="#C0322B" if light else "#CC3D3D",
    )


def _dialog_qss(c: dict) -> str:
    """Stylesheet shared by Settings and the popovers. The window background is
    only set on Settings (#settingsDialog): a popover's own top level stays
    transparent so its chamfered corners show."""
    return f"""
        QDialog#settingsDialog {{ background: {c['bg']}; }}
        QLabel {{ color: {c['text']}; background: transparent; }}
        QLineEdit {{ background: {c['field']}; color: {c['accent']};
                     border: 1px solid {c['edge']}; padding: 6px 8px;
                     placeholder-text-color: {c['ghost']};
                     selection-background-color: {c['check']};
                     selection-color: {c['tick']}; }}
        QLineEdit:focus {{ border-color: {c['check']}; }}
        QTableWidget {{ background: {c['field']}; color: {c['text']};
                        alternate-background-color: {c['zebra']};
                        border: 1px solid {c['edge']}; outline: none; }}
        QTableWidget:focus {{ border-color: {c['check']}; }}
        QTableWidget::item {{ padding: 6px 8px; border: none;
                              border-bottom: 1px solid {c['sep']}; }}
        QTableWidget::item:selected {{ background: {c['sel']};
                                       color: {c['accent']}; }}
        QTableCornerButton::section {{ background: {c['field']}; border: none; }}
        QToolTip {{ background: {c['field']}; color: {c['text']};
                    border: 1px solid {c['check']}; padding: 6px 8px;
                    font-family: 'Share Tech Mono'; font-size: 10pt; }}
    """ + _thin_scrollbar_qss()


def _spaced(text: str) -> str:
    """'App list' -> 'A P P   L I S T': the HUD's spaced-out eyebrow lettering."""
    return "   ".join(" ".join(word) for word in text.upper().split())


def _eyebrow(text: str, color: str) -> QLabel:
    lbl = QLabel(_spaced(text))
    lbl.setFont(font_display(8))
    lbl.setStyleSheet(f"color: {color}; letter-spacing: 2px;")
    return lbl


def _stacked_field(label: str, field, c: dict) -> QVBoxLayout:
    """The design's TextField: a tracked caps label stacked above the field (a
    widget, or a layout such as field + Browse)."""
    box = QVBoxLayout()
    box.setSpacing(4)
    lbl = QLabel(label)
    lbl.setFont(font_display(8))
    lbl.setStyleSheet(f"color: {c['label']}; letter-spacing: 1px;")
    box.addWidget(lbl)
    if isinstance(field, QWidget):
        box.addWidget(field)
    else:
        box.addLayout(field)
    return box


def _key(text: str, kind: str = "secondary", height: int = 32) -> ChamferedButton:
    """A chamfered dialog button in the bottom bar's style (themed palette).
    Not an auto-default: only the dialog's primary button answers Enter."""
    btn = ChamferedButton(text, chamfer=CHAMFER_CARD, **_bar_colors(kind))
    btn.setFont(font_display(9))
    btn.setFixedHeight(height)
    btn.setMinimumWidth(84)
    btn.setAutoDefault(False)
    return btn


class HudCheckBox(QCheckBox):
    """The design's square HUD checkbox: a hairline box on the field colour that
    fills with the accent and a dark tick when checked. Painted rather than QSS,
    which can't draw the tick without an image asset."""

    _BOX = 15

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._c = _dialog_colors()
        self.setFixedSize(self._BOX + 1, self._BOX + 1)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def hitButton(self, pos) -> bool:  # noqa: N802
        return self.rect().contains(pos)

    def paintEvent(self, _event) -> None:  # noqa: N802
        c, on = self._c, self.isChecked()
        fill = QColor(c["check"] if on else c["field"])
        edge = QColor(c["check"] if on else c["edge"])
        if not self.isEnabled():     # locked during a session
            fill.setAlphaF(0.45)
            edge.setAlphaF(0.45)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = self._BOX
        p.setPen(QPen(edge, 1))
        p.setBrush(fill)
        p.drawRect(QRectF(0.5, 0.5, s - 1, s - 1))
        if on:   # the design's "M1 5 L4 8 L9 2" tick, scaled into the box
            k, o = (s - 5) / 10.0, 2.5
            tick = QPainterPath()
            tick.moveTo(o + 1 * k, o + 5 * k)
            tick.lineTo(o + 4 * k, o + 8 * k)
            tick.lineTo(o + 9 * k, o + 2 * k)
            pen = QPen(QColor(c["tick"]), 1.8)
            pen.setCapStyle(Qt.PenCapStyle.SquareCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(tick)
        p.end()


class InfoMark(QWidget):
    """The "there's a tooltip here" marker on a Settings row: an "i" in a small
    chamfered box, painted rather than a font glyph. It brightens to the accent
    on hover; the tip itself is set on the row, so the whole row answers."""

    _BOX = 14

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._c = _dialog_colors()
        self._hover = False
        self.setFixedSize(self._BOX + 1, self._BOX + 1)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        ink = QColor(self._c["check" if self._hover else "meta"])
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = self._BOX
        p.setPen(QPen(ink, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(_chamfer_path(QRectF(0.5, 0.5, s - 1, s - 1), 3))
        # The "i": a square dot over a stem, on whole pixels so it stays crisp.
        x = s / 2 - 1
        p.fillRect(QRectF(x, 3, 2, 2), ink)
        p.fillRect(QRectF(x, 6, 2, 5), ink)
        p.end()


class HudPopover(QDialog):
    """A frameless, chamfered modal popover centred over its parent window: the
    HUD stand-in for small native dialogs (the app editor, notices, confirms).
    Content goes in `self.body`. Esc rejects it, as with any QDialog."""

    def __init__(self, parent: Optional[QWidget] = None,
                 width: int = 420) -> None:
        super().__init__(parent, Qt.WindowType.Dialog
                         | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self._c = c = _dialog_colors()
        self.setStyleSheet(_dialog_qss(c))
        self.setMinimumWidth(width)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        frame = ChamferedFrame(chamfer=CHAMFER_PANEL, bg=QColor(c["bg"]),
                               border=QColor(c["check"]))
        frame.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)
        self.body = QVBoxLayout(frame)
        self.body.setSpacing(12)
        self.body.setContentsMargins(22, 18, 22, 16)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        host = self.parentWidget()
        if host is not None:
            geo = self.frameGeometry()
            geo.moveCenter(host.window().frameGeometry().center())
            self.move(geo.topLeft())


class _HudMessage(HudPopover):
    """Title, wrapped text, and one or two keys: `yes` accepts, `no` rejects.
    With a `no`, Enter answers `no` -- the safe choice."""

    def __init__(self, parent: Optional[QWidget], title: str, text: str,
                 yes: str, no: Optional[str] = None) -> None:
        super().__init__(parent)
        c = self._c
        self.body.addWidget(_eyebrow(title, c["meta"]))
        msg = QLabel(text)
        msg.setTextFormat(Qt.TextFormat.PlainText)
        msg.setWordWrap(True)
        msg.setFont(font_mono(10))
        msg.setStyleSheet(f"color: {c['text']};")
        self.body.addWidget(msg)
        row = QHBoxLayout()
        row.addStretch()
        yes_btn = _key(yes, "launch")
        yes_btn.clicked.connect(self.accept)
        default = yes_btn
        if no is not None:
            no_btn = _key(no)
            no_btn.clicked.connect(self.reject)
            row.addWidget(no_btn)
            default = no_btn
        row.addWidget(yes_btn)
        default.setDefault(True)
        default.setFocus()
        self.body.addLayout(row)


def _run_dialog(dlg: QDialog) -> bool:
    """exec() a modal dialog, then free it -- it's parented to a long-lived
    window, so it would otherwise linger, hidden, until EDLS exits. The
    deletion waits for the event loop, so the caller can still read it.
    True if it was accepted."""
    try:
        return dlg.exec() == QDialog.DialogCode.Accepted
    finally:
        dlg.deleteLater()


def hud_notice(parent: Optional[QWidget], title: str, text: str) -> None:
    """HUD stand-in for QMessageBox.information: a title, the text, and OK."""
    _run_dialog(_HudMessage(parent, title, text, "OK"))


def hud_confirm(parent: Optional[QWidget], title: str, text: str,
                yes: str = "YES", no: str = "CANCEL") -> bool:
    """HUD stand-in for QMessageBox.question. True only if `yes` was chosen."""
    return _run_dialog(_HudMessage(parent, title, text, yes, no))


# ── Add / edit app dialog ─────────────────────────────────────────────────────
class AppEntryDialog(HudPopover):
    """Modal editor for a single companion app: a Name plus a browsable file
    path. The process name is derived from the file (see `_derive_process`), so
    it isn't shown here. A problem with the entry shows inline, in red."""

    def __init__(self, name: str = "", path: str = "", *,
                 taken_names: Optional[list[str]] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, width=480)
        self.setWindowTitle("Edit app" if name else "Add app")
        # Names already in use (case-insensitive) — a name is the key for status
        # routing and launch tracking, so duplicates would collide silently.
        self._taken = {n.strip().lower() for n in (taken_names or [])}
        c = self._c

        self.body.addWidget(_eyebrow("Edit app" if name else "Add app", c["meta"]))

        self.name_edit = QLineEdit(name)
        self.name_edit.setFont(font_mono(10))
        self.body.addLayout(_stacked_field("NAME", self.name_edit, c))

        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        self.path_edit = QLineEdit(path)
        self.path_edit.setFont(font_mono(10))
        browse_btn = _key("BROWSE…")
        browse_btn.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse_btn)
        self.body.addLayout(_stacked_field("FILE", path_row, c))

        self.error_lbl = QLabel()
        self.error_lbl.setTextFormat(Qt.TextFormat.PlainText)   # quotes the name
        self.error_lbl.setWordWrap(True)
        self.error_lbl.setFont(font_mono(9))
        self.error_lbl.setStyleSheet(f"color: {c['error']};")
        self.error_lbl.hide()
        self.body.addWidget(self.error_lbl)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel_btn = _key("CANCEL")
        save_btn = _key("SAVE", "launch")
        cancel_btn.clicked.connect(self.reject)
        save_btn.clicked.connect(self._on_accept)
        save_btn.setDefault(True)
        btns.addWidget(cancel_btn)
        btns.addWidget(save_btn)
        self.body.addLayout(btns)

    def _browse(self) -> None:
        start = os.path.dirname(self.path_edit.text().strip().strip('"'))
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
            self.error_lbl.setText(err.replace("\n", " "))
            self.error_lbl.show()
            self.name_edit.setFocus()
            return
        self.accept()

    def values(self) -> tuple[str, str]:
        """(name, path), both stripped -- the path of quotes too, which
        Explorer's "Copy as path" wraps it in."""
        return (self.name_edit.text().strip(),
                self.path_edit.text().strip().strip('"').strip())


# ── Settings dialog ───────────────────────────────────────────────────────────
# Hover help for the Settings toggles, keyed by the toggle's attribute name:
# (shown, text). `shown` is a development switch, not a user setting -- flip it
# to hide a tip without losing its text.
_TOGGLE_TIPS: dict[str, tuple[bool, str]] = {
    "toggle_inactive": (True,
        "Disabled apps still get a card on the main window, so you can see "
        "whether they're running. Off: only enabled apps get a card."),
    "toggle_scope": (True,
        "On: SHUTDOWN and the end-of-session teardown close every enabled app, "
        "even ones you started yourself.\nOff: only apps EDLS started, or took "
        "over because they were already running when you pressed LAUNCH (or "
        "their START), are closed.\nSteam and Elite: Dangerous are never "
        "closed."),
    "toggle_admin": (True,
        "Some tools, like EDHM-UI, need administrator rights.\nOn: Windows "
        "asks you (the UAC prompt) when EDLS starts one, and again if EDLS has "
        "to close it.\nOff: EDLS never asks, so those tools aren't started, "
        "and EDLS can't close one that's running as administrator."),
    "toggle_close_exit": (True,
        "When Elite: Dangerous exits, EDLS closes your tools and then closes "
        "itself after a short countdown. Off: the window stays open."),
    "toggle_close_shutdown": (True,
        "After SHUTDOWN closes your tools, EDLS closes itself after a short "
        "countdown. Off: the window stays open."),
    "toggle_updates": (True,
        "At startup EDLS asks GitHub whether a newer version is out and, if "
        "so, shows an 'update' link next to the version. EDLS never downloads "
        "or installs anything itself.\nOff: EDLS never checks."),
    "toggle_light": (False,
        "Lightens the window, header and bottom bar. The status and log "
        "panes stay dark."),
}


class SettingsDialog(QDialog):
    """Data-driven settings editor (collapses the two dialogs into one).
    In first-run mode it shows a welcome header and all apps checked by default."""

    saved = Signal(object)   # emits updated Settings
    locked_attempt = Signal()   # tried to edit the app list during a session
    theme_preview = Signal(object)   # switched the live Theme (or put it back)

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
        self._first_run = first_run
        self.setWindowTitle("EDLS — First-Time Setup" if first_run else "EDLS — Settings")
        self.setModal(True)
        self.setMinimumSize(640, 560)
        # Open tall enough to show a good run of the app list (first run has
        # the intro text too), but never taller than the screen.
        avail = self.screen().availableGeometry().height() - 60
        self.resize(660, min(780 if first_run else 700, avail))
        self.setObjectName("settingsDialog")
        self._default_cmdr = config.Settings().cmdr_name
        # The Light mode toggle previews its theme live (see _preview_theme);
        # closing any way but Save puts this one back (see done).
        self._open_theme = _live_theme()
        self._finished = False
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._body: Optional[QWidget] = None
        self._build(settings)

    def _build(self, state: Settings,
               rows: Optional[list[tuple]] = None) -> None:
        """Build the dialog's contents from `state` -- the app list from `rows`
        (see _rows) when given -- in the active theme. The colours are baked in
        here, so a theme preview calls this again and swaps the contents."""
        # Styled after the design's Settings panel. It follows the active theme
        # (see _dialog_colors); the window keeps its native title bar.
        self._c = c = _dialog_colors()
        # Dim brush for the secondary (process/path) cells, so the app NAME reads
        # as primary and the rest recedes — mirrors the design's name/proc split.
        self._row_dim = QColor(c["meta"])
        self.setStyleSheet(_dialog_qss(c))

        body = QWidget()
        vbox = QVBoxLayout(body)
        vbox.setSpacing(12)
        vbox.setContentsMargins(22, 18, 22, 16)

        if self._first_run:
            welcome = QLabel("WELCOME TO EDLS, CMDR")
            welcome.setFont(font_display(16))
            welcome.setStyleSheet(f"color: {c['accent']}; letter-spacing: 1px;")
            vbox.addWidget(welcome)
            desc = QLabel(
                "Select which companion apps to enable on launch. "
                "You can add or remove them at any time in Settings.\n\n"
                "Please report any crashes using the 'report issue' link. o7"
            )
            desc.setWordWrap(True)
            desc.setFont(font_mono(10))
            desc.setStyleSheet(f"color: {c['label']};")
            vbox.addWidget(desc)
        else:
            vbox.addWidget(_eyebrow("Settings", c["meta"]))

        # CMDR name. The default name is ghost (placeholder) text, never real
        # text, so clearing the field and saving puts the default back.
        custom = state.cmdr_name != self._default_cmdr
        self.cmdr_edit = QLineEdit(state.cmdr_name if custom else "")
        self.cmdr_edit.setPlaceholderText(self._default_cmdr)
        self.cmdr_edit.setFont(font_mono(11))
        vbox.addLayout(_stacked_field("CMDR NAME", self.cmdr_edit, c))

        # Boolean options as HUD toggles (label left, switch right), each in
        # its own chamfered box with a gap between, so the rows read apart.
        toggles = QVBoxLayout()
        toggles.setSpacing(8)
        vbox.addLayout(toggles)

        def _toggle_row(key: str, text: str, checked: bool,
                        kind: type = ToggleSwitch) -> ToggleSwitch:
            frame = ChamferedFrame(chamfer=CHAMFER_CARD, bg=QColor(c["field"]),
                                   border=QColor(c["edge"]))
            frame.setContentsMargins(0, 0, 0, 0)
            row = QHBoxLayout(frame)
            row.setContentsMargins(12, 6, 10, 6)
            row.setSpacing(10)
            lbl = QLabel(text)
            lbl.setFont(font_mono(10))
            row.addWidget(lbl)
            row.addStretch()
            shown, tip = _TOGGLE_TIPS.get(key, (False, ""))
            if shown and tip:
                # On the row, so hovering anywhere on it shows the tip (children
                # without a tip of their own pass the hover up).
                frame.setToolTip(tip)
                row.addWidget(InfoMark())
            tog = kind(checked)
            row.addWidget(tog)
            toggles.addWidget(frame)
            return tog

        self.toggle_inactive = _toggle_row(
            "toggle_inactive",
            "Show cards for apps that aren't enabled",
            state.show_inactive_cards)
        self.toggle_scope = _toggle_row(
            "toggle_scope",
            "Shutdown closes all enabled apps (not just those EDLS launched)",
            state.shutdown_scope is ShutdownScope.ALL_ENABLED)
        self.toggle_admin = _toggle_row(
            "toggle_admin",
            "Ask for admin rights when a tool needs them",
            state.allow_elevation)
        self.toggle_close_exit = _toggle_row(
            "toggle_close_exit",
            "Close EDLS when the game exits",
            state.close_on_game_exit)
        self.toggle_close_shutdown = _toggle_row(
            "toggle_close_shutdown",
            "Close EDLS after SHUTDOWN",
            state.close_on_shutdown)
        self.toggle_updates = _toggle_row(
            "toggle_updates",
            "Check for EDLS updates at startup",
            state.check_for_updates)
        self.toggle_light = _toggle_row(
            "toggle_light",
            "Light mode",
            state.theme is Theme.LIGHT, ThemeToggle)
        self.toggle_light.toggled.connect(self._on_theme_toggled)

        # Companion-app list. Styled as a flat HUD row-list rather than a grid:
        # no column headers, no gridlines, zebra rows, hairline container.
        vbox.addSpacing(4)
        apps_lbl = _eyebrow("Companion apps", c["meta"])
        if self._locked:
            apps_hdr = QHBoxLayout()
            apps_hdr.addWidget(apps_lbl)
            apps_hdr.addStretch()
            lock_lbl = QLabel("LOCKED WHILE A SESSION IS RUNNING")
            lock_lbl.setFont(font_display(7))
            lock_lbl.setStyleSheet(f"color: {c['check']}; letter-spacing:2px;")
            apps_hdr.addWidget(lock_lbl)
            vbox.addLayout(apps_hdr)
        else:
            vbox.addWidget(apps_lbl)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["ON", "NAME", "PROCESS", "PATH"])
        hh = self.table.horizontalHeader()
        hh.setVisible(False)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 40)   # the HudCheckBox column
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
        self.table.setFont(font_mono(10))
        if rows is None:
            self._populate_table(state.apps)
        else:
            self._fill(rows)
        vbox.addWidget(self.table, 1)

        # add / edit / remove keys
        tbl_btns = QHBoxLayout()
        tbl_btns.setSpacing(8)
        add_btn = _key("+ ADD", height=28)
        edit_btn = _key("EDIT", height=28)
        # The pencil is drawn as a coloured icon (via 'Segoe UI Symbol') rather
        # than a text glyph: in the button's inherited font U+270E falls back to
        # an emoji font that ignores the colour and clashes with the theme.
        edit_btn.setIcon(_glyph_icon("✎", QColor(_bar_colors("secondary")["fg"]),
                                     px=14))
        edit_btn.setIconSize(QSize(14, 14))
        rem_btn = _key("— REMOVE", height=28)
        add_btn.clicked.connect(self._add_row)
        edit_btn.clicked.connect(self._edit_selected)
        rem_btn.clicked.connect(self._remove_row)
        tbl_btns.addWidget(add_btn)
        tbl_btns.addWidget(edit_btn)
        tbl_btns.addWidget(rem_btn)
        tbl_btns.addStretch()
        vbox.addLayout(tbl_btns)

        # Bottom bar: utility keys (reset + open log folder) on the left,
        # CANCEL / SAVE on the right. Reset/folder don't close the dialog.
        # SAVE is the default button, so Enter in the CMDR field still saves.
        reset_btn = _key("RESTORE DEFAULTS")
        folder_btn = _key("OPEN LOG FOLDER")
        reset_btn.clicked.connect(self._reset_defaults)
        folder_btn.clicked.connect(self._open_data_dir)
        cancel_btn = _key("CANCEL")
        save_btn = _key("BEGIN" if self._first_run else "SAVE", "launch")
        cancel_btn.clicked.connect(self.reject)
        save_btn.clicked.connect(self._save)
        save_btn.setDefault(True)

        vbox.addSpacing(4)
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        bottom.addWidget(reset_btn)
        bottom.addWidget(folder_btn)
        bottom.addStretch()
        bottom.addWidget(cancel_btn)
        bottom.addWidget(save_btn)
        vbox.addLayout(bottom)

        if self._body is None:
            self._outer.addWidget(body)
        else:   # a rebuild: swap the old contents out
            self._outer.replaceWidget(self._body, body)
            self._body.hide()
            self._body.deleteLater()
        self._body = body

    def _on_theme_toggled(self, _on: bool) -> None:
        # Let the knob finish its slide before everything changes colour.
        QTimer.singleShot(ToggleSwitch.SLIDE_MS, self, self._preview_theme)

    def _preview_theme(self) -> None:
        """Switch the whole app to the Light mode toggle's theme now, as a
        preview: Save keeps it, anything else puts the opening theme back. The
        dialog is rebuilt in the new colours, keeping everything typed, ticked
        and edited so far."""
        want = Theme.LIGHT if self.toggle_light.isChecked() else Theme.DARK
        # Closed during the slide, or flipped back again before it landed.
        if self._finished or want is _live_theme():
            return
        self._set_live_theme(want)
        rows, row = self._rows(), self.table.currentRow()
        scroll = self.table.verticalScrollBar().value()
        focus = self.toggle_light.hasFocus()
        ring = self.toggle_light._kb_focus
        self._build(self._form(), rows)
        self.table.setCurrentCell(row, 1)
        self.table.verticalScrollBar().setValue(scroll)
        if focus:
            self.toggle_light.setFocus(Qt.FocusReason.TabFocusReason if ring
                                       else Qt.FocusReason.OtherFocusReason)

    def _set_live_theme(self, theme: Theme) -> None:
        apply_theme(theme)
        _apply_app_palette()
        self.theme_preview.emit(theme)

    def done(self, r: int) -> None:
        """Every close (Save, Cancel, Esc, the X) ends here. Anything but Save
        puts back the theme the dialog opened in."""
        self._finished = True
        if (r != QDialog.DialogCode.Accepted
                and _live_theme() is not self._open_theme):
            self._set_live_theme(self._open_theme)
        super().done(r)

    def _set_row(self, r: int, name: str, process: str,
                 path: str, enabled: bool, origin: str = "") -> None:
        """Fill an (already inserted) table row: a centred ON checkbox plus the
        name/process/path cells. Shared by initial fill, + ADD, and reset.
        `origin` is the registry name this row derives from (empty for custom
        apps); it's stashed on the name cell so a rename still re-attaches the
        default's resolver/path on save (see `_save`)."""
        chk = HudCheckBox()
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
        # An empty item under the checkbox so the row's separator line and
        # selection tint run under the checkbox column too.
        self.table.setItem(r, 0, QTableWidgetItem())
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
        self._fill([(a.name, a.process_name, a.path or "", a.enabled,
                     a.origin or (a.name if a.name in config.REGISTRY_BY_NAME
                                  else ""))
                    for a in apps])

    def _fill(self, rows: list[tuple]) -> None:
        """Replace all table rows with `rows`, each one _set_row's arguments."""
        self.table.setRowCount(0)
        for r, row in enumerate(rows):
            self.table.insertRow(r)
            self._set_row(r, *row)

    def _rows(self) -> list[tuple]:
        """The table as it stands: (name, process, path, enabled, origin) per
        row -- origin being the registry name stashed on the name cell."""
        def _text(row: int, col: int) -> str:
            item = self.table.item(row, col)
            return item.text().strip() if item else ""

        rows = []
        for r in range(self.table.rowCount()):
            cell = self.table.cellWidget(r, 0)
            chk = cell.findChild(QCheckBox) if cell else None
            name_item = self.table.item(r, 1)
            origin = (name_item.data(Qt.ItemDataRole.UserRole) or "") if name_item else ""
            rows.append((_text(r, 1), _text(r, 2), _text(r, 3),
                         chk.isChecked() if chk else True, origin))
        return rows

    def _existing_names(self, exclude_row: Optional[int] = None) -> list[str]:
        """Names the add/edit dialog won't accept: every app in the table (col
        1) except `exclude_row`, plus Steam and Elite -- the built-in cards'
        keys, which a custom app of the same name would collide with."""
        names = [KEY_STEAM, KEY_ELITE]
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
        hud_notice(self, "App list locked", self.LOCKED_MSG)
        return True

    def _add_row(self) -> None:
        if self._blocked():
            return
        dlg = AppEntryDialog(taken_names=self._existing_names(), parent=self)
        if not _run_dialog(dlg):
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
        if not _run_dialog(dlg):
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
        if not hud_confirm(
                self, "Restore defaults",
                "Reset all settings, apps, and config to their defaults?\n\n"
                "Nothing is saved until you press Save.", yes="RESET"):
            return
        defaults = config.default_settings()
        self.cmdr_edit.clear()   # back to the default, shown as ghost text
        self.toggle_inactive.setChecked(defaults.show_inactive_cards)
        self.toggle_scope.setChecked(
            defaults.shutdown_scope is ShutdownScope.ALL_ENABLED)
        self.toggle_admin.setChecked(defaults.allow_elevation)
        self.toggle_close_exit.setChecked(defaults.close_on_game_exit)
        self.toggle_close_shutdown.setChecked(defaults.close_on_shutdown)
        self.toggle_updates.setChecked(defaults.check_for_updates)
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

    def _form(self) -> Settings:
        """The settings as the form stands, the app list aside (see _rows)."""
        return replace(
            self._settings,
            # An empty field (ghost text showing) means the default name.
            cmdr_name=self.cmdr_edit.text().strip() or self._default_cmdr,
            show_inactive_cards=self.toggle_inactive.isChecked(),
            shutdown_scope=(ShutdownScope.ALL_ENABLED
                            if self.toggle_scope.isChecked()
                            else ShutdownScope.LAUNCHED_ONLY),
            allow_elevation=self.toggle_admin.isChecked(),
            close_on_game_exit=self.toggle_close_exit.isChecked(),
            close_on_shutdown=self.toggle_close_shutdown.isChecked(),
            check_for_updates=self.toggle_updates.isChecked(),
            theme=Theme.LIGHT if self.toggle_light.isChecked() else Theme.DARK,
        )

    def _save(self) -> None:
        new_apps: list[AppDescriptor] = []
        for name, process, path, enabled, origin in self._rows():
            if not name:
                continue
            # Prefer the row's registry origin so a renamed default still re-links
            # to its resolver/default path; fall back to the (unchanged) name.
            reg = config.REGISTRY_BY_NAME.get(origin) or config.REGISTRY_BY_NAME.get(name)
            new_apps.append(AppDescriptor(
                name=name,
                process_name=process or name,
                path=path or (reg.path if reg else None),
                enabled=enabled,
                resolver=reg.resolver if reg else None,
                origin=reg.name if reg else "",
            ))

        removed = config.recompute_removed_defaults(
            new_apps, self._settings.removed_defaults)

        updated = replace(self._form(), apps=new_apps,
                          removed_defaults=removed, setup_complete=True)
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
        self._counting = False   # the close countdown runs: LAUNCH is STAY OPEN
        # Last state per card. Rebuilding the cards (Settings save, theme switch)
        # re-applies these, so "Waiting..." or ERROR survive the rebuild.
        self._card_states: dict[str, tuple[AppState, Optional[int]]] = {}
        # The newer version the update check found -- kept here so a theme
        # rebuild restores the link.
        self._update: Optional[str] = None

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
        engine.on_update.connect(self._on_update)
        engine.on_countdown.connect(self._on_countdown)

        self._build_content()

        self._log.append("Elite: Dangerous Launch Suite ready.", "success")
        for warning in config.LOAD_WARNINGS:
            self._log.append(warning, "warning")

    def _visible_apps(self) -> list[AppDescriptor]:
        """Apps that get a status card: the enabled ones, plus — when the CMDR
        asks for them — the ones they haven't enabled (shown read-only), after
        all the enabled ones. Both groups keep Settings order. Both panel-build
        paths go through here so the rule can't drift between them."""
        if self._settings.show_inactive_cards:
            return sorted(self._settings.apps, key=lambda a: not a.enabled)
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
        # stop each pane at its minimum height instead of collapsing it to 0
        self._splitter.setChildrenCollapsible(False)
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
        self._btns.update_clicked.connect(self._open_update)
        self._btns.set_busy(self._busy, self._counting)
        self._show_update_link()
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
            not self._busy and not self._counting
            and elite is AppState.WAITING)

    def _on_launch_clicked(self) -> None:
        if self._counting:   # it reads STAY OPEN
            self._engine.stay_open()
            return
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
        self._btns.set_busy(busy, self._counting)
        self._update_launch_mute()

    @Slot(bool)
    def _on_countdown(self, counting: bool) -> None:
        self._counting = counting
        self._btns.set_busy(self._busy, counting)
        self._update_launch_mute()

    @Slot()
    def _on_quit(self) -> None:
        # The countdown ran out with a dialog open (Settings, a popover, the
        # file picker). Closing now would pull the window out from under it,
        # so EDLS stays open instead.
        if QApplication.activeModalWidget() is not None:
            self._on_countdown(False)
            self._log.append("EDLS stayed open - a dialog was open.", "info")
            return
        self.close()

    # ---- updates ----
    def _show_update_link(self) -> None:
        self._btns.show_update(self._update)

    @Slot(str)
    def _on_update(self, version: str) -> None:
        self._update = version
        self._show_update_link()
        self._log.append(f"EDLS {version} is available - click "
                         "'update' next to the version below.", "success")

    def _open_update(self) -> None:
        """The update link: how to update, and the way to the release page.
        EDLS doesn't download or install anything itself."""
        version = self._update
        if version and hud_confirm(
                self, "Update available",
                f"EDLS {version} is out - you have {config.APP_VERSION}.\n\n"
                f"To update, download EDLS-Setup-{version}.exe from the "
                "release page (under Assets) and run it. It installs over "
                "this version and keeps your settings. Setup closes EDLS for "
                "you; Steam, the game and your tools stay open.",
                yes="RELEASE PAGE", no="LATER"):
            QDesktopServices.openUrl(QUrl(config.LATEST_RELEASE_URL))

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
        dlg.theme_preview.connect(self._set_theme)
        dlg.locked_attempt.connect(lambda: self._log.append(
            "The companion app list is locked during a session - it unlocks "
            "when the game closes, or when EDLS restarts.", "warning"))
        _run_dialog(dlg)

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
    def _set_theme(self, theme: Theme) -> None:
        """Switch the window to `theme` live (Settings' preview, or a save).
        A theme switch must rebuild all content so every baked-in stylesheet
        picks up the new palette. Carry the activity log across the swap (its
        per-line accent colours are theme-independent). Card states carry over
        via _card_states."""
        log_html = self._log.log.toHtml()
        apply_theme(theme)
        _apply_app_palette()
        self._build_content()
        self._log.log.setHtml(log_html)
        sb = self._log.log.verticalScrollBar()
        sb.setValue(sb.maximum())
        self._bg.update()

    @Slot(object)
    def _on_settings_saved(self, updated: Settings) -> None:
        # Settings' live preview has usually switched the theme already.
        theme_changed = updated.theme is not _live_theme()
        self._settings = updated
        self._save_settings(updated)
        self._engine.settings = updated

        if theme_changed:
            self._set_theme(updated.theme)
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
    # After boot, so the request never competes with the window appearing.
    QTimer.singleShot(follow_delay + 1000, engine.check_for_updates)

    return app.exec()
