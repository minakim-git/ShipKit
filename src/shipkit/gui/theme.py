"""디자인 시스템: 색·글꼴·아이콘·스타일시트를 한곳에서 관리한다.

밝은 배경 위에 흰 카드, 둥근 모서리, 강조색(인디고) 하나로 정리한 요즘 앱 스타일.
"""
from __future__ import annotations

from importlib import resources
from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

# ---------------------------------------------------------------------- 색
BG = "#F4F6FA"
SURFACE = "#FFFFFF"
BORDER = "#E4E7EC"
BORDER_STRONG = "#D0D5DD"
HOVER = "#F2F4F7"
TEXT = "#101828"
MUTED = "#667085"
SUBTLE = "#98A2B3"
PRIMARY = "#4F46E5"
PRIMARY_HOVER = "#4338CA"
PRIMARY_SOFT = "#EEF2FF"
PRIMARY_DISABLED = "#C7D2FE"

# 상태 색: (글자색, 배경색)
TONES = {
    "success": ("#027A48", "#ECFDF3"),
    "warning": ("#B54708", "#FFFAEB"),
    "danger": ("#B42318", "#FEF3F2"),
    "info": ("#175CD3", "#EFF8FF"),
    "primary": (PRIMARY, PRIMARY_SOFT),
    "neutral": ("#344054", "#F2F4F7"),
}

FONT_FAMILY = '"Segoe UI Variable Text", "Segoe UI", "Malgun Gothic"'


def assets_dir() -> Path:
    return Path(str(resources.files("shipkit") / "gui" / "assets"))


def asset(name: str) -> str:
    """스타일시트 url() 에 넣을 경로 (슬래시)."""
    return (assets_dir() / name).as_posix()


def svg_icon(name: str, color: str, size: int = 20, selected_color: str = "") -> QIcon:
    """stroke="currentColor" SVG 를 원하는 색으로 그린 아이콘. selected_color 를 주면 선택 상태 색도 만든다."""
    source = (assets_dir() / name).read_text(encoding="utf-8")
    icon = QIcon()
    for mode, c in ((QIcon.Mode.Normal, color), (QIcon.Mode.Selected, selected_color or color)):
        renderer = QSvgRenderer(QByteArray(source.replace("currentColor", c).encode("utf-8")))
        pm = QPixmap(QSize(size * 2, size * 2))  # 고해상도 화면에서도 선명하게
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        renderer.render(painter)
        painter.end()
        pm.setDevicePixelRatio(2)
        icon.addPixmap(pm, mode)
    return icon


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


def stylesheet() -> str:
    chips = "\n".join(
        f'QLabel#chip[tone="{name}"] {{ color: {fg}; background: {bg}; }}' for name, (fg, bg) in TONES.items())
    return f"""
* {{ font-family: {FONT_FAMILY}; font-size: 10pt; color: {TEXT}; }}
QMainWindow, QDialog, QWidget#pageArea {{ background: {BG}; }}
QToolTip {{ background: {TEXT}; color: white; border: none; padding: 6px 8px; border-radius: 6px; }}

/* ---------- 사이드바 ---------- */
QWidget#sidebar {{ background: {SURFACE}; border-right: 1px solid {BORDER}; }}
QLabel#brand {{ font-size: 14pt; font-weight: 700; }}
QLabel#brandSub {{ color: {SUBTLE}; font-size: 8.5pt; }}
QListWidget#nav {{ background: transparent; border: none; outline: 0; }}
QListWidget#nav::item {{ color: {MUTED}; padding: 9px 12px; margin: 2px 12px; border-radius: 8px; }}
QListWidget#nav::item:hover {{ background: {HOVER}; color: {TEXT}; }}
QListWidget#nav::item:selected {{ background: {PRIMARY_SOFT}; color: {PRIMARY}; font-weight: 600; }}
QLabel#sideFooter {{ color: {SUBTLE}; font-size: 8.5pt; }}

/* ---------- 글자 ---------- */
QLabel#pageTitle {{ font-size: 18pt; font-weight: 700; }}
QLabel#pageSubtitle {{ color: {MUTED}; }}
QLabel#cardTitle {{ font-size: 11pt; font-weight: 600; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#fieldLabel {{ color: #344054; font-weight: 500; }}
QLabel#chip {{ border-radius: 11px; padding: 3px 10px; font-size: 9pt; font-weight: 600; }}
{chips}

/* ---------- 카드 ---------- */
QFrame#card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 12px; }}
QFrame#banner {{ border-radius: 12px; }}
QFrame#banner[tone="success"] {{ background: {TONES['success'][1]}; border: 1px solid #ABEFC6; }}
QFrame#banner[tone="danger"] {{ background: {TONES['danger'][1]}; border: 1px solid #FECDCA; }}
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
    background: #F9FAFB; color: {SUBTLE}; border-color: {BORDER};
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
QCheckBox::indicator:checked {{ background: {PRIMARY}; border-color: {PRIMARY}; image: url("{asset('check.svg')}"); }}
QCheckBox:disabled {{ color: {SUBTLE}; }}
QCheckBox::indicator:checked:disabled {{ background: {PRIMARY_DISABLED}; border-color: {PRIMARY_DISABLED}; }}
QCheckBox#switch::indicator {{ width: 36px; height: 20px; border: none; background: transparent; image: url("{asset('switch-off.svg')}"); }}
QCheckBox#switch::indicator:checked {{ image: url("{asset('switch-on.svg')}"); }}

/* ---------- 버튼 ---------- */
QPushButton {{
    background: {SURFACE}; border: 1px solid {BORDER_STRONG}; border-radius: 8px;
    padding: 0 14px; min-height: 34px; font-weight: 500; color: #344054;
}}
QPushButton:hover {{ background: #F9FAFB; }}
QPushButton:pressed {{ background: {HOVER}; }}
QPushButton:disabled {{ color: {SUBTLE}; border-color: {BORDER}; }}
QPushButton#primary {{ background: {PRIMARY}; border: 1px solid {PRIMARY}; color: white; font-weight: 600; padding: 0 18px; }}
QPushButton#primary:hover {{ background: {PRIMARY_HOVER}; border-color: {PRIMARY_HOVER}; }}
QPushButton#primary:disabled {{ background: {PRIMARY_DISABLED}; border-color: {PRIMARY_DISABLED}; color: white; }}
QPushButton#ghost {{ background: transparent; border: none; color: {PRIMARY}; font-weight: 600; padding: 0 8px; }}
QPushButton#ghost:hover {{ background: {PRIMARY_SOFT}; }}
QPushButton#danger {{ color: {TONES['danger'][0]}; }}
QPushButton#danger:hover {{ background: {TONES['danger'][1]}; border-color: #FDA29B; }}

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
    selection-background-color: {PRIMARY_SOFT}; selection-color: {TEXT}; alternate-background-color: #FAFBFC;
}}
QTableWidget::item {{ padding: 4px 8px; border-bottom: 1px solid #F2F4F7; }}
QHeaderView::section {{
    background: #F9FAFB; color: {MUTED}; font-size: 9pt; font-weight: 600;
    border: none; border-bottom: 1px solid {BORDER}; padding: 8px;
}}
QTableCornerButton::section {{ background: #F9FAFB; border: none; }}
QListWidget#messages {{ background: transparent; border: none; outline: 0; }}
QListWidget#messages::item {{ padding: 6px 10px; border-radius: 8px; margin: 2px 0; }}

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
