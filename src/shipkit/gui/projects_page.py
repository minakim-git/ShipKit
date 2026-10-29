"""프로젝트 화면: 목록, 추가·편집·삭제, 기존 툴에서 가져오기."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFormLayout, QHBoxLayout, QHeaderView, QLineEdit, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from shipkit import PROJECT_FILE, config_store, legacy
from shipkit.config import PRESETS, ConfigError, load_project_file
from shipkit.gui.context import AppContext
from shipkit.gui.widgets import Card, PathField, StatusDelegate, button, label, open_path, page_header

INHERIT = "(따름)"


class ProjectDialog(QDialog):
    """프로젝트 추가·편집. 비운 칸은 저장하지 않아서 .shipkit.yml 이나 기본값을 따른다."""

    def __init__(self, parent: Optional[QWidget], name: str = "", entry: Optional[Dict[str, Any]] = None,
                 defaults: Optional[Dict[str, Any]] = None):
        super().__init__(parent)
        self.setWindowTitle("프로젝트 편집" if name else "프로젝트 추가")
        self.setMinimumWidth(680)
        entry = entry or {}
        defaults = defaults or {}

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.name_edit = QLineEdit(name)
        self.name_edit.setPlaceholderText("예: erp")
        self.root_field = PathField(placeholder="로컬 git 저장소 최상위 폴더")
        self.root_field.setText(str(entry.get("root", "")))
        self.root_field.edit.textChanged.connect(self._show_project_file)
        self.output_field = PathField(placeholder=f"비우면 기본값: {defaults.get('output_dir', '(설정 화면에서 지정)')}")
        self.output_field.setText(str(entry.get("output_dir", "")))
        self.preset_combo = QComboBox()
        self.preset_combo.addItem(INHERIT, "")
        for key, preset in PRESETS.items():
            self.preset_combo.addItem(f"{key}  -  {preset['description']}", key)
        self._select(self.preset_combo, entry.get("preset", ""))
        self.os_combo = QComboBox()
        self.os_combo.addItem(INHERIT, "")
        self.os_combo.addItem("리눅스 (shipkit.sh)", "linux")
        self.os_combo.addItem("Windows (shipkit.ps1)", "windows")
        self._select(self.os_combo, entry.get("server_os", ""))
        self.server_edit = QLineEdit(str(entry.get("server_path", "")))
        self.server_edit.setPlaceholderText("예: /app/tomcat/webapps/ROOT  (Windows: D:/tomcat/webapps/ROOT)")
        self.gitlab_edit = QLineEdit(str(entry.get("gitlab_project", "")))
        self.gitlab_edit.setPlaceholderText("GitLab 프로젝트 ID 또는 그룹/프로젝트")
        self.github_edit = QLineEdit(str(entry.get("github_repo", "")))
        self.github_edit.setPlaceholderText("owner/name")

        for text, widget in (("이름", self.name_edit), ("로컬 경로", self.root_field),
                             ("패키지 생성 경로", self.output_field), ("프리셋", self.preset_combo),
                             ("서버 OS", self.os_combo), ("서버 배포 경로", self.server_edit),
                             ("GitLab 프로젝트", self.gitlab_edit), ("GitHub 저장소", self.github_edit)):
            form.addRow(label(text, "fieldLabel", wrap=False), widget)

        self.project_file_label = label("", "muted")
        extra = sorted(set(entry) - set(config_store.PROJECT_FIELDS))
        extra_label = label(
            f"이 화면에 없는 설정({', '.join(extra)})은 그대로 유지됩니다." if extra else
            "hooks·제외 규칙 같은 고급 설정은 프로젝트의 .shipkit.yml 이나 설정 파일에서 지정합니다.", "muted")

        card = Card("프로젝트 편집" if name else "새 프로젝트", "비워 둔 칸은 저장소의 .shipkit.yml 이나 기본값을 따릅니다.")
        card.body.addLayout(form)
        card.body.addWidget(self.project_file_label)
        card.body.addWidget(extra_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = button("취소")
        cancel.clicked.connect(self.reject)
        save = button("저장", "primary")
        save.setDefault(True)
        save.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(save)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)
        layout.addWidget(card)
        layout.addLayout(buttons)
        self._show_project_file()

    @staticmethod
    def _select(combo: QComboBox, value: str) -> None:
        idx = combo.findData(value or "")
        combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _show_project_file(self) -> None:
        root = self.root_field.text()
        path = Path(root) / PROJECT_FILE if root else None
        if not path or not path.is_file():
            self.project_file_label.setText(
                f"저장소에 {PROJECT_FILE} 가 없습니다. 프리셋과 서버 배포 경로를 직접 입력하세요.")
            return
        try:
            shared = load_project_file(path)
        except ConfigError as e:
            self.project_file_label.setText(f"{PROJECT_FILE} 오류: {e}")
            return
        keys = ", ".join(f"{k}: {shared[k]}" for k in ("preset", "server_os", "server_path", "gitlab_project",
                                                       "github_repo") if k in shared)
        self.project_file_label.setText(
            f"{PROJECT_FILE} 을 찾았습니다 ({keys or '값 없음'}). 비워 둔 칸은 이 값을 따르고, 입력한 칸이 우선합니다.")

    def values(self) -> Dict[str, Any]:
        return {
            "root": self.root_field.text(),
            "output_dir": self.output_field.text(),
            "preset": self.preset_combo.currentData(),
            "server_os": self.os_combo.currentData(),
            "server_path": self.server_edit.text().strip(),
            "gitlab_project": self.gitlab_edit.text().strip(),
            "github_repo": self.github_edit.text().strip(),
        }

    def name(self) -> str:
        return self.name_edit.text().strip()


class ProjectsPage(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.dialog_factory = ProjectDialog  # 테스트에서 바꿔 끼운다
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        add_btn = button("+  프로젝트 추가", "primary")
        add_btn.clicked.connect(self.add_project)
        layout.addWidget(page_header(
            "프로젝트", f"배포할 프로젝트를 등록합니다. 팀이 함께 쓰는 값은 저장소의 {PROJECT_FILE} 에 두면 "
                     "로컬 경로만 등록해도 됩니다.", add_btn))

        toolbar = QWidget()
        bar = QHBoxLayout(toolbar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(6)
        for text, slot, kind in (("편집", self.edit_project, ""), ("삭제", self.delete_project, "danger")):
            b = button(text, kind)
            b.clicked.connect(slot)
            bar.addWidget(b)
        card = Card("등록된 프로젝트", "행을 두 번 누르면 편집합니다.", right=toolbar)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["이름", "방식", "서버", "로컬 경로", "연동", PROJECT_FILE])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.horizontalHeader().setHighlightSections(False)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for col in (1, 5):
            self.table.setItemDelegateForColumn(col, StatusDelegate(self.table))
        self.table.cellDoubleClicked.connect(lambda *_: self.edit_project())
        card.body.addWidget(self.table, 1)
        layout.addWidget(card, 1)

        links = QHBoxLayout()
        legacy_btn = button("기존 툴에서 가져오기", "ghost")
        legacy_btn.clicked.connect(self.import_legacy)
        open_btn = button("설정 파일 열기", "ghost")
        open_btn.clicked.connect(lambda: self.ctx.config_path.is_file() and open_path(self.ctx.config_path))
        links.addWidget(legacy_btn)
        links.addWidget(open_btn)
        links.addStretch(1)
        layout.addLayout(links)
        ctx.listeners.append(self.refresh)

    def refresh(self) -> None:
        self.table.setRowCount(0)
        cfg = self.ctx.cfg
        if not cfg:
            return
        for name in sorted(cfg.projects):
            p = cfg.projects[name]
            links = ", ".join(x for x in (f"GitLab {p.gitlab_project}" if p.gitlab_project else "",
                                          f"GitHub {p.github_repo}" if p.github_repo else "") if x)
            server = f"{p.server_path}" + (" (Windows)" if p.server_os == "windows" else "")
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [name, p.preset or ("sync" if p.mode == "sync" else "직접 규칙"), server,
                      str(p.root), links or "-", "있음" if p.project_file else "없음"]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if col == 0:
                    font = QFont(item.font())
                    font.setWeight(QFont.Weight.DemiBold)
                    item.setFont(font)
                elif col == 1:
                    item.setData(Qt.ItemDataRole.UserRole, "primary")
                elif col == 5:
                    item.setData(Qt.ItemDataRole.UserRole, "success" if p.project_file else "neutral")
                self.table.setItem(row, col, item)
        self.table.resizeColumnsToContents()
        for col in (1, 5):
            self.table.setColumnWidth(col, self.table.columnWidth(col) + 30)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

    def selected_name(self) -> Optional[str]:
        row = self.table.currentRow()
        return self.table.item(row, 0).text() if row >= 0 and self.table.item(row, 0) else None

    def _save(self, mutate, select: Optional[str] = None) -> bool:
        """설정을 고쳐 저장하고 다시 읽는다. select: 저장 뒤 작업 프로젝트로 고를 이름."""
        data = config_store.load_raw(self.ctx.config_path)
        before = copy.deepcopy(data)
        try:
            mutate(data)
            config_store.save_raw(self.ctx.config_path, data)
        except ConfigError as e:
            self.ctx.dialogs.error("저장하지 못했습니다", str(e))
            return False
        if data != before:
            self.ctx.reload(select)
        return True

    def add_project(self) -> None:
        self._edit(None)

    def edit_project(self) -> None:
        name = self.selected_name()
        if not name:
            self.ctx.dialogs.error("프로젝트 선택", "편집할 프로젝트를 고르세요.")
            return
        self._edit(name)

    def _edit(self, old_name: Optional[str]) -> None:
        data = config_store.load_raw(self.ctx.config_path)
        entry = (data.get("projects") or {}).get(old_name, {}) if old_name else {}
        dialog = self.dialog_factory(self, old_name or "", entry, data.get("defaults") or {})
        while dialog.exec():
            if self._save(lambda d: config_store.set_project(d, dialog.name(), dialog.values(), old_name),
                          select=dialog.name()):
                return

    def delete_project(self) -> None:
        name = self.selected_name()
        if not name:
            self.ctx.dialogs.error("프로젝트 선택", "삭제할 프로젝트를 고르세요.")
            return
        if self.ctx.dialogs.ask("삭제 확인", f"'{name}' 프로젝트를 목록에서 지울까요?\n(로컬 저장소와 파일은 그대로 둡니다)"):
            self._save(lambda d: config_store.delete_project(d, name))

    def import_legacy(self) -> None:
        db = legacy.legacy_db_path()
        if not db.is_file():
            self.ctx.dialogs.error("기존 툴 설정 없음", f"기존 툴(compiled-java-deployer) DB 가 없습니다:\n{db}")
            return
        result: Dict[str, Any] = {}

        def mutate(data):
            result["added"], result["skipped"] = config_store.merge_legacy(data, db)

        if self._save(mutate):
            added, skipped = result["added"], result["skipped"]
            text = f"{len(added)}개 프로젝트를 가져왔습니다." + (f"\n이미 있어서 건너뜀: {', '.join(skipped)}" if skipped else "")
            text += "\n토큰은 가져오지 않습니다. 설정 화면에서 토큰을 저장하세요."
            self.ctx.dialogs.info("가져오기 완료", text)
