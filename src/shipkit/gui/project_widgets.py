"""프로젝트 설정 화면이 쓰는 입력 위젯과 '새 프로젝트' 창 (projects_page 에서 떼어 냄).

LineList: 한 줄에 하나씩 적는 목록 / RowTable: 열이 정해진 표 (규칙·알림·서버 명령) / OverrideBox: 팀 공유 값을 이 PC 에서 덮어쓸지
ProjectDialog: 프로젝트 추가 창
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLineEdit,
    QPlainTextEdit, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from shipkit import PROJECT_FILE
from shipkit.config import PRESETS, ConfigError, load_project_file
from shipkit.gui import theme
from shipkit.gui.widgets import Card, PathField, button, label
from shipkit.projectfile import detect_preset

INHERIT = "(따름)"


def form_layout() -> QFormLayout:
    form = QFormLayout()
    form.setHorizontalSpacing(16)
    form.setVerticalSpacing(10)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return form


# ---------------------------------------------------------------------- 편집용 위젯
class LineList(QPlainTextEdit):
    """한 줄에 하나씩 적는 목록."""

    def __init__(self, placeholder: str, height: int = 92):
        super().__init__()
        self.setPlaceholderText(placeholder)
        self.setFixedHeight(height)

    def lines(self) -> List[str]:
        return [x.strip() for x in self.toPlainText().splitlines() if x.strip()]

    def set_lines(self, values: Sequence[str]) -> None:
        self.setPlainText("\n".join(str(v) for v in values))


class _Table(QTableWidget):
    """줄이 하나도 없으면 예시 글자(placeholder)를 흐리게 보여 주는 표."""

    def __init__(self, rows: int, columns: int, placeholder: str = ""):
        super().__init__(rows, columns)
        self.placeholder = placeholder

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.rowCount() == 0 and self.placeholder:
            painter = QPainter(self.viewport())
            painter.setPen(QColor(theme.SUBTLE))
            painter.drawText(self.viewport().rect().adjusted(12, 0, -12, 0),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.placeholder)
            painter.end()


class RowTable(QWidget):
    """칸을 직접 고치는 표 + [추가]/[지우기]. rows() 는 칸 이름 -> 값 사전 목록 (모두 빈 행은 뺀다)."""
    changed = Signal()

    def __init__(self, columns: Sequence[Tuple[str, str]], stretch: int = 0, height: int = 150,
                 placeholder: str = ""):
        super().__init__()
        self.keys = [k for k, _ in columns]
        self.table = _Table(0, len(columns), placeholder)
        self.table.setHorizontalHeaderLabels([h for _, h in columns])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.SelectedClicked
                                   | QAbstractItemView.EditTrigger.AnyKeyPressed)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(36)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setHighlightSections(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
        for col in range(len(columns)):
            if col != stretch:
                self.table.setColumnWidth(col, 200)
        self.table.setFixedHeight(height)  # 줄이 많으면 표 안에서 스크롤
        self.table.itemChanged.connect(lambda *_: self.changed.emit())
        self.add_btn = button("+ 추가", "ghost")
        self.add_btn.clicked.connect(self.add_row)
        self.remove_btn = button("고른 줄 지우기", "ghost")
        self.remove_btn.clicked.connect(self.remove_selected)
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(self.add_btn)
        bar.addWidget(self.remove_btn)
        bar.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.table)
        layout.addLayout(bar)

    def rows(self) -> List[Dict[str, str]]:
        result = []
        for r in range(self.table.rowCount()):
            row = {k: (self.table.item(r, c).text().strip() if self.table.item(r, c) else "")
                   for c, k in enumerate(self.keys)}
            if any(row.values()):
                result.append(row)
        return result

    def set_rows(self, rows: Sequence[Dict[str, Any]]) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for row in rows:
            self._insert(row)
        self.table.blockSignals(False)

    def _insert(self, row: Dict[str, Any]) -> int:
        r = self.table.rowCount()
        self.table.insertRow(r)
        for c, k in enumerate(self.keys):
            value = row.get(k)
            self.table.setItem(r, c, QTableWidgetItem("" if value is None else str(value)))
        return r

    def add_row(self) -> None:
        r = self._insert({})
        self.table.setCurrentCell(r, 0)
        self.table.editItem(self.table.item(r, 0))

    def remove_selected(self) -> None:
        for r in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(r)
        self.changed.emit()

    def set_editable(self, editable: bool) -> None:
        self.table.setEnabled(editable)
        self.add_btn.setVisible(editable)
        self.remove_btn.setVisible(editable)


class OverrideBox(QFrame):
    """팀 공유 파일·프리셋에서 오는 값을 따를지, 이 PC 에서 직접 지정할지 고르는 띠.

    물려받는 값이 없으면 띠를 숨기고 항상 직접 지정으로 본다.
    """
    toggled = Signal(bool)

    def __init__(self, what: str):
        super().__init__()
        self.setObjectName("override")
        self.what = what
        self.text = label("", "muted")
        self.check = QCheckBox("직접 지정")
        self.check.setCursor(Qt.CursorShape.PointingHandCursor)
        self.check.toggled.connect(self.toggled.emit)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 6, 12, 6)
        row.addWidget(self.text, 1)
        row.addWidget(self.check)
        self.source: Optional[str] = None

    def set_source(self, source: Optional[str]) -> None:
        self.source = source
        self.setVisible(bool(source))
        if source:
            self.text.setText(f"{self.what}: <b>{source}</b> 값을 따릅니다. 이 PC 에서 바꾸려면 직접 지정하세요."
                              if not self.check.isChecked() else
                              f"{self.what}: 직접 지정한 값을 씁니다 ({source} 값 대신).")

    def overridden(self) -> bool:
        return self.check.isChecked() or not self.source

    def set_overridden(self, value: bool) -> None:
        self.check.blockSignals(True)
        self.check.setChecked(value)
        self.check.blockSignals(False)
        self.set_source(self.source)


# ---------------------------------------------------------------------- 프로젝트 추가
class ProjectDialog(QDialog):
    """프로젝트 추가. 비운 칸은 저장하지 않아서 .shipkit.yml 이나 기본값을 따른다. 자세한 설정은 추가한 뒤 화면에서."""

    def __init__(self, parent: Optional[QWidget], name: str = "", entry: Optional[Dict[str, Any]] = None,
                 defaults: Optional[Dict[str, Any]] = None):
        super().__init__(parent)
        self.setWindowTitle("프로젝트 추가")
        self.setMinimumWidth(680)
        entry = entry or {}
        defaults = defaults or {}

        form = form_layout()
        self.name_edit = QLineEdit(name)
        self.name_edit.setPlaceholderText("예: erp")
        self.root_field = PathField(placeholder="로컬 git 저장소 최상위 폴더")
        self.root_field.setText(str(entry.get("root", "")))
        self.root_field.edit.textChanged.connect(self._show_project_file)
        self.output_field = PathField(placeholder=f"비우면 기본값: {defaults.get('output_dir', '([앱 설정]에서 지정)')}")
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
        card = Card("새 프로젝트", "반영 규칙·서버 명령 같은 자세한 설정은 추가한 뒤 프로젝트 화면에서 바꿉니다.")
        card.body.addLayout(form)
        card.body.addWidget(self.project_file_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = button("취소")
        cancel.clicked.connect(self.reject)
        save = button("추가", "primary")
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
            if root and Path(root).is_dir() and not self.preset_combo.currentData():
                preset, why = detect_preset(Path(root))
                self._select(self.preset_combo, preset)
                self.project_file_label.setText(f"저장소에 {PROJECT_FILE} 가 없습니다. 프리셋은 {why} 를 보고 "
                                                f"{preset} 으로 골랐습니다. 서버 배포 경로를 입력하세요.")
            else:
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


# ---------------------------------------------------------------------- 프로젝트 설정 화면
