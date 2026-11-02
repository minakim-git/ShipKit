"""앱 설정 화면: 화면 테마, 패키지 기본 위치, 설정 파일, 기존 툴 가져오기, 처음 실행 안내, 버전 정보."""
from __future__ import annotations

import copy

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QFormLayout, QHBoxLayout, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from shipkit import __app_name__, __tagline__, __version__, config_store
from shipkit.config import ConfigError
from shipkit.gui import theme
from shipkit.gui.context import AppContext
from shipkit.gui.guide_view import show_guide
from shipkit.gui.widgets import Card, Chip, PathField, button, label, open_path, page_header


def _form() -> QFormLayout:
    form = QFormLayout()
    form.setHorizontalSpacing(16)
    form.setVerticalSpacing(10)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return form


class SettingsPage(QWidget):
    setup_requested = Signal()    # [처음 실행 안내 다시 보기]
    import_requested = Signal()   # [기존 툴에서 가져오기]

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)
        self.save_btn = button("저장", "primary")
        self.save_btn.setMinimumWidth(110)
        self.save_btn.clicked.connect(self.save)
        outer.addWidget(page_header("앱 설정", "", self.save_btn))

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(14)

        look = Card("화면")
        row = QHBoxLayout()
        row.setSpacing(6)
        self.theme_group = QButtonGroup(self)
        self.theme_buttons = {}
        for key, text in theme.SETTINGS:
            b = QPushButton(text)
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self.theme_group.addButton(b)
            self.theme_buttons[key] = b
            b.clicked.connect(lambda _=False, key=key: self.set_theme(key))
            row.addWidget(b)
        row.addStretch(1)
        look.body.addLayout(row)
        layout.addWidget(look)

        defaults = Card("패키지 기본값")
        form = _form()
        self.output_field = PathField(placeholder="예: D:/deploy/{project}   ({project} = 프로젝트 이름)")
        self.package_name = QLineEdit()
        self.package_name.setPlaceholderText("%Y-%m-%d-%H%M   (예: 2026-10-26-1133)")
        form.addRow(label("패키지 생성 경로", "fieldLabel", wrap=False), self.output_field)
        form.addRow(label("날짜 폴더 이름", "fieldLabel", wrap=False), self.package_name)
        defaults.body.addLayout(form)
        layout.addWidget(defaults)

        self.file_chip = Chip("", "neutral")
        files = Card("설정 파일", right=self.file_chip)
        self.path_label = label("", "code")
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        files.body.addWidget(self.path_label)
        row = QHBoxLayout()
        self.open_file_btn = button("파일 열기", "ghost")
        self.open_file_btn.clicked.connect(lambda: self.ctx.config_path.is_file() and open_path(self.ctx.config_path))
        open_dir = button("폴더 열기", "ghost")
        open_dir.clicked.connect(lambda: self.ctx.config_path.parent.is_dir() and open_path(self.ctx.config_path.parent))
        row.addWidget(self.open_file_btn)
        row.addWidget(open_dir)
        row.addStretch(1)
        files.body.addLayout(row)
        layout.addWidget(files)

        start = Card("시작하기")
        row = QHBoxLayout()
        setup = button("처음 실행 안내 다시 보기")
        setup.clicked.connect(self.setup_requested.emit)
        legacy = button("기존 툴에서 가져오기")
        legacy.clicked.connect(self.import_requested.emit)
        row.addWidget(setup)
        row.addWidget(legacy)
        row.addStretch(1)
        start.body.addLayout(row)
        layout.addWidget(start)

        about = Card("정보")
        about.body.addWidget(label(f"<b>{__app_name__}</b>  v{__version__}  ·  {__tagline__}", "muted"))
        row = QHBoxLayout()
        guide = button("사용자 가이드")
        guide.clicked.connect(lambda: show_guide(self))
        row.addWidget(guide)
        row.addStretch(1)
        about.body.addLayout(row)
        layout.addWidget(about)
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        scroll.viewport().setAutoFillBackground(False)
        content.setAutoFillBackground(False)
        outer.addWidget(scroll, 1)
        ctx.listeners.append(self.refresh)

    def set_theme(self, key: str) -> None:
        """테마를 바로 바꾸고 이 PC 에 기억한다 (저장 버튼과 상관없음)."""
        QSettings().setValue("ui/theme", key)
        self.theme_buttons[key].setChecked(True)
        app = QApplication.instance()
        if app is not None:
            theme.apply(app, key)

    def refresh(self) -> None:
        current = str(QSettings().value("ui/theme", "system"))
        self.theme_buttons.get(current, self.theme_buttons["system"]).setChecked(True)
        exists = self.ctx.config_path.is_file()
        self.path_label.setText(str(self.ctx.config_path))
        if self.ctx.load_error:
            self.file_chip.set("오류", "danger")
        else:
            self.file_chip.set("있음" if exists else "아직 없음", "success" if exists else "neutral")
        self.open_file_btn.setEnabled(exists)
        try:
            data = config_store.load_raw(self.ctx.config_path)
        except ConfigError:
            data = {}
        defaults = data.get("defaults") or {}
        self.output_field.setText(str(defaults.get("output_dir", "")))
        self.package_name.setText(str(defaults.get("package_name", "")))

    def save(self) -> None:
        """기본값(defaults)만 저장한다. 다른 값은 그대로 둔다."""
        data = config_store.load_raw(self.ctx.config_path)
        before = copy.deepcopy(data)
        defaults = data.setdefault("defaults", {}) or {}
        data["defaults"] = defaults
        for key, value in (("output_dir", self.output_field.text()), ("package_name", self.package_name.text().strip())):
            if value:
                defaults[key] = value
            else:
                defaults.pop(key, None)
        try:
            config_store.save_raw(self.ctx.config_path, data)
        except ConfigError as e:
            self.ctx.dialogs.error("저장하지 못했습니다", str(e))
            return
        if data != before or not before:
            self.ctx.reload()
        self.ctx.dialogs.info("앱 설정", "저장했습니다.")
