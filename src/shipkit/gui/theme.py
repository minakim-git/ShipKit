"""디자인 시스템: 색·글꼴·아이콘·스타일시트를 한곳에서 관리한다.

밝은 배경 위에 흰 카드, 둥근 모서리, 강조색(인디고) 하나로 정리한 요즘 앱 스타일. 어두운 테마도 같은 이름의 색 묶음으로
만들어 두고 apply() 로 바꾼다. 다른 화면은 theme.PRIMARY 처럼 이 모듈의 이름으로 색을 읽으므로, 테마를 바꾸면
다음에 그릴 때부터 새 색이 쓰인다. 미리 만들어 둔 아이콘은 bind_icon() 으로 등록해 두면 테마가 바뀔 때 다시 그린다.
"""
from __future__ import annotations

import re
import tempfile
import weakref
from importlib import resources
from pathlib import Path
from typing import Callable, Dict, List, Tuple

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer

# ---------------------------------------------------------------------- 색 묶음
# 이름은 쓰임새 기준. HEAD: 표 머리글·옅은 바탕, LINE: 행 구분선, LABEL: 입력 칸 이름·보조 버튼 글자,
# BTN: 채운 버튼·체크 상자 바탕(어두운 테마에서는 글자용 PRIMARY 보다 진하게)
LIGHT: Dict[str, str] = {
    "BG": "#F4F6FA", "SURFACE": "#FFFFFF", "BORDER": "#E4E7EC", "BORDER_STRONG": "#D0D5DD", "HOVER": "#F2F4F7",
    "TEXT": "#101828", "MUTED": "#667085", "SUBTLE": "#98A2B3", "LABEL": "#344054",
    "HEAD": "#F9FAFB", "LINE": "#F2F4F7", "ALT": "#FAFBFC",
    "BTN": "#4F46E5", "BTN_HOVER": "#4338CA", "BTN_DISABLED": "#C7D2FE",
    "PRIMARY": "#4F46E5", "PRIMARY_HOVER": "#4338CA", "PRIMARY_SOFT": "#EEF2FF", "PRIMARY_DISABLED": "#C7D2FE",
    "TIP_BG": "#101828",
    "OK_BORDER": "#ABEFC6", "ERR_BORDER": "#FECDCA", "DANGER_HOVER_BORDER": "#FDA29B",
    "WARN_BORDER": "#FEDF89", "WARN_HOVER": "#FEF0C7", "WARN_HOVER_BORDER": "#FEC84B",
}
DARK: Dict[str, str] = {
    "BG": "#111318", "SURFACE": "#1A1D24", "BORDER": "#2A2F3A", "BORDER_STRONG": "#3A4150", "HOVER": "#232833",
    "TEXT": "#E6E8EE", "MUTED": "#9AA3B2", "SUBTLE": "#6B7385", "LABEL": "#C9CED8",
    "HEAD": "#20242D", "LINE": "#242832", "ALT": "#1D2028",
    "BTN": "#6366F1", "BTN_HOVER": "#7C7FF5", "BTN_DISABLED": "#34366B",
    "PRIMARY": "#8B93FA", "PRIMARY_HOVER": "#A5ACFC", "PRIMARY_SOFT": "#272A4A", "PRIMARY_DISABLED": "#3B3F6B",
    "TIP_BG": "#2E3440",
    "OK_BORDER": "#1F5133", "ERR_BORDER": "#5C2626", "DANGER_HOVER_BORDER": "#7F2D2D",
    "WARN_BORDER": "#5A4515", "WARN_HOVER": "#3A2E12", "WARN_HOVER_BORDER": "#7A5D18",
}
# 상태 색: (글자색, 배경색)
LIGHT_TONES: Dict[str, Tuple[str, str]] = {
    "success": ("#027A48", "#ECFDF3"), "warning": ("#B54708", "#FFFAEB"), "danger": ("#B42318", "#FEF3F2"),
    "info": ("#175CD3", "#EFF8FF"), "primary": ("#4F46E5", "#EEF2FF"), "neutral": ("#344054", "#F2F4F7"),
}
DARK_TONES: Dict[str, Tuple[str, str]] = {
    "success": ("#4ADE80", "#14291D"), "warning": ("#FBBF24", "#2E2410"), "danger": ("#F87171", "#341A1A"),
    "info": ("#60A5FA", "#13243A"), "primary": ("#8B93FA", "#272A4A"), "neutral": ("#C9CED8", "#262B35"),
}
THEMES = {"light": (LIGHT, LIGHT_TONES), "dark": (DARK, DARK_TONES)}
SETTINGS = (("system", "시스템 설정 따르기"), ("light", "밝게"), ("dark", "어둡게"))  # 앱 설정의 선택지

FONT_FAMILY = '"Segoe UI Variable Text", "Segoe UI", "Malgun Gothic"'

# 지금 쓰는 색 (use() 가 바꾼다). 편집기가 이름을 알 수 있게 밝은 테마 값으로 먼저 둔다.
MODE = "light"
BG = SURFACE = BORDER = BORDER_STRONG = HOVER = TEXT = MUTED = SUBTLE = LABEL = HEAD = LINE = ALT = ""
BTN = BTN_HOVER = BTN_DISABLED = PRIMARY = PRIMARY_HOVER = PRIMARY_SOFT = PRIMARY_DISABLED = TIP_BG = ""
OK_BORDER = ERR_BORDER = DANGER_HOVER_BORDER = WARN_BORDER = WARN_HOVER = WARN_HOVER_BORDER = ""
TONES: Dict[str, Tuple[str, str]] = {}

_listeners: List[Callable[[], None]] = []
_bound: List[Tuple["weakref.ref", tuple]] = []


def use(mode: str) -> None:
    """색 묶음만 바꾼다 (화면에 적용은 apply)."""
    global MODE, TONES
    colors, tones = THEMES[mode]
    MODE = mode
    globals().update(colors)
    TONES = dict(tones)


use("light")


def is_dark() -> bool:
    return MODE == "dark"


def system_mode() -> str:
    """Windows 앱 모드(밝게/어둡게)."""
    app = QGuiApplication.instance()
    if app is None:
        return "light"
    return "dark" if QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark else "light"


def resolve(setting: str) -> str:
    return system_mode() if setting not in ("light", "dark") else setting


def apply(app, setting: str = "system") -> str:
    """설정(system/light/dark)에 맞는 테마를 앱 전체에 적용하고, 등록해 둔 아이콘·화면을 다시 그린다."""
    use(resolve(setting))
    app.setPalette(_palette())
    app.setStyleSheet(stylesheet())
    for ref, args in list(_bound):
        obj = ref()
        try:
            if obj is None:
                raise RuntimeError
            obj.setIcon(svg_icon(*args))
        except RuntimeError:  # 이미 닫힌 위젯
            _bound.remove((ref, args))
    for listener in list(_listeners):
        try:
            listener()
        except RuntimeError:  # 이미 닫힌 창의 함수
            _listeners.remove(listener)
    return MODE


def on_change(listener: Callable[[], None]) -> None:
    """테마가 바뀌면 부를 함수 (미리 칠해 둔 글자색 등을 다시 그릴 때)."""
    _listeners.append(listener)


def _palette() -> QPalette:
    """스타일시트가 닿지 않는 곳(메시지 창, 메뉴, 입력 칸 안내 글자 등)의 색."""
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: BG, QPalette.ColorRole.WindowText: TEXT, QPalette.ColorRole.Base: SURFACE,
        QPalette.ColorRole.AlternateBase: HEAD, QPalette.ColorRole.Text: TEXT, QPalette.ColorRole.Button: SURFACE,
        QPalette.ColorRole.ButtonText: TEXT, QPalette.ColorRole.Highlight: PRIMARY_SOFT,
        QPalette.ColorRole.HighlightedText: TEXT, QPalette.ColorRole.ToolTipBase: TIP_BG,
        QPalette.ColorRole.ToolTipText: "#FFFFFF", QPalette.ColorRole.PlaceholderText: SUBTLE,
        QPalette.ColorRole.Link: PRIMARY, QPalette.ColorRole.BrightText: "#FFFFFF",
        QPalette.ColorRole.Light: BORDER, QPalette.ColorRole.Midlight: BORDER, QPalette.ColorRole.Mid: BORDER_STRONG,
        QPalette.ColorRole.Dark: BORDER_STRONG, QPalette.ColorRole.Shadow: "#000000",
    }
    for role, color in roles.items():
        pal.setColor(role, QColor(color))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(SUBTLE))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(SUBTLE))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(SUBTLE))
    return pal


# ---------------------------------------------------------------------- 그림 파일
def assets_dir() -> Path:
    return Path(str(resources.files("shipkit") / "gui" / "assets"))


def _color_map() -> Dict[str, str]:
    """밝은 테마 색 -> 지금 테마 색 (그림 파일에 직접 적힌 색을 바꿀 때)."""
    if MODE == "light":
        return {}
    pairs = list(zip(LIGHT.values(), DARK.values()))
    for key in LIGHT_TONES:
        pairs += list(zip(LIGHT_TONES[key], DARK_TONES[key]))
    mapping: Dict[str, str] = {}
    for light, dark in pairs:
        mapping.setdefault(light.lower(), dark)  # 같은 색이 여러 이름에 있으면 앞의 것 (BTN 이 PRIMARY 보다 앞)
    return mapping


def _recolor(svg: str) -> str:
    mapping = _color_map()
    if not mapping:
        return svg
    return re.sub(r"#[0-9A-Fa-f]{6}\b", lambda m: mapping.get(m.group(0).lower(), m.group(0)), svg)


def asset(name: str) -> str:
    """스타일시트 url() 에 넣을 경로 (슬래시). 어두운 테마에서는 색을 바꾼 사본을 임시 폴더에 만든다."""
    source = assets_dir() / name
    if MODE == "light":
        return source.as_posix()
    target = Path(tempfile.gettempdir()) / "shipkit-theme" / MODE / name
    text = _recolor(source.read_text(encoding="utf-8"))
    if not target.is_file() or target.read_text(encoding="utf-8") != text:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return target.as_posix()


def color(key: str) -> str:
    """색 이름(PRIMARY 등)·상태 이름(warning 등)·색 값(#fff)을 지금 테마의 색 값으로."""
    if key in TONES:
        return TONES[key][0]
    return globals().get(key, key) if key.isupper() else key


def svg_icon(name: str, color_key: str, size: int = 20, selected_key: str = "") -> QIcon:
    """stroke="currentColor" SVG 를 원하는 색으로 그린 아이콘. selected_key 를 주면 선택 상태 색도 만든다.
    색은 이름(PRIMARY, warning)이나 값(#FFFFFF)으로 준다. 이름으로 주면 지금 테마의 색이 쓰인다."""
    source = _recolor((assets_dir() / name).read_text(encoding="utf-8"))
    icon = QIcon()
    for mode, c in ((QIcon.Mode.Normal, color_key), (QIcon.Mode.Selected, selected_key or color_key)):
        renderer = QSvgRenderer(QByteArray(source.replace("currentColor", color(c) if c else "").encode("utf-8")))
        pm = QPixmap(QSize(size * 2, size * 2))  # 고해상도 화면에서도 선명하게
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        renderer.render(painter)
        painter.end()
        pm.setDevicePixelRatio(2)
        icon.addPixmap(pm, mode)
    return icon


def bind_icon(obj, name: str, color_key: str, size: int = 20, selected_key: str = "") -> None:
    """obj.setIcon(아이콘) 을 하고, 테마가 바뀌면 같은 아이콘을 새 색으로 다시 넣는다."""
    args = (name, color_key, size, selected_key)
    obj.setIcon(svg_icon(*args))
    if len(_bound) > 300:  # 닫힌 위젯 정리 (기록 화면처럼 자주 다시 만드는 곳)
        _bound[:] = [b for b in _bound if b[0]() is not None]
    _bound.append((weakref.ref(obj), args))


def logo_pixmap(size: int) -> QPixmap:
    renderer = QSvgRenderer(str(assets_dir() / "logo.svg"))
    pm = QPixmap(QSize(size * 2, size * 2))
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    renderer.render(painter)
    painter.end()
    pm.setDevicePixelRatio(2)
    return pm


def tone_color(tone: str) -> QColor:
    return QColor(TONES.get(tone, TONES["neutral"])[0])


def report_colors() -> Dict[str, object]:
    """보고서 창(report.render_html)에 쓸 색."""
    return {"text": TEXT, "muted": MUTED, "border": BORDER, "head": HEAD, "code": HOVER,
            "success": TONES["success"], "warning": TONES["warning"], "danger": TONES["danger"],
            "neutral": TONES["neutral"], "primary": TONES["primary"]}


def stylesheet() -> str:
    chips = "\n".join(
        f'QLabel#chip[tone="{name}"] {{ color: {fg}; background: {bg}; }}' for name, (fg, bg) in TONES.items())
    return f"""
* {{ font-family: {FONT_FAMILY}; font-size: 10pt; color: {TEXT}; }}
QMainWindow, QDialog, QWidget#pageArea {{ background: {BG}; }}
QToolTip {{ background: {TIP_BG}; color: white; border: none; padding: 6px 8px; border-radius: 6px; }}

/* ---------- 사이드바 ---------- */
QWidget#sidebar {{ background: {SURFACE}; border-right: 1px solid {BORDER}; }}
QLabel#brand {{ font-size: 14pt; font-weight: 700; }}
QLabel#brandSub {{ color: {SUBTLE}; font-size: 8.5pt; }}
QListWidget#nav {{ background: transparent; border: none; outline: 0; }}
QListWidget#nav::item {{ color: {MUTED}; padding: 9px 12px; margin: 2px 12px; border-radius: 8px; }}
QListWidget#nav::item:hover {{ background: {HOVER}; color: {TEXT}; }}
QListWidget#nav::item:selected {{ background: {PRIMARY_SOFT}; color: {PRIMARY}; font-weight: 600; }}
QLabel#sideFooter {{ color: {SUBTLE}; font-size: 8.5pt; }}
QLabel#sideCaption {{ color: {SUBTLE}; font-size: 8.5pt; font-weight: 600; padding-left: 2px; }}
QLabel#sideMeta {{ color: {MUTED}; font-size: 9pt; padding-left: 2px; }}
QLabel#sideStatus {{ color: {MUTED}; font-size: 9pt; }}
QComboBox#projectSwitcher {{ background: {HEAD}; border-color: {BORDER}; border-radius: 10px; padding: 0 8px; min-height: 58px; }}
QComboBox#projectSwitcher:hover {{ border-color: {PRIMARY}; background: {SURFACE}; }}
QComboBox#projectSwitcher QAbstractItemView {{ padding: 4px; }}

/* ---------- 글자 ---------- */
QLabel#pageTitle {{ font-size: 18pt; font-weight: 700; }}
QLabel#pageSubtitle {{ color: {MUTED}; }}
QLabel#cardTitle {{ font-size: 11pt; font-weight: 600; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#fieldLabel {{ color: {LABEL}; font-weight: 500; }}
QLabel#chip {{ border-radius: 11px; padding: 3px 10px; font-size: 9pt; font-weight: 600; }}
{chips}

/* ---------- 카드 ---------- */
QFrame#card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 12px; }}
QFrame#banner {{ border-radius: 12px; }}
QFrame#detail {{ background: {HEAD}; border: 1px solid {BORDER}; border-radius: 8px; }}
QFrame#banner[tone="success"] {{ background: {TONES['success'][1]}; border: 1px solid {OK_BORDER}; }}
QFrame#banner[tone="danger"] {{ background: {TONES['danger'][1]}; border: 1px solid {ERR_BORDER}; }}
QFrame#divider {{ background: {BORDER}; max-height: 1px; min-height: 1px; border: none; }}

/* ---------- 입력 ---------- */
QLineEdit, QComboBox, QSpinBox {{
    background: {SURFACE}; border: 1px solid {BORDER_STRONG}; border-radius: 8px;
    padding: 0 10px; min-height: 34px; selection-background-color: {PRIMARY_SOFT}; selection-color: {TEXT};
}}
QPlainTextEdit {{
    background: {SURFACE}; border: 1px solid {BORDER_STRONG}; border-radius: 8px; padding: 8px;
    selection-background-color: {PRIMARY_SOFT}; selection-color: {TEXT};
    font-family: "Cascadia Mono", Consolas, "Malgun Gothic"; font-size: 9.5pt;
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus {{ border: 1px solid {PRIMARY}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QPlainTextEdit:disabled {{
    background: {HEAD}; color: {SUBTLE}; border-color: {BORDER};
}}
QComboBox::drop-down {{ border: none; width: 30px; }}
QComboBox::down-arrow {{ image: url("{asset('chevron-down.svg')}"); width: 14px; height: 14px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; padding: 4px; outline: 0;
    selection-background-color: {PRIMARY_SOFT}; selection-color: {PRIMARY};
}}
QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: none; }}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 5px; border: 1px solid {BORDER_STRONG}; background: {SURFACE}; }}
QCheckBox::indicator:hover {{ border-color: {PRIMARY}; }}
QCheckBox::indicator:checked {{ background: {BTN}; border-color: {BTN}; image: url("{asset('check.svg')}"); }}
QCheckBox:disabled {{ color: {SUBTLE}; }}
QCheckBox::indicator:checked:disabled {{ background: {BTN_DISABLED}; border-color: {BTN_DISABLED}; }}
QCheckBox#switch::indicator {{ width: 36px; height: 20px; border: none; background: transparent; image: url("{asset('switch-off.svg')}"); }}
QCheckBox#switch::indicator:checked {{ image: url("{asset('switch-on.svg')}"); }}

/* ---------- 버튼 ---------- */
QPushButton {{
    background: {SURFACE}; border: 1px solid {BORDER_STRONG}; border-radius: 8px;
    padding: 0 14px; min-height: 34px; font-weight: 500; color: {LABEL};
}}
QPushButton:hover {{ background: {HEAD}; }}
QPushButton:pressed {{ background: {HOVER}; }}
QPushButton:disabled {{ color: {SUBTLE}; border-color: {BORDER}; }}
QPushButton#primary {{ background: {BTN}; border: 1px solid {BTN}; color: white; font-weight: 600; padding: 0 18px; }}
QPushButton#primary:hover {{ background: {BTN_HOVER}; border-color: {BTN_HOVER}; }}
QPushButton#primary:disabled {{ background: {BTN_DISABLED}; border-color: {BTN_DISABLED}; color: {MUTED if MODE == 'dark' else 'white'}; }}
QPushButton#ghost {{ background: transparent; border: none; color: {PRIMARY}; font-weight: 600; padding: 0 8px; }}
QPushButton#ghost:hover {{ background: {PRIMARY_SOFT}; }}
QPushButton#danger {{ color: {TONES['danger'][0]}; }}
QPushButton#danger:hover {{ background: {TONES['danger'][1]}; border-color: {DANGER_HOVER_BORDER}; }}

/* ---------- 세그먼트 탭 ---------- */
QTabWidget::pane {{ border: none; top: 8px; }}
QTabBar {{ qproperty-drawBase: 0; }}
QTabBar::tab {{
    background: {HOVER}; color: {MUTED}; border: 1px solid {HOVER}; padding: 6px 16px; margin: 0;
    font-weight: 500; min-height: 22px;
}}
QTabBar::tab:first {{ border-top-left-radius: 8px; border-bottom-left-radius: 8px; }}
QTabBar::tab:last {{ border-top-right-radius: 8px; border-bottom-right-radius: 8px; }}
QTabBar::tab:only-one {{ border-radius: 8px; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::tab:selected {{ background: {SURFACE}; color: {PRIMARY}; border: 1px solid {BORDER_STRONG}; font-weight: 600; }}

/* ---------- 표 ---------- */
QTableWidget {{
    background: {SURFACE}; border: none; gridline-color: transparent; outline: 0;
    selection-background-color: {PRIMARY_SOFT}; selection-color: {TEXT}; alternate-background-color: {ALT};
}}
QTableWidget::item {{ padding: 4px 8px; border-bottom: 1px solid {LINE}; }}
QHeaderView::section {{
    background: {HEAD}; color: {MUTED}; font-size: 9pt; font-weight: 600;
    border: none; border-bottom: 1px solid {BORDER}; padding: 8px;
}}
QTableCornerButton::section {{ background: {HEAD}; border: none; }}
QListWidget#messages {{ background: transparent; border: none; outline: 0; }}
QListWidget#messages::item {{ padding: 6px 10px; border-radius: 8px; margin: 2px 0; }}

/* ---------- 배포 단계 ---------- */
QPushButton#step {{
    background: transparent; border: 1px solid {BORDER}; border-radius: 15px; min-height: 28px;
    padding: 0 12px; color: {SUBTLE}; font-weight: 600;
}}
QPushButton#step:hover {{ background: {HOVER}; }}
QPushButton#step[state="done"] {{ color: {PRIMARY}; background: {PRIMARY_SOFT}; border-color: {PRIMARY_SOFT}; }}
QPushButton#step[state="current"], QPushButton#step[state="current"]:disabled {{
    color: white; background: {BTN}; border-color: {BTN};
}}
QPushButton#step:disabled {{ border-color: {BORDER}; }}
QFrame#stepLine {{ background: {BORDER_STRONG}; border: none; }}
QFrame#preflight {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px; }}
QFrame#override {{ background: {PRIMARY_SOFT}; border-radius: 8px; }}
QPushButton#chipAction {{
    background: {TONES['warning'][1]}; color: {TONES['warning'][0]}; border: 1px solid {WARN_BORDER}; border-radius: 12px;
    min-height: 24px; max-height: 24px; padding: 0 10px; font-size: 9pt; font-weight: 600;
}}
QPushButton#chipAction:hover {{ background: {WARN_HOVER}; border-color: {WARN_HOVER_BORDER}; }}
QPushButton#chipAction:disabled {{ color: {SUBTLE}; background: {HOVER}; border-color: {BORDER}; }}
QTableWidget:disabled {{ color: {MUTED}; background: {HEAD}; }}
QPushButton#seg {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 15px; min-height: 28px;
    padding: 0 12px; color: {MUTED}; font-weight: 500;
}}
QPushButton#seg:hover {{ background: {HOVER}; }}
QPushButton#seg:checked {{ background: {PRIMARY_SOFT}; color: {PRIMARY}; border-color: {PRIMARY_DISABLED}; font-weight: 600; }}
QTreeWidget {{ background: {SURFACE}; border: none; outline: 0; }}
QTreeWidget::item {{ padding: 6px 4px; border-bottom: 1px solid {LINE}; }}
QTreeView::branch {{ background: transparent; border-bottom: 1px solid {LINE}; }}
QTreeView::branch:has-children:closed {{ image: url("{asset('chevron-right.svg')}"); }}
QTreeView::branch:has-children:open {{ image: url("{asset('chevron-open.svg')}"); }}
QLabel#code {{
    font-family: "Cascadia Mono", Consolas, "Malgun Gothic"; font-size: 9.5pt; color: {TEXT};
}}
QFrame#cmdRow {{ background: {HEAD}; border: 1px solid {BORDER}; border-radius: 8px; }}
QLabel#stepNum {{ background: {PRIMARY_SOFT}; color: {PRIMARY}; border-radius: 12px; font-size: 9pt; font-weight: 700; }}
QLabel#doneTitle {{ font-size: 15pt; font-weight: 700; }}

/* ---------- 기록 ---------- */
QListWidget#records {{ background: transparent; border: none; outline: 0; }}
QListWidget#records::item {{ border-radius: 8px; margin: 1px 0; }}
QListWidget#records::item:hover {{ background: {HOVER}; }}
QListWidget#records::item:selected {{ background: {PRIMARY_SOFT}; }}
QListWidget#serverList {{ background: {HEAD}; border: 1px solid {BORDER}; border-radius: 8px; padding: 4px; outline: 0; }}
QListWidget#serverList::item {{ padding: 8px 10px; border-radius: 6px; margin: 1px 0; }}
QListWidget#serverList::item:hover {{ background: {HOVER}; }}
QListWidget#serverList::item:selected {{ background: {PRIMARY_SOFT}; color: {PRIMARY}; font-weight: 600; }}
QPlainTextEdit#serverLog {{ background: {HEAD}; font-size: 9pt; }}
QListWidget#fileList {{
    background: {SURFACE}; border: none; outline: 0;
    font-family: "Cascadia Mono", Consolas, "Malgun Gothic"; font-size: 9.5pt;
}}
QListWidget#fileList::item {{ padding: 4px 6px; border-bottom: 1px solid {LINE}; }}
QLabel#rowTitle {{ font-weight: 600; }}
QLabel#detailTitle {{ font-size: 14pt; font-weight: 700; }}
QLabel#refLabel {{ color: {PRIMARY}; font-weight: 600; }}
QLabel#emptyText {{ color: {MUTED}; font-size: 11pt; }}
QTextBrowser#report {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px; }}

/* ---------- 스크롤바·진행 표시 ---------- */
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{ background: {BORDER_STRONG}; border-radius: 4px; min-height: 30px; min-width: 30px; }}
QScrollBar::handle:hover {{ background: {SUBTLE}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QProgressBar {{ border: none; background: {HOVER}; border-radius: 3px; max-height: 6px; min-height: 6px; }}
QProgressBar::chunk {{ background: {PRIMARY}; border-radius: 3px; }}
QStatusBar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; color: {MUTED}; }}
QStatusBar QLabel {{ color: {MUTED}; }}
QSplitter::handle {{ background: transparent; }}
QMessageBox QLabel {{ min-width: 280px; }}
"""
