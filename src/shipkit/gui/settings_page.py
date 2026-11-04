"""앱 설정 화면: 화면 테마, 패키지 기본 위치, 설정 파일, 기존 툴 가져오기, 처음 실행 안내, 버전 정보."""
from __future__ import annotations

import copy

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFormLayout, QHBoxLayout, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from shipkit import __app_name__, __tagline__, __version__, config_store, update
from shipkit.config import ConfigError
from shipkit.gui import diagnostics, theme
from shipkit.gui.context import AppContext
from shipkit.gui.guide_view import show_guide
from shipkit.gui.widgets import Card, Chip, ElidedLabel, PathField, button, label, open_location, open_path, page_header


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
        self.follow_system = QCheckBox("Windows 의 밝게/어둡게 설정 따르기 (밝을 때와 어두울 때 쓸 테마를 하나씩 고릅니다)")
        self.follow_system.toggled.connect(self._toggle_follow)
        look.body.addWidget(self.follow_system)
        size_row = QHBoxLayout()
        size_row.setSpacing(6)
        size_row.addWidget(label("글자 크기", "fieldLabel", wrap=False))
        self.font_buttons = {}
        for key, text, _scale in theme.FONT_SCALES:
            b = QPushButton(text)
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, key=key: self.set_font_scale(key))
            self.font_buttons[key] = b
            size_row.addWidget(b)
        size_row.addStretch(1)
        look.body.addLayout(size_row)
        self.theme_buttons = {}
        for caption, dark in (("밝은 테마", False), ("어두운 테마", True)):
            look.body.addWidget(label(caption, "fieldLabel", wrap=False))
            row = QHBoxLayout()
            row.setSpacing(6)
            for t in theme.themes(dark):
                b = QPushButton(t.name)
                b.setObjectName("seg")
                b.setCheckable(True)
                b.setIcon(theme.swatch(t.key))
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                self.theme_buttons[t.key] = b
                b.clicked.connect(lambda _=False, key=t.key: self.set_theme(key))
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
        self.path_label = ElidedLabel("code")   # 경로가 길어도(글자를 키워도) 카드 폭을 밀어내지 않게 가운데를 줄인다
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

        updates = Card("새 버전 확인")
        form = _form()
        self.update_location = QLineEdit()
        self.update_location.setPlaceholderText("version.txt 가 있는 공유 폴더(예: \\\\fileserver\\tools\\ShipKit) 또는 URL. 비우면 확인하지 않음")
        self.update_check_btn = button("지금 확인", "ghost")
        self.update_check_btn.clicked.connect(self.check_update)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.update_location, 1)
        row.addWidget(self.update_check_btn)
        form.addRow(label("확인 위치", "fieldLabel", wrap=False), row)
        updates.body.addLayout(form)
        layout.addWidget(updates)

        about = Card("정보")
        about.body.addWidget(label(f"<b>{__app_name__}</b>  v{__version__}  ·  {__tagline__}", "muted"))
        row = QHBoxLayout()
        guide = button("사용자 가이드")
        guide.clicked.connect(lambda: show_guide(self))
        log_btn = button("로그 파일 열기", "ghost")
        log_btn.setToolTip("오류가 났을 때 담당자에게 보낼 기록입니다.")
        log_btn.clicked.connect(self.open_log)
        row.addWidget(guide)
        row.addWidget(log_btn)
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

    def open_log(self) -> None:
        path = diagnostics.log_path()
        if path.is_file():
            open_path(path)
        else:
            self.ctx.dialogs.toast("아직 로그 파일이 없습니다.", "info")

    def check_update(self) -> None:
        """[지금 확인]: 칸에 적힌 위치(저장 전이어도)의 version.txt 를 읽어 결과를 알린다."""
        location = self.update_location.text().strip()
        if not location:
            self.ctx.dialogs.toast("확인 위치를 먼저 넣으세요.", "warning")
            return

        def done(info):
            if info is None:
                self.ctx.dialogs.toast(f"지금 쓰는 v{__version__} 이 최신입니다.")
            elif self.ctx.dialogs.ask("새 버전 확인", f"새 버전 v{info.version} 이 있습니다 (지금 v{__version__}).\n"
                                      + (info.notes + "\n\n" if info.notes else "\n") + "받는 곳을 열까요?"):
                open_location(info.location)

        self.ctx.runner.run(lambda: update.check(location), done,
                            on_failed=lambda msg: self.ctx.dialogs.error("새 버전 확인", msg), quiet=True)

    def set_theme(self, key: str) -> None:
        """테마를 바로 바꾸고 이 PC 에 기억한다 (저장 버튼과 상관없음). 시스템 설정을 따르는 중이면
        그 테마가 밝은지 어두운지에 따라 '밝을 때/어두울 때' 테마로 기억하고, 지금 모드와 맞으면 바로 적용된다."""
        key = theme.ALIASES.get(key, key)
        settings = QSettings()
        if self.follow_system.isChecked():
            settings.setValue("ui/theme_dark" if theme.THEMES[key].dark else "ui/theme_light", key)
            setting = "system"
        else:
            settings.setValue("ui/theme", key)
            setting = key
        self._apply(setting)

    def set_font_scale(self, key: str) -> None:
        """글자 크기(작게/보통/크게)를 바로 바꾸고 이 PC 에 기억한다. 고른 단추 하나만 눌린 상태로 둔다."""
        QSettings().setValue("ui/font_scale", key)
        theme.set_font_scale(key)
        self._sync_font_buttons()
        self._apply(str(QSettings().value("ui/theme", "system")))

    def _sync_font_buttons(self) -> None:
        size = str(QSettings().value("ui/font_scale", "normal"))
        if size not in self.font_buttons:
            size = "normal"
        for key, b in self.font_buttons.items():
            b.setChecked(key == size)

    def _toggle_follow(self, on: bool) -> None:
        settings = QSettings()
        if on:
            settings.setValue("ui/theme", "system")
            self._apply("system")
        else:
            key = theme.THEME  # 지금 보이는 테마를 그대로 고정한다
            settings.setValue("ui/theme", key)
            self._apply(key)

    def _apply(self, setting: str) -> None:
        app = QApplication.instance()
        if app is not None:
            theme.apply(app, setting)
        self._sync_theme_buttons()

    def _sync_theme_buttons(self) -> None:
        """시스템을 따르면 밝은 쪽·어두운 쪽에서 하나씩, 아니면 지금 테마 하나만 눌린 상태로."""
        if self.follow_system.isChecked():
            checked = {theme.preferred("light"), theme.preferred("dark")}
        else:
            checked = {theme.THEME}
        for key, b in self.theme_buttons.items():
            b.setChecked(key in checked)

    def refresh(self) -> None:
        self._sync_font_buttons()
        current = str(QSettings().value("ui/theme", "system"))
        self.follow_system.blockSignals(True)
        self.follow_system.setChecked(current not in theme.THEMES and current not in theme.ALIASES)
        self.follow_system.blockSignals(False)
        self._sync_theme_buttons()
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
        self.update_location.setText(str(data.get("update_location") or ""))

    def save(self) -> None:
        """기본값(defaults)과 새 버전 확인 위치만 저장한다. 다른 값은 그대로 둔다."""
        data = config_store.load_raw(self.ctx.config_path)
        before = copy.deepcopy(data)
        location = self.update_location.text().strip()
        if location:
            data["update_location"] = location
        else:
            data.pop("update_location", None)
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
        self.ctx.dialogs.toast("앱 설정을 저장했습니다.")
