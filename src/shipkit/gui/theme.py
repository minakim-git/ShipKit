"""디자인 시스템: 색·글꼴·아이콘·스타일시트를 한곳에서 관리한다.

밝은 배경 위에 흰 카드, 둥근 모서리, 강조색 하나로 정리한 요즘 앱 스타일. 테마(색 묶음)는 여러 개를 THEMES 에
등록해 두고 apply() 로 바꾼다 (어두운 테마 여럿, 밝은 테마 여럿). 다른 화면은 theme.PRIMARY 처럼 이 모듈의 이름으로
색을 읽으므로, 테마를 바꾸면 다음에 그릴 때부터 새 색이 쓰인다. 미리 만들어 둔 아이콘은 bind_icon() 으로 등록해 두면
테마가 바뀔 때 다시 그린다. 그림 파일(svg)에는 기본 밝은 테마(ivory)의 색이 적혀 있고, 다른 테마에서는 같은 자리의
색으로 바꿔 그린다.
"""
from __future__ import annotations

import re
import tempfile
import weakref
from importlib import resources
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QByteArray, QSettings, QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer

# ---------------------------------------------------------------------- 색 묶음
# 이름은 쓰임새 기준. HEAD: 표 머리글·옅은 바탕, LINE: 행 구분선, LABEL: 입력 칸 이름·보조 버튼 글자,
# BTN: 채운 버튼·체크 상자 바탕(어두운 테마에서는 글자용 PRIMARY 보다 진하게)
# 밝은 테마: 아이보리 바탕에 로고(스패너·망치 선 그림)와 같은 회갈색 포인트. 눈이 편하도록 채도를 낮췄다.
LIGHT: Dict[str, str] = {
    "BG": "#F3F1E9", "SURFACE": "#FBFAF6", "BORDER": "#E5E1D5", "BORDER_STRONG": "#D3CDBC", "HOVER": "#EFEBE0",
    "TEXT": "#2F2C25", "MUTED": "#7C7668", "SUBTLE": "#A39C8C", "LABEL": "#4E493F",
    "HEAD": "#F6F4EC", "LINE": "#EEEAE0", "ALT": "#F8F6F0",
    "BTN": "#8B8468", "BTN_HOVER": "#766F56", "BTN_DISABLED": "#D4CEBC",
    "PRIMARY": "#7D7660", "PRIMARY_HOVER": "#655F4B", "PRIMARY_SOFT": "#ECE8DB", "PRIMARY_DISABLED": "#D4CEBC",
    "TIP_BG": "#2F2C25",
    "OK_BORDER": "#BFD6B8", "ERR_BORDER": "#EBC2B6", "DANGER_HOVER_BORDER": "#E0A896",
    "WARN_BORDER": "#EBD3A3", "WARN_HOVER": "#F7EEDC", "WARN_HOVER_BORDER": "#E2BD7A",
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
LIGHT_TONES: Dict[str, Tuple[str, str]] = {   # 아이보리 위에서 튀지 않게 채도를 낮춘 초록·황갈·벽돌·회청
    "success": ("#4F7A4A", "#EAF1E6"), "warning": ("#A3692B", "#F8EEDD"), "danger": ("#B4533E", "#F7E6E1"),
    "info": ("#5B7590", "#E8EEF3"), "primary": ("#7D7660", "#ECE8DB"), "neutral": ("#4E493F", "#EEEAE0"),
}
DARK_TONES: Dict[str, Tuple[str, str]] = {
    "success": ("#4ADE80", "#14291D"), "warning": ("#FBBF24", "#2E2410"), "danger": ("#F87171", "#341A1A"),
    "info": ("#60A5FA", "#13243A"), "primary": ("#8B93FA", "#272A4A"), "neutral": ("#C9CED8", "#262B35"),
}


def _pal(**over: str) -> Dict[str, str]:
    """색 묶음을 짧게 적기 위한 도우미: 빠진 이름은 없게 한다."""
    missing = set(LIGHT) - set(over)
    assert not missing, missing
    return dict(over)


def _tones(**over: Tuple[str, str]) -> Dict[str, Tuple[str, str]]:
    assert set(over) == set(LIGHT_TONES), set(over) ^ set(LIGHT_TONES)
    return dict(over)


@dataclass(frozen=True)
class Theme:
    key: str
    name: str
    dark: bool
    colors: Dict[str, str]
    tones: Dict[str, Tuple[str, str]]
    logo: Optional[Dict[str, str]] = None   # 로고 색 (bg/stroke/accent). 없으면 색 묶음에서 만든다


# ---- 어두운 테마
_OCEANIC = _pal(
    BG="#263238", SURFACE="#2E3C43", BORDER="#37474F", BORDER_STRONG="#455A64", HOVER="#314549",
    TEXT="#EEFFFF", MUTED="#B0BEC5", SUBTLE="#607D8B", LABEL="#CFD8DC",
    HEAD="#2A373E", LINE="#32424A", ALT="#2A3940",
    BTN="#009688", BTN_HOVER="#26A69A", BTN_DISABLED="#1F4B49",
    PRIMARY="#80CBC4", PRIMARY_HOVER="#A7DBD6", PRIMARY_SOFT="#2A4A4A", PRIMARY_DISABLED="#3A5A5A",
    TIP_BG="#37474F",
    OK_BORDER="#2E5D3A", ERR_BORDER="#6B2F2F", DANGER_HOVER_BORDER="#8A3A3A",
    WARN_BORDER="#6A5520", WARN_HOVER="#45391A", WARN_HOVER_BORDER="#8A6D20",
)
_OCEANIC_TONES = _tones(success=("#C3E88D", "#2F4A2A"), warning=("#FFCB6B", "#4A3E1F"), danger=("#F07178", "#4A2A2E"),
                        info=("#82AAFF", "#2A3A55"), primary=("#80CBC4", "#2A4A4A"), neutral=("#CFD8DC", "#37474F"))
_FOREST = _pal(
    BG="#152019", SURFACE="#1C2A22", BORDER="#2A3A30", BORDER_STRONG="#3A4D40", HOVER="#22332A",
    TEXT="#E3ECE5", MUTED="#9BB0A2", SUBTLE="#6A8071", LABEL="#C6D5CA",
    HEAD="#1F2E25", LINE="#263730", ALT="#1E2C24",
    BTN="#4CAF50", BTN_HOVER="#66BB6A", BTN_DISABLED="#2E5A33",
    PRIMARY="#8BD49A", PRIMARY_HOVER="#A8E1B3", PRIMARY_SOFT="#26402D", PRIMARY_DISABLED="#3B5A42",
    TIP_BG="#2A3A30",
    OK_BORDER="#2F5D3A", ERR_BORDER="#5C2E2E", DANGER_HOVER_BORDER="#7A3A3A",
    WARN_BORDER="#5A4A1A", WARN_HOVER="#3A3418", WARN_HOVER_BORDER="#7A6420",
)
_FOREST_TONES = _tones(success=("#8BD49A", "#26402D"), warning=("#E6C07B", "#3F3620"), danger=("#E57373", "#40262A"),
                       info=("#7FB7E0", "#243A4C"), primary=("#8BD49A", "#26402D"), neutral=("#C6D5CA", "#2A3A30"))
_VOLCANO = _pal(
    BG="#1E1512", SURFACE="#27201C", BORDER="#3A2E29", BORDER_STRONG="#4D3D36", HOVER="#2F2622",
    TEXT="#F1E8E2", MUTED="#B8A79E", SUBTLE="#7E6E66", LABEL="#D9CCC5",
    HEAD="#2B2320", LINE="#332A26", ALT="#29211E",
    BTN="#E0672E", BTN_HOVER="#EB7F4A", BTN_DISABLED="#5A3A2A",
    PRIMARY="#F29A6B", PRIMARY_HOVER="#F7B590", PRIMARY_SOFT="#45302A", PRIMARY_DISABLED="#5A463E",
    TIP_BG="#3A2E29",
    OK_BORDER="#3F5A35", ERR_BORDER="#6B2E2E", DANGER_HOVER_BORDER="#8A3A3A",
    WARN_BORDER="#6A5520", WARN_HOVER="#45391A", WARN_HOVER_BORDER="#8A6D20",
)
_VOLCANO_TONES = _tones(success=("#A3D977", "#2F4028"), warning=("#F2C26B", "#45391A"), danger=("#F27D7D", "#4A2A2A"),
                        info=("#8FC3E8", "#2A3A4A"), primary=("#F29A6B", "#45302A"), neutral=("#D9CCC5", "#3A2E29"))
_DRACULA = _pal(
    BG="#282A36", SURFACE="#2F3240", BORDER="#3E4254", BORDER_STRONG="#4F5470", HOVER="#363A4A",
    TEXT="#F8F8F2", MUTED="#A8ABC0", SUBTLE="#6272A4", LABEL="#D6D8E4",
    HEAD="#2C2F3C", LINE="#373B4A", ALT="#2B2E3A",
    BTN="#9D6FE8", BTN_HOVER="#B08BF2", BTN_DISABLED="#4E4468",
    PRIMARY="#BD93F9", PRIMARY_HOVER="#D2B2FB", PRIMARY_SOFT="#3C3552", PRIMARY_DISABLED="#55496F",
    TIP_BG="#3E4254",
    OK_BORDER="#2F5D3A", ERR_BORDER="#6B2F3A", DANGER_HOVER_BORDER="#8A3A4A",
    WARN_BORDER="#6A5A20", WARN_HOVER="#45401A", WARN_HOVER_BORDER="#8A7A20",
)
_DRACULA_TONES = _tones(success=("#50FA7B", "#26402D"), warning=("#F1FA8C", "#3E4020"), danger=("#FF5555", "#4A2A2E"),
                        info=("#8BE9FD", "#2A3F4A"), primary=("#BD93F9", "#3C3552"), neutral=("#D6D8E4", "#3E4254"))
_GITHUB_DARK = _pal(
    BG="#0D1117", SURFACE="#161B22", BORDER="#30363D", BORDER_STRONG="#444C56", HOVER="#21262D",
    TEXT="#E6EDF3", MUTED="#8B949E", SUBTLE="#6E7681", LABEL="#C9D1D9",
    HEAD="#1A2029", LINE="#21262D", ALT="#121820",
    BTN="#238636", BTN_HOVER="#2EA043", BTN_DISABLED="#1C4A28",
    PRIMARY="#58A6FF", PRIMARY_HOVER="#79B8FF", PRIMARY_SOFT="#1F2D44", PRIMARY_DISABLED="#2C3E5C",
    TIP_BG="#30363D",
    OK_BORDER="#2E6B3A", ERR_BORDER="#6E2A2A", DANGER_HOVER_BORDER="#8E3535",
    WARN_BORDER="#6B5A1E", WARN_HOVER="#3A3418", WARN_HOVER_BORDER="#8A7A2A",
)
_GITHUB_DARK_TONES = _tones(success=("#3FB950", "#1C3A26"), warning=("#D29922", "#3A2F15"), danger=("#F85149", "#4A2224"),
                            info=("#58A6FF", "#1F2D44"), primary=("#58A6FF", "#1F2D44"), neutral=("#C9D1D9", "#30363D"))

# ---- 밝은 테마
_PAPER = _pal(
    BG="#F4F5F7", SURFACE="#FFFFFF", BORDER="#E3E6EB", BORDER_STRONG="#CBD0D8", HOVER="#EDEFF3",
    TEXT="#1F2430", MUTED="#6B7280", SUBTLE="#9AA1AE", LABEL="#3C4452",
    HEAD="#F7F8FA", LINE="#EEF0F3", ALT="#F9FAFB",
    BTN="#4F46E5", BTN_HOVER="#4338CA", BTN_DISABLED="#C7C9F0",
    PRIMARY="#4F46E5", PRIMARY_HOVER="#3730A3", PRIMARY_SOFT="#EEF0FD", PRIMARY_DISABLED="#C7C9F0",
    TIP_BG="#1F2430",
    OK_BORDER="#BBE5C8", ERR_BORDER="#F3C2BE", DANGER_HOVER_BORDER="#E89C96",
    WARN_BORDER="#F2D9A0", WARN_HOVER="#FBF3E0", WARN_HOVER_BORDER="#E8C070",
)
_PAPER_TONES = _tones(success=("#15803D", "#E8F7EE"), warning=("#B45309", "#FDF3E1"), danger=("#B91C1C", "#FBE9E7"),
                      info=("#1D4ED8", "#E8EFFD"), primary=("#4F46E5", "#EEF0FD"), neutral=("#3C4452", "#EDEFF3"))
_SKY = _pal(
    BG="#EEF4F9", SURFACE="#FBFDFF", BORDER="#D9E4EE", BORDER_STRONG="#BCCCDB", HOVER="#E4EDF5",
    TEXT="#1E2A35", MUTED="#647A8E", SUBTLE="#93A6B6", LABEL="#3A4C5C",
    HEAD="#F2F7FB", LINE="#E6EEF4", ALT="#F6FAFD",
    BTN="#1E88E5", BTN_HOVER="#1976D2", BTN_DISABLED="#B6D4F0",
    PRIMARY="#1E78CC", PRIMARY_HOVER="#155FA6", PRIMARY_SOFT="#E1EEFA", PRIMARY_DISABLED="#B6D4F0",
    TIP_BG="#1E2A35",
    OK_BORDER="#B9DEC6", ERR_BORDER="#F0C3BE", DANGER_HOVER_BORDER="#E39B94",
    WARN_BORDER="#EFD6A0", WARN_HOVER="#FBF2DF", WARN_HOVER_BORDER="#E4BE70",
)
_SKY_TONES = _tones(success=("#2E7D32", "#E6F3E8"), warning=("#B26A00", "#FBF0DC"), danger=("#C62828", "#FBE7E5"),
                    info=("#1E78CC", "#E1EEFA"), primary=("#1E78CC", "#E1EEFA"), neutral=("#3A4C5C", "#E4EDF5"))
_SAND = _pal(
    BG="#F7EFE3", SURFACE="#FFFBF4", BORDER="#ECDFCB", BORDER_STRONG="#DCCBB0", HOVER="#F2E8D8",
    TEXT="#3A2E22", MUTED="#8A7A66", SUBTLE="#B3A48F", LABEL="#5C4D3D",
    HEAD="#F9F3E8", LINE="#F0E6D6", ALT="#FBF6EC",
    BTN="#C98A3C", BTN_HOVER="#B07530", BTN_DISABLED="#E9D6B8",
    PRIMARY="#B5782F", PRIMARY_HOVER="#93601F", PRIMARY_SOFT="#F6E8D2", PRIMARY_DISABLED="#E9D6B8",
    TIP_BG="#3A2E22",
    OK_BORDER="#C4DCB0", ERR_BORDER="#EEC2B3", DANGER_HOVER_BORDER="#E09E8A",
    WARN_BORDER="#EACF9A", WARN_HOVER="#F8EDD6", WARN_HOVER_BORDER="#DDB868",
)
_SAND_TONES = _tones(success=("#5A7F3A", "#ECF2E2"), warning=("#A2611E", "#F8EBD6"), danger=("#B4473A", "#F8E5DF"),
                     info=("#5E7A9A", "#E8EEF4"), primary=("#B5782F", "#F6E8D2"), neutral=("#5C4D3D", "#F2E8D8"))
_GITHUB_LIGHT = _pal(
    BG="#F6F8FA", SURFACE="#FFFFFF", BORDER="#D0D7DE", BORDER_STRONG="#AFB8C1", HOVER="#EAEEF2",
    TEXT="#1F2328", MUTED="#656D76", SUBTLE="#8C959F", LABEL="#424A53",
    HEAD="#F3F5F7", LINE="#EAEEF2", ALT="#F9FBFC",
    BTN="#1F883D", BTN_HOVER="#1A7F37", BTN_DISABLED="#B7DFC2",
    PRIMARY="#0969DA", PRIMARY_HOVER="#0550AE", PRIMARY_SOFT="#DDF4FF", PRIMARY_DISABLED="#B6D3F2",
    TIP_BG="#24292F",
    OK_BORDER="#ACEEBB", ERR_BORDER="#FFC1C1", DANGER_HOVER_BORDER="#F7A1A1",
    WARN_BORDER="#F0D28A", WARN_HOVER="#FFF8C5", WARN_HOVER_BORDER="#E3B341",
)
_GITHUB_LIGHT_TONES = _tones(success=("#1A7F37", "#DAFBE1"), warning=("#9A6700", "#FFF8C5"), danger=("#CF222E", "#FFEBE9"),
                             info=("#0969DA", "#DDF4FF"), primary=("#0969DA", "#DDF4FF"), neutral=("#424A53", "#EAEEF2"))

THEMES: Dict[str, Theme] = {t.key: t for t in (
    Theme("ivory", "아이보리", False, LIGHT, LIGHT_TONES, {"bg": "#EDEBE2", "stroke": "#8B8468", "accent": "#C9B99A"}),
    Theme("paper", "종이", False, _PAPER, _PAPER_TONES),
    Theme("sky", "하늘", False, _SKY, _SKY_TONES),
    Theme("sand", "모래", False, _SAND, _SAND_TONES),
    Theme("github-light", "GitHub 밝게", False, _GITHUB_LIGHT, _GITHUB_LIGHT_TONES),
    Theme("midnight", "미드나이트", True, DARK, DARK_TONES, {"bg": "#2A2F3A", "stroke": "#D8D1B8", "accent": "#8F8870"}),
    Theme("oceanic", "오션", True, _OCEANIC, _OCEANIC_TONES),
    Theme("forest", "숲", True, _FOREST, _FOREST_TONES),
    Theme("volcano", "화산", True, _VOLCANO, _VOLCANO_TONES),
    Theme("dracula", "드라큘라", True, _DRACULA, _DRACULA_TONES),
    Theme("github-dark", "GitHub 어둡게", True, _GITHUB_DARK, _GITHUB_DARK_TONES),
)}
DEFAULT_THEME = {"light": "ivory", "dark": "midnight"}   # 시스템 설정을 따를 때 따로 고르지 않았으면 이 테마
ALIASES = {"light": "ivory", "dark": "midnight"}         # 예전 설정값
SETTINGS = (("system", "시스템 설정 따르기"),) + tuple((t.key, t.name) for t in THEMES.values())  # 설정값으로 허용되는 것


def themes(dark: bool) -> List[Theme]:
    return [t for t in THEMES.values() if t.dark == dark]

FONT_FAMILY = '"Segoe UI Variable Text", "Segoe UI", "Malgun Gothic"'

# 지금 쓰는 색 (use() 가 바꾼다). 편집기가 이름을 알 수 있게 밝은 테마 값으로 먼저 둔다.
MODE = "light"      # light / dark (지금 테마가 어두운지)
THEME = "ivory"     # 지금 테마의 키
BG = SURFACE = BORDER = BORDER_STRONG = HOVER = TEXT = MUTED = SUBTLE = LABEL = HEAD = LINE = ALT = ""
BTN = BTN_HOVER = BTN_DISABLED = PRIMARY = PRIMARY_HOVER = PRIMARY_SOFT = PRIMARY_DISABLED = TIP_BG = ""
OK_BORDER = ERR_BORDER = DANGER_HOVER_BORDER = WARN_BORDER = WARN_HOVER = WARN_HOVER_BORDER = ""
TONES: Dict[str, Tuple[str, str]] = {}

_listeners: List[Callable[[], None]] = []
_bound: List[Tuple["weakref.ref", tuple]] = []


def use(key: str) -> None:
    """색 묶음만 바꾼다 (화면에 적용은 apply). key 는 테마 키 (light/dark 는 기본 테마로)."""
    global MODE, THEME, TONES
    t = THEMES[ALIASES.get(key, key)]
    MODE = "dark" if t.dark else "light"
    THEME = t.key
    globals().update(t.colors)
    TONES = dict(t.tones)


use("ivory")


def is_dark() -> bool:
    return MODE == "dark"


def current() -> Theme:
    return THEMES[THEME]


def system_mode() -> str:
    """Windows 앱 모드(밝게/어둡게)."""
    app = QGuiApplication.instance()
    if app is None:
        return "light"
    return "dark" if QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark else "light"


def preferred(mode: str) -> str:
    """시스템 설정을 따를 때 그 모드(light/dark)에서 쓸 테마 키 (앱 설정에서 고른 것, 없으면 기본)."""
    key = str(QSettings().value(f"ui/theme_{mode}", "")) if QGuiApplication.instance() is not None else ""
    if key in THEMES and THEMES[key].dark == (mode == "dark"):
        return key
    return DEFAULT_THEME[mode]


def resolve(setting: str) -> str:
    """설정값(system / 테마 키 / 예전 light·dark) → 지금 써야 할 테마 키."""
    if setting in THEMES:
        return setting
    if setting in ALIASES:
        return ALIASES[setting]
    return preferred(system_mode())


def apply(app, setting: str = "system") -> str:
    """설정(system 또는 테마 키)에 맞는 테마를 앱 전체에 적용하고, 등록해 둔 아이콘·화면을 다시 그린다. 반환: 테마 키."""
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
    return THEME


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
    """기본 밝은 테마(ivory) 색 -> 지금 테마 색 (그림 파일에 직접 적힌 색을 바꿀 때)."""
    if THEME == DEFAULT_THEME["light"]:
        return {}
    t = current()
    pairs = [(LIGHT[k], t.colors[k]) for k in LIGHT]
    for key in LIGHT_TONES:
        pairs += list(zip(LIGHT_TONES[key], t.tones[key]))
    mapping: Dict[str, str] = {}
    for light, now in pairs:
        mapping.setdefault(light.lower(), now)  # 같은 색이 여러 이름에 있으면 앞의 것 (BTN 이 PRIMARY 보다 앞)
    return mapping


def _recolor(svg: str) -> str:
    mapping = _color_map()
    if not mapping:
        return svg
    return re.sub(r"#[0-9A-Fa-f]{6}\b", lambda m: mapping.get(m.group(0).lower(), m.group(0)), svg)


def asset(name: str) -> str:
    """스타일시트 url() 에 넣을 경로 (슬래시). 기본 밝은 테마가 아니면 색을 바꾼 사본을 임시 폴더에 만든다."""
    source = assets_dir() / name
    if THEME == DEFAULT_THEME["light"]:
        return source.as_posix()
    target = Path(tempfile.gettempdir()) / "shipkit-theme" / THEME / name
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


# 로고(logo.svg)의 색: 파일에는 기본 밝은 테마(ivory) 값이 적혀 있고, 다른 테마에서는 그 테마의 로고 색으로 바꾼다
LOGO_COLORS = THEMES["ivory"].logo


def logo_colors(key: str) -> Dict[str, str]:
    """테마의 로고 색. 따로 정하지 않은 테마는 색 묶음에서 만든다 (바탕은 옅은 면, 선은 강조색)."""
    t = THEMES[ALIASES.get(key, key)]
    if t.logo:
        return t.logo
    c = t.colors
    if t.dark:
        return {"bg": c["BORDER"], "stroke": c["PRIMARY"], "accent": c["BORDER_STRONG"]}
    return {"bg": c["HOVER"], "stroke": c["BTN"], "accent": c["BORDER_STRONG"]}


def logo_svg(frame: bool = True, mode: Optional[str] = None) -> str:
    """로고 SVG 글. frame=False 면 네모 바탕을 빼고 공구만 꽉 차게 (사이드바용). mode 는 테마 키(또는 light/dark)."""
    text = (assets_dir() / "logo.svg").read_text(encoding="utf-8")
    if not frame:
        text = re.sub(r"<rect [^>]*?/>", "", text, count=1).replace('viewBox="0 0 64 64"', 'viewBox="5 5 54 54"')
    colors = logo_colors(mode or THEME)
    if colors is not LOGO_COLORS:
        for key, base in LOGO_COLORS.items():
            text = text.replace(base, colors[key])
    return text


def swatch(key: str, size: int = 18) -> QIcon:
    """앱 설정의 테마 단추에 붙는 색 견본: 그 테마의 바탕 위에 카드와 강조색 점."""
    t = THEMES[key]
    pm = QPixmap(QSize(size * 2, size * 2))
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QColor(t.colors["BORDER_STRONG"]))
    painter.setBrush(QColor(t.colors["BG"]))
    painter.drawRoundedRect(1, 1, size * 2 - 2, size * 2 - 2, 6, 6)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(t.colors["SURFACE"]))
    painter.drawRoundedRect(6, 10, size * 2 - 12, size * 2 - 16, 4, 4)
    painter.setBrush(QColor(t.colors["BTN"]))
    painter.drawEllipse(size - 5, size - 2, 10, 10)
    painter.end()
    pm.setDevicePixelRatio(2)
    return QIcon(pm)


def logo_pixmap(size: int, frame: bool = True, mode: Optional[str] = None) -> QPixmap:
    """창 아이콘은 mode='light' 로 고정해 쓴다 (작업 표시줄은 앱 테마를 따르지 않으므로)."""
    renderer = QSvgRenderer(QByteArray(logo_svg(frame, mode).encode("utf-8")))
    pm = QPixmap(QSize(size * 2, size * 2))
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    renderer.render(painter)
    painter.end()
    pm.setDevicePixelRatio(2)
    return pm


def bind_logo(label, size: int, frame: bool = True) -> None:
    """label 에 로고를 넣고, 테마가 바뀌면 그 테마 색으로 다시 그린다."""
    def refresh() -> None:
        label.setPixmap(logo_pixmap(size, frame))
    refresh()
    on_change(refresh)


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
QFrame#toast {{ background: {TIP_BG}; border: none; border-radius: 10px; }}
QLabel#toastText {{ color: white; font-weight: 500; background: transparent; }}

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
QPushButton#sideLink {{ background: transparent; border: none; color: {MUTED}; font-size: 9.5pt; padding: 4px 6px; text-align: left; }}
QPushButton#sideLink:hover {{ color: {PRIMARY}; background: {HOVER}; border-radius: 6px; }}
QPushButton#sideUpdate {{ background: {PRIMARY_SOFT}; border: none; color: {PRIMARY}; font-size: 9.5pt; font-weight: 600;
    padding: 5px 8px; text-align: left; border-radius: 6px; }}
QPushButton#sideUpdate:hover {{ background: {HOVER}; }}
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
QLabel#doneTitle {{ font-size: 13pt; font-weight: 700; }}

/* ---------- 기록 ---------- */
QListWidget#records {{ background: transparent; border: none; outline: 0; }}
QLabel#groupHeader {{ color: {SUBTLE}; font-size: 8.5pt; font-weight: 600; padding: 8px 10px 0; background: transparent; }}
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
"""
