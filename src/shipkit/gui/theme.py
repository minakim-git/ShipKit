"""디자인 시스템: 색·글꼴·아이콘·스타일시트를 한곳에서 관리한다.

밝은 배경 위에 흰 카드, 얇은 선, 강조색 하나로 정리한 요즘 앱 스타일. 테마는 밝은 쪽·어두운 쪽에 기본 하나와
널리 쓰이는 팔레트 몇 개(GitHub·Solarized·One·Dracula·Nord)를 THEMES 에 두고 apply() 로 바꾼다. 다른 화면은 theme.PRIMARY 처럼 이 모듈의 이름으로 색을 읽으므로, 테마를 바꾸면 다음에 그릴 때부터
새 색이 쓰인다. 미리 만들어 둔 아이콘은 bind_icon() 으로 등록해 두면 테마가 바뀔 때 다시 그린다. 그림 파일(svg)에는
기본 밝은 테마(light)의 색이 적혀 있고, 어두운 테마에서는 같은 자리의 색으로 바꿔 그린다.
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
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer

# ---------------------------------------------------------------------- 색 묶음
# 이름은 쓰임새 기준. HEAD: 표 머리글·옅은 바탕, LINE: 행 구분선, LABEL: 입력 칸 이름·보조 버튼 글자,
# BTN: 채운 버튼·체크 상자 바탕(어두운 테마에서는 글자용 PRIMARY 보다 진하게)
# 테마는 둘뿐이다. 밝게: 흰 면에 회색 선, 강조색 파랑 하나 (GitHub·Notion·Linear 같은 요즘 앱의 중립 팔레트).
# 어둡게: 짙은 남색(slate) 면에 주황 강조 (HeyCheck 와 같은 느낌). 그림 파일(svg)에는 '밝게' 색이 적혀 있다.
LIGHT: Dict[str, str] = {
    "BG": "#F7F7F8", "SURFACE": "#FFFFFF", "BORDER": "#E5E7EB", "BORDER_STRONG": "#D1D5DB", "HOVER": "#F3F4F6",
    "TEXT": "#111827", "MUTED": "#6B7280", "SUBTLE": "#9CA3AF", "LABEL": "#374151",
    "HEAD": "#F9FAFB", "LINE": "#F3F4F6", "ALT": "#FAFAFA",
    "BTN": "#2563EB", "BTN_HOVER": "#1D4ED8", "BTN_DISABLED": "#BFDBFE",
    "PRIMARY": "#2563EB", "PRIMARY_HOVER": "#1E40AF", "PRIMARY_SOFT": "#EFF6FF", "PRIMARY_DISABLED": "#93C5FD",
    "TIP_BG": "#111827",
    "OK_BORDER": "#BBF7D0", "ERR_BORDER": "#FECACA", "DANGER_HOVER_BORDER": "#FCA5A5",
    "WARN_BORDER": "#FDE68A", "WARN_HOVER": "#FEF3C7", "WARN_HOVER_BORDER": "#FCD34D",
}
DARK: Dict[str, str] = {
    "BG": "#0F172A", "SURFACE": "#1E293B", "BORDER": "#334155", "BORDER_STRONG": "#475569", "HOVER": "#283548",
    "TEXT": "#F1F5F9", "MUTED": "#94A3B8", "SUBTLE": "#64748B", "LABEL": "#CBD5E1",
    "HEAD": "#172033", "LINE": "#2B3A4F", "ALT": "#1B2638",
    "BTN": "#EA580C", "BTN_HOVER": "#F97316", "BTN_DISABLED": "#5C2D12",
    "PRIMARY": "#FB923C", "PRIMARY_HOVER": "#FDBA74", "PRIMARY_SOFT": "#3B2416", "PRIMARY_DISABLED": "#7C3F1D",
    "TIP_BG": "#334155",
    "OK_BORDER": "#166534", "ERR_BORDER": "#7F1D1D", "DANGER_HOVER_BORDER": "#991B1B",
    "WARN_BORDER": "#78350F", "WARN_HOVER": "#3A2A10", "WARN_HOVER_BORDER": "#92400E",
}
# 상태 색: (글자색, 배경색)
LIGHT_TONES: Dict[str, Tuple[str, str]] = {
    "success": ("#15803D", "#DCFCE7"), "warning": ("#B45309", "#FEF3C7"), "danger": ("#B91C1C", "#FEE2E2"),
    "info": ("#1D4ED8", "#DBEAFE"), "primary": ("#2563EB", "#EFF6FF"), "neutral": ("#374151", "#F3F4F6"),
}
DARK_TONES: Dict[str, Tuple[str, str]] = {
    "success": ("#4ADE80", "#14291D"), "warning": ("#FBBF24", "#2E2410"), "danger": ("#F87171", "#341A1A"),
    "info": ("#60A5FA", "#13243A"), "primary": ("#FB923C", "#3B2416"), "neutral": ("#CBD5E1", "#283548"),
}


@dataclass(frozen=True)
class Theme:
    key: str
    name: str
    dark: bool
    colors: Dict[str, str]
    tones: Dict[str, Tuple[str, str]]
    logo: Optional[Dict[str, str]] = None   # 로고 색 (bg/stroke/accent). 없으면 색 묶음에서 만든다


def _pal(**over: str) -> Dict[str, str]:
    """색 묶음을 짧게 적기 위한 도우미: 빠진 이름은 없게 한다."""
    missing = set(LIGHT) - set(over)
    assert not missing, missing
    return dict(over)


def _tones(**over: Tuple[str, str]) -> Dict[str, Tuple[str, str]]:
    assert set(over) == set(LIGHT_TONES), set(over) ^ set(LIGHT_TONES)
    return dict(over)


# ---- 더 있는 밝은 테마: 널리 쓰이는 팔레트 그대로 (GitHub Primer, Solarized, Atom One Light)
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
_SOLARIZED_LIGHT = _pal(
    BG="#EEE8D5", SURFACE="#FDF6E3", BORDER="#E4DCC3", BORDER_STRONG="#D3CBB0", HOVER="#F5EFDC",
    TEXT="#073642", MUTED="#657B83", SUBTLE="#93A1A1", LABEL="#586E75",
    HEAD="#F7F1DF", LINE="#EFE8D3", ALT="#FAF5E6",
    BTN="#268BD2", BTN_HOVER="#1E6FA8", BTN_DISABLED="#B9D4E8",
    PRIMARY="#268BD2", PRIMARY_HOVER="#1E6FA8", PRIMARY_SOFT="#E3EEF4", PRIMARY_DISABLED="#9CC3DF",
    TIP_BG="#073642",
    OK_BORDER="#C9D9A0", ERR_BORDER="#F0C0B8", DANGER_HOVER_BORDER="#E8A094",
    WARN_BORDER="#E8D59A", WARN_HOVER="#F7EFD2", WARN_HOVER_BORDER="#D9C070",
)
_SOLARIZED_LIGHT_TONES = _tones(success=("#6C7D00", "#EEF0D6"), warning=("#B58900", "#F7EFD2"), danger=("#DC322F", "#F8E3E0"),
                                info=("#268BD2", "#E3EEF4"), primary=("#268BD2", "#E3EEF4"), neutral=("#586E75", "#EEE8D5"))
_ONE_LIGHT = _pal(
    BG="#F0F0F1", SURFACE="#FAFAFA", BORDER="#E1E1E3", BORDER_STRONG="#CFCFD3", HOVER="#EAEAEB",
    TEXT="#383A42", MUTED="#696C77", SUBTLE="#A0A1A7", LABEL="#50525B",
    HEAD="#F3F3F4", LINE="#ECECED", ALT="#F6F6F7",
    BTN="#4078F2", BTN_HOVER="#2E63D9", BTN_DISABLED="#C3D3F8",
    PRIMARY="#4078F2", PRIMARY_HOVER="#2E63D9", PRIMARY_SOFT="#E8EFFD", PRIMARY_DISABLED="#A6BEF5",
    TIP_BG="#383A42",
    OK_BORDER="#BFE0BE", ERR_BORDER="#F5C4BE", DANGER_HOVER_BORDER="#EFA49A",
    WARN_BORDER="#EFD59A", WARN_HOVER="#FBF2DA", WARN_HOVER_BORDER="#E2BF6A",
)
_ONE_LIGHT_TONES = _tones(success=("#50A14F", "#E6F3E6"), warning=("#C18401", "#FBF2DA"), danger=("#E45649", "#FBE6E3"),
                          info=("#0184BC", "#E1F1F8"), primary=("#4078F2", "#E8EFFD"), neutral=("#50525B", "#EAEAEB"))

# ---- 더 있는 어두운 테마 (Atom One Dark, Dracula, Nord)
_ONE_DARK = _pal(
    BG="#21252B", SURFACE="#282C34", BORDER="#3A3F4B", BORDER_STRONG="#4B5263", HOVER="#2C313A",
    TEXT="#D7DAE0", MUTED="#9DA5B4", SUBTLE="#5C6370", LABEL="#ABB2BF",
    HEAD="#262A31", LINE="#333842", ALT="#2A2E36",
    BTN="#4A88E0", BTN_HOVER="#61AFEF", BTN_DISABLED="#2E4366",
    PRIMARY="#61AFEF", PRIMARY_HOVER="#8CC7F5", PRIMARY_SOFT="#2C3A4F", PRIMARY_DISABLED="#3E5A7D",
    TIP_BG="#3A3F4B",
    OK_BORDER="#3F6B3E", ERR_BORDER="#6B3A3E", DANGER_HOVER_BORDER="#8A474D",
    WARN_BORDER="#6B5A2E", WARN_HOVER="#3E3828", WARN_HOVER_BORDER="#8A7540",
)
_ONE_DARK_TONES = _tones(success=("#98C379", "#2E3D2E"), warning=("#E5C07B", "#3E3828"), danger=("#E06C75", "#3E2A2E"),
                         info=("#56B6C2", "#243A3E"), primary=("#61AFEF", "#2C3A4F"), neutral=("#ABB2BF", "#3A3F4B"))
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
_NORD = _pal(
    BG="#2E3440", SURFACE="#3B4252", BORDER="#434C5E", BORDER_STRONG="#4C566A", HOVER="#434C5E",
    TEXT="#ECEFF4", MUTED="#A7B1C2", SUBTLE="#7B879B", LABEL="#D8DEE9",
    HEAD="#363D4B", LINE="#434C5E", ALT="#394150",
    BTN="#5E81AC", BTN_HOVER="#81A1C1", BTN_DISABLED="#3F5068",
    PRIMARY="#88C0D0", PRIMARY_HOVER="#A5D3DF", PRIMARY_SOFT="#3C4B5A", PRIMARY_DISABLED="#5A7A88",
    TIP_BG="#4C566A",
    OK_BORDER="#5A6E4E", ERR_BORDER="#6E4248", DANGER_HOVER_BORDER="#8A5058",
    WARN_BORDER="#6E6240", WARN_HOVER="#4A4536", WARN_HOVER_BORDER="#8A7A50",
)
_NORD_TONES = _tones(success=("#A3BE8C", "#3A4639"), warning=("#EBCB8B", "#4A4536"), danger=("#BF616A", "#46353A"),
                     info=("#81A1C1", "#374455"), primary=("#88C0D0", "#3C4B5A"), neutral=("#D8DEE9", "#434C5E"))

THEMES: Dict[str, Theme] = {t.key: t for t in (
    Theme("light", "기본", False, LIGHT, LIGHT_TONES, {"bg": "#EFF6FF", "stroke": "#2563EB", "accent": "#BFDBFE"}),
    Theme("github-light", "GitHub", False, _GITHUB_LIGHT, _GITHUB_LIGHT_TONES),
    Theme("solarized-light", "Solarized", False, _SOLARIZED_LIGHT, _SOLARIZED_LIGHT_TONES),
    Theme("one-light", "One Light", False, _ONE_LIGHT, _ONE_LIGHT_TONES),
    Theme("dark", "기본", True, DARK, DARK_TONES, {"bg": "#334155", "stroke": "#FB923C", "accent": "#7C3F1D"}),
    Theme("one-dark", "One Dark", True, _ONE_DARK, _ONE_DARK_TONES),
    Theme("dracula", "Dracula", True, _DRACULA, _DRACULA_TONES),
    Theme("nord", "Nord", True, _NORD, _NORD_TONES),
)}
DEFAULT_THEME = {"light": "light", "dark": "dark"}   # 밝을 때/어두울 때 따로 고르지 않았으면 이 테마
# 설정값 ui/theme 는 모드: system(Windows 설정 따르기) / light / dark. 밝을 때·어두울 때 쓸 테마는 ui/theme_light·ui/theme_dark
MODES = (("system", "시스템 설정 따르기"), ("light", "밝게"), ("dark", "어둡게"))
# 예전 설정값(없어진 테마 이름)은 밝은/어두운 쪽으로 넘긴다
ALIASES = {"ivory": "light", "paper": "light", "sky": "light", "sand": "light",
           "midnight": "dark", "oceanic": "dark", "forest": "dark", "volcano": "dark", "github-dark": "dark"}
SETTINGS = MODES + tuple((t.key, t.name) for t in THEMES.values() if t.key not in ("light", "dark"))  # ui/theme 로 허용되는 값


def themes(dark: bool) -> List[Theme]:
    return [t for t in THEMES.values() if t.dark == dark]

# 글꼴: 같이 들어 있는 Pretendard(OFL, assets/fonts) 를 앱 시작 때 등록해 쓴다. 한글·영문이 한 글꼴로, 어느 PC 에서나 같게
FONT_FAMILY = '"Pretendard", "Segoe UI Variable Text", "Segoe UI", "Malgun Gothic"'
_fonts_loaded = False
FONT_SCALES = (("small", "작게", 0.9), ("normal", "보통", 1.0), ("large", "크게", 1.15))   # 앱 설정의 글자 크기
FONT_SCALE = 1.0
_PT = re.compile(r"(\d+(?:\.\d+)?)pt\b")


def set_font_scale(key_or_value) -> float:
    """글자 크기 배율. 키(small/normal/large)나 숫자. 스타일시트의 모든 pt 값에 곱한다 (적용은 apply)."""
    global FONT_SCALE
    scale = dict((k, v) for k, _n, v in FONT_SCALES).get(key_or_value)
    if scale is None:
        try:
            scale = float(key_or_value)
        except (TypeError, ValueError):
            scale = 1.0
    FONT_SCALE = min(max(scale, 0.7), 1.6)
    return FONT_SCALE


def _scale_pts(css: str) -> str:
    if FONT_SCALE == 1.0:
        return css
    return _PT.sub(lambda m: f"{float(m.group(1)) * FONT_SCALE:.2f}pt", css)

# 지금 쓰는 색 (use() 가 바꾼다). 편집기가 이름을 알 수 있게 밝은 테마 값으로 먼저 둔다.
MODE = "light"      # light / dark (지금 테마가 어두운지)
THEME = "light"     # 지금 테마의 키
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


use("light")


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
    """그 모드(light/dark)에서 쓸 테마 키: 앱 설정에서 고른 것(ui/theme_light·ui/theme_dark), 없으면 기본."""
    key = str(QSettings().value(f"ui/theme_{mode}", "")) if QGuiApplication.instance() is not None else ""
    if key in THEMES and THEMES[key].dark == (mode == "dark"):
        return key
    return DEFAULT_THEME[mode]


def resolve(setting: str) -> str:
    """설정값(system / light / dark / 테마 키 / 예전 이름) → 지금 써야 할 테마 키."""
    setting = ALIASES.get(setting, setting)
    if setting in ("light", "dark"):
        return preferred(setting)
    if setting in THEMES:
        return setting
    return preferred(system_mode())


def load_fonts() -> List[str]:
    """assets/fonts 의 글꼴 파일을 앱에 등록한다 (한 번만). 반환: 등록된 글꼴 가족 이름들."""
    global _fonts_loaded
    families: List[str] = []
    folder = assets_dir() / "fonts"
    if _fonts_loaded or QGuiApplication.instance() is None or not folder.is_dir():
        return families
    for path in sorted(folder.glob("*.otf")) + sorted(folder.glob("*.ttf")):
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id >= 0:
            families += [f for f in QFontDatabase.applicationFontFamilies(font_id) if f not in families]
    _fonts_loaded = True
    return families


def app_font() -> QFont:
    """앱 기본 글꼴 (스타일시트가 닿지 않는 메뉴·대화 상자·직접 그리는 글자용)."""
    font = QFont()
    font.setFamilies(["Pretendard", "Segoe UI Variable Text", "Segoe UI", "Malgun Gothic"])
    font.setPointSizeF(10 * FONT_SCALE)
    return font


def apply(app, setting: str = "system") -> str:
    """설정(system 또는 테마 키)에 맞는 테마를 앱 전체에 적용하고, 등록해 둔 아이콘·화면을 다시 그린다. 반환: 테마 키."""
    use(resolve(setting))
    load_fonts()
    app.setFont(app_font())
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
    """기본 밝은 테마(light) 색 -> 지금 테마 색 (그림 파일에 직접 적힌 색을 바꿀 때)."""
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


# 로고(logo.svg)의 색: 파일에는 기본 밝은 테마(light) 값이 적혀 있고, 다른 테마에서는 그 테마의 로고 색으로 바꾼다
LOGO_COLORS = THEMES["light"].logo


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
    return _scale_pts(_stylesheet())


def _stylesheet() -> str:
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
QLabel#brand {{ font-size: 12.5pt; font-weight: 600; }}
QLabel#brandSub {{ color: {SUBTLE}; font-size: 8.5pt; }}
QListWidget#nav {{ background: transparent; border: none; outline: 0; }}
QListWidget#nav::item {{ color: {LABEL}; padding: 7px 10px; margin: 1px 10px; border-radius: 6px; }}
QListWidget#nav::item:hover {{ background: {HOVER}; color: {TEXT}; }}
QListWidget#nav::item:selected {{ background: {PRIMARY_SOFT}; color: {PRIMARY}; font-weight: 500; }}
QLabel#sideFooter {{ color: {SUBTLE}; font-size: 8.5pt; }}
QLabel#sideCaption {{ color: {SUBTLE}; font-size: 8.5pt; font-weight: 600; padding-left: 2px; }}
QLabel#sideMeta {{ color: {MUTED}; font-size: 9pt; padding-left: 2px; }}
QLabel#sideStatus {{ color: {MUTED}; font-size: 9pt; }}
QComboBox#projectSwitcher {{ background: {SURFACE}; border-color: {BORDER}; border-radius: 8px; padding: 0 8px; min-height: 52px; }}
QComboBox#projectSwitcher:hover {{ border-color: {PRIMARY}; background: {SURFACE}; }}
QComboBox#projectSwitcher QAbstractItemView {{ padding: 4px; }}

/* ---------- 글자 ---------- */
QLabel#pageTitle {{ font-size: 15pt; font-weight: 600; }}
QLabel#pageSubtitle {{ color: {MUTED}; }}
QLabel#cardTitle {{ font-size: 10.5pt; font-weight: 600; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#fieldLabel {{ color: {LABEL}; font-weight: 500; }}
QLabel#chip {{ border-radius: 10px; padding: 2px 8px; font-size: 8.5pt; font-weight: 600; }}
{chips}

/* ---------- 카드 ---------- */
QFrame#card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px; }}
QFrame#banner {{ border-radius: 10px; }}
QFrame#detail {{ background: {HEAD}; border: 1px solid {BORDER}; border-radius: 6px; }}
QFrame#banner[tone="success"] {{ background: {TONES['success'][1]}; border: 1px solid {OK_BORDER}; }}
QFrame#banner[tone="danger"] {{ background: {TONES['danger'][1]}; border: 1px solid {ERR_BORDER}; }}
QFrame#divider {{ background: {BORDER}; max-height: 1px; min-height: 1px; border: none; }}

/* ---------- 입력 ---------- */
QLineEdit, QComboBox, QSpinBox {{
    background: {SURFACE}; border: 1px solid {BORDER_STRONG}; border-radius: 6px;
    padding: 0 8px; min-height: 30px; selection-background-color: {PRIMARY_SOFT}; selection-color: {TEXT};
}}
QPlainTextEdit {{
    background: {SURFACE}; border: 1px solid {BORDER_STRONG}; border-radius: 6px; padding: 8px;
    selection-background-color: {PRIMARY_SOFT}; selection-color: {TEXT};
    font-family: "Cascadia Mono", Consolas, "Malgun Gothic"; font-size: 9.5pt;
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus {{ border: 1px solid {PRIMARY}; }}
QLineEdit[invalid="true"], QSpinBox[invalid="true"] {{ border: 1px solid {TONES['danger'][0]}; }}
QLabel#fieldError {{ color: {TONES['danger'][0]}; font-size: 9pt; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QPlainTextEdit:disabled {{
    background: {HEAD}; color: {SUBTLE}; border-color: {BORDER};
}}
QComboBox::drop-down {{ border: none; width: 30px; }}
QComboBox::down-arrow {{ image: url("{asset('chevron-down.svg')}"); width: 14px; height: 14px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px; outline: 0;
    selection-background-color: {PRIMARY_SOFT}; selection-color: {PRIMARY};
}}
QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: none; }}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {BORDER_STRONG}; background: {SURFACE}; }}
QCheckBox::indicator:hover {{ border-color: {PRIMARY}; }}
QCheckBox::indicator:checked {{ background: {BTN}; border-color: {BTN}; image: url("{asset('check.svg')}"); }}
QCheckBox:disabled {{ color: {SUBTLE}; }}
QCheckBox::indicator:checked:disabled {{ background: {BTN_DISABLED}; border-color: {BTN_DISABLED}; }}
QCheckBox#switch::indicator {{ width: 36px; height: 20px; border: none; background: transparent; image: url("{asset('switch-off.svg')}"); }}
QCheckBox#switch::indicator:checked {{ image: url("{asset('switch-on.svg')}"); }}

/* ---------- 버튼 ---------- */
QPushButton {{
    background: {SURFACE}; border: 1px solid {BORDER_STRONG}; border-radius: 6px;
    padding: 0 12px; min-height: 30px; font-weight: 500; color: {LABEL};
}}
QPushButton:hover {{ background: {HEAD}; }}
QPushButton:pressed {{ background: {HOVER}; }}
QPushButton:disabled {{ color: {SUBTLE}; border-color: {BORDER}; }}
QPushButton#primary {{ background: {BTN}; border: 1px solid {BTN}; color: white; font-weight: 600; padding: 0 14px; }}
QPushButton#primary:hover {{ background: {BTN_HOVER}; border-color: {BTN_HOVER}; }}
QPushButton#primary:disabled {{ background: {BTN_DISABLED}; border-color: {BTN_DISABLED}; color: {MUTED if MODE == 'dark' else 'white'}; }}
QPushButton#ghost {{ background: transparent; border: none; color: {PRIMARY}; font-weight: 500; padding: 0 8px; }}
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
    background: {HOVER}; color: {MUTED}; border: 1px solid {HOVER}; padding: 4px 14px; margin: 0;
    font-weight: 500; min-height: 20px;
}}
QTabBar::tab:first {{ border-top-left-radius: 6px; border-bottom-left-radius: 6px; }}
QTabBar::tab:last {{ border-top-right-radius: 6px; border-bottom-right-radius: 6px; }}
QTabBar::tab:only-one {{ border-radius: 6px; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::tab:selected {{ background: {SURFACE}; color: {PRIMARY}; border: 1px solid {BORDER_STRONG}; font-weight: 600; }}

/* ---------- 표 ---------- */
QTableWidget {{
    background: {SURFACE}; border: none; gridline-color: transparent; outline: 0;
    selection-background-color: {PRIMARY_SOFT}; selection-color: {TEXT}; alternate-background-color: {ALT};
}}
QTableWidget::item {{ padding: 3px 8px; border-bottom: 1px solid {LINE}; }}
QHeaderView::section {{
    background: {HEAD}; color: {MUTED}; font-size: 9pt; font-weight: 600;
    border: none; border-bottom: 1px solid {BORDER}; padding: 6px 8px;
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
QFrame#preflight {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; }}
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
QTreeWidget::item {{ padding: 5px 4px; border-bottom: 1px solid {LINE}; }}
QTreeView::branch {{ background: transparent; border-bottom: 1px solid {LINE}; }}
QTreeView::branch:has-children:closed {{ image: url("{asset('chevron-right.svg')}"); }}
QTreeView::branch:has-children:open {{ image: url("{asset('chevron-open.svg')}"); }}
QLabel#code {{
    font-family: "Cascadia Mono", Consolas, "Malgun Gothic"; font-size: 9.5pt; color: {TEXT};
}}
QFrame#cmdRow {{ background: {HEAD}; border: 1px solid {BORDER}; border-radius: 8px; }}
QLabel#stepNum {{ background: {PRIMARY_SOFT}; color: {PRIMARY}; border-radius: 12px; font-size: 9pt; font-weight: 700; }}
QLabel#doneTitle {{ font-size: 12.5pt; font-weight: 600; }}

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
QLabel#detailTitle {{ font-size: 12.5pt; font-weight: 600; }}
QLabel#refLabel {{ color: {PRIMARY}; font-weight: 600; }}
QLabel#emptyText {{ color: {MUTED}; font-size: 11pt; }}
QTextBrowser#report {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; }}

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
