"""앱 설정 화면: 화면 테마, 패키지 기본 위치, 설정 파일, 기존 툴 가져오기, 처음 실행 안내, 버전 정보."""
from __future__ import annotations

import copy
from datetime import date
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFormLayout, QHBoxLayout, QLineEdit, QPushButton, QRadioButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from shipkit import __app_name__, __tagline__, __version__, config_store, settings_io, update
from shipkit.config import ConfigError
from shipkit.gui import diagnostics, theme
from shipkit.gui.context import AppContext
from shipkit.gui.guide_view import show_guide
from shipkit.gui.widgets import (
    Card, Chip, ElidedLabel, MessageDialog, PathField, button, label, open_location, open_path, page_header,
)


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
        def seg_row(caption, items, handler, with_swatch):
            row = QHBoxLayout()
            row.setSpacing(6)
            row.addWidget(label(caption, "fieldLabel", wrap=False))
            buttons = {}
            for key, text in items:
                b = QPushButton(text)
                b.setObjectName("seg")
                b.setCheckable(True)
                if with_swatch:
                    b.setIcon(theme.swatch(key))
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.clicked.connect(lambda _=False, key=key: handler(key))
                buttons[key] = b
                row.addWidget(b)
            row.addStretch(1)
            look.body.addLayout(row)
            return buttons

        # 모드(시스템 설정 따르기 / 밝게 / 어둡게)와, 밝을 때·어두울 때 쓸 테마를 따로 고른다
        self.mode_buttons = seg_row("테마", theme.MODES, self.set_theme, False)
        self.theme_buttons = seg_row("밝은 테마", [(t.key, t.name) for t in theme.themes(False)], self.set_palette, True)
        self.theme_buttons.update(seg_row("어두운 테마", [(t.key, t.name) for t in theme.themes(True)], self.set_palette, True))
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
        layout.addWidget(look)

        defaults = Card("패키지 기본값")
        form = _form()
        self.output_field = PathField(placeholder="예: D:/deploy/{project}   ({project} = 프로젝트 이름)")
        self.package_name = QLineEdit()
        self.package_name.setPlaceholderText("%Y-%m-%d-%H%M → 2026-10-26-1133   /   %Y%m%d_%H%M → 20261026_1133")
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
        self.export_btn = button("내보내기…")
        self.export_btn.setToolTip("프로젝트·서버 연결·GitLab/GitHub 주소·패키지 기본값·화면 설정을 파일 하나로 저장합니다.\n"
                                   "토큰·비밀번호는 들어가지 않습니다.")
        self.export_btn.clicked.connect(lambda: self.export_settings())
        self.import_btn = button("가져오기…")
        self.import_btn.setToolTip("내보낸 파일을 읽어 지금 설정에 합치거나, 지금 설정을 파일 내용으로 바꿉니다.")
        self.import_btn.clicked.connect(lambda: self.import_settings())
        row.addWidget(self.export_btn)
        row.addWidget(self.import_btn)
        files.body.addLayout(row)
        files.body.addWidget(label("내보내기·가져오기: 다른 PC 로 옮기거나 팀원과 프로젝트·서버 연결 설정을 나눌 때. "
                                   "토큰·비밀번호는 파일에 들어가지 않으니 가져온 뒤 연결 화면에서 다시 저장하세요.", "muted"))
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

    # ------------------------------------------------------------------ 내보내기·가져오기
    def _ui_settings(self) -> dict:
        settings = QSettings()
        return {key: settings.value(f"ui/{key}") for key in settings_io.UI_KEYS if settings.contains(f"ui/{key}")}

    def export_settings(self, path: Optional[Path] = None) -> Optional[Path]:
        """[내보내기…]: 개인 설정 파일 내용과 화면 설정을 YAML 파일 하나로."""
        if path is None:
            start = str(Path.home() / f"shipkit-settings-{date.today():%Y-%m-%d}.yaml")
            chosen, _ = QFileDialog.getSaveFileName(self, "설정 내보내기", start, "ShipKit 설정 (*.yaml)")
            if not chosen:
                return None
            path = Path(chosen)
        try:
            bundle = settings_io.build(config_store.load_raw(self.ctx.config_path), self._ui_settings())
            settings_io.write(path, bundle)
        except (ConfigError, OSError) as e:
            self.ctx.dialogs.error("설정 내보내기", f"내보내지 못했습니다.\n\n{e}")
            return None
        summary = settings_io.summarize(bundle)
        self.ctx.dialogs.toast(f"설정을 내보냈습니다: {path.name}  (프로젝트 {len(summary.projects)}개, 서버 연결 {summary.servers}개)")
        return path

    def import_settings(self, path: Optional[Path] = None, mode: Optional[str] = None,
                        apply_ui: Optional[bool] = None) -> bool:
        """[가져오기…]: 내보낸 파일을 읽어 내용을 보여 주고, 합치기(없는 프로젝트만 추가) 또는 바꾸기(전체 교체)로 저장한다.
        mode / apply_ui 를 주면(테스트) 묻지 않는다."""
        if path is None:
            chosen, _ = QFileDialog.getOpenFileName(self, "설정 가져오기", str(Path.home()), "ShipKit 설정 (*.yaml);;모든 파일 (*)")
            if not chosen:
                return False
            path = Path(chosen)
        try:
            bundle = settings_io.read(path)
        except ConfigError as e:
            self.ctx.dialogs.error("설정 가져오기", f"이 파일은 가져올 수 없습니다.\n\n{e}")
            return False
        summary = settings_io.summarize(bundle)
        if mode is None:
            choice = self._ask_import(path, summary)
            if choice is None:
                return False
            mode, apply_ui = choice
        if apply_ui is None:
            apply_ui = bool(summary.ui)
        try:
            _cfg, added, skipped = settings_io.apply(self.ctx.config_path, bundle, mode)
        except ConfigError as e:
            self.ctx.dialogs.error("설정 가져오기", f"파일의 설정이 올바르지 않아 가져오지 못했습니다. 지금 설정은 그대로입니다.\n\n{e}")
            return False
        self.ctx.reload(select=added[0] if added and mode == "replace" else None)
        if apply_ui and summary.ui:
            self._apply_ui_settings(summary.ui)
        text = (f"설정을 파일 내용으로 바꿨습니다 (프로젝트 {len(added)}개)." if mode == "replace" else
                f"프로젝트 {len(added)}개를 더했습니다." + (f" 같은 이름 {len(skipped)}개는 건너뛰었습니다: {', '.join(skipped[:4])}"
                                                      + (" …" if len(skipped) > 4 else "") if skipped else ""))
        self.ctx.dialogs.toast(text, "success" if added or mode == "replace" else "info")
        return True

    def _ask_import(self, path: Path, summary: settings_io.Summary) -> Optional[tuple]:
        """가져올 내용과 방법(합치기/바꾸기, 화면 설정 포함)을 묻는다. 취소하면 None."""
        has_current = bool((self.ctx.cfg and self.ctx.cfg.projects) or self.ctx.config_path.is_file())
        lines = summary.lines()
        if summary.exported_at or summary.computer:
            lines.append(f"내보낸 곳: {summary.computer or '?'}  ·  {summary.exported_at[:16].replace('T', ' ')}"
                         + (f"  ·  v{summary.app}" if summary.app else ""))
        text = f"{path.name}\n\n" + "\n".join(f"- {line}" for line in lines)
        if summary.missing_roots:
            text += "\n\n이 PC 에 없는 로컬 경로가 있습니다. 가져온 뒤 프로젝트 화면에서 고치세요:\n" + "\n".join(
                f"- {name}: {root}" for name, root in summary.missing_roots[:5])
        extra = QWidget()
        col = QVBoxLayout(extra)
        col.setContentsMargins(0, 4, 0, 0)
        col.setSpacing(6)
        merge = QRadioButton("합치기 — 없는 프로젝트만 더하고 지금 설정은 그대로 둡니다")
        replace = QRadioButton("바꾸기 — 지금 설정을 파일 내용으로 전부 바꿉니다 (이전 파일은 .bak)")
        (merge if has_current else replace).setChecked(True)
        ui_check = QCheckBox("화면 설정(테마·글자 크기·사이드바)도 가져오기")
        ui_check.setChecked(bool(summary.ui))
        ui_check.setVisible(bool(summary.ui))
        col.addWidget(merge)
        col.addWidget(replace)
        col.addWidget(ui_check)
        dialog = MessageDialog(self, "설정 가져오기", text, "question", [("취소", False), ("가져오기", True)], extra=extra)
        if not dialog.exec():
            return None
        return ("replace" if replace.isChecked() else "merge"), ui_check.isChecked()

    def _apply_ui_settings(self, ui: dict) -> None:
        """가져온 화면 설정을 이 PC 에 기억하고 바로 적용한다."""
        settings = QSettings()
        for key, value in ui.items():
            if key == "theme" and not (value == "system" or value in theme.THEMES or value in theme.ALIASES):
                continue
            if key in ("theme_light", "theme_dark") and value not in theme.THEMES:
                continue
            if key == "font_scale" and value not in dict((k, t) for k, t, _s in theme.FONT_SCALES):
                continue
            settings.setValue(f"ui/{key}", value)
        theme.set_font_scale(str(settings.value("ui/font_scale", "normal")))
        self._apply(str(settings.value("ui/theme", "system")))
        window = self.window()
        if hasattr(window, "set_sidebar_collapsed") and "sidebar_collapsed" in ui:
            window.set_sidebar_collapsed(str(ui["sidebar_collapsed"]).lower() == "true")
        self.refresh()

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
        """모드(system / light / dark)를 바로 바꾸고 이 PC 에 기억한다 (저장 버튼과 상관없음).
        테마 키를 주면 그 테마를 그 모드의 테마로 고르고 모드도 그쪽으로 바꾼다."""
        key = theme.ALIASES.get(key, key)
        if key in theme.THEMES and key not in ("light", "dark"):
            self.set_palette(key)
            return
        if key not in ("light", "dark"):
            key = "system"
        QSettings().setValue("ui/theme", key)
        self._apply(key)

    def set_palette(self, key: str) -> None:
        """밝을 때/어두울 때 쓸 테마를 고른다. 시스템 설정을 따르는 중이면 기억만 하고(그 모드가 되면 쓰임),
        모드를 고정해 둔 상태면 그 테마 쪽 모드로 바꿔 바로 보여 준다."""
        t = theme.THEMES.get(theme.ALIASES.get(key, key))
        if t is None:
            return
        mode = "dark" if t.dark else "light"
        settings = QSettings()
        settings.setValue(f"ui/theme_{mode}", t.key)
        current = str(settings.value("ui/theme", "system"))
        if current != "system":
            settings.setValue("ui/theme", mode)
            current = mode
        self._apply(current)

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

    def _apply(self, setting: str) -> None:
        app = QApplication.instance()
        if app is not None:
            theme.apply(app, setting)
        self._sync_theme_buttons()

    def _sync_theme_buttons(self) -> None:
        """모드 단추는 설정값(예전 테마 이름은 밝은/어두운 쪽으로)대로 하나, 테마 단추는 밝은 쪽·어두운 쪽에서 하나씩."""
        current = str(QSettings().value("ui/theme", "system"))
        current = theme.ALIASES.get(current, current)
        if current in theme.THEMES and current not in ("light", "dark"):
            current = "dark" if theme.THEMES[current].dark else "light"
        if current not in dict(theme.MODES):
            current = "system"
        for key, b in self.mode_buttons.items():
            b.setChecked(key == current)
        chosen = {theme.preferred("light"), theme.preferred("dark")}
        for key, b in self.theme_buttons.items():
            b.setChecked(key in chosen)

    def refresh(self) -> None:
        self._sync_font_buttons()
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
