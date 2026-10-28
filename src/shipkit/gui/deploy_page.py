"""배포 화면: 프로젝트 고르기 -> 무엇을 배포할지 고르기 -> 미리보기 -> 패키지 만들기."""
from __future__ import annotations

import re
from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QProgressBar, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from shipkit import packager, workflow
from shipkit.config import ProjectConfig
from shipkit.gui import theme
from shipkit.gui.context import AppContext
from shipkit.gui.widgets import Banner, Card, Chip, StatusDelegate, button, label, open_path, page_header
from shipkit.models import FileChange, SourceInfo, Status
from shipkit.sources import git_local, providers

TAB_REQUESTS, TAB_GIT, TAB_FILES, TAB_BUILD = range(4)
STATES = [("머지됨", "merged"), ("열림", "opened"), ("닫힘", "closed"), ("전체", "all")]
STATUS_TEXT = {Status.OK: "성공", Status.WARN: "경고", Status.ERROR: "실패", Status.SKIPPED: "제외"}
STATUS_TONE = {Status.OK: "success", Status.WARN: "warning", Status.ERROR: "danger", Status.SKIPPED: "neutral"}
SERVICE_LABEL = {"gitlab": "GitLab MR", "github": "GitHub PR"}


def _table(headers: List[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setShowGrid(False)
    t.setAlternatingRowColors(False)
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(38)
    t.setWordWrap(False)
    t.setTextElideMode(Qt.TextElideMode.ElideMiddle)
    t.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    t.horizontalHeader().setHighlightSections(False)
    return t


class DeployPage(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.settings = QSettings()
        self.project: Optional[ProjectConfig] = None
        # (프로젝트, 변경 목록, 출처 정보, 미리보기 결과, 배포 계획)
        self.prepared: Optional[Tuple[ProjectConfig, List[FileChange], SourceInfo, packager.PackageResult, list]] = None
        self.last_result: Optional[packager.PackageResult] = None
        self._build_ui()
        ctx.listeners.append(self.refresh)

    # ------------------------------------------------------------------ 화면 구성
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)
        root.addWidget(page_header("배포", "바뀐 파일만 골라 서버에 올릴 패키지를 만듭니다."))

        # 프로젝트
        project_card = Card(padding=16)
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(label("프로젝트", "fieldLabel", wrap=False))
        self.project_combo = QComboBox()
        self.project_combo.setMinimumWidth(280)
        self.project_combo.currentIndexChanged.connect(self._on_project_changed)
        row.addWidget(self.project_combo)
        self.mode_chip = Chip("", "primary")
        self.os_chip = Chip("", "neutral")
        row.addWidget(self.mode_chip)
        row.addWidget(self.os_chip)
        self.mode_label = label("", "muted", wrap=False)
        row.addWidget(self.mode_label, 1)
        project_card.body.addLayout(row)
        root.addWidget(project_card)

        # 무엇을 배포할지
        source_card = Card(padding=14)  # 탭 버튼이 제목 역할을 한다
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._requests_tab(), "MR / PR")
        self.tabs.addTab(self._git_tab(), "git 범위")
        self.tabs.addTab(self._files_tab(), "파일 목록")
        self.tabs.addTab(self._build_tab(), "빌드 결과 전체")
        self.tabs.currentChanged.connect(self._invalidate)
        source_card.body.addWidget(self.tabs)

        # 미리보기
        stats = QWidget()
        stats_row = QHBoxLayout(stats)
        stats_row.setContentsMargins(0, 0, 0, 0)
        stats_row.setSpacing(6)
        self.stat_chips = {key: Chip("", tone) for key, tone in
                           (("apply", "primary"), ("delete", "neutral"), ("warn", "warning"), ("error", "danger"))}
        for chip in self.stat_chips.values():
            chip.setVisible(False)
            stats_row.addWidget(chip)
        preview_card = Card("미리보기", right=stats, padding=16)
        self.summary_label = label("미리보기를 누르면 서버에 반영될 파일이 여기에 나옵니다.", "muted")
        self.messages = QListWidget()
        self.messages.setObjectName("messages")
        self.messages.setVisible(False)
        self.result_table = _table(["상태", "구분", "소스 파일", "서버 반영 / 삭제", "비고"])
        self.result_table.setItemDelegateForColumn(0, StatusDelegate(self.result_table))
        self.result_table.horizontalHeader().setStretchLastSection(True)
        preview_card.body.addWidget(self.summary_label)
        preview_card.body.addWidget(self.messages)
        preview_card.body.addWidget(self.result_table, 1)

        self.splitter = splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setHandleWidth(14)
        splitter.addWidget(source_card)
        splitter.addWidget(preview_card)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 5)
        splitter.setChildrenCollapsible(False)
        self._sized = False
        root.addWidget(splitter, 1)

        # 완료 안내
        self.done_banner = Banner("success")
        self.done_label = self.done_banner.text
        self.open_folder_btn = button("폴더 열기")
        self.open_report_btn = button("보고서 열기")
        self.open_folder_btn.clicked.connect(lambda: self.last_result and open_path(self.last_result.out_dir))
        self.open_report_btn.clicked.connect(
            lambda: self.last_result and open_path(self.last_result.out_dir / "report.md"))
        self.done_banner.layout_.addWidget(self.open_folder_btn)
        self.done_banner.layout_.addWidget(self.open_report_btn)
        root.addWidget(self.done_banner)
        self._show_done(False)

        # 실행 줄
        actions = QHBoxLayout()
        actions.setSpacing(16)
        self.archive_check = QCheckBox("압축 파일도 만들기")
        self.strict_check = QCheckBox("경고도 실패로 처리")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(160)
        self.progress.setVisible(False)
        self.preview_btn = button("미리보기")
        self.preview_btn.setMinimumWidth(110)
        self.preview_btn.clicked.connect(self.preview)
        self.make_btn = button("패키지 만들기", "primary")
        self.make_btn.setMinimumWidth(140)
        self.make_btn.setEnabled(False)
        self.make_btn.clicked.connect(self.make_package)
        actions.addWidget(self.archive_check)
        actions.addWidget(self.strict_check)
        actions.addStretch(1)
        actions.addWidget(self.progress)
        actions.addWidget(self.preview_btn)
        actions.addWidget(self.make_btn)
        root.addLayout(actions)
        self.ctx.runner.busy_changed.connect(self._set_busy)

    def _requests_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(10)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.service_combo = QComboBox()
        self.service_combo.currentIndexChanged.connect(self._invalidate)
        self.state_combo = QComboBox()
        for text, value in STATES:
            self.state_combo.addItem(text, value)
        self.state_combo.setFixedWidth(110)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("제목으로 검색")
        self.search_edit.returnPressed.connect(self.load_requests)
        self.load_btn = button("목록 불러오기")
        self.load_btn.clicked.connect(self.load_requests)
        bar.addWidget(self.service_combo)
        bar.addWidget(self.state_combo)
        bar.addWidget(self.search_edit, 1)
        bar.addWidget(self.load_btn)
        layout.addLayout(bar)
        self.req_table = _table(["", "번호", "제목", "작성자", "머지 / 수정 시각"])
        header = self.req_table.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.req_table.setColumnWidth(0, 44)
        self.req_table.cellClicked.connect(self._toggle_row)
        self.req_table.itemChanged.connect(self._invalidate)
        self.req_table.setMinimumHeight(60)
        layout.addWidget(self.req_table, 1)
        extra = QHBoxLayout()
        extra.setSpacing(12)
        self.extra_edit = QLineEdit()
        self.extra_edit.setPlaceholderText("목록에 없는 번호 직접 입력 (예: 612 615)")
        self.extra_edit.setMaximumWidth(340)
        self.extra_edit.textChanged.connect(self._invalidate)
        self.req_hint = label("", "muted", wrap=False)
        extra.addWidget(self.extra_edit, 1)
        extra.addWidget(self.req_hint, 2)
        layout.addLayout(extra)
        return w

    def _git_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 4, 0, 0)
        row = QHBoxLayout()
        row.addWidget(label("범위", "fieldLabel", wrap=False))
        self.range_edit = QLineEdit()
        self.range_edit.setPlaceholderText("예: v1.2..HEAD, abc123..def456")
        self.range_edit.textChanged.connect(self._invalidate)
        row.addWidget(self.range_edit, 1)
        layout.addLayout(row)
        layout.addWidget(label("두 커밋(또는 태그) 사이에 바뀐 파일을 배포합니다. 기본값은 가장 최근 태그부터 지금(HEAD)까지입니다.",
                               "muted"))
        layout.addStretch(1)
        return w

    def _files_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 4, 0, 0)
        self.files_edit = QPlainTextEdit()
        self.files_edit.setPlaceholderText(
            "한 줄에 경로 하나씩 붙여넣으세요.\n"
            "src/main/webapp/WEB-INF/jsp/a.jsp\n"
            "git diff --name-status 결과도 됩니다 (D<TAB>경로 = 삭제)")
        self.files_edit.setMinimumHeight(70)
        self.files_edit.textChanged.connect(self._invalidate)
        layout.addWidget(self.files_edit)
        return w

    def _build_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 4, 0, 0)
        self.build_hint = label("", "muted")
        layout.addWidget(self.build_hint)
        layout.addStretch(1)
        return w

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._sized:  # 처음 보일 때 한 번, 레이아웃이 잡힌 뒤: 입력 영역은 작게, 미리보기는 크게
            self._sized = True
            QTimer.singleShot(0, self._initial_split)

    def _initial_split(self) -> None:
        total = sum(self.splitter.sizes()) or self.splitter.height()
        top = max(210, int(total * 0.36))
        self.splitter.setSizes([top, max(total - top, 200)])

    # ------------------------------------------------------------------ 상태
    def refresh(self) -> None:
        cfg = self.ctx.cfg
        current = self.project_combo.currentText() or self.settings.value("deploy/project", "")
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        if cfg:
            for name in sorted(cfg.projects):
                self.project_combo.addItem(name)
        idx = self.project_combo.findText(current)
        self.project_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.project_combo.blockSignals(False)
        self._on_project_changed()

    def _on_project_changed(self) -> None:
        cfg, name = self.ctx.cfg, self.project_combo.currentText()
        self.project = cfg.projects.get(name) if cfg and name else None
        self._invalidate()
        self._clear_preview()
        self.req_table.setRowCount(0)
        enabled = self.project is not None
        for w in (self.tabs, self.preview_btn):
            w.setEnabled(enabled)
        self.mode_chip.setVisible(enabled)
        self.os_chip.setVisible(enabled)
        if not self.project:
            self.mode_label.setText("프로젝트 화면에서 프로젝트를 먼저 등록하세요." if cfg else "")
            return
        self.settings.setValue("deploy/project", name)
        p = self.project
        self.mode_chip.set(p.preset or ("빌드 결과 동기화" if p.mode == "sync" else "직접 규칙"), "primary")
        self.os_chip.set("Windows 서버" if p.server_os == "windows" else "리눅스 서버", "neutral")
        self.mode_label.setText(f"{p.describe_mode}  ·  서버 경로 {p.server_path}")
        self.archive_check.setText("압축 파일도 만들기 (.zip)" if p.server_os == "windows" else "압축 파일도 만들기 (.tar.gz)")

        kinds = providers.available(cfg, p)
        self.service_combo.blockSignals(True)
        self.service_combo.clear()
        for kind in kinds:
            self.service_combo.addItem(SERVICE_LABEL[kind], kind)
        self.service_combo.blockSignals(False)
        self.service_combo.setVisible(len(kinds) > 1)
        is_git = p.root.is_dir() and git_local.is_git_repo(p.root)
        self.tabs.setTabVisible(TAB_REQUESTS, bool(kinds))
        if kinds:
            self.tabs.setTabText(TAB_REQUESTS, " / ".join(SERVICE_LABEL[k] for k in kinds))
            self.req_hint.setText("행을 누르면 선택됩니다. 여러 개를 고르면 머지된 순서대로 합칩니다.")
        self.tabs.setTabVisible(TAB_GIT, is_git)
        self.tabs.setTabVisible(TAB_BUILD, p.mode == "sync")
        self.build_hint.setText(f"{p.build_dir} 폴더 전체를 패키지에 담고, 서버에서는 내용이 다른 파일만 반영합니다. "
                                "이번 빌드에 없는 옛 파일은 정리 폴더에서 지워집니다." if p.mode == "sync" else "")
        if is_git and not self.range_edit.text():
            self.range_edit.setText(workflow.latest_tag_range(p))
        default = TAB_BUILD if p.mode == "sync" else (TAB_REQUESTS if kinds else TAB_FILES)
        self.tabs.setCurrentIndex(default)

    def _invalidate(self, *_args) -> None:
        self.prepared = None
        self.make_btn.setEnabled(False)

    def _clear_preview(self) -> None:
        self.result_table.setRowCount(0)
        self.messages.clear()
        self.messages.setVisible(False)
        for chip in self.stat_chips.values():
            chip.setVisible(False)
        self.summary_label.setText("미리보기를 누르면 서버에 반영될 파일이 여기에 나옵니다.")

    def _set_busy(self, busy: bool) -> None:
        self.progress.setVisible(busy)
        for w in (self.preview_btn, self.load_btn, self.project_combo):
            w.setEnabled(not busy)
        if busy:
            self.make_btn.setEnabled(False)
        else:
            self.make_btn.setEnabled(self.prepared is not None and self._has_output())

    def _has_output(self) -> bool:
        result = self.prepared[3] if self.prepared else None
        return bool(result and (result.manifest or result.deletes))

    def _show_done(self, visible: bool) -> None:
        self.done_banner.setVisible(visible)

    # ------------------------------------------------------------------ MR/PR 목록
    def load_requests(self) -> None:
        if not self.project or self.service_combo.count() == 0:
            return
        kind = self.service_combo.currentData()
        state = self.state_combo.currentData()
        search = self.search_edit.text().strip() or None
        cfg, project = self.ctx.cfg, self.project

        def fetch():
            provider = providers.make_provider(cfg, project, kind)
            return provider, provider.list(state=state, limit=50, search=search)

        self.ctx.runner.run(fetch, self._show_requests, lambda msg: self.ctx.dialogs.error("목록 불러오기 실패", msg))

    def _show_requests(self, payload) -> None:
        provider, items = payload
        self.req_table.blockSignals(True)
        self.req_table.setRowCount(0)
        for it in items:
            row = self.req_table.rowCount()
            self.req_table.insertRow(row)
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check.setCheckState(Qt.CheckState.Unchecked)
            check.setData(Qt.ItemDataRole.UserRole, it["iid"])
            when = (it.get("merged_at") or it.get("updated_at") or "")[:16].replace("T", " ")
            values = [f"{provider.prefix}{it['iid']}", it.get("title", ""), (it.get("author") or {}).get("name", ""), when]
            self.req_table.setItem(row, 0, check)
            for col, value in enumerate(values, 1):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if col == 1:
                    item.setForeground(QColor(theme.PRIMARY))
                self.req_table.setItem(row, col, item)
        self.req_table.blockSignals(False)
        for col in (1, 3, 4):
            self.req_table.resizeColumnToContents(col)
        self.req_table.setColumnWidth(0, 44)
        self.req_hint.setText("조회된 항목이 없습니다. 상태(머지됨/열림)나 검색어를 바꿔 보세요." if not items else
                              f"{len(items)}개를 불러왔습니다. 행을 누르면 선택됩니다. 여러 개를 고르면 머지된 순서대로 합칩니다.")
        self._invalidate()

    def _toggle_row(self, row: int, col: int) -> None:
        if col == 0:  # 체크 칸은 Qt 가 직접 토글한다
            return
        item = self.req_table.item(row, 0)
        checked = item.checkState() == Qt.CheckState.Checked
        item.setCheckState(Qt.CheckState.Unchecked if checked else Qt.CheckState.Checked)

    def selected_numbers(self) -> List[int]:
        numbers = []
        for row in range(self.req_table.rowCount()):
            item = self.req_table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                numbers.append(int(item.data(Qt.ItemDataRole.UserRole)))
        numbers += [int(n) for n in re.findall(r"\d+", self.extra_edit.text())]
        return list(dict.fromkeys(numbers))

    # ------------------------------------------------------------------ 미리보기 / 만들기
    def _collector(self) -> Optional[Callable[[], Tuple[List[FileChange], SourceInfo]]]:
        """현재 탭의 입력으로 변경 수집 함수를 만든다. 입력이 부족하면 안내하고 None."""
        cfg, project, tab = self.ctx.cfg, self.project, self.tabs.currentIndex()
        if tab == TAB_REQUESTS:
            numbers = self.selected_numbers()
            if not numbers:
                self.ctx.dialogs.error("입력 확인", "배포할 MR/PR 을 하나 이상 고르세요.")
                return None
            kind = self.service_combo.currentData()
            return lambda: workflow.collect_requests(providers.make_provider(cfg, project, kind), project, numbers)
        if tab == TAB_GIT:
            rng = self.range_edit.text().strip()
            if not rng:
                self.ctx.dialogs.error("입력 확인", "git 범위를 입력하세요. 예: v1.2..HEAD")
                return None
            return lambda: workflow.collect_git(project, rng)
        if tab == TAB_FILES:
            lines = [line for line in self.files_edit.toPlainText().splitlines() if line.strip()]
            if not lines:
                self.ctx.dialogs.error("입력 확인", "배포할 파일 경로를 붙여넣으세요.")
                return None
            return lambda: workflow.collect_lines(lines, "직접 입력 (붙여넣기)")
        return lambda: workflow.collect_build(project)

    def preview(self) -> None:
        if not self.project:
            return
        collect = self._collector()
        if collect is None:
            return
        project = self.project
        self._show_done(False)

        def run():
            changes, source = collect()
            entries = workflow.plan(project, changes, source)
            result = packager.build_package(project, entries, source, dry_run=True, source_changes=changes)
            return project, changes, source, entries, result

        self.ctx.runner.run(run, self._show_preview, lambda msg: self.ctx.dialogs.error("미리보기 실패", msg))

    def _show_preview(self, payload) -> None:
        project, changes, source, entries, result = payload
        self.prepared = (project, changes, source, result, entries)
        self.messages.clear()
        for text in source.warnings:
            self._message(f"⚠  {text}", "warning")
        for text in source.notes:
            self._message(f"ℹ  {text}", "info")
        for stage, commands in result.hooks.items():
            for cmd in commands:
                self._message(f"▶  서버에서 실행할 명령 ({stage}): {cmd}", "neutral")
        self.messages.setVisible(self.messages.count() > 0)
        self.messages.setFixedHeight(min(self.messages.count(), 3) * 34 + 6)  # 내용만큼만 (최대 3줄, 넘치면 스크롤)

        rows = entries
        if project.mode == "sync":
            rows = [e for e in entries if e.status in (Status.ERROR, Status.WARN)]
        order = {Status.ERROR: 0, Status.WARN: 1, Status.OK: 2, Status.SKIPPED: 3}
        rows = sorted(rows, key=lambda e: (order[e.status], e.change.path))  # 확인할 항목을 위로
        self.result_table.setRowCount(0)
        for e in rows:
            outputs = [a.target for a in e.artifacts] + [f"(삭제) {d}" for d in e.deletes]
            self._add_row([STATUS_TEXT[e.status], e.change.change.label, e.change.path,
                           ",  ".join(outputs) or "-", "; ".join(e.messages)], e.status)
        self.result_table.resizeColumnsToContents()
        self.result_table.setColumnWidth(0, max(self.result_table.columnWidth(0), 76))
        for col in (2, 3):  # 긴 경로가 '비고' 칸을 밀어내지 않게 (전체 내용은 툴팁)
            self.result_table.setColumnWidth(col, min(self.result_table.columnWidth(col), 360))
        self.result_table.horizontalHeader().setStretchLastSection(True)

        counts = {s: result.count(s) for s in Status}
        head = f"{source.description}  ·  "
        if project.mode == "sync":
            head += f"빌드 결과 {len(result.manifest)}개 파일 (서버와 다른 파일만 반영)"
        else:
            head += f"서버 반영 {len(result.manifest)}개 · 서버 삭제 {len(result.deletes)}개"
        head += (f"  ·  성공 {counts[Status.OK]} / 경고 {counts[Status.WARN]} / 실패 {counts[Status.ERROR]}"
                 f" / 제외 {counts[Status.SKIPPED]}")
        if not (result.manifest or result.deletes):
            head += "  -  서버에 반영할 파일이 없습니다."
        self.summary_label.setText(head)

        chips = self.stat_chips
        chips["apply"].setText(f"반영 {len(result.manifest)}")
        chips["delete"].setText(f"삭제 {len(result.deletes)}")
        chips["warn"].setText(f"경고 {counts[Status.WARN] + len(source.warnings)}")
        chips["error"].setText(f"실패 {counts[Status.ERROR]}")
        chips["apply"].setVisible(True)
        chips["delete"].setVisible(bool(result.deletes))
        chips["warn"].setVisible(bool(counts[Status.WARN] or source.warnings))
        chips["error"].setVisible(bool(counts[Status.ERROR]))
        self.make_btn.setEnabled(bool(result.manifest or result.deletes))

    def _message(self, text: str, tone: str) -> None:
        fg, bg = theme.TONES[tone]
        item = QListWidgetItem(text)
        item.setForeground(QColor(fg))
        item.setBackground(QColor(bg))
        item.setToolTip(text)
        self.messages.addItem(item)

    def _add_row(self, values: List[str], status: Status) -> None:
        row = self.result_table.rowCount()
        self.result_table.insertRow(row)
        for col, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setToolTip(value)
            if col == 0:
                item.setData(Qt.ItemDataRole.UserRole, STATUS_TONE[status])
            elif status == Status.ERROR and col == 4:
                item.setForeground(QColor(theme.TONES["danger"][0]))
            elif status == Status.SKIPPED:
                item.setForeground(QColor(theme.SUBTLE))
            self.result_table.setItem(row, col, item)

    def make_package(self) -> None:
        if not self.prepared:
            return
        project, changes, source, preview, entries = self.prepared
        strict = self.strict_check.isChecked()
        if workflow.is_blocked(entries, source, strict):
            failed = preview.count(Status.ERROR)
            question = (f"실패 항목 {failed}개가 있습니다. 실패 항목을 빼고 나머지로 패키지를 만들까요?" if failed
                        else "경고가 있습니다('경고도 실패로 처리' 선택됨). 그래도 패키지를 만들까요?")
            if not self.ctx.dialogs.ask("확인", question):
                return
        archive = ("zip" if project.server_os == "windows" else "tar") if self.archive_check.isChecked() else None

        def run():
            return packager.build_package(project, entries, source, archive=archive, source_changes=changes)

        self.ctx.runner.run(run, self._show_created, lambda msg: self.ctx.dialogs.error("패키지 만들기 실패", msg))

    def _show_created(self, result: packager.PackageResult) -> None:
        self.last_result = result
        text = f"✓  패키지를 만들었습니다  ·  {result.out_dir}"
        if result.archive:
            text += f"\n     압축 파일  ·  {result.archive}"
        sh = result.project.script_name
        if result.project.server_os == "windows":
            text += (f"\n     서버에서: powershell -ExecutionPolicy Bypass -File .\\{sh} check → backup → apply"
                     "  (원복: rollback)")
        else:
            text += f"\n     서버에서: ./{sh} check → ./{sh} backup → ./{sh} apply  (원복: rollback)"
        self.done_label.setText(text)
        self._show_done(True)
        self._invalidate()
