"""앱 설정 화면: 패키지 기본 위치, 설정 파일, 기존 툴 가져오기, 처음 실행 안내, 버전 정보."""
from __future__ import annotations

import copy

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QLineEdit, QScrollArea, QVBoxLayout, QWidget

from shipkit import CONFIG_NAME, __app_name__, __tagline__, __version__, config_store
from shipkit.config import ConfigError
from shipkit.gui.context import AppContext
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
        outer.addWidget(page_header("앱 설정", "모든 프로젝트에 공통으로 쓰는 값과 설정 파일을 관리합니다.", self.save_btn))

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(14)

        defaults = Card("패키지 기본값", "프로젝트 설정에서 따로 정하지 않으면 이 값을 씁니다.")
        form = _form()
        self.output_field = PathField(placeholder="예: D:/deploy/{project}   ({project} = 프로젝트 이름)")
        self.package_name = QLineEdit()
        self.package_name.setPlaceholderText("%Y-%m-%d-%H%M   (예: 2026-10-26-1133)")
        form.addRow(label("패키지 생성 경로", "fieldLabel", wrap=False), self.output_field)
        form.addRow(label("날짜 폴더 이름", "fieldLabel", wrap=False), self.package_name)
        defaults.body.addLayout(form)
        defaults.body.addWidget(label("배포할 때마다 [패키지 생성 경로]/[날짜 폴더] 에 패키지를 만듭니다. "
                                      "예: D:/deploy/erp/2026-10-29-1140", "muted"))
        layout.addWidget(defaults)

        self.file_chip = Chip("", "neutral")
        files = Card("설정 파일", f"화면에서 저장한 값은 이 파일({CONFIG_NAME})에 들어갑니다. "
                                  "토큰은 이 파일이 아니라 Windows 자격 증명 관리자에 저장합니다.", right=self.file_chip)
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

        start = Card("시작하기", "처음 쓰는 PC 라면 안내를 따라 연결·패키지 위치·프로젝트를 한 번에 정할 수 있습니다.")
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
        layout.addWidget(about)
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        scroll.viewport().setAutoFillBackground(False)
        content.setAutoFillBackground(False)
        outer.addWidget(scroll, 1)
        ctx.listeners.append(self.refresh)

    def refresh(self) -> None:
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
